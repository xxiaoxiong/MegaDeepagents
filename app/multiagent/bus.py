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


class MessageBus:
    """消息总线。不直接依赖 store，但可关联 store 作持久化。

    初始化时传入 Agent 列表和 Room ID，以便路由。
    """

    def __init__(
        self,
        room_id: str,
        task_id: str,
        agents: list[AgentSpec],
        store: Any | None = None,
    ):
        self.room_id = room_id
        self.task_id = task_id
        self._agents = {a.name: a for a in agents}
        self._agent_subscriptions: dict[str, AgentSubscription] = {
            a.name: a.get_subscription() for a in agents
        }
        self.store = store
        self._transcript: list[AgentMessage] = []

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

        # 别名归一化：LLM 常用 "TesterAgent"/"DeveloperAgent" 等，
        # 如果在已知名字基础上加减 "Agent"/"er" 后缀能匹配到真实 agent，则重写
        normalized_targets: list[str] = []
        aliases_used = False
        for t in targets:
            if t in known_agents:
                normalized_targets.append(t)
                continue
            # 尝试常见 LLM 命名偏差：去掉或加上 "Agent" 后缀
            candidate = t
            matched = None
            for ka in known_agents:
                if ka in t or t in ka:
                    matched = ka
                    break
                ka_no_suffix = ka.replace("Agent", "")
                t_no_suffix = t.replace("Agent", "")
                if ka_no_suffix and t_no_suffix and (ka_no_suffix == t_no_suffix):
                    matched = ka
                    break
                # 角色名小写匹配
                if ka.lower() in t.lower() or t.lower() in ka.lower():
                    matched = ka
                    break
            if matched:
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

        for name in normalized_targets:
            self._deliver_to_inbox(name, message)

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
