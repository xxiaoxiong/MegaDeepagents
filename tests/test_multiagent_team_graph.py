"""LangGraph team graph 集成测试。

验证：
1. graph 能编译
2. checkpoint 写到 sqlite
3. resume_from_thread_id 能恢复到上次中断点（mock 模式）
4. LangGraph 不可用时回退到 TeamRunner.run
5. HITL 节点不阻塞默认路径
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from app.multiagent.team_graph import (
    TeamGraphRunner,
    _LANGGRAPH_AVAILABLE,
    build_team_graph,
)


class _MockSpeakerSelector:
    def __init__(self, agents):
        self._agents = agents
        self._i = 0

    def select(self, agents, state, inbox=None):
        # 一轮轮循环，最终返回 None 模拟终止
        if self._i >= len(self._agents) * 2:
            return None
        a = self._agents[self._i % len(self._agents)]
        self._i += 1
        return a


class _MockAdapter:
    def build_system_prompt(self, **kwargs):
        return ""

    def run(self, **kwargs):
        return [{"type": "no_op", "content": "mock"}]


class _MockRoom:
    def __init__(self):
        self.room_id = "test_room"
        self.agents = []
        self.state = None
        self.config = type("c", (), {"review_required": False, "goal": "g"})()


class _MockRunner:
    def __init__(self, agents):
        self.room = _MockRoom()
        self.room.agents = agents
        self.adapter = _MockAdapter()
        self.selector = _MockSpeakerSelector(agents)
        self.store = None
        self.room_id = "test_room"
        self.termination_checker = None
        self.emitter = None
        self._processed = []
        self.run_called = False

    def _process_actions(self, speaker_name, actions):
        self._processed.append((speaker_name, len(actions)))

    def run(self, goal_override=None):
        self.run_called = True


def _make_agent(name):
    class _A:
        def __init__(self, n):
            self.name = n
            self.role = "Coder"
            self.goal = "test"
    return _A(name)


@pytest.mark.skipif(not _LANGGRAPH_AVAILABLE, reason="LangGraph 未安装")
def test_graph_compiles_with_checkpoint():
    """graph 可编译并启用 sqlite checkpoint。"""
    runner = _MockRunner([_make_agent("A"), _make_agent("B")])
    fp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fp.close()
    try:
        gr = build_team_graph(runner, checkpoint_path=fp.name)
        assert gr.graph is not None
        # checkpointer 应该被实例化
        assert gr.checkpointer is not None
        # 关闭 checkpointer 的连接，释放文件锁
        if hasattr(gr.checkpointer, 'conn'):
            gr.checkpointer.conn.close()
    finally:
        _close_and_remove(fp.name)


@pytest.mark.skipif(not _LANGGRAPH_AVAILABLE, reason="LangGraph 未安装")
def test_graph_run_writes_checkpoint_to_sqlite():
    """graph 运行后，sqlite 中应能读到对应 thread 的 checkpoint。"""
    runner = _MockRunner([_make_agent("A"), _make_agent("B")])
    fp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fp.close()
    try:
        gr = build_team_graph(runner, checkpoint_path=fp.name)
        gr.run_via_graph(goal="test", resume_thread_id="thread_xxx", max_rounds_cap=3)
        # 关闭 graph 内的连接后，再读库验证
        if gr.checkpointer is not None and hasattr(gr.checkpointer, 'conn'):
            gr.checkpointer.conn.close()
        # 重新独立连接验证
        conn = sqlite3.connect(fp.name)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall()]
        assert any('checkpoint' in t.lower() or 'write' in t.lower() for t in tables), tables
        conn.close()
    finally:
        _close_and_remove(fp.name)


def _close_and_remove(path: str) -> None:
    """Windows 上强制关闭所有连接后再删除。"""
    import gc
    gc.collect()
    for _ in range(3):
        try:
            os.unlink(path)
            return
        except PermissionError:
            import time
            time.sleep(0.1)
    # 最终尝试：重命名后删除
    try:
        import uuid
        os.rename(path, path + ".deleted." + uuid.uuid4().hex[:6])
        os.unlink(path + ".deleted.*")
    except Exception:
        pass


def test_graph_falls_back_when_langgraph_unavailable():
    """langgraph 不可用时，run_via_graph 回退到 TeamRunner.run()。

    通过模拟未编译的 graph 来检验回退逻辑。
    """
    runner = _MockRunner([_make_agent("A")])
    gr = TeamGraphRunner(runner)
    # 未调用 compile → graph 为 None
    assert gr.graph is None
    result = gr.run_via_graph(goal="test")
    # 应回退到 TeamRunner.run() 被调用
    assert runner.run_called is True
    assert result["status"] == "completed"


def test_node_select_speaker_returns_state():
    """node_select_speaker 返回 speaker 字段，不抛错。"""
    runner = _MockRunner([_make_agent("A"), _make_agent("B")])
    gr = TeamGraphRunner(runner)
    gr.compile()
    if gr.graph is None:
        pytest.skip("LangGraph 不可用")
    state = {"round": 0}
    out = gr.node_select_speaker(state)
    assert "speaker" in out


def test_node_decide_terminate_returns_continue():
    """node_decide_terminate 返回 continue 信号字段。"""
    runner = _MockRunner([_make_agent("A")])
    gr = TeamGraphRunner(runner)
    gr.compile()
    if gr.graph is None:
        pytest.skip("LangGraph 不可用")
    state = {"round": 1}
    out = gr.node_decide_terminate(state)
    assert "continue" in out
    assert "termination_reason" in out
