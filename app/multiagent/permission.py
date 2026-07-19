"""Structured, persistent permission control for TASK_TEAM tools."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
from app.multiagent.store import _get_conn


class PermissionKind(str, Enum):
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL = "shell"
    NETWORK = "network"
    GIT_BRANCH = "git_branch"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    PACKAGE_INSTALL = "package_install"
    ENVIRONMENT = "environment_access"
    SECRET = "secret_access"
    EXTERNAL_API = "external_api"
    MCP_TOOL = "mcp_tool"
    TEAMMATE_SPAWN = "teammate_spawn"
    TASK_CREATE = "task_creation"
    DESTRUCTIVE = "destructive_operation"


class PermissionDecision(str, Enum):
    APPROVE_ONCE = "approve_once"
    APPROVE_FOR_RUN = "approve_for_run"
    DENY = "deny"
    DENY_WITH_FEEDBACK = "deny_with_feedback"


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    REQUEST = "request"
    DENY = "deny"


class PermissionRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: "preq_" + uuid.uuid4().hex[:16])
    run_id: str
    agent_id: str
    kind: PermissionKind
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    fingerprint: str = ""
    status: str = "pending"
    decision: PermissionDecision | None = None
    decided_by: str | None = None
    decision_reason: str = ""
    scope: str = "once"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: datetime | None = None
    used_at: datetime | None = None

    def model_post_init(self, _context: Any) -> None:
        if not self.fingerprint:
            canonical = json.dumps(
                {"kind": self.kind.value, "operation": self.operation,
                 "parameters": self.parameters}, sort_keys=True, separators=(",", ":"),
            )
            self.fingerprint = hashlib.sha256(canonical.encode()).hexdigest()


class PermissionRequired(RuntimeError):
    def __init__(self, request: PermissionRequest) -> None:
        super().__init__(f"permission_required:{request.request_id}")
        self.request = request


class PermissionDenied(RuntimeError):
    pass


class PermissionPolicy(BaseModel):
    """Two-tier policy after AgentProfile static tool filtering.

    Read-only operations are safe by default.  Writes, network access and Git
    mutations require an explicit approval unless the project policy grants
    them.  Destructive/secret/privilege operations are denied by default.
    """

    allowed: set[PermissionKind] = Field(default_factory=lambda: {PermissionKind.FILE_READ})
    denied: set[PermissionKind] = Field(default_factory=lambda: {
        PermissionKind.SECRET, PermissionKind.DESTRUCTIVE,
    })
    request_by_default: bool = True

    def evaluate(self, kind: PermissionKind, parameters: dict[str, Any] | None = None) -> PolicyOutcome:
        if kind in self.denied:
            return PolicyOutcome.DENY
        if kind in self.allowed:
            return PolicyOutcome.ALLOW
        return PolicyOutcome.REQUEST if self.request_by_default else PolicyOutcome.DENY


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS structured_permission_requests (
            request_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            operation TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_structured_permissions_run
            ON structured_permission_requests(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_structured_permissions_grant
            ON structured_permission_requests(run_id, agent_id, fingerprint, status);
        """
    )
    conn.commit()


