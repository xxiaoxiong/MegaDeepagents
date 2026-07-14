"""AgentInstance — 运行时 Agent 实例模型。

AgentProfile 是静态能力模板，AgentInstance 是运行中的 Agent 实体。
每个 AgentInstance 拥有独立 Session、Thread、Inbox 和生命周期。

要求（docs/MegaDeepagents_Agent_Teams_改造任务书.md §8）：
- agent_id / team_id / run_id
- session_id / thread_id / checkpoint_namespace
- 独立状态机
- 心跳和租约
- 持久化（SQLite）
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    """AgentInstance 状态机。"""
    CREATED = "created"
    SPAWNING = "spawning"
    IDLE = "idle"
    CLAIMING = "claiming"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_PERMISSION = "waiting_permission"
    BLOCKED = "blocked"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RECOVERING = "recovering"


# 合法状态转换
_AGENT_LEGAL_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.CREATED: {AgentStatus.SPAWNING, AgentStatus.IDLE, AgentStatus.STOPPED},
    AgentStatus.SPAWNING: {AgentStatus.IDLE, AgentStatus.FAILED},
    AgentStatus.IDLE: {AgentStatus.CLAIMING, AgentStatus.RUNNING, AgentStatus.BLOCKED,
                       AgentStatus.FAILED, AgentStatus.STOPPING, AgentStatus.STOPPED},
    AgentStatus.CLAIMING: {AgentStatus.RUNNING, AgentStatus.IDLE, AgentStatus.FAILED},
    AgentStatus.RUNNING: {AgentStatus.IDLE, AgentStatus.WAITING_TOOL, AgentStatus.WAITING_PERMISSION,
                          AgentStatus.BLOCKED, AgentStatus.FAILED, AgentStatus.STOPPING},
    AgentStatus.WAITING_TOOL: {AgentStatus.RUNNING, AgentStatus.IDLE, AgentStatus.FAILED},
    AgentStatus.WAITING_PERMISSION: {AgentStatus.RUNNING, AgentStatus.IDLE, AgentStatus.FAILED, AgentStatus.STOPPING},
    AgentStatus.BLOCKED: {AgentStatus.RUNNING, AgentStatus.IDLE, AgentStatus.FAILED, AgentStatus.STOPPING},
    AgentStatus.FAILED: {AgentStatus.IDLE, AgentStatus.STOPPING, AgentStatus.STOPPED, AgentStatus.RECOVERING},
    AgentStatus.STOPPING: {AgentStatus.STOPPED, AgentStatus.FAILED},
    AgentStatus.STOPPED: set(),
    AgentStatus.RECOVERING: {AgentStatus.IDLE, AgentStatus.FAILED, AgentStatus.STOPPED},
}


def is_legal_agent_transition(from_status: AgentStatus, to_status: AgentStatus) -> bool:
    return to_status in _AGENT_LEGAL_TRANSITIONS.get(from_status, set())


class AgentInstance(BaseModel):
    """运行时 Agent 实例。"""

    agent_id: str
    team_id: str
    run_id: str

    profile_id: str
    name: str
    role: str
    description: str = ""

    session_id: str
    thread_id: str
    checkpoint_namespace: str

    status: AgentStatus = AgentStatus.CREATED
    current_task_id: str | None = None

    workspace_root: str = ""
    last_heartbeat_at: datetime | None = None

    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    stopped_at: datetime | None = None

    # 并发控制
    max_concurrency: int = 1

    def update_status(self, new_status: AgentStatus) -> bool:
        if is_legal_agent_transition(self.status, new_status):
            self.status = new_status
            self.updated_at = datetime.utcnow()
            if new_status in (AgentStatus.STOPPED, AgentStatus.FAILED):
                self.stopped_at = datetime.utcnow()
            return True
        return False

    def heartbeat(self) -> None:
        self.last_heartbeat_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def is_alive(self) -> bool:
        return self.status not in (AgentStatus.STOPPED, AgentStatus.FAILED)

    def is_idle(self) -> bool:
        return self.status == AgentStatus.IDLE

    def can_work(self) -> bool:
        return self.status in (AgentStatus.IDLE, AgentStatus.RUNNING)


def make_agent_id() -> str:
    import uuid
    return "agent_" + uuid.uuid4().hex[:12]


def make_session_id() -> str:
    import uuid
    return "sess_" + uuid.uuid4().hex[:12]
