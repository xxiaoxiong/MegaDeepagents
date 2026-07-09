"""TeamRoom：多智能体任务环境。

类比 MetaGPT 的 Environment。一个 TeamRoom 对应一次多 Agent 任务：
- 加载 TeamSpec 并实例化 AgentSpec[]
- 持有 MessageBus + AgentInbox + SharedTeamState
- 管理生命周期（created → ... → completed/failed/cancelled）
- 与 store 集成，持久化所有消息 / 入箱 / 状态变更

注意：TeamRoom 不直接驱动 LLM 执行，那是 TeamRunner 的职责。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import AgentSpec, TeamSpec, TeamRunConfig
from app.multiagent.bus import MessageBus
from app.multiagent.inbox import AgentInbox
from app.multiagent.messages import AgentMessage, MessageType, make_message_id
from app.multiagent.state import SharedTeamState, TeamPhase


class TeamRoom:
    """多智能体任务环境。"""

    def __init__(
        self,
        room_id: str,
        task_id: str,
        config: TeamRunConfig,
        team_spec: TeamSpec,
        store: Any | None = None,
    ):
        self.room_id = room_id
        self.task_id = task_id
        self.config = config
        self.team_spec = team_spec
        self.agents: list[AgentSpec] = list(team_spec.agents)
        self.store = store

        self.bus = MessageBus(
            room_id=room_id,
            task_id=task_id,
            agents=self.agents,
            store=store,
        )
        self.inbox = AgentInbox(store=store, room_id=room_id, task_id=task_id)
        self.state = SharedTeamState(
            room_id=room_id,
            task_id=task_id,
            goal=config.goal,
            phase=TeamPhase.CREATED,
            max_rounds=config.max_rounds or team_spec.max_rounds,
        )

        self._terminated = False
        self._cancel_requested = False

    # ========== 工厂方法 ==========

    @classmethod
    def create(
        cls,
        task_id: str,
        config: TeamRunConfig,
        team_spec: TeamSpec,
        store: Any | None = None,
        room_id: str | None = None,
    ) -> "TeamRoom":
        """创建并持久化一个 TeamRoom。"""
        room_id = room_id or ("room_" + uuid.uuid4().hex[:12])
        room = cls(
            room_id=room_id,
            task_id=task_id,
            config=config,
            team_spec=team_spec,
            store=store,
        )
        if store:
            store.save_room(room)
            for agent in room.agents:
                store.save_agent(room_id, agent)
            store.save_state(room.state)
        logger.info(f"TeamRoom created: room_id={room_id}, task_id={task_id}, agents={len(room.agents)}")
        return room

    @classmethod
    def load(
        cls,
        room_id: str,
        store: Any,
    ) -> "TeamRoom | None":
        """从 store 加载已存在的 TeamRoom。"""
        meta = store.load_room(room_id)
        if not meta:
            return None
        team_spec = meta["team_spec"]
        config = meta["config"]
        task_id = meta["task_id"]
        agents = store.load_agents(room_id)
        # 用加载的 agents 覆盖 team_spec.agents，保证一致性
        team_spec = team_spec.model_copy(update={"agents": agents})
        state = store.load_state(room_id) or SharedTeamState(room_id=room_id, task_id=task_id, goal=config.goal)

        room = cls(
            room_id=room_id,
            task_id=task_id,
            config=config,
            team_spec=team_spec,
            store=store,
        )
        room.state = state
        return room

    # ========== 消息 ==========

    def publish(self, message: AgentMessage) -> None:
        message.room_id = self.room_id
        message.task_id = self.task_id
        self.bus.publish(message)
        if self.store:
            self.store.save_room_meta_timestamp(self.room_id, self.state)

    def send_system_message(
        self,
        content: str,
        message_type: MessageType = MessageType.USER_REQUEST,
        cause_by: str | None = "user",
    ) -> AgentMessage:
        """以框架身份发一条 system / 初始消息，通常用于注入用户目标。"""
        msg = AgentMessage(
            id=make_message_id(),
            task_id=self.task_id,
            room_id=self.room_id,
            from_agent="system",
            visibility=(MessageType.USER_REQUEST == message_type)
            and __import__("app.multiagent.messages", fromlist=["MessageVisibility"]).MessageVisibility.BROADCAST
            or __import__("app.multiagent.messages", fromlist=["MessageVisibility"]).MessageVisibility.BROADCAST,
            message_type=message_type,
            content=content,
            cause_by=cause_by,
        )
        self.publish(msg)
        return msg

    # ========== 状态访问 ==========

    def get_agent(self, name: str) -> AgentSpec | None:
        for a in self.agents:
            if a.name == name:
                return a
        return None

    def get_state_for_prompt(self) -> str:
        return self.state.to_prompt_context()

    def get_inbox_for_prompt(self, agent_name: str, max_items: int = 8) -> str:
        return self.inbox.get_relevant_context(agent_name, max_items)

    # ========== 生命周期 ==========

    def update_state(self, mutator) -> None:
        """对 SharedTeamState 应用一段修改，写回 store。"""
        changes = mutator(self.state)
        self.state.updated_at = datetime.utcnow()
        if self.store:
            self.store.save_state(self.state)
            self.store.save_room_meta_timestamp(self.room_id, self.state)
        return changes

    def mark_terminated(self) -> None:
        self._terminated = True
        if self.store:
            self.store.save_state(self.state)

    def is_terminated(self) -> bool:
        return self._terminated

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def is_cancel_requested(self) -> bool:
        return self._cancel_requested

    def is_idle(self) -> bool:
        """如果所有 Agent 都没有 requires_response 消息，则视为 idle。"""
        try:
            for agent in self.agents:
                unread = self.inbox.list_unread(agent.name)
                for m in unread:
                    if m.requires_response:
                        return False
            return True
        except Exception:
            return False
