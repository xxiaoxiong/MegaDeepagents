"""Cancel isolation tests (Req 9 / Test req 8).

验证：
- cancel() 在持久化状态中写入 cancel_requested
- 主循环每轮前检查取消状态，cancel 后不再执行后续轮次
- 取消后状态保持 CANCELLED，不被后续操作覆盖
- 运行中触发 cancel：当前轮结束后下一轮不再执行（race-condition 路径回归）
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def _reset_global_store(tmp_path):
    """重置全局 store 单例 + sqlite_path.

    说明：tests/conftest.py 的 autouse fixture 已为每个测试隔离 sqlite，
    但部分老测试仍手动调用此函数做二次保险，保留不删。
    """
    import app.core.config as cfg
    import app.multiagent.store as ma_store
    ma_store.close_connection()
    if hasattr(ma_store, "_store"):
        ma_store._store = None
    cfg.settings.sqlite_path = str(tmp_path / "cancel.sqlite3")


class _SlowAdapter:
    def __init__(self):
        self.run_count = 0

    def run(self, agent, inbox_messages, shared_state, **kw):
        time.sleep(0.08)
        self.run_count += 1
        return [{"type": "no_op", "content": "waiting"}]

    def build_system_prompt(self, *a, **kw):
        return ""

    def actions_to_messages(self, *a, **kw):
        return []


class _GatedAdapter:
    """门控 adapter：每轮阻塞 until_cancel 被 set 或超时。

    用于 cancel race-condition 测试：
    - run() 阻塞，让外部线程在轮执行期间触发 cancel
    - 主循环下一轮入口看到 cancel_requested → 终止，不再执行总轮数 > 1
    - 所有 run() 调用都被记录，便于断言"只跑了有限的轮"
    """

    def __init__(self, gate: threading.Event, max_rounds_before_cancel: int):
        self.gate = gate
        self.max_rounds_before_cancel = max_rounds_before_cancel
        self.run_count = 0
        self.lock = threading.Lock()

    def run(self, agent, inbox_messages, shared_state, **kw):
        with self.lock:
            self.run_count += 1
            round_n = shared_state.current_round
        # 只有前 max_rounds_before_cancel 轮会被 gate 阻塞（让 cancel 有机会触发）
        if round_n <= self.max_rounds_before_cancel:
            # 最多等 5s，cancel 未到也避免 hang 死
            self.gate.wait(timeout=5.0)
        return [{"type": "no_op", "content": "gated"}]

    def build_system_prompt(self, *a, **kw):
        return ""

    def actions_to_messages(self, *a, **kw):
        return []


def test_cancel_sets_persistent_flag(tmp_path):
    """cancel() 在状态中写入 cancel_requested。"""
    _reset_global_store(tmp_path)
    from app.multiagent.team_runner import TeamRunner

    runner = TeamRunner.create(
        goal="test cancel", team_name="software_dev_team",
        max_rounds=10, review_required=False,
    )

    ok = runner.cancel()
    assert ok is True
    assert runner.room.state.metadata.get("cancel_requested") is True
    assert runner.room.state.phase.value == "cancelled"


def test_cancel_before_run_exits_immediately(tmp_path):
    """run() 前 cancel()，立即退出，不执行轮次。"""
    _reset_global_store(tmp_path)
    from app.multiagent.team_runner import TeamRunner

    runner = TeamRunner.create(
        goal="test", team_name="software_dev_team",
        max_rounds=10, review_required=False,
    )
    runner.adapter = _SlowAdapter()
    runner._init_executor()

    runner.cancel()
    result = runner.run()
    assert result.total_rounds == 0
    assert result.status == "cancelled"
    assert result.termination_reason == "cancel_requested"
    assert runner.room.state.phase.value == "cancelled"


def test_cancel_after_completion_preserves_state(tmp_path):
    """完成后 cancel 不覆盖已完成状态。"""
    _reset_global_store(tmp_path)
    from app.multiagent.team_runner import TeamRunner

    runner = TeamRunner.create(
        goal="test", team_name="software_dev_team",
        max_rounds=2, review_required=False,
    )
    result = runner.run()
    assert result.total_rounds > 0
    completed = runner.room.state.phase.value
    runner.cancel()
    assert runner.room.state.phase.value == completed


def test_cancel_stops_mid_execution(tmp_path):
    """运行中触发 cancel：当前轮结束后下一轮不再执行。

    关键 race-condition 回归（Test req 8）：cancel 必须在主循环每轮入口被检查，
    不能等到下一轮的 select_speaker 才发现。
    """
    _reset_global_store(tmp_path)
    from app.multiagent.team_runner import TeamRunner

    runner = TeamRunner.create(
        goal="race cancel", team_name="software_dev_team",
        max_rounds=20, review_required=False,
    )

    gate = threading.Event()
    adapter = _GatedAdapter(gate=gate, max_rounds_before_cancel=2)
    runner.adapter = adapter
    runner._init_executor()

    # 起一个后台线程跑 runner.run()
    result_box: dict = {}

    def _run_thread():
        try:
            result_box["result"] = runner.run()
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = exc

    t = threading.Thread(target=_run_thread, daemon=True)
    t.start()

    # 等到第 2 轮的 adapter 阻塞在 gate 上（轮询 room.state.current_round）
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if adapter.run_count >= 1:
            # 给一点时间让主循环进入第二轮的 gate.wait
            time.sleep(0.05)
            break
        time.sleep(0.01)

    # 触发 cancel：写持久化标记
    runner.cancel()
    # 释放 gate 让被阻塞的当前轮完成
    gate.set()

    t.join(timeout=10.0)
    assert not t.is_alive(), "runner.run 线程卡死，cancel 未生效"

    # 断言：cancel 生效，status=cancelled，termination_reason=cancel_requested
    result = result_box.get("result")
    assert result is not None, f"runner.run 未正常返回：{result_box}"
    assert result.status == "cancelled"
    assert result.termination_reason == "cancel_requested"
    # 关键：cancel 之后不应继续工作很多轮。GatedAdapter 已允许第一轮进入第二次 round，
    # gate 释放后当前轮结束；下一轮入口检测到 cancel 即终止 → run_count 不会无限大
    assert adapter.run_count <= 2, f"cancel 后仍执行了 {adapter.run_count} 轮"
    assert runner.room.state.phase.value == "cancelled"
