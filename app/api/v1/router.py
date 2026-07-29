"""Unified `/api/v1` routes.  Legacy routes are read-compatible adapters only."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.v1.schemas import (
    AgentMessageBody,
    AgentDetailResponse,
    AgentResponse,
    ArtifactContentResponse,
    ArtifactResponse,
    ControlResponse,
    CreateRunRequest,
    DeliveryResponse,
    EventEnvelopeResponse,
    RunExecutionResponse,
    FlexibleResponse,
    PermissionDecisionBody,
    PlanDecisionBody,
    ResumeRunBody,
    RetryResponse,
    RetryRunBody,
    RunMessageBody,
    RunResponse,
    SettingsResponse,
    TaskGraphResponse,
    TaskResponse,
)
from app.application.runs.service import get_run_service
from app.core.config import settings
from app.infrastructure.database.run_store import get_agent_run_history


router = APIRouter(prefix="/api/v1")
_stream_sleep = asyncio.sleep


def _require_run(run_id: str) -> dict:
    run = get_run_service().get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# Control-plane decisions (permission / plan approval) are the highest-trust
# operations in the runtime: authorizing one lets an agent run a destructive
# shell command or push to git.  When ``control_plane_api_token`` is set, every
# decision endpoint requires ``Authorization: Bearer <token>``.  When unset,
# the runtime is single-user loopback-only and the guard is a no-op, but a
# non-loopback host with an empty token is rejected at startup.
_control_plane_security = HTTPBearer(auto_error=False)


def _require_control_plane_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_control_plane_security),
) -> None:
    """Gate HITL decision endpoints behind a shared bearer token.

    The token uses ``secrets.compare_digest`` to avoid timing side-channels.
    Loopback callers without a configured token are still admitted so local
    dev keeps working; any remote caller without a token is rejected once a
    token is configured.
    """
    expected = settings.control_plane_api_token
    if not expected:
        # No token configured: only loopback callers may decide.  This keeps
        # the default single-user dev flow working while preventing a remote
        # attacker (e.g. via a CORS reflection or exposed Docker port) from
        # approving permissions.
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(
                status_code=403,
                detail="Control-plane decisions require a configured API token for non-loopback callers",
            )
        return
    provided = credentials.credentials if credentials else ""
    import secrets as _secrets

    if not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid control-plane token")


def _public_run(run: dict) -> dict:
    """Remove host filesystem details from the browser-facing contract."""
    return {key: value for key, value in run.items() if key != "workspace_root"}


def _public_operational_record(value):
    """Redact absolute host paths while preserving operational metadata."""
    if isinstance(value, list):
        return [_public_operational_record(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {
            "workspace_root", "worktree_path", "source_repository",
            "repository_path",
        } and isinstance(item, str):
            result[key] = Path(item).name
        else:
            result[key] = _public_operational_record(item)
    return result


@router.post("/runs", status_code=202, response_model=RunResponse)
async def create_run(request: CreateRunRequest):
    run = await get_run_service().create(
        goal=request.goal,
        mode=request.mode,
        team_template=request.team_template,
        repository_path=request.repository_path,
        base_branch=request.base_branch,
        review_required=request.review_required,
        auto_approve_low_risk=request.auto_approve_low_risk,
        metadata=request.metadata,
        max_rounds=request.max_rounds,
    )
    return _public_run(run)


@router.get("/runs", response_model=list[RunResponse])
def list_runs(limit: int = Query(50, ge=1, le=500)):
    return [_public_run(run) for run in get_run_service().list(limit)]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str):
    return _public_run(_require_run(run_id))


@router.post("/runs/{run_id}/pause", response_model=ControlResponse)
async def pause_run(run_id: str):
    _require_run(run_id)
    if not await get_run_service().pause(run_id):
        raise HTTPException(status_code=409, detail="Run cannot be paused")
    return {"run_id": run_id, "status": "paused"}


@router.post(
    "/runs/{run_id}/resume", status_code=202, response_model=ControlResponse
)
async def resume_run(run_id: str, body: ResumeRunBody | None = None):
    _require_run(run_id)
    if not await get_run_service().resume(
        run_id,
        decision=body.decision if body is not None else "continue",
        feedback=body.feedback if body is not None else "",
    ):
        raise HTTPException(status_code=409, detail="Run cannot be resumed")
    return {"run_id": run_id, "status": "running"}


@router.post("/runs/{run_id}/cancel", response_model=ControlResponse)
async def cancel_run(run_id: str):
    _require_run(run_id)
    if not await get_run_service().cancel(run_id):
        raise HTTPException(status_code=409, detail="Run cannot be cancelled")
    return {"run_id": run_id, "status": "cancelled"}


@router.post("/runs/{run_id}/retry", status_code=202, response_model=RetryResponse)
async def retry_run(run_id: str, body: RetryRunBody):
    _require_run(run_id)
    result = await get_run_service().retry(
        run_id,
        task_id=body.task_id,
        reason=body.reason,
        reset_attempts=body.reset_attempts,
    )
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Run has no retryable task or cannot be retried",
        )
    return result


@router.get(
    "/runs/{run_id}/events", response_model=list[EventEnvelopeResponse]
)
def list_events(
    run_id: str,
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2_000),
):
    _require_run(run_id)
    return get_agent_run_history().list_event_envelopes(
        run_id, after_sequence, limit
    )


@router.get(
    "/runs/{run_id}/messages", response_model=list[FlexibleResponse]
)
def list_messages(run_id: str):
    _require_run(run_id)
    return get_agent_run_history().list_mailbox_messages(run_id=run_id)


@router.post(
    "/runs/{run_id}/messages", status_code=202, response_model=DeliveryResponse
)
async def send_run_message(run_id: str, body: RunMessageBody):
    _require_run(run_id)
    delivered = await get_run_service().broadcast_message(run_id, body.content)
    if delivered == 0:
        raise HTTPException(
            status_code=409, detail="Run has no active teammates to receive the message"
        )
    return {"run_id": run_id, "status": "delivered", "delivered": delivered}


@router.get("/runs/{run_id}/stream")
def stream_events(run_id: str, after_sequence: int = Query(0, ge=0)):
    _require_run(run_id)

    async def generate():
        cursor = after_sequence
        idle_deadline = time.monotonic() + 300
        next_keepalive = 0.0
        while time.monotonic() < idle_deadline:
            events = get_agent_run_history().list_event_envelopes(
                run_id, cursor, 200
            )
            if events:
                idle_deadline = time.monotonic() + 300
                for event in events:
                    cursor = max(cursor, int(event["sequence"]))
                    yield (
                        f"id: {cursor}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
            else:
                now = time.monotonic()
                if now >= next_keepalive:
                    yield ": keepalive\n\n"
                    next_keepalive = now + 15
                # A one-second database polling interval made token chunks
                # arrive in visible bursts.  Five lightweight reads per
                # second keeps perceived latency low while keepalives remain
                # sparse enough for proxies and browser tooling.
                await _stream_sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/diagnostics", response_model=FlexibleResponse)
def get_run_diagnostics(run_id: str):
    _require_run(run_id)
    from app.application.runs.diagnostics import RunDiagnosticsService

    result = RunDiagnosticsService().inspect(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _public_operational_record(result)


@router.get(
    "/runs/{run_id}/execution", response_model=RunExecutionResponse
)
def get_run_execution(run_id: str):
    """Return a replay-derived multi-agent execution explanation."""
    _require_run(run_id)
    from app.application.runs.execution_intelligence import (
        RunExecutionIntelligenceService,
    )

    result = RunExecutionIntelligenceService().inspect(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/runs/{run_id}/task-graph", response_model=TaskGraphResponse)
def get_task_graph(run_id: str):
    _require_run(run_id)
    graph = get_agent_run_history().load_task_graph(run_id)
    if graph is None:
        return {"root_task_id": None, "version": 0, "nodes": {}}
    return graph


@router.get("/runs/{run_id}/tasks", response_model=list[TaskResponse])
def list_tasks(run_id: str):
    _require_run(run_id)
    from app.multiagent.task_board import get_task_board

    board = get_task_board()
    if not board.list_by_run(run_id):
        board.restore_run(run_id)
    return [task.model_dump(mode="json") for task in board.list_by_run(run_id)]


@router.get("/runs/{run_id}/agents", response_model=list[AgentResponse])
def list_agents(run_id: str):
    _require_run(run_id)
    from app.multiagent.agent_registry import get_agent_registry

    agents = get_agent_registry().list_by_run(run_id)
    if agents:
        return [agent.model_dump(mode="json") for agent in agents]
    return get_agent_run_history().list_by_run(run_id)


@router.post(
    "/runs/{run_id}/agents/{agent_id}/messages",
    response_model=DeliveryResponse,
)
async def send_agent_message(run_id: str, agent_id: str, body: AgentMessageBody):
    _require_run(run_id)
    from app.multiagent.team_runtime import get_team_runtime

    if not await get_team_runtime().send_message(run_id, agent_id, body.content):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"run_id": run_id, "agent_id": agent_id, "status": "delivered"}


def _agent_detail(agent_id: str) -> dict:
    history = get_agent_run_history()
    agent = history.get_agent_instance(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    run_id = agent["run_id"]
    messages = [
        row for row in history.list_mailbox_messages(run_id=run_id)
        if row.get("to_agent_id") == agent_id or row.get("from_agent_id") == agent_id
    ]
    events = [
        row for row in history.list_event_envelopes(run_id, 0, 2_000)
        if row.get("agent_id") == agent_id
    ]
    return {"agent": agent, "messages": messages, "events": events}


@router.get(
    "/runs/{run_id}/agents/{agent_id}", response_model=AgentDetailResponse
)
def get_run_agent(run_id: str, agent_id: str):
    _require_run(run_id)
    detail = _agent_detail(agent_id)
    if detail["agent"]["run_id"] != run_id:
        raise HTTPException(status_code=404, detail="Agent not found in run")
    return detail


@router.post(
    "/runs/{run_id}/agents/{agent_id}/stop", response_model=DeliveryResponse
)
async def stop_run_agent(run_id: str, agent_id: str):
    _require_run(run_id)
    agent = get_agent_run_history().get_agent_instance(agent_id)
    if agent is None or agent.get("run_id") != run_id:
        raise HTTPException(status_code=404, detail="Agent not found in run")
    if not await get_run_service().stop_agent(run_id, agent_id):
        raise HTTPException(status_code=409, detail="Agent is not active")
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "status": "stopping",
        "delivered": 1,
    }


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
def get_agent(agent_id: str):
    return _agent_detail(agent_id)


@router.post("/agents/{agent_id}/stop", response_model=DeliveryResponse)
async def stop_agent(agent_id: str):
    agent = get_agent_run_history().get_agent_instance(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    run_id = agent["run_id"]
    if not await get_run_service().stop_agent(run_id, agent_id):
        raise HTTPException(status_code=409, detail="Agent is not active")
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "status": "stopping",
        "delivered": 1,
    }


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, run_id: str | None = Query(default=None)):
    matches = get_agent_run_history().find_task_board_task(task_id, run_id)
    if not matches:
        raise HTTPException(status_code=404, detail="Task not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Task id is ambiguous; provide the run_id query parameter",
        )
    return matches[0]


@router.get("/runs/{run_id}/tasks/{task_id}", response_model=TaskResponse)
def get_run_task(run_id: str, task_id: str):
    _require_run(run_id)
    matches = get_agent_run_history().find_task_board_task(task_id, run_id)
    if not matches:
        raise HTTPException(status_code=404, detail="Task not found in run")
    return matches[0]


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactResponse])
def list_artifacts(run_id: str):
    _require_run(run_id)
    return [
        {**item, "path": item.get("relative_path", "")}
        for item in get_agent_run_history().list_artifacts_by_run(run_id)
    ]


def _artifact(run_id: str, artifact_id: str) -> tuple[dict, dict]:
    run = _require_run(run_id)
    artifact = next(
        (
            {**item, "path": item.get("relative_path", "")}
            for item in get_agent_run_history().list_artifacts_by_run(run_id)
            if item.get("artifact_id") == artifact_id
        ),
        None,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return run, artifact


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}", response_model=ArtifactResponse
)
def get_artifact(run_id: str, artifact_id: str):
    _, artifact = _artifact(run_id, artifact_id)
    return artifact


def _global_artifact(artifact_id: str) -> tuple[dict, dict]:
    artifact = get_agent_run_history().get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    artifact = {**artifact, "path": artifact.get("relative_path", "")}
    run = _require_run(artifact["run_id"])
    return run, artifact


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def get_artifact_by_id(artifact_id: str):
    _, artifact = _global_artifact(artifact_id)
    return artifact


def _artifact_path(run: dict, artifact: dict) -> Path:
    workspace = Path(run["workspace_root"]).resolve()
    candidate = (workspace / artifact["path"]).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Artifact path escaped workspace") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Artifact file is missing")
    return candidate


def _artifact_content(artifact_id: str, run: dict, artifact: dict):
    candidate = _artifact_path(run, artifact)
    max_bytes = 512 * 1024
    raw = candidate.read_bytes()
    truncated = len(raw) > max_bytes
    sample = raw[:max_bytes]
    if b"\x00" in sample:
        raise HTTPException(status_code=415, detail="Binary artifact has no text preview")
    return {
        "artifact_id": artifact_id,
        "path": artifact["path"],
        "content": sample.decode("utf-8", errors="replace"),
        "encoding": "utf-8",
        "truncated": truncated,
    }


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}/content",
    response_model=ArtifactContentResponse,
)
def get_artifact_content(run_id: str, artifact_id: str):
    run, artifact = _artifact(run_id, artifact_id)
    return _artifact_content(artifact_id, run, artifact)


@router.get(
    "/artifacts/{artifact_id}/content", response_model=ArtifactContentResponse
)
def get_artifact_content_by_id(artifact_id: str):
    run, artifact = _global_artifact(artifact_id)
    return _artifact_content(artifact_id, run, artifact)


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}/lineage",
    response_model=list[ArtifactResponse],
)
def get_artifact_lineage(run_id: str, artifact_id: str):
    _, artifact = _artifact(run_id, artifact_id)
    artifacts = [
        {**item, "path": item.get("relative_path", "")}
        for item in get_agent_run_history().list_artifacts_by_run(run_id)
    ]
    by_id = {item["artifact_id"]: item for item in artifacts}
    lineage = []
    seen: set[str] = set()
    current = artifact
    while current and current["artifact_id"] not in seen:
        seen.add(current["artifact_id"])
        lineage.append(current)
        parent = current.get("predecessor_id") or current.get("parent_artifact_id")
        current = by_id.get(parent) if parent else None
    return list(reversed(lineage))


@router.get("/runs/{run_id}/artifacts/{artifact_id}/download")
def download_artifact(run_id: str, artifact_id: str):
    run, artifact = _artifact(run_id, artifact_id)
    candidate = _artifact_path(run, artifact)
    return FileResponse(candidate, filename=Path(artifact["path"]).name)


@router.get(
    "/runs/{run_id}/worktrees", response_model=list[FlexibleResponse]
)
def list_worktrees(run_id: str):
    _require_run(run_id)
    from app.multiagent.git_workspace import _ensure_schema
    from app.multiagent.store import _get_conn

    _ensure_schema()
    rows = _get_conn().execute(
        "SELECT payload FROM worktree_leases WHERE run_id=? ORDER BY acquired_at",
        (run_id,),
    ).fetchall()
    return [
        _public_operational_record(json.loads(row["payload"])) for row in rows
    ]


@router.get("/runs/{run_id}/git", response_model=FlexibleResponse)
def get_git_state(run_id: str):
    run = _require_run(run_id)
    from app.multiagent.git_workspace import _ensure_schema
    from app.multiagent.store import _get_conn

    _ensure_schema()
    conn = _get_conn()
    leases = conn.execute(
        "SELECT payload FROM worktree_leases WHERE run_id=? ORDER BY acquired_at",
        (run_id,),
    ).fetchall()
    merges = conn.execute(
        "SELECT payload FROM merge_queue WHERE run_id=? ORDER BY created_at",
        (run_id,),
    ).fetchall()
    metadata = run.get("metadata") or {}
    return {
        "repository": Path(
            metadata.get("repository")
            or metadata.get("source_repository_path")
            or ""
        ).name,
        "worktrees": [
            _public_operational_record(json.loads(row["payload"]))
            for row in leases
        ],
        "merge_queue": [
            _public_operational_record(json.loads(row["payload"]))
            for row in merges
        ],
        "pull_request": metadata.get("pull_request"),
    }


@router.get(
    "/runs/{run_id}/permissions", response_model=list[FlexibleResponse]
)
def list_permissions(run_id: str):
    _require_run(run_id)
    from app.multiagent.permission import get_permission_broker

    return [
        item.model_dump(mode="json")
        for item in get_permission_broker().list_pending(run_id)
    ]


@router.post(
    "/runs/{run_id}/permissions/{request_id}/decision",
    response_model=FlexibleResponse,
)
def decide_permission(
    run_id: str, request_id: str, body: PermissionDecisionBody,
    _auth: None = Depends(_require_control_plane_auth),
):
    _require_run(run_id)
    from app.multiagent.permission import PermissionDecision, get_permission_broker

    try:
        item = get_permission_broker().decide(
            request_id,
            PermissionDecision(body.decision),
            decided_by="user",
            reason=body.reason,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if item.run_id != run_id:
        raise HTTPException(status_code=404, detail="Permission not in run")
    return item.model_dump(mode="json")


@router.get("/runs/{run_id}/plans", response_model=list[FlexibleResponse])
def list_plans(run_id: str):
    _require_run(run_id)
    from app.multiagent.plan_approval import PlanApprovalService

    return [
        item.model_dump(mode="json")
        for item in PlanApprovalService().list_pending(run_id)
    ]


@router.post(
    "/runs/{run_id}/plans/{plan_id}/decision",
    response_model=FlexibleResponse,
)
def decide_plan(
    run_id: str, plan_id: str, body: PlanDecisionBody,
    _auth: None = Depends(_require_control_plane_auth),
):
    _require_run(run_id)
    from app.multiagent.plan_approval import PlanApprovalService

    try:
        item = PlanApprovalService().decide(
            plan_id,
            body.approved,
            decided_by="user",
            feedback=body.feedback,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if item.run_id != run_id:
        raise HTTPException(status_code=404, detail="Plan not in run")
    return item.model_dump(mode="json")


@router.get(
    "/runs/{run_id}/verification",
    response_model=list[EventEnvelopeResponse],
)
def list_verification(run_id: str):
    _require_run(run_id)
    events = get_agent_run_history().list_event_envelopes(run_id, 0, 5_000)
    return [
        event for event in events
        if "verification" in event.get("event_type", "").lower()
        or event.get("event_type") in {"BeforeToolUse", "AfterToolUse"}
    ]


@router.get("/runs/{run_id}/errors", response_model=FlexibleResponse)
def list_errors(run_id: str):
    _require_run(run_id)
    tasks = list_tasks(run_id)
    events = get_agent_run_history().list_event_envelopes(run_id, 0, 5_000)
    return {
        "tasks": [
            task for task in tasks
            if task.get("last_error")
            or task.get("status") in {"failed", "repair_required", "blocked"}
        ],
        "events": [
            event for event in events
            if any(
                token in event.get("event_type", "").lower()
                for token in ("failed", "error", "conflict", "repair")
            )
        ],
    }


@router.get("/settings", response_model=SettingsResponse)
def get_settings():
    return {
        "app_env": settings.app_env,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "llm_api_key_configured": bool(settings.llm_api_key),
        "langsmith_enabled": settings.langsmith_enabled,
        "langsmith_project": settings.langsmith_project,
        "max_concurrency": settings.max_concurrency,
        "max_team_size": settings.max_team_size,
        "task_execution_timeout_seconds": settings.task_execution_timeout_seconds,
        "retry_base_delay_seconds": settings.retry_base_delay_seconds,
        "retry_max_delay_seconds": settings.retry_max_delay_seconds,
        "stalled_run_threshold_seconds": settings.stalled_run_threshold_seconds,
        "default_auto_approve_low_risk": settings.default_auto_approve_low_risk,
        "legacy_api_enabled": settings.enable_legacy_api,
    }
