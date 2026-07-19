"""Persistent, stable teammate sessions for the TASK_TEAM runtime.

An :class:`AgentInstance` is the control-plane identity.  A
:class:`TeammateSession` is the durable execution identity behind it.  The
session survives task boundaries and process restarts; commands and events are
stored before delivery so a restarted actor resumes the same conversation,
mailbox cursor, worktree and checkpoint namespace.

This module is intentionally not a scheduler.  ``ParallelTeamScheduler``
still owns task selection and atomic claims.  The supervisor only turns an
already-selected assignment into a command for the stable actor.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.multiagent.store import _get_conn


class TeammateLifecycle(str, Enum):
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
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


_LEGAL: dict[TeammateLifecycle, set[TeammateLifecycle]] = {
    TeammateLifecycle.CREATED: {TeammateLifecycle.SPAWNING, TeammateLifecycle.IDLE,
                                TeammateLifecycle.STOPPING, TeammateLifecycle.FAILED},
    TeammateLifecycle.SPAWNING: {TeammateLifecycle.IDLE, TeammateLifecycle.FAILED},
    TeammateLifecycle.IDLE: {TeammateLifecycle.CLAIMING, TeammateLifecycle.PLANNING,
                             TeammateLifecycle.RUNNING, TeammateLifecycle.BLOCKED,
                             TeammateLifecycle.STOPPING, TeammateLifecycle.FAILED},
    TeammateLifecycle.CLAIMING: {TeammateLifecycle.PLANNING, TeammateLifecycle.RUNNING,
                                 TeammateLifecycle.IDLE, TeammateLifecycle.STOPPING,
                                 TeammateLifecycle.FAILED},
    TeammateLifecycle.PLANNING: {TeammateLifecycle.WAITING_PLAN_APPROVAL,
                                 TeammateLifecycle.RUNNING, TeammateLifecycle.IDLE,
                                 TeammateLifecycle.STOPPING, TeammateLifecycle.FAILED},
    TeammateLifecycle.WAITING_PLAN_APPROVAL: {TeammateLifecycle.PLANNING,
                                              TeammateLifecycle.RUNNING,
                                              TeammateLifecycle.IDLE,
                                              TeammateLifecycle.STOPPING,
                                              TeammateLifecycle.FAILED},
    TeammateLifecycle.RUNNING: {TeammateLifecycle.WAITING_TOOL,
                                TeammateLifecycle.WAITING_PERMISSION,
                                TeammateLifecycle.BLOCKED, TeammateLifecycle.IDLE,
                                TeammateLifecycle.STOPPING, TeammateLifecycle.FAILED},
    TeammateLifecycle.WAITING_TOOL: {TeammateLifecycle.RUNNING,
                                     TeammateLifecycle.WAITING_PERMISSION,
                                     TeammateLifecycle.IDLE, TeammateLifecycle.STOPPING,
                                     TeammateLifecycle.FAILED},
    TeammateLifecycle.WAITING_PERMISSION: {TeammateLifecycle.RUNNING,
                                           TeammateLifecycle.IDLE,
                                           TeammateLifecycle.STOPPING,
                                           TeammateLifecycle.FAILED},
    TeammateLifecycle.BLOCKED: {TeammateLifecycle.IDLE, TeammateLifecycle.RUNNING,
                                TeammateLifecycle.STOPPING, TeammateLifecycle.FAILED},
    TeammateLifecycle.STOPPING: {TeammateLifecycle.STOPPED, TeammateLifecycle.FAILED},
    TeammateLifecycle.STOPPED: set(),
    TeammateLifecycle.FAILED: {TeammateLifecycle.IDLE, TeammateLifecycle.STOPPING,
                               TeammateLifecycle.STOPPED},
}


class TeammateCommandType(str, Enum):
    ASSIGN_TASK = "assign_task"
    MESSAGE = "message"
    WAKE = "wake"
    CANCEL = "cancel"
    STOP = "stop"
    PERMISSION_DECISION = "permission_decision"
    PLAN_DECISION = "plan_decision"


class TeammateEventType(str, Enum):
    STATE_CHANGED = "state_changed"
    TASK_ACCEPTED = "task_accepted"
    TASK_PRODUCED = "task_produced"
    MESSAGE_RECEIVED = "message_received"
    SAFETY_POINT = "safety_point"
    ERROR = "error"


class QueueItem(BaseModel):
    item_id: str = Field(default_factory=lambda: "q_" + uuid.uuid4().hex[:16])
    session_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeammateSession(BaseModel):
    run_id: str
    agent_id: str
    profile_id: str
    session_id: str
    thread_id: str
    checkpoint_namespace: str
    current_task_id: str | None = None
    conversation_state: dict[str, Any] = Field(default_factory=dict)
    lifecycle_state: TeammateLifecycle = TeammateLifecycle.CREATED
    workspace: str = ""
    worktree: str = ""
    inbox: list[dict[str, Any]] = Field(default_factory=list)
    mailbox_cursor: int = 0
    permission_request_ids: list[str] = Field(default_factory=list)
    last_activity_at: datetime = Field(default_factory=datetime.utcnow)
    current_tool_call: dict[str, Any] | None = None
    cancellation_requested: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def transition(self, target: TeammateLifecycle) -> None:
        if target == self.lifecycle_state:
            return
        if target not in _LEGAL.get(self.lifecycle_state, set()):
            raise ValueError(f"illegal teammate transition: {self.lifecycle_state.value}->{target.value}")
        self.lifecycle_state = target
        self.last_activity_at = datetime.utcnow()


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS teammate_sessions (
            session_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            agent_id TEXT NOT NULL UNIQUE,
            profile_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_teammate_sessions_run
            ON teammate_sessions(run_id);
        CREATE TABLE IF NOT EXISTS teammate_queue_items (
            item_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            queue_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            UNIQUE(session_id, queue_type, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_teammate_queue_pending
            ON teammate_queue_items(session_id, queue_type, status, sequence);
        """
    )
    conn.commit()


