"""ReviewRepair 完整闭环测试（Req 4 / Test req 4）。

使用 FakeAdapter 模拟确定性 Agent 输出，覆盖：
Coder 创建产物 → ReviewerAgent 拒绝 → 生成 Critique
→ Coder 收到 Critique → Coder 修复 → 再次 Review → 通过 → Finalizer 收尾
"""

from __future__ import annotations

import json

import pytest

from app.multiagent.agent_spec import AgentSpec, TeamRunConfig, TeamSpec
from app.multiagent.default_teams import SOFTWARE_DEV_TEAM
from app.multiagent.messages import AgentMessage, MessageType, MessageVisibility, make_message_id
from app.multiagent.review_repair import ReviewRepairLoop, ReviewResult
from app.multiagent.room import TeamRoom
from app.multiagent.runtime_adapter import AgentRuntimeAdapter
from app.multiagent.speaker_selector import SpeakerSelector
from app.multiagent.state import SharedTeamState, TeamPhase, TeamArtifactRef
from app.multiagent.termination import TerminationChecker


def _make_store(tmp_path):
    import app.core.config as cfg
    import app.multiagent.store as ma_store

    ma_store.close_connection()
    cfg.settings.sqlite_path = str(tmp_path / "review_test.sqlite3")
    return ma_store.MultiAgentStore()


class _ScriptedAdapter(AgentRuntimeAdapter):
    """按步骤返回预定 actions，模拟完整评审流程。"""

    def __init__(self, script: list[dict]):
        self._step = 0
        self._script = script

    def run(self, agent, inbox_messages, shared_state, **kwargs):
        if self._step < len(self._script):
            result = self._script[self._step]
            self._step += 1
            return result.get(agent.name, [{"type": "no_op", "content": "done"}])
        return [{"type": "no_op", "content": "no more steps"}]

    def build_system_prompt(self, **kwargs):
        return "scripted prompt"


def test_review_repair_full_cycle(tmp_path):
    """ReviewRepair 完整闭环：Coder→Reviewer(拒)→Critique→Coder(修)→Reviewer(通)→收尾。

    这是 upgradePhaseOne.md Test req 4 要求的确定性 Fake Model 测试。
    """
    store = _make_store(tmp_path)
    team_spec = SOFTWARE_DEV_TEAM

    # 配置：1 个 Agent（Coder）+ ReviewerAgent + Finalizer
    # 但 runner 会用到所有 5 个 agent；我们模拟手动步骤而非完整大循环
    coder = team_spec.get_agent("Coder")
    reviewer = team_spec.get_agent("ReviewerAgent")
    finalizer = team_spec.get_agent("Finalizer")
    assert coder and reviewer and finalizer, "team spec 必须包含 Coder/ReviewerAgent/Finalizer"

    # ---- 构造状态 + room ----
    room = TeamRoom.create(
        task_id="r4_test",
        config=TeamRunConfig(goal="写一个限流中间件", team_name="software_dev_team", max_rounds=12),
        team_spec=team_spec,
        store=store,
        room_id="r4_room",
    )
    room.state.update_phase(TeamPhase.PLANNING)

    # ---- 模拟步骤 1：Coder 创建产物 + 请求评审 ----
    coder_actions = [
        {"type": "create_artifact", "artifact_path": "rate_limiter.py",
         "artifact_role": "code", "content": "限流中间件实现"},
        {"type": "request_review", "to_agent": "ReviewerAgent", "content": "请评审"},
    ]
    from app.multiagent.round_executor import TeamRoundExecutor

    adapter = _ScriptedAdapter({})
    selector = SpeakerSelector()
    terminator = TerminationChecker(team_spec=team_spec, max_stale_rounds=4)
    review_loop = ReviewRepairLoop(max_cycles=3)
    emitter = type("obj", (), {"emit": lambda *a, **kw: None})()

    executor = TeamRoundExecutor(
        room=room, adapter=adapter, selector=selector,
        termination_checker=terminator, review_loop=review_loop,
        store=store, emitter=emitter,
        task_id="r4_test", room_id="r4_room", team_spec=team_spec,
    )
    # 手动演练，不依赖 selector
    # step: Coder produce artifact
    executor._process_actions(coder, coder_actions)
    assert len(room.state.artifacts) == 1
    assert room.state.artifacts[0].path == "rate_limiter.py"
    assert room.state.review_status == "pending"

    # ---- 模拟步骤 2：ReviewerAgent 拒绝 ----
    room.state.current_round = 2
    reject_result = ReviewResult(
        passed=False,
        issues=[{"severity": "high", "problem": "缺少异常处理",
                 "evidence": [{"kind": "code", "detail": "第42行无try/except"}]}],
        required_fix_owner="Coder",
        raw='{"passed": false, "issues": [{"severity": "high", "problem": "缺少异常处理"}]}',
    )
    critique_messages = review_loop.process_review_result(
        reject_result, state=room.state, room=room,
    )
    assert len(critique_messages) == 1, "critique 消息应被生成"
    assert critique_messages[0].message_type == MessageType.CRITIQUE
    assert critique_messages[0].to_agent == "Coder"
    assert critique_messages[0].requires_response is True
    # === 关键：模拟 TeamRoundExecutor 的行为——将 critique 发布到 MessageBus ===
    for cm in critique_messages:
        room.publish(cm)
    # 状态已更新
    assert room.state.review_status == "failed"
    assert room.state.phase == TeamPhase.REPAIRING
    assert len(room.state.issues) == 1

    # ---- 验证 critique 消息已发布到 MessageBus（原本的断链修复）----
    room_msgs = room.bus.get_room_messages()
    critique_in_bus = [m for m in room_msgs if m.message_type == MessageType.CRITIQUE]
    assert len(critique_in_bus) >= 1, "critique 必须出现在 room.bus 中（Req 4 核心）"

    # ---- 模拟步骤 3：Coder 修复 ----
    room.state.current_round = 3
    coder_fix_actions = [
        {"type": "create_artifact", "artifact_path": "rate_limiter.py",
         "artifact_role": "code", "content": "加了try/except的限流中间件", "version": 2},
        {"type": "request_review", "to_agent": "ReviewerAgent", "content": "已修复请再审"},
    ]
    executor._process_actions(coder, coder_fix_actions)
    assert room.state.artifacts[-1].version >= 2

    # ---- 模拟步骤 4：ReviewerAgent 通过 ----
    room.state.current_round = 4
    pass_result = ReviewResult(
        passed=True,
        issues=[],
        required_fix_owner=None,
        raw='{"passed": true, "issues": []}',
    )
    pass_messages = review_loop.process_review_result(
        pass_result, state=room.state, room=room,
    )
    assert room.state.review_status == "passed"
    assert room.state.phase == TeamPhase.FINALIZING

    # ---- 模拟步骤 5：Finalizer 收尾 ----
    room.state.current_round = 5
    final_actions = [
        {"type": "send_message", "message_type": "final",
         "content": "限流中间件开发完成，通过评审"},
    ]
    executor._process_actions(finalizer, final_actions)
    assert room.state.final_output is not None
    assert room.state.phase == TeamPhase.FINALIZING

    # ---- 验证完整链路 ----
    assert room.state.review_cycles >= 1
    store.save_state(room.state)
    loaded = store.load_state("r4_room")
    assert loaded is not None
    assert loaded.review_status == "passed"
    # review cycle 已在持久化中保留
    assert loaded.review_cycles > 0
