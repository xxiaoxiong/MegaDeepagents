"""Contracts exchanged between the governed scheduler and worker harness."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskExecutionResult:
    task_id: str
    success: bool
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    attempted: bool = False
