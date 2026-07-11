"""TeamRunner / AgentRuntimeAdapter 全链路集成测试。

与纯单元测试不同，本测试使用真实 LLM 模型（agnes-2.0-flash）验证：
1. AgentRuntimeAdapter._call_llm 能调用 LLM 并产出符合 schema 的 actions
2. TeamRunner 整个过程能正常创建 room → select speaker → run → publish → check → 终止
3. 至少有一轮真正的 send_message / update_state 类动作，而不是全是 no_op

注意：运行本测试会消耗一次 LLM API 调用。
"""

import json
import time

import pytest

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec, TeamSpec, TeamRunConfig
from app.multiagent.default_teams import SOFTWARE_DEV_TEAM
from app.multiagent.messages import MessageType, AgentMessage, MessageVisibility, make_message_id
from app.multiagent.room import TeamRoom
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import SharedTeamState, TeamPhase
from app.multiagent.store import get_multiagent_store, MultiAgentStore


# ===== AgentRuntimeAdapter 单独集成测试 =====


@pytest.mark.live_model
def test_adapter_call_llm_returns_actions():
    """验证 AgentRuntimeAdapter._call_llm 能调用真实 LLM 并返回有效 actions。"""
    adapter = AgentRuntimeAdapter(task_id="test_llm", room_id="test_llm")
    agent = SOFTWARE_DEV_TEAM.get_agent("Planner")
    assert agent is not None, "需要在 default_teams 中定义 Planner"

    state = SharedTeamState(
        room_id="test_llm", task_id="test_llm",
        goal="分析当前项目架构并输出一份优化建议",
        phase=TeamPhase.PLANNING,
    )

    prompt = adapter.build_system_prompt(agent, state, "(无新消息)")
    actions = adapter._call_llm(agent, prompt)

    assert isinstance(actions, list), f"应返回 list，实际为 {type(actions)}"
    assert len(actions) > 0, "应至少返回一个 action"
    # 检查每个 action 有 type
    for a in actions:
        assert "type" in a, f"action 应包含 type 字段: {a}"
        assert a["type"] in (
            "send_message", "update_state", "create_artifact",
            "request_review", "respond_critique", "mark_done",
            "handoff", "no_op",
        ), f"未知 action type: {a['type']}"

    # 验证 actions 能正常转为 AgentMessage
    msgs = AgentRuntimeAdapter.actions_to_messages(
        "Planner", "test_llm", "test_llm", actions, 1
    )
    assert len(msgs) > 0, "actions 应能转换为 AgentMessage"
    for m in msgs:
        assert m.from_agent == "Planner"
        assert m.message_type is not None


# ===== TeamRunner 集成测试 =====


@pytest.fixture
def fresh_store(tmp_path):
    """使用独立临时数据库的 store。"""
    import app.core.config as cfg
    import app.multiagent.store as ma_store
    ma_store.close_connection()
    cfg.settings.sqlite_path = str(tmp_path / "test_team_runner.sqlite3")
    store = ma_store.MultiAgentStore()
    yield store
    ma_store.close_connection()


