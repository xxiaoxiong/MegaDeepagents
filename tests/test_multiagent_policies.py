"""EffectiveRunPolicy 与 review_required 联动测试（Req 6 / Test req 6）。

验证：
- TeamSpec 默认值与 RunConfig 运行时覆盖生效一致
- review_required=False 时 TeamRunner 不进入 REVIEWING 阶段、不调用 ReviewRepairLoop
- max_review_cycles 在所有组件（TerminationChecker / ReviewRepairLoop / API）保持一致
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.multiagent.agent_spec import AgentSpec, TeamRunConfig, TeamSpec
from app.multiagent.policies import EffectiveRunPolicy


def _spec(review_required: bool = True, max_rounds: int = 20, max_review_cycles: int = 3):
    agents = [
        AgentSpec(name="Planner", role="Planner", goal="g", system_prompt="p"),
        AgentSpec(name="Coder", role="Coder", goal="g", system_prompt="c"),
    ]
    return TeamSpec(
        name="mini",
        description="d",
        agents=agents,
        max_rounds=max_rounds,
        review_required=review_required,
        max_review_cycles=max_review_cycles,
    )


# ===== Req 6：EffectiveRunPolicy 计算 =====


def test_policy_defaults_from_team_spec():
    spec = _spec(review_required=True, max_rounds=8, max_review_cycles=2)
    policy = EffectiveRunPolicy.from_team_and_run_config(spec, None)
    assert policy.review_required is True
    assert policy.max_rounds == 8
    assert policy.max_review_cycles == 2


def test_policy_run_config_overrides_review_required():
    spec = _spec(review_required=True, max_rounds=8)
    cfg = TeamRunConfig(goal="g", team_name="mini", max_rounds=8, review_required=False)
    policy = EffectiveRunPolicy.from_team_and_run_config(spec, cfg)
    assert policy.review_required is False
    assert policy.max_rounds == 8


def test_policy_run_config_overrides_max_rounds_only_when_explicit():
    spec = _spec(review_required=True, max_rounds=8)
    cfg = TeamRunConfig(goal="g", team_name="mini", max_rounds=15, review_required=True)
    policy = EffectiveRunPolicy.from_team_and_run_config(spec, cfg)
    assert policy.max_rounds == 15
    assert policy.max_review_cycles == 3


def test_policy_max_review_cycles_propagates_to_review_loop():
    """EffectiveRunPolicy.max_review_cycles 经 TeamRunner.create() 注入到 ReviewRepairLoop。"""
    from app.multiagent.review_repair import ReviewRepairLoop

    loop = ReviewRepairLoop()
    assert loop.max_cycles == 3
    loop.reset_max_cycles(7)
    assert loop.max_cycles == 7


# ===== Req 6：review_required=False 跳过评审 =====


class _DoneAdapter:
    """模拟 Finalizer 角色的 stub adapter，每次回一个 mark_done 结束循环。"""

    def build_system_prompt(self, agent=None, shared_state=None, inbox_context="",
                            team_agents=None, recent_actions=None):
        return "stub"

    def run(self, agent, inbox_messages, shared_state, workspace_path=None, artifacts=None):
        return [{"type": "send_message", "to_agent": "Coder", "message_type": "plan",
                 "content": "test"}]


def test_review_required_false_skips_review_in_runner(tmp_path):
    """review_required=False 时 TeamRunner 实际运行不进入 REVIEWING。"""
    from app.multiagent.runtime_adapter import AgentRuntimeAdapter
    from app.multiagent.room import TeamRoom
    from app.multiagent.termination import TerminationChecker
    from app.multiagent.team_runner import TeamRunner
    import app.core.config as cfg
    import app.multiagent.store as ma_store

    ma_store.close_connection()
    cfg.settings.sqlite_path = str(tmp_path / "policy.sqlite3")
    store = ma_store.MultiAgentStore()

    team_spec = _spec(review_required=False, max_rounds=2)
    runner = TeamRunner(task_id="t_pol", room_id="r_pol", store=store)
    runner._team_spec = team_spec
    runner._effective_policy = EffectiveRunPolicy(
        review_required=False, max_rounds=2, max_review_cycles=3,
    )
    runner.room = TeamRoom.create(
        task_id="t_pol",
        config=TeamRunConfig(goal="g", team_name="mini", max_rounds=2, review_required=False),
        team_spec=team_spec,
        store=store,
        room_id="r_pol",
    )
    # Stub adapter：每次回一个 mark_done 让大循环能尽快收尾
    runner.adapter = _DoneAdapter()
    runner.termination_checker = TerminationChecker(team_spec=team_spec, max_stale_rounds=2,
                                                    review_required=False)
    runner.review_loop.reset_max_cycles(3)
    runner._init_executor()

    runner.room.state.goal = "g"
    result = runner.run()

    assert runner.room.state.phase.value != "reviewing", \
        f"review_required=False 不应进入评审，phase={runner.room.state.phase}"
    assert result is not None
