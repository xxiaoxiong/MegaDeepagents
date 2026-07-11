"""MessageBus：多智能体之间的结构化消息路由。

能力：
- publish(message)：发布一条消息到总线
- broadcast(message)：向所有匹配订阅的 Agent 分发
- direct_send(message)：直接投递给指定 Agent
- route_to_subscribers(message)：根据 subscription 规则投递
- get_room_messages(room_id)：查询房间全量消息
- get_agent_inbox(room_id, agent_name)：查询 Agent 收件箱
- ack_message(message_id, agent_name)：标记已读

设计原则：
1. visibility=direct → 只投递给 to_agent
2. visibility=broadcast → 根据 watched_message_types / cause_by / from_agent 投递
3. visibility=system → 投递给所有 Agent
4. 所有消息写入 room transcript
5. 所有投递写入 agent inbox

注意：本模块是内存数据结构 + store 后端的双层设计。
in-memory 层用于快速路由判断，store 层用于持久化。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec, AgentSubscription
from app.multiagent.messages import (
    AgentMessage,
    MessageVisibility,
    MessageType,
)


# 显式别名表：覆盖 LLM 常用变体到规范名的确定性映射。
# 任何不在此表、且经后缀规则也不命中的目标都视为未知（dead-letter 或 fallback）。
EXPLICIT_ALIASES: dict[str, str] = {
    "DeveloperAgent": "Coder",
    "Developer": "Coder",
    "CodeAgent": "Coder",
    "Reviewer": "ReviewerAgent",
    "TestAgent": "Tester",
    "PlanAgent": "Planner",
    "FinalizeAgent": "Finalizer",
    "ResearchAgent": "Researcher",
}


def resolve_alias(target: str, known_agents: set[str] | list[str]) -> str | None:
    """确定性别名映射（模块级函数，便于单测）。

    规则顺序（先命中先返回）：
    1. 完全相等
    2. EXPLICIT_ALIASES 显式表
    3. 去 'Agent' 后缀后再精确比较
    4. 加 'Agent' 后缀后再精确比较

    其他情况（包括子串包含、小写包含等模糊匹配）一律视为未知，
    由调用方按 dead-letter / fallback 策略处理。
    """
    known = set(known_agents)
    if target in known:
        return target
    mapped = EXPLICIT_ALIASES.get(target)
    if mapped and mapped in known:
        return mapped
    # 反查：若 target 是某规范名去掉 Agent 后缀的形式
    if target.endswith("Agent"):
        stripped = target[: -len("Agent")]
        if stripped and stripped in known:
            return stripped
    candidate = target + "Agent"
    if candidate in known:
        return candidate
    return None


class MessageBus:
    """消息总线。不直接依赖 store，但可关联 store 作持久化。

    初始化时传入 Agent 列表和 Room ID，以便路由。

    路由策略（V2 - 安全优先）：
    - DIRECT + 已知 to_agent：准确投递
    - DIRECT + 已知别名（TesterAgent → Tester）：别名归一化后投递
    - DIRECT + 未知 to_agent + allow_broadcast_fallback=True：回退广播
    - DIRECT + 未知 to_agent + allow_broadcast_fallback=False（默认）：拒绝，写入 dead-letter
    - BROADCAST：按订阅规则匹配
    """

    def __init__(
        self,
        room_id: str,
        task_id: str,
        agents: list[AgentSpec],
        store: Any | None = None,
        allow_broadcast_fallback: bool = False,
    ):
        self.room_id = room_id
        self.task_id = task_id
        self._agents = {a.name: a for a in agents}
        self._agent_subscriptions: dict[str, AgentSubscription] = {
            a.name: a.get_subscription() for a in agents
        }
        self.store = store
        self.allow_broadcast_fallback = allow_broadcast_fallback
        self._transcript: list[AgentMessage] = []
        self._dead_letters: list[AgentMessage] = []

    def get_all_agent_names(self) -> list[str]:
        return list(self._agents.keys())

    def get_agent(self, name: str) -> AgentSpec | None:
        return self._agents.get(name)

    def get_subscriptions(self, name: str) -> AgentSubscription | None:
        return self._agent_subscriptions.get(name)

    # ========== 发布 ==========

    def publish(self, message: AgentMessage) -> None:
        """发布一条消息：写入 transcript + 按 visibility 投递。"""
        self._transcript.append(message)

        if self.store:
            self.store.save_message(message)

        if message.visibility == MessageVisibility.DIRECT:
            self._direct_deliver(message)
        elif message.visibility == MessageVisibility.SYSTEM:
            self._deliver_to_all(message)
        elif message.visibility == MessageVisibility.BROADCAST:
            self._route_to_subscribers(message)
        else:
            self._route_to_subscribers(message)

        logger.debug(
            f"[MessageBus] published: {message.id} type={message.message_type.value} "
            f"from={message.from_agent} vis={message.visibility.value}"
        )

    def broadcast(self, message: AgentMessage) -> None:
        """快捷：以 broadcast visibility 发布。"""
        message.visibility = MessageVisibility.BROADCAST
        self.publish(message)

    def direct_send(self, message: AgentMessage) -> None:
        """快捷：以 direct visibility 发布。"""
        message.visibility = MessageVisibility.DIRECT
        self.publish(message)

    # ========== 内部投递逻辑 ==========

    def _direct_deliver(self, message: AgentMessage) -> None:
        """direct 投递：只送入 to_agent 的 inbox。

        若 to_agent 不存在但能匹配到别名（如 TesterAgent → Tester），重写到真实 agent 后投递。
        若仍不存在则回退 broadcast。
        """
        targets: list[str] = []
        to_agent = message.to_agent
        if isinstance(to_agent, str):
            targets = [to_agent]
        elif isinstance(to_agent, list):
            targets = to_agent
        if not targets:
            logger.warning(f"[MessageBus] direct message {message.id} has no to_agent, falling back to broadcast")
            self._route_to_subscribers(message)
            return

        known_agents = set(self._agents.keys())

        # 确定性别名归一化（不再使用 'ka in t' 这类模糊子串匹配）
        normalized_targets: list[str] = []
        aliases_used = False
        for t in targets:
            matched = resolve_alias(t, known_agents)
            if matched and matched != t:
                normalized_targets.append(matched)
                aliases_used = True
                logger.info(
                    f"[MessageBus] alias: '{t}' → '{matched}' for message {message.id}"
                )
            else:
                normalized_targets.append(t)

        if aliases_used:
            if not message.metadata:
                message.metadata = {}
            message.metadata["alias_resolved"] = True
            message.to_agent = normalized_targets[0] if isinstance(to_agent, str) else normalized_targets

        unknown = [t for t in normalized_targets if t not in known_agents]
        known = [t for t in normalized_targets if t in known_agents]

        if unknown:
            # 未知 agent 路由：根据策略回退广播或写 dead-letter
            if self.allow_broadcast_fallback:
                logger.warning(
                    f"[MessageBus] direct message {message.id} to unknown agent(s): {unknown}. "
                    f"Known agents: {list(known_agents)}. Falling back to broadcast routing."
                )
                if not message.metadata:
                    message.metadata = {}
                message.metadata["routing_fallback"] = True
                message.metadata["routing_original_to"] = targets
                self._route_to_subscribers(message)
                return
            else:
                # 默认安全策略：拒绝投递到未知 agent，写入 dead-letter 队列
                logger.warning(
                    f"[MessageBus] REJECTED direct message {message.id} to unknown agent(s): {unknown}. "
                    f"Known agents: {list(known_agents)}. Routed to dead-letter."
                )
                if not message.metadata:
                    message.metadata = {}
                message.metadata["routing_rejected"] = True
                message.metadata["routing_original_to"] = targets
                message.metadata["unknown_agents"] = unknown
                self._dead_letters.append(message)
                return

        for name in normalized_targets:
            self._deliver_to_inbox(name, message)

    def get_dead_letters(self) -> list[AgentMessage]:
        """返回被拒绝投递的消息列表（用于诊断 / 审计 / 测试）。"""
        return list(self._dead_letters)

    def _deliver_to_all(self, message: AgentMessage) -> None:
        """system visibility：投递给所有 Agent。"""
        for name in self._agents:
            self._deliver_to_inbox(name, message)

    def _route_to_subscribers(self, message: AgentMessage) -> None:
        """broadcast visibility：按订阅规则路由。"""
        for name, sub in self._agent_subscriptions.items():
            if sub.matches(message.message_type, message.cause_by, message.from_agent):
                self._deliver_to_inbox(name, message)

    def get_subscriptions_for_message(self, message: AgentMessage) -> list[str]:
        """返回订阅了该消息的 agent 名列表（用于生产性投递检测）。"""
        return [
            name
            for name, sub in self._agent_subscriptions.items()
            if sub.matches(message.message_type, message.cause_by, message.from_agent)
        ]

    def _deliver_to_inbox(self, agent_name: str, message: AgentMessage) -> None:
        """投递到指定 Agent 的 inbox。"""
        if self.store:
            self.store.deliver_to_inbox(
                agent_name=agent_name,
                message_id=message.id,
                room_id=self.room_id,
                task_id=self.task_id,
                from_agent=message.from_agent,
                message_type=message.message_type.value,
            )

    # ========== 查询 ==========

    def get_room_messages(self) -> list[AgentMessage]:
        return list(self._transcript)

    def get_agent_inbox(self, agent_name: str) -> list[AgentMessage]:
        """获取指定 Agent 的所有未读消息。"""
        if not self.store:
            return []
        return self.store.get_agent_inbox(self.room_id, agent_name)

    def ack_message(self, message_id: str, agent_name: str) -> None:
        """标记消息已读。"""
        if self.store:
            self.store.ack_message(message_id, agent_name)