class _PersistentQueue:
    def __init__(self, session_id: str, queue_type: str) -> None:
        self.session_id = session_id
        self.queue_type = queue_type
        self._condition = threading.Condition()
        _ensure_schema()

    def put(self, kind: str, payload: dict[str, Any] | None = None) -> QueueItem:
        conn = _get_conn()
        # SQLite serializes this short sequence allocation.  The item id also
        # makes a retried insert idempotent at the API boundary.
        with self._condition:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM teammate_queue_items "
                "WHERE session_id=? AND queue_type=?",
                (self.session_id, self.queue_type),
            ).fetchone()
            item = QueueItem(session_id=self.session_id, kind=kind,
                             payload=payload or {}, sequence=int(row["seq"]))
            conn.execute(
                "INSERT INTO teammate_queue_items "
                "(item_id, session_id, queue_type, kind, payload, sequence, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (item.item_id, item.session_id, self.queue_type, item.kind,
                 json.dumps(item.payload), item.sequence, item.created_at.isoformat()),
            )
            conn.commit()
            self._condition.notify_all()
            return item

    def pending(self, limit: int = 100) -> list[QueueItem]:
        rows = _get_conn().execute(
            "SELECT * FROM teammate_queue_items WHERE session_id=? AND queue_type=? "
            "AND status='pending' ORDER BY sequence LIMIT ?",
            (self.session_id, self.queue_type, limit),
        ).fetchall()
        return [QueueItem(item_id=row["item_id"], session_id=row["session_id"],
                          kind=row["kind"], payload=json.loads(row["payload"] or "{}"),
                          sequence=row["sequence"],
                          created_at=datetime.fromisoformat(row["created_at"])) for row in rows]

    def ack(self, item_id: str) -> bool:
        cur = _get_conn().execute(
            "UPDATE teammate_queue_items SET status='consumed', consumed_at=? "
            "WHERE item_id=? AND session_id=? AND status='pending'",
            (datetime.utcnow().isoformat(), item_id, self.session_id),
        )
        _get_conn().commit()
        return cur.rowcount == 1

    async def wait(self, timeout: float | None = None) -> QueueItem | None:
        existing = self.pending(limit=1)
        if existing:
            return existing[0]
        await asyncio.to_thread(self._wait_sync, timeout)
        items = self.pending(limit=1)
        return items[0] if items else None

    def _wait_sync(self, timeout: float | None) -> None:
        with self._condition:
            self._condition.wait(timeout)


class TeammateCommandQueue(_PersistentQueue):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, "command")


class TeammateEventQueue(_PersistentQueue):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, "event")


