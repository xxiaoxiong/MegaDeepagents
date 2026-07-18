"""Governed intelligent-lead surface over deterministic control plane state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.multiagent.plan_approval import PlanApprovalService, TeammatePlan


@dataclass
class LeadRecommendation:
    action: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


class LeadCoordinatorAgent:
    """Observe and recommend; never mutate Scheduler/TaskBoard directly."""

    def __init__(self, control_plane: Any | None = None,
                 plan_approvals: PlanApprovalService | None = None) -> None:
        self.control_plane = control_plane
        self.plan_approvals = plan_approvals or PlanApprovalService()

    def inspect(self, run_id: str) -> list[LeadRecommendation]:
        if self.control_plane is None:
            return []
        tasks = self.control_plane.team_list_tasks(run_id, self.control_plane.lead_agent_id)
        members = self.control_plane.team_list_members(run_id, self.control_plane.lead_agent_id)
        recommendations: list[LeadRecommendation] = []
        if any(task["status"] == "repair_required" for task in tasks):
            recommendations.append(LeadRecommendation("request_replan",
                                                       "verification repair is pending"))
        if tasks and all(task["status"] in {"succeeded", "cancelled"} for task in tasks):
            recommendations.append(LeadRecommendation("finalize", "all necessary tasks verified"))
        if not any(member["status"] == "idle" for member in members):
            recommendations.append(LeadRecommendation("observe", "all teammates are occupied"))
        return recommendations

    def review_plan(self, plan: TeammatePlan) -> TeammatePlan:
        return self.plan_approvals.submit(plan)
