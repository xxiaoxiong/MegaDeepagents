"""AgentInstance — 运行时 Agent 实例模型。

AgentProfile 是静态能力模板，AgentInstance 是运行中的 Agent 实体。
每个 AgentInstance 拥有独立 Session、Thread、Inbox 和生命周期。

V3 Agent identity requirements：
- agent_id / team_id / run_id
- session_id / thread_id / checkpoint_namespace
- 独立状态机
- 心跳和租约
- 持久化（SQLite）
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr


class AgentStatus(str, Enum):
    """AgentInstance 状态机。"""
    CREATED = "created"
    SPAWNING = "spawning"
    IDLE = "idle"
    CLAIMING = "claiming"
    PLANNING = "planning"
    WAITING_PLAN_APPROVAL = "waiting_plan_approval"
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
    AgentStatus.CLAIMING: {
        AgentStatus.PLANNING, AgentStatus.RUNNING, AgentStatus.IDLE,
        AgentStatus.FAILED, AgentStatus.STOPPING,
    },
    AgentStatus.PLANNING: {
        AgentStatus.WAITING_PLAN_APPROVAL, AgentStatus.RUNNING,
        AgentStatus.IDLE, AgentStatus.FAILED, AgentStatus.STOPPING,
    },
    AgentStatus.WAITING_PLAN_APPROVAL: {
        AgentStatus.PLANNING, AgentStatus.RUNNING, AgentStatus.IDLE,
        AgentStatus.FAILED, AgentStatus.STOPPING,
    },
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
    worktree_path: str = ""
    mailbox_cursor: int = 0
    last_heartbeat_at: datetime | None = None

    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stopped_at: datetime | None = None

    # 并发控制
    max_concurrency: int = 1

    # Per-instance lock guarding the check-then-act in ``update_status`` and
    # ``heartbeat``.  Pydantic v2 ignores non-field attributes during model
    # serialization, so a plain ``threading.Lock`` is safe here.  The registry
    # also holds its own RLock around batches of transitions, but callers
    # sometimes mutate an agent outside the registry (e.g. ``stop`` issued
    # from a worker thread while ``cleanup_expired`` runs in the heartbeat
    # loop), so the instance needs its own guard to make the state machine
    # transition atomic.
    _state_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def update_status(self, new_status: AgentStatus) -> bool:
        """Atomically validate and apply a state transition.

        ``is_legal_agent_transition(self.status, ...)`` followed by
        ``self.status = ...`` was a check-then-act with no synchronization:
        ``cleanup_expired`` (in the heartbeat loop) and ``stop`` (from a
        worker thread) could both observe the old status, both decide the
        transition was legal, and both apply it — leaving the agent in an
        inconsistent state (e.g. ``FAILED`` without ``stopped_at``).
        """
        with self._state_lock:
            if is_legal_agent_transition(self.status, new_status):
                self.status = new_status
                self.updated_at = datetime.now(UTC)
                if new_status in (AgentStatus.STOPPED, AgentStatus.FAILED):
                    self.stopped_at = datetime.now(UTC)
                return True
            return False

    def heartbeat(self) -> None:
        with self._state_lock:
            self.last_heartbeat_at = datetime.now(UTC)
            self.updated_at = datetime.now(UTC)

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
