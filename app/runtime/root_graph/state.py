"""Checkpoint-safe, lightweight LangGraph state."""

from __future__ import annotations

from typing import Any, TypedDict


class AgentRunState(TypedDict, total=False):
    run_id: str
    goal: str
    requested_mode: str
    mode: str
    phase: str
    task_graph_version: int
    task_graph_json: str
    active_task_ids: list[str]
    completed_task_ids: list[str]
    blocked_task_ids: list[str]
    pending_permission_ids: list[str]
    pending_plan_ids: list[str]
    verification_summary: dict[str, Any]
    supervisor_decision: dict[str, Any] | None
    dispatch_status: str
    dispatch_rounds: int
    repair_round: int
    final_output: str | None
    error: str | None
    status: str
