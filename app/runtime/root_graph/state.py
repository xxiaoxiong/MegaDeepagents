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
    # Per-base-task repair round counter.  ``repair_round`` (above) is a global
    # safety net; this dict tracks how many repair tasks have been created for
    # each original task chain (keyed by the base id — the prefix before the
    # first ``__repair_v`` marker).  Without per-task tracking, one task's
    # repair chain can exhaust the global budget and starve every other task
    # (run_55507ebfce5744e8: task_1 used 3/5 global rounds, task_2 and task_3
    # shared the remaining 2, and the run failed even though v15 and v33
    # succeeded).
    repair_rounds_by_task: dict[str, int]
    final_output: str | None
    error: str | None
    status: str
