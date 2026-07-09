"""SpeakerSelector：选下一发言 Agent。

规则优先级（rule-first）：
1. 有 requires_response=True 未读消息的 Agent
2. 有未读用户问题 / 评审请求 / 测试请求 / critique 的 Agent
3. 当前 phase 对应角色（planning → Planner；executing → Coder/Reviewer；reviewing → ReviewerAgent）
4. 指定 from_agent（reply_to 时）
5. LLM 兜底（可选）

返回 None 表示无候选可执行 → 由 TerminationChecker 决定是否终止。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec, TeamSpec
from app.multiagent.inbox import AgentInbox
from app.multiagent.messages import AgentMessage, MessageType
from app.multiagent.state import SharedTeamState, TeamPhase


# Phase → 候选 Agent 角色
PHASE_ROLE_BIAS: dict[TeamPhase, list[str]] = {
    TeamPhase.PLANNING: ["Planner", "planner"],
    TeamPhase.EXECUTING: ["Coder", "Developer", "coder", "developer"],
    TeamPhase.REVIEWING: ["Reviewer", "ReviewerAgent", "reviewer"],
    TeamPhase.REPAIRING: ["Coder", "Developer", "coder", "Developer"],
    TeamPhase.FINALIZING: ["Finalizer", "finalizer", "summarizer"],
}


class SpeakerSelector:
    """选下一发言 Agent。"""

    def select(
        self,
        shared_state: SharedTeamState,
        agents: list[AgentSpec],
        inbox: AgentInbox,
        last_speaker: str | None = None,
        last_message: AgentMessage | None = None,
    ) -> AgentSpec | None:
        if not agents:
            return None

        agent_by_name = {a.name: a for a in agents}
        # 准备每个 Agent 的未读消息
        unread: dict[str, list[AgentMessage]] = {}
        for a in agents:
            try:
                unread[a.name] = inbox.list_unread(a.name)
            except Exception as exc:
                logger.warning(f"[SpeakerSelector] failed to list unread for {a.name}: {exc}")
                unread[a.name] = []

        # 规则 1：requires_response 直接指明谁必须响应
        for a in agents:
            for m in unread.get(a.name, []):
                if m.requires_response and (m.to_agent == a.name if m.to_agent else True):
                    logger.debug(f"[SpeakerSelector] rule1: requires_response -> {a.name}")
                    return a

        # 规则 2：特殊动作类型必须响应
        must_act_types = {
            MessageType.USER_REQUEST,
            MessageType.REVIEW_REQUEST,
            MessageType.TEST_REQUEST,
            MessageType.CRITIQUE,
            MessageType.QUESTION,
            MessageType.REVISION_PLAN,
            MessageType.DECISION,
        }
        for a in agents:
            for m in unread.get(a.name, []):
                if m.message_type in must_act_types:
                    logger.debug(
                        f"[SpeakerSelector] rule2: must-act message_type={m.message_type.value} -> {a.name}"
                    )
                    return a

        # 规则 3：phase 对应角色；优先未读中.
        phase_roles = PHASE_ROLE_BIAS.get(shared_state.phase, [])
        phase_agents = [a for a in agents if a.name in phase_roles or a.role.lower() in phase_roles]
        # 偏好：有未读且匹配 phase
        for a in phase_agents:
            if unread.get(a.name):
                logger.debug(f"[SpeakerSelector] rule3: phase={shared_state.phase.value} unread -> {a.name}")
                return a
        # phase 对应 Agent 无未读，也直接选 phase Agent（除非他就是 last_speaker）
        for a in phase_agents:
            if a.name != last_speaker:
                logger.debug(f"[SpeakerSelector] rule3b: phase={shared_state.phase.value} agent -> {a.name}")
                return a

        # 规则 4：未读消息但任意 Agent
        for a in agents:
            if unread.get(a.name) and a.name != last_speaker:
                logger.debug(f"[SpeakerSelector] rule4: any unread -> {a.name}")
                return a

        # 规则 5：reply_to 显式指定接收方
        if last_message and last_message.reply_to:
            for a in agents:
                if a.name.lower() in last_message.reply_to.lower():
                    logger.debug(f"[SpeakerSelector] rule5: reply_to ref -> {a.name}")
                    return a

        # 规则 6：根据 phase 直接选首个匹配 Agent（即使无未读），避免卡住
        # 但只有首轮或确有语义上需要推进时才强选；所有 inbox 都空时不要强行选 agent
        total_unread = sum(len(v) for v in unread.values())
        if total_unread > 0 or shared_state.current_round == 0:
            for a in phase_agents:
                logger.debug(f"[SpeakerSelector] rule6: phase fallback -> {a.name}")
                return a

        # 规则 7：long-stall 启发：让最后一次发言之外的最近活跃 Agent 说话
        if last_speaker and len(agents) > 1:
            for a in agents:
                if a.name != last_speaker:
                    logger.debug(f"[SpeakerSelector] rule7: anti-stall -> {a.name}")
                    return a

        # 规则 8：兜底：首个 Agent
        if agents:
            logger.debug(f"[SpeakerSelector] rule8: first agent -> {agents[0].name}")
            return agents[0]
        return None

    def select_by_llm_fallback(
        self,
        shared_state: SharedTeamState,
        agents: list[AgentSpec],
        inbox: AgentInbox,
        recent_inquiry: AgentMessage | None,
        llm_call: Any | None = None,
    ) -> AgentSpec | None:
        """LLM 兜底：当规则选不出时，可用 LLM 判断。

        当前实现：若 llm_call 提供，调用它返回 Agent name；否则返回 None。
        """
        if llm_call is None:
            return None
        try:
            names = [a.name for a in agents]
            full_prompt = (
                f"Team state: {shared_state.to_prompt_context()}\n"
                f"Available agents: {', '.join(names)}\n"
                f"Recent message: {recent_inquiry.content[:300] if recent_inquiry else ''}\n"
                "Returns ONLY the name of the agent that should speak next."
            )
            chosen_name = llm_call(full_prompt)
        except Exception as exc:
            logger.warning(f"[SpeakerSelector] LLM fallback failed: {exc}")
            return None
        chosen_name = (chosen_name or "").strip()
        for a in agents:
            if a.name == chosen_name:
                return a
        return None