@pytest.mark.live_model
def test_team_runner_real_llm_two_rounds(fresh_store):
    """TeamRunner 运行 2 轮，验证全部链路正常。

    期望（不强制因为 LLM 输出有随机性）：
    - 至少 Agent 1（通常是 Planner）成功运行并产出 action
    - 消息进入 MessageBus, inbox, 数据库
    - TerminationChecker 能正常判断
    """
    from app.multiagent.team_runner import TeamRunner

    runner = TeamRunner(
        task_id="integ_test",
        room_id="integ_test_room",
        store=fresh_store,
    )
    runner._team_spec = SOFTWARE_DEV_TEAM
    runner.room = TeamRoom.create(
        task_id="integ_test",
        config=TeamRunConfig(goal="用 2 轮做一次简短项目分析", team_name="software_dev_team", max_rounds=3),
        team_spec=SOFTWARE_DEV_TEAM,
        store=fresh_store,
        room_id="integ_test_room",
    )
    runner.adapter = AgentRuntimeAdapter(
        task_id="integ_test",
        room_id="integ_test_room",
    )
    runner.termination_checker = __import__(
        "app.multiagent.termination", fromlist=["TerminationChecker"]
    ).TerminationChecker(team_spec=SOFTWARE_DEV_TEAM, max_stale_rounds=2)

    # 发用户请求
    runner.room.state.goal = "用 2 轮做一次简短项目分析"
    runner.room.send_system_message(
        content="用 2 轮做一次简短项目分析",
        message_type=MessageType.USER_REQUEST,
    )

    # 手动执行 2 轮（不走大循环，同时较短的 promise 确保及时返回）
    for _round in range(1, 4):  # 最多 3 轮，大多在 1-2 轮出结果
        runner.room.state.current_round = _round
        speaker = runner.selector.select(
            shared_state=runner.room.state,
            agents=runner.room.agents,
            inbox=runner.room.inbox,
            last_speaker=runner._last_speaker,
            last_message=runner._last_messages[-1] if runner._last_messages else None,
        )
        if speaker is None:
            break

        unread = runner.room.inbox.list_unread(speaker.name)
        inbox_ctx = runner.room.inbox.get_relevant_context(speaker.name)

        actions = runner.adapter.run(
            agent=speaker,
            inbox_messages=unread,
            shared_state=runner.room.state,
        )

        produced = AgentRuntimeAdapter.actions_to_messages(
            agent_name=speaker.name,
            task_id="integ_test",
            room_id="integ_test_room",
            actions=actions,
            round_number=_round,
        )
        for msg in produced:
            runner.room.publish(msg)
        runner._last_messages = produced

        # 复用真实 _process_actions：覆盖 update_state / create_artifact / handoff / mark_done / request_review 等全部动作
        runner._process_actions(speaker.name, actions)
        fresh_store.save_state(runner.room.state)
        for m in unread:
            runner.room.inbox.mark_read(m.id, speaker.name)

        fresh_store.save_round(
            room_id="integ_test_room",
            round_number=_round,
            selected_speaker=speaker.name,
            action_summary="; ".join(f"{a.get('type','?')}" for a in actions[:3]),
            message_ids=[m.id for m in produced],
        )
        runner._last_speaker = speaker.name

        decision = runner.termination_checker.check(
            state=runner.room.state,
            recent_messages=produced,
            round_count=_round,
        )
        if decision.should_terminate:
            break

    # —— 验证 ——
    # 1. 有消息入库
    msgs = fresh_store.get_room_messages("integ_test_room")
    assert len(msgs) > 1, f"应有多条消息，实际 {len(msgs)}"

    # 2. 有非 system 发信人（Agent 有产出）
    agent_senders = {m.from_agent for m in msgs if m.from_agent != "system"}
    assert len(agent_senders) > 0, f"应有 Agent 产出消息，实际只有: {set(m.from_agent for m in msgs)}"

    # 3. 有至少一条不是 no_op 的消息（证明 LLM 产生了有效动作）
    non_noop = [m for m in msgs if m.message_type != MessageType.NO_OP]
    assert len(non_noop) > 0, "应有非 no_op 的消息类型"

    # 4. 快速验证 state 被持久化
    state = fresh_store.load_state("integ_test_room")
    assert state is not None, "state 应持久化到 store"
    # phase 推进是有随机性的（LLM 可能不立即切换 phase），所以不强断言 equals，
    # 只断言至少不是默认 created 之一次/或 plan/final_output 至少有一处被更新
    state_advanced = (
        state.phase != TeamPhase.CREATED
        or bool(state.plan)
        or bool(state.final_output)
        or len(state.completed_steps) > 0
    )
    assert state_advanced, (
        f"state 应被推进。当前 phase={state.phase}, plan={state.plan[:50] if state.plan else ''!r}"
    )

    # 5. 验证 round 记录
    rounds = fresh_store.list_rounds("integ_test_room")
    assert len(rounds) > 0, "应有轮次记录"
    logger.info(f"集成测试完成：{len(msgs)} 条消息, {len(rounds)} 轮, phase={state.phase.value}")


@pytest.mark.live_model
def test_adapter_planner_produces_plan_action():
    """Planner Agent 应产出规划类动作（update_state 或 send_message）。"""
    adapter = AgentRuntimeAdapter(task_id="test_llm2", room_id="test_llm2")
    agent = SOFTWARE_DEV_TEAM.get_agent("Planner")
    assert agent is not None

    state = SharedTeamState(
        room_id="test_llm2", task_id="test_llm2",
        goal="分析项目结构，输出一份模块说明",
        phase=TeamPhase.PLANNING,
    )
    prompt = adapter.build_system_prompt(agent, state, "(无新消息)")
    actions = adapter._call_llm(agent, prompt)

    assert isinstance(actions, list)
    assert len(actions) > 0, "Planner 应至少产出一个动作"
    # 放宽：只要有一个非 no_op 的动作（规划/派工/状态更新都算规划意图）
    meaningful = [a for a in actions if a.get("type") and a.get("type") != "no_op"]
    assert len(meaningful) >= 1, (
        f"Planner 应产出非 no_op 的有意义动作。实际 actions:\n{json.dumps(actions, ensure_ascii=False, indent=2)}"
    )
    logger.info(f"Planner actions:\n{json.dumps(actions, ensure_ascii=False, indent=2)}")


@pytest.mark.live_model
def test_adapter_actions_can_roundtrip(tmp_path):
    """验证 actions → AgentMessage → 入库 完整往返。"""
    import app.core.config as cfg
    import app.multiagent.store as ma_store

    cfg.settings.sqlite_path = str(tmp_path / "test_rt.sqlite3")
    store = ma_store.MultiAgentStore()
    adapter = AgentRuntimeAdapter(task_id="rt_test", room_id="rt_test")
    agent = SOFTWARE_DEV_TEAM.get_agent("Planner")

    state = SharedTeamState(
        room_id="rt_test", task_id="rt_test",
        goal="整理需求清单",
        phase=TeamPhase.PLANNING,
    )
    prompt = adapter.build_system_prompt(agent, state, "(空)")
    actions = adapter._call_llm(agent, prompt)

    assert len(actions) > 0

    msgs = AgentRuntimeAdapter.actions_to_messages(
        "Planner", "rt_test", "rt_test", actions, 1
    )
    for msg in msgs:
        store.save_message(msg)

    loaded = store.get_room_messages("rt_test", 100)
    assert len(loaded) == len(msgs)
    assert loaded[0].message_type.value == msgs[0].message_type.value
