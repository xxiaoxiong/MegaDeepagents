"""Durable teammate plan approval gate."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.store import _get_conn


class PlanStatus(str, Enum):
    PLANNING = "planning"
    WAITING_PLAN_APPROVAL = "waiting_plan_approval"
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"


class TeammatePlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: "plan_" + uuid.uuid4().hex[:16])
    run_id: str
    agent_id: str
    task_id: str
    files: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    test_strategy: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    rollback: str = ""
    status: PlanStatus = PlanStatus.WAITING_PLAN_APPROVAL
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None
    feedback: str = ""


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS teammate_plans (
            plan_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            task_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
            submitted_at TEXT NOT NULL, decided_at TEXT
        )"""
    )
    conn.commit()


class PlanApprovalService:
    def __init__(self, require_human_for_high_risk: bool = True) -> None:
        self.require_human_for_high_risk = require_human_for_high_risk
        _ensure_schema()

    def submit(self, plan: TeammatePlan) -> TeammatePlan:
        if not plan.steps or not plan.test_strategy:
            plan.status = PlanStatus.PLAN_REJECTED
            plan.feedback = "plan requires steps and test strategy"
            plan.decided_by = "lead:auto"
            plan.decided_at = datetime.utcnow()
        elif not plan.risks and len(plan.files) <= 10:
            plan.status = PlanStatus.PLAN_APPROVED
            plan.decided_by = "lead:auto"
            plan.decided_at = datetime.utcnow()
        else:
            plan.status = PlanStatus.WAITING_PLAN_APPROVAL
        self._save(plan)
        return plan

    def decide(self, plan_id: str, approved: bool, *, decided_by: str,
               feedback: str = "") -> TeammatePlan:
        if not (decided_by.startswith("user") or decided_by.startswith("lead")):
            raise PermissionError("plan decision requires lead or user")
        plan = self.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        plan.status = PlanStatus.PLAN_APPROVED if approved else PlanStatus.PLAN_REJECTED
        plan.decided_by = decided_by
        plan.decided_at = datetime.utcnow()
        plan.feedback = feedback
        self._save(plan)
        return plan

    def get(self, plan_id: str) -> TeammatePlan | None:
        row = _get_conn().execute("SELECT payload FROM teammate_plans WHERE plan_id=?",
                                  (plan_id,)).fetchone()
        return TeammatePlan.model_validate(json.loads(row["payload"])) if row else None

    def list_pending(self, run_id: str) -> list[TeammatePlan]:
        rows = _get_conn().execute(
            "SELECT payload FROM teammate_plans WHERE run_id=? AND status=?",
            (run_id, PlanStatus.WAITING_PLAN_APPROVAL.value),
        ).fetchall()
        return [TeammatePlan.model_validate(json.loads(row["payload"])) for row in rows]

    @staticmethod
    def _save(plan: TeammatePlan) -> None:
        _get_conn().execute(
            "INSERT INTO teammate_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status, payload=excluded.payload, "
            "decided_at=excluded.decided_at",
            (plan.plan_id, plan.run_id, plan.agent_id, plan.task_id, plan.status.value,
             json.dumps(plan.model_dump(mode="json")), plan.submitted_at.isoformat(),
             plan.decided_at.isoformat() if plan.decided_at else None),
        )
        _get_conn().commit()
