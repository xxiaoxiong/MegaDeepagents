"""Public V3/V1 HTTP contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.runs.models import RunMode


class ApiModel(BaseModel):
    """Stable public model that can tolerate additive server-side fields."""

    model_config = ConfigDict(extra="ignore")


class FlexibleResponse(BaseModel):
    """Typed OpenAPI envelope for compatibility-shaped domain projections."""

    model_config = ConfigDict(extra="allow")


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=50_000)
    mode: RunMode = RunMode.AUTO
    team_template: str = "software_dev_team"
    repository_path: str | None = None
    base_branch: str | None = None
    review_required: bool = True
    auto_approve_low_risk: bool = False
    max_rounds: int = Field(default=80, ge=1, le=400)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("goal must not be blank")
        return value


class PermissionDecisionBody(BaseModel):
    decision: Literal["approve_once", "approve_run", "deny"]
    reason: str = ""


class PlanDecisionBody(BaseModel):
    approved: bool
    feedback: str = ""


class AgentMessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class RunMessageBody(AgentMessageBody):
    """A user message broadcast to every active teammate in a run."""


class RunResponse(ApiModel):
    run_id: str
    goal: str = ""
    mode: str = "auto"
    resolved_mode: str | None = None
    team_template: str = ""
    status: str
    review_required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class ControlResponse(ApiModel):
    run_id: str
    status: str


class DeliveryResponse(ApiModel):
    run_id: str
    status: str = "delivered"
    delivered: int = 0
    agent_id: str | None = None


class EventEnvelopeResponse(ApiModel):
    event_id: str
    run_id: str
    event_type: str
    sequence: int
    timestamp: datetime | str
    payload: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None


class TaskResponse(ApiModel):
    task_id: str
    run_id: str
    title: str = ""
    objective: str = ""
    status: str
    dependencies: list[str] = Field(default_factory=list)
    claimed_by: str | None = None
    attempts: int = 0
    max_attempts: int = 0
    last_error: str | None = None
    produced_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(ApiModel):
    agent_id: str
    run_id: str
    name: str = ""
    role: str = ""
    status: str
    current_task_id: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentDetailResponse(ApiModel):
    agent: AgentResponse
    messages: list[dict[str, Any]] = Field(default_factory=list)
    events: list[EventEnvelopeResponse] = Field(default_factory=list)


class ArtifactResponse(ApiModel):
    artifact_id: str
    run_id: str
    task_id: str
    type: str = "any"
    path: str
    content_hash: str = ""
    size_bytes: int = 0
    version: int = 1
    produced_by: str = ""
    status: str = "published"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactContentResponse(ApiModel):
    artifact_id: str
    path: str
    content: str
    encoding: Literal["utf-8"]
    truncated: bool = False


class TaskGraphResponse(ApiModel):
    root_task_id: str | None = None
    version: int = 0
    nodes: dict[str, Any] = Field(default_factory=dict)


class SettingsResponse(ApiModel):
    app_env: str
    llm_provider: str
    llm_model: str
    llm_base_url: str
    llm_api_key_configured: bool
    langsmith_enabled: bool
    langsmith_project: str
    max_concurrency: int
    max_team_size: int
    default_auto_approve_low_risk: bool
    legacy_api_enabled: bool
