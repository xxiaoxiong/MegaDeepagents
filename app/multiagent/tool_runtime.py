"""Durable idempotency journal for tool side effects and restart recovery."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.store import _get_conn


class ToolInvocationStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_COMPENSATION = "needs_compensation"
    NEEDS_HUMAN = "needs_human"


class ToolInvocation(BaseModel):
    invocation_id: str = Field(default_factory=lambda: "tool_" + uuid.uuid4().hex[:16])
    idempotency_key: str
    run_id: str
    agent_id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    side_effecting: bool = True
    status: ToolInvocationStatus = ToolInvocationStatus.STARTED
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def key_for(cls, run_id: str, agent_id: str, task_id: str,
                tool_name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps({"run_id": run_id, "agent_id": agent_id,
                                "task_id": task_id, "tool": tool_name,
                                "arguments": arguments}, sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_invocations (
            invocation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
            tool_name TEXT NOT NULL, status TEXT NOT NULL, side_effecting INTEGER NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()


class ToolSideEffectJournal:
    def __init__(self) -> None:
        _ensure_schema()

    def begin(self, invocation: ToolInvocation) -> tuple[ToolInvocation, bool]:
        existing = self.get(invocation.idempotency_key)
        if existing is not None:
            return existing, False
        self._save(invocation)
        return invocation, True

    def complete(self, idempotency_key: str, result: dict[str, Any]) -> ToolInvocation:
        item = self._require(idempotency_key)
        item.status = ToolInvocationStatus.COMPLETED
        item.result = result
        item.updated_at = datetime.utcnow()
        self._save(item)
        return item

    def fail(self, idempotency_key: str, error: str, *, cancelled: bool = False) -> ToolInvocation:
        item = self._require(idempotency_key)
        item.status = ToolInvocationStatus.CANCELLED if cancelled else ToolInvocationStatus.FAILED
        item.error = error
        item.updated_at = datetime.utcnow()
        self._save(item)
        return item

    def recover_incomplete(self, run_id: str) -> list[ToolInvocation]:
        rows = _get_conn().execute(
            "SELECT payload FROM tool_invocations WHERE run_id=? AND status='started'",
            (run_id,),
        ).fetchall()
        recovered = []
        for row in rows:
            item = ToolInvocation.model_validate(json.loads(row["payload"]))
            item.status = (ToolInvocationStatus.NEEDS_HUMAN if item.side_effecting
                           else ToolInvocationStatus.FAILED)
            item.error = "process_restarted_during_tool"
            item.updated_at = datetime.utcnow()
            self._save(item)
            recovered.append(item)
        return recovered

    def get(self, idempotency_key: str) -> ToolInvocation | None:
        row = _get_conn().execute(
            "SELECT payload FROM tool_invocations WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return ToolInvocation.model_validate(json.loads(row["payload"])) if row else None

    def _require(self, key: str) -> ToolInvocation:
        item = self.get(key)
        if item is None:
            raise KeyError(key)
        return item

    @staticmethod
    def _save(item: ToolInvocation) -> None:
        _get_conn().execute(
            "INSERT INTO tool_invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status, "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (item.invocation_id, item.idempotency_key, item.run_id, item.agent_id,
             item.task_id, item.tool_name, item.status.value, int(item.side_effecting),
             json.dumps(item.model_dump(mode="json")), item.created_at.isoformat(),
             item.updated_at.isoformat()),
        )
        _get_conn().commit()
