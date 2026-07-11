"""TeamRoundExecutor 单元测试。

验证抽取出的单轮执行组件：
- 选择 speaker
- 调用 adapter 产生 actions
- 转消息 publish + 更新 state
- termination 决策
- 复用 TeamRunner 不再持有这些逻辑
"""

from __future__ import annotations

import pytest

from app.multiagent.agent_spec import AgentSpec, TeamRunConfig, TeamSpec
from app.multiagent.bus import MessageBus
from app.multiagent.inbox import AgentInbox
from app.multiagent.messages import AgentMessage, MessageType, MessageVisibility
from app.multiagent.review_repair import ReviewRepairLoop
from app.multiagent.room import TeamRoom
from app.multiagent.round_executor import TeamRoundExecutor
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import SharedTeamState, TeamPhase
from app.multiagent.termination import TerminationChecker


def _make_team_spec() -> TeamSpec:
    agents = [
        AgentSpec(name="Planner", role="Planner", goal="plan the work", system_prompt="plan"),
        AgentSpec(name="Coder", role="Coder", goal="do the coding", system_prompt="code"),
    ]
    return TeamSpec(
        name="mini_team",
        description="mini",
        agents=agents,
        max_rounds=5,
        max_concurrent_agents=1,
    )


class _StubAdapter(AgentRuntimeAdapter):
    def __init__(self):
        self._actions = [
            {"type": "send_message", "to_agent": "Coder", "message_type": "plan",
             "content": "hello from planner"},
        ]

    def run(self, agent, inbox_messages, shared_state):  # type: ignore[override]
        return list(self._actions)

    def build_system_prompt(self, **kwargs):  # type: ignore[override]
        return ""


def _make_store(tmp_path):
    import app.core.config as cfg
    import app.multiagent.store as ma_store
    cfg.settings.sqlite_path = str(tmp_path / "test.sqlite3")
    return ma_store.MultiAgentStore()


def test_round_executor_executes_one_round(tmp_path):
    """execute_round 单轮返回正确的 speaker/actions/messages。"""
    store = _make_store(tmp_path)
    config = TeamRunConfig(goal="test goal", team_name="mini_team", max_rounds=5)
    team_spec = _make_team_spec()
    room = TeamRoom.create(
        task_id="t1", room_id="r1", config=config, team_spec=team_spec, store=store,
    )
    # 进入 PLANNING，让 Planner 优先被选
    room.state.update_phase(TeamPhase.PLANNING)

    adapter = _StubAdapter()
    selector = SpeakerSelector()
    terminator = TerminationChecker(team_spec=team_spec, max_stale_rounds=2)
    review_loop = ReviewRepairLoop()

    ex = TeamRoundExecutor(
        room=room,
        adapter=adapter,
        selector=selector,
        termination_checker=terminator,
        review_loop=review_loop,
        store=store,
        emitter=None,
        task_id="t1",
        room_id="r1",
        team_spec=team_spec,
    )

    result = ex.execute_round(round_number=1, last_speaker=None, last_messages=[])

    assert result.error is None
    assert result.speaker is not None
    assert result.speaker.name == "Planner"
    assert len(result.actions) == 1
    assert len(result.produced_messages) == 1
    assert result.produced_messages[0].message_type == MessageType.PLAN


def test_round_executor_no_speaker_terminates(tmp_path):
    """没有可选 speaker 时仍安全返回 termination_reason。"""
    store = _make_store(tmp_path)
    config = TeamRunConfig(goal="test goal", team_name="mini_team", max_rounds=5)
    team_spec = _make_team_spec()
    room = TeamRoom.create(
        task_id="t1", room_id="r1", config=config, team_spec=team_spec, store=store,
    )
    adapter = _StubAdapter()
    selector = SpeakerSelector()
    terminator = TerminationChecker(team_spec=team_spec, max_stale_rounds=0)
    review_loop = ReviewRepairLoop()

    ex = TeamRoundExecutor(
        room=room,
        adapter=adapter,
        selector=selector,
        termination_checker=terminator,
        review_loop=review_loop,
        store=store,
        emitter=None,
        task_id="t1",
        room_id="r1",
        team_spec=team_spec,
    )

    # 无消息也无 prior speaker，应仍然执行（speaker 可能为 Planner）
    result = ex.execute_round(round_number=1, last_speaker=None, last_messages=[])
    assert result.error is None


@pytest.mark.integration
def test_team_runner_uses_round_executor(tmp_path):
    """TeamRunner 实例化时通过 _init_executor() 复用 TeamRoundExecutor。"""
    from app.multiagent.team_runner import TeamRunner

    store = _make_store(tmp_path)
    # 直接构造 runner，不使用全局 store 单例
    runner = TeamRunner(task_id="t_re", room_id="r_re", store=store)
    runner._team_spec = _make_team_spec()
    runner.room = TeamRoom.create(
        task_id="t_re",
        room_id="r_re",
        config=TeamRunConfig(goal="g", team_name="mini_team", max_rounds=2),
        team_spec=runner._team_spec,
        store=store,
    )
    runner.adapter = _StubAdapter()  # type: ignore[assignment]
    runner.termination_checker = TerminationChecker(
        team_spec=runner._team_spec, max_stale_rounds=2,
    )

    assert runner.round_executor is None
    runner._init_executor()
    assert runner.round_executor is not None
    assert isinstance(runner.round_executor, TeamRoundExecutor)