class PermissionBroker:
    """The only component allowed to create and decide permission requests."""

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self.policy = policy or PermissionPolicy()
        _ensure_schema()

    def authorize(
        self, *, run_id: str, agent_id: str, kind: PermissionKind,
        operation: str, parameters: dict[str, Any] | None = None,
        reason: str = "",
    ) -> bool:
        request = PermissionRequest(
            run_id=run_id, agent_id=agent_id, kind=kind,
            operation=operation, parameters=parameters or {}, reason=reason,
        )
        grant = self._matching_grant(request)
        if grant is not None:
            if grant.decision == PermissionDecision.APPROVE_ONCE:
                self._mark_used(grant.request_id)
            return True
        outcome = self.policy.evaluate(kind, parameters)
        if outcome == PolicyOutcome.ALLOW:
            self._audit("permission_auto_allowed", request)
            return True
        if outcome == PolicyOutcome.DENY:
            self._audit("permission_denied_by_policy", request)
            raise PermissionDenied(f"{kind.value}:{operation}")
        self._insert(request)
        self._audit("PermissionRequested", request)
        self._emit_requested_hook(request)
        if request.status == "denied":
            raise PermissionDenied(request.decision_reason)
        raise PermissionRequired(request)

    def request(
        self, *, run_id: str, agent_id: str, kind: PermissionKind,
        operation: str, parameters: dict[str, Any] | None = None,
        reason: str = "",
    ) -> PermissionRequest:
        request = PermissionRequest(run_id=run_id, agent_id=agent_id, kind=kind,
                                    operation=operation, parameters=parameters or {},
                                    reason=reason)
        self._insert(request)
        self._audit("PermissionRequested", request)
        self._emit_requested_hook(request)
        return request

    def decide(
        self, request_id: str, decision: PermissionDecision,
        *, decided_by: str, reason: str = "",
    ) -> PermissionRequest:
        # An Agent cannot approve itself or manufacture user authorization in
        # a mailbox message.  API users and the governed lead identity are the
        # only accepted decision principals.
        if not (decided_by == "user" or decided_by.startswith("user:")
                or decided_by == "lead" or decided_by.startswith("lead:")):
            raise PermissionError("permission decisions require user or lead principal")
        current = self.get(request_id)
        if current is None:
            raise KeyError(request_id)
        if current.status != "pending":
            return current
        current.status = "approved" if decision in (
            PermissionDecision.APPROVE_ONCE, PermissionDecision.APPROVE_FOR_RUN,
        ) else "denied"
        current.decision = decision
        current.decided_by = decided_by
        current.decision_reason = reason
        current.scope = "run" if decision == PermissionDecision.APPROVE_FOR_RUN else "once"
        current.decided_at = datetime.utcnow()
        self._update(current)
        self._audit("PermissionDecided", current)
        return current

    def get(self, request_id: str) -> PermissionRequest | None:
        row = _get_conn().execute(
            "SELECT payload FROM structured_permission_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return PermissionRequest.model_validate(json.loads(row["payload"])) if row else None

    def list_pending(self, run_id: str) -> list[PermissionRequest]:
        rows = _get_conn().execute(
            "SELECT payload FROM structured_permission_requests WHERE run_id=? "
            "AND status='pending' ORDER BY created_at", (run_id,),
        ).fetchall()
        return [PermissionRequest.model_validate(json.loads(row["payload"])) for row in rows]

    def _matching_grant(self, request: PermissionRequest) -> PermissionRequest | None:
        rows = _get_conn().execute(
            "SELECT payload FROM structured_permission_requests WHERE run_id=? AND agent_id=? "
            "AND fingerprint=? AND status='approved' ORDER BY created_at DESC",
            (request.run_id, request.agent_id, request.fingerprint),
        ).fetchall()
        for row in rows:
            grant = PermissionRequest.model_validate(json.loads(row["payload"]))
            if grant.decision == PermissionDecision.APPROVE_FOR_RUN or grant.used_at is None:
                return grant
        return None

    def _insert(self, request: PermissionRequest) -> None:
        _get_conn().execute(
            "INSERT OR IGNORE INTO structured_permission_requests "
            "(request_id, run_id, agent_id, kind, operation, fingerprint, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request.request_id, request.run_id, request.agent_id, request.kind.value,
             request.operation, request.fingerprint,
             json.dumps(request.model_dump(mode="json")), request.status,
             request.created_at.isoformat()),
        )
        _get_conn().commit()

    def _update(self, request: PermissionRequest) -> None:
        _get_conn().execute(
            "UPDATE structured_permission_requests SET payload=?, status=?, decided_at=?, used_at=? "
            "WHERE request_id=?",
            (json.dumps(request.model_dump(mode="json")), request.status,
             request.decided_at.isoformat() if request.decided_at else None,
             request.used_at.isoformat() if request.used_at else None, request.request_id),
        )
        _get_conn().commit()

    def _mark_used(self, request_id: str) -> None:
        request = self.get(request_id)
        if request is None:
            return
        request.used_at = datetime.utcnow()
        self._update(request)

    @staticmethod
    def _audit(event_type: str, request: PermissionRequest) -> None:
        get_agent_run_history().record_event(
            event_id=make_run_event_id(), run_id=request.run_id,
            event_type=event_type, agent_id=request.agent_id,
            payload={"request_id": request.request_id, "kind": request.kind.value,
                     "operation": request.operation, "status": request.status,
                     "decided_by": request.decided_by},
        )

    @staticmethod
    def _emit_requested_hook(request: PermissionRequest) -> None:
        from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
        result = get_lifecycle_hook_engine().emit(
            LifecycleEvent.PERMISSION_REQUESTED,
            {"run_id": request.run_id, "agent_id": request.agent_id,
             "request_id": request.request_id, "kind": request.kind.value,
             "operation": request.operation, "parameters": request.parameters},
        )
        if result.block:
            # A hook may tighten policy, never approve the request.
            request.status = "denied"
            request.decision = PermissionDecision.DENY_WITH_FEEDBACK
            request.decided_by = "policy:hook"
            request.decision_reason = result.feedback or "PermissionRequested hook denied"
            request.decided_at = datetime.utcnow()
            _get_conn().execute(
                "UPDATE structured_permission_requests SET payload=?, status=?, decided_at=? "
                "WHERE request_id=?",
                (json.dumps(request.model_dump(mode="json")), request.status,
                 request.decided_at.isoformat(), request.request_id),
            )
            _get_conn().commit()


_broker: PermissionBroker | None = None


def get_permission_broker() -> PermissionBroker:
    global _broker
    if _broker is None:
        _broker = PermissionBroker()
    return _broker


def reset_permission_broker() -> None:
    global _broker
    _broker = None
