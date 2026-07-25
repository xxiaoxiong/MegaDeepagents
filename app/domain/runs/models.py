"""Run-domain models shared by API, runtime, and persistence."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunMode(str, Enum):
    AUTO = "auto"
    SINGLE = "single"
    TEAM = "team"


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SupervisorDecision(BaseModel):
    """A decision proposal; mutation is still performed by the control plane."""

    action: Literal[
        "create_tasks",
        "dispatch",
        "spawn_teammate",
        "wait",
        "repair",
        "replan",
        "request_human",
        "finalize",
    ]
    selected_mode: Literal["single", "team"] = "team"
    task_ids: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    reason_summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
