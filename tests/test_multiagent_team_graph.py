"""LangGraph team graph 集成测试（与生产接口对齐版本）。

覆盖：
1. graph 可编译并启用 sqlite checkpoint
2. run_via_graph 走真实 TeamRoom 时 checkpoint 真写入 sqlite
3. langgraph 不可用时 run_via_graph 回退到 TeamRunner.run()

注意：本文件**不再使用 _MockSpeakerSelector / _MockRunner** 等迎合错误接口的 mock
（与 Req 1 「测试必须直接使用与生产实现一致的调用契约」一致）。节点单元语义已由
`test_multiagent_team_graph_checkpoint.py` 用真实 fixture 覆盖。
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from app.multiagent.team_graph import (
    TeamGraphRunner,
    _LANGGRAPH_AVAILABLE,
    build_team_graph,
)


def _reset_global_store(tmp_path):
    import app.core.config as cfg
    import app.multiagent.store as ma_store
    ma_store.close_connection()
    if hasattr(ma_store, "_store"):
        ma_store._store = None
    cfg.settings.sqlite_path = str(tmp_path / "graph.sqlite3")


def _build_runner(tmp_path, room_id: str, max_rounds: int = 2):
    """构造真实 TeamRunner（带 TeamRoom + Store + Selector + Termination）。"""
    from app.multiagent.agent_spec import TeamRunConfig
    from app.multiagent.default_teams import SOFTWARE_DEV_TEAM
    from app.multiagent.room import TeamRoom
    from app.multiagent.team_runner import TeamRunner
    from app.multiagent.termination import TerminationChecker
    from app.multiagent.store import get_multiagent_store

    store = get_multiagent_store()
    team_spec = SOFTWARE_DEV_TEAM
    config = TeamRunConfig(
        goal="g", team_name=team_spec.name,
        max_rounds=max_rounds, review_required=False,
    )
    runner = TeamRunner(task_id="t_graph", room_id=room_id, store=store)
    runner._team_spec = team_spec
    runner.room = TeamRoom.create(
        task_id="t_graph", room_id=room_id,
        config=config, team_spec=team_spec, store=store,
    )
    runner.termination_checker = TerminationChecker(team_spec=team_spec, max_stale_rounds=4)
    runner.review_loop.reset_max_cycles(3)
    runner._init_executor()
    return runner


class _ScriptedAdapter:
    """确定性 Adapter：按 (agent_name, round) 脚本返回 actions。"""

    def __init__(self, script: dict | None = None):
        self._script = script or {}

    def run(self, agent, inbox_messages, shared_state, **kw):
        return self._script.get((agent.name, shared_state.current_round), [])

    def actions_to_messages(self, *a, **kw):
        return []


def _close_checkpointer(gr) -> None:
    if gr.checkpointer is not None and hasattr(gr.checkpointer, "conn"):
        try:
            gr.checkpointer.conn.close()
        except Exception:
            pass


@pytest.mark.skipif(not _LANGGRAPH_AVAILABLE, reason="LangGraph 未安装")
def test_graph_compiles_with_checkpoint(tmp_path):
    """graph 可编译并启用 sqlite checkpoint。"""
    _reset_global_store(tmp_path)
    runner = _build_runner(tmp_path, "r_compile")
    ckpt = str(tmp_path / "ckpt.sqlite3")
    gr = build_team_graph(runner, checkpoint_path=ckpt)
    try:
        assert gr.graph is not None
        assert gr.checkpointer is not None
    finally:
        _close_checkpointer(gr)


@pytest.mark.skipif(not _LANGGRAPH_AVAILABLE, reason="LangGraph 未安装")
def test_graph_run_writes_checkpoint_to_sqlite(tmp_path):
    """graph 真实运行后，sqlite 应含 LangGraph checkpoint 表。"""
    _reset_global_store(tmp_path)
    runner = _build_runner(tmp_path, "r_write", max_rounds=2)
    runner.adapter = _ScriptedAdapter()
    runner._init_executor()

    ckpt = str(tmp_path / "ckpt.sqlite3")
    gr = build_team_graph(runner, checkpoint_path=ckpt)
    try:
        gr.run_via_graph(goal="g", resume_thread_id="r_write", max_rounds_cap=3)
    finally:
        _close_checkpointer(gr)

    conn = sqlite3.connect(ckpt)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert any("checkpoint" in t.lower() or "write" in t.lower() for t in tables), tables


def test_graph_falls_back_when_not_compiled(tmp_path):
    """graph 未编译时 run_via_graph 回退到 TeamRunner.run()。"""
    _reset_global_store(tmp_path)
    runner = _build_runner(tmp_path, "r_fb", max_rounds=1)

    gr = TeamGraphRunner(runner)
    assert gr.graph is None

    result = gr.run_via_graph(goal="g")
    assert "status" in result
    assert result.get("thread_id") == "r_fb"