class TeammateSessionActor:
    """Long-lived actor facade around one durable session."""

    def __init__(self, session: TeammateSession, supervisor: "TeammateSupervisor") -> None:
        self.session = session
        self.supervisor = supervisor
        self.commands = TeammateCommandQueue(session.session_id)
        self.events = TeammateEventQueue(session.session_id)
        self._stop = asyncio.Event()

    def safety_point(self) -> dict[str, Any]:
        """Consume control commands at a tool-loop boundary.

        Messages are appended to the preserved conversation state.  Stop and
        cancel are sticky so a late tool result cannot become task evidence.
        Permission/plan decisions remain explicit control-plane records and
        are merely surfaced to the actor here.
        """
        observed: dict[str, Any] = {"messages": [], "decisions": []}
        for item in self.commands.pending():
            if item.kind == TeammateCommandType.MESSAGE.value:
                observed["messages"].append(item.payload)
                self.session.inbox.append(item.payload)
                self.session.conversation_state.setdefault("messages", []).append(item.payload)
            elif item.kind in (TeammateCommandType.CANCEL.value, TeammateCommandType.STOP.value):
                self.session.cancellation_requested = True
                observed["cancelled"] = True
            elif item.kind in (TeammateCommandType.PERMISSION_DECISION.value,
                               TeammateCommandType.PLAN_DECISION.value):
                observed["decisions"].append(item.payload)
            self.commands.ack(item.item_id)
        self.session.last_activity_at = datetime.utcnow()
        self.supervisor.persist(self.session)
        self.events.put(TeammateEventType.SAFETY_POINT.value, observed)
        return observed

    async def run(self, handler: Callable[[QueueItem, TeammateSession], Awaitable[Any]]) -> None:
        """Wait for commands until explicitly stopped; task completion returns to IDLE."""
        if self.session.lifecycle_state == TeammateLifecycle.CREATED:
            self.session.transition(TeammateLifecycle.SPAWNING)
            self.session.transition(TeammateLifecycle.IDLE)
            self.supervisor.persist(self.session)
        while not self._stop.is_set() and self.session.lifecycle_state != TeammateLifecycle.STOPPED:
            item = await self.commands.wait(timeout=0.25)
            if item is None:
                continue
            if item.kind == TeammateCommandType.STOP.value:
                self.commands.ack(item.item_id)
                self.stop()
                break
            if item.kind != TeammateCommandType.ASSIGN_TASK.value:
                self.safety_point()
                continue
            self.commands.ack(item.item_id)
            self.session.current_task_id = item.payload.get("task_id")
            self.session.transition(TeammateLifecycle.CLAIMING)
            self.session.transition(TeammateLifecycle.RUNNING)
            self.supervisor.persist(self.session)
            try:
                await handler(item, self.session)
                if not self.session.cancellation_requested:
                    self.session.transition(TeammateLifecycle.IDLE)
            except Exception as exc:
                self.events.put(TeammateEventType.ERROR.value, {"error": str(exc)})
                self.session.transition(TeammateLifecycle.FAILED)
            finally:
                self.session.current_task_id = None
                self.supervisor.persist(self.session)

    def stop(self) -> None:
        self._stop.set()
        if self.session.lifecycle_state not in (TeammateLifecycle.STOPPED, TeammateLifecycle.FAILED):
            self.session.transition(TeammateLifecycle.STOPPING)
            self.session.transition(TeammateLifecycle.STOPPED)
        self.supervisor.persist(self.session)


class TeammateSupervisor:
    """Own one stable session/actor per AgentInstance."""

    def __init__(self) -> None:
        self._sessions: dict[str, TeammateSession] = {}
        self._actors: dict[str, TeammateSessionActor] = {}
        self._lock = threading.RLock()
        _ensure_schema()

    def ensure_session(self, agent: Any) -> TeammateSession:
        with self._lock:
            cached = self._sessions.get(agent.agent_id)
            if cached is not None:
                return cached
            restored = self.load(agent.agent_id)
            if restored is not None:
                if (restored.run_id, restored.session_id, restored.thread_id) != (
                    agent.run_id, agent.session_id, agent.thread_id
                ):
                    raise RuntimeError("persisted teammate identity does not match AgentInstance")
                self._sessions[agent.agent_id] = restored
                return restored
            session = TeammateSession(
                run_id=agent.run_id, agent_id=agent.agent_id,
                profile_id=agent.profile_id, session_id=agent.session_id,
                thread_id=agent.thread_id,
                checkpoint_namespace=agent.checkpoint_namespace,
                lifecycle_state=TeammateLifecycle.IDLE,
                workspace=agent.workspace_root,
                worktree=getattr(agent, "worktree_path", "") or agent.metadata.get("worktree_path", ""),
                mailbox_cursor=getattr(agent, "mailbox_cursor", 0),
            )
            self.persist(session)
            self._sessions[agent.agent_id] = session
            return session

    def actor_for(self, agent: Any) -> TeammateSessionActor:
        with self._lock:
            actor = self._actors.get(agent.agent_id)
            if actor is None:
                actor = TeammateSessionActor(self.ensure_session(agent), self)
                self._actors[agent.agent_id] = actor
            return actor

    def persist(self, session: TeammateSession) -> None:
        _ensure_schema()
        payload = session.model_dump(mode="json")
        _get_conn().execute(
            "INSERT INTO teammate_sessions(session_id, run_id, agent_id, profile_id, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (session.session_id, session.run_id, session.agent_id, session.profile_id,
             json.dumps(payload), datetime.utcnow().isoformat()),
        )
        _get_conn().commit()

    def load(self, agent_id: str) -> TeammateSession | None:
        row = _get_conn().execute(
            "SELECT payload FROM teammate_sessions WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        return TeammateSession.model_validate(json.loads(row["payload"]))

    def list_by_run(self, run_id: str) -> list[TeammateSession]:
        rows = _get_conn().execute(
            "SELECT payload FROM teammate_sessions WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [TeammateSession.model_validate(json.loads(row["payload"])) for row in rows]


_supervisor: TeammateSupervisor | None = None


def get_teammate_supervisor() -> TeammateSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = TeammateSupervisor()
    return _supervisor


def reset_teammate_supervisor() -> None:
    global _supervisor
    _supervisor = None
