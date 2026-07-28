"""Execution-intelligence projection and public API coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.application.runs.execution_intelligence import (
    RunExecutionIntelligenceService,
)
from app.infrastructure.database.run_store import (
    get_agent_run_history,
    make_run_event_id,
)
from app.main import app
from app.multiagent.task_board import get_task_board


def _event(
    run_id: str,
    event_type: str,
    at: datetime,
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    payload: dict | None = None,
) -> None:
    get_agent_run_history().record_event(
        event_id=make_run_event_id(),
        run_id=run_id,
        event_type=event_type,
        timestamp=at,
        agent_id=agent_id,
        task_id=task_id,
        payload=payload or {},
    )


def _build_run() -> str:
    run_id = "run_execution_projection"
    history = get_agent_run_history()
    history.save_team_run(
        run_id=run_id,
        goal="Explain multi-agent execution",
        team_id="software_dev_team",
        mode="team",
        workspace_root=".",
        status="failed",
        max_rounds=20,
        review_required=False,
    )
    board = get_task_board()
    board.create_task(
        task_id="plan",
        run_id=run_id,
        title="Plan",
        objective="Plan the work",
        required_capabilities=["planning"],
    )
    board.create_task(
        task_id="code",
        run_id=run_id,
        title="Implement",
        objective="Implement the plan",
        dependencies=["plan"],
        required_capabilities=["coding"],
        max_attempts=3,
    )
    board.create_task(
        task_id="test",
        run_id=run_id,
        title="Verify",
        objective="Verify the implementation",
        dependencies=["code"],
        required_capabilities=["testing"],
    )
    assert board.claim("plan", "agent_planner", run_id=run_id).success
    assert board.start("plan", "agent_planner", run_id=run_id)
    assert board.complete(
        "plan", "agent_planner", ["artifact_plan"], run_id=run_id
    )
    assert board.claim("code", "agent_coder", run_id=run_id).success
    assert board.start("code", "agent_coder", run_id=run_id)
    assert board.fail(
        "code",
        "agent_coder",
        "tests failed",
        run_id=run_id,
        retryable=False,
    )

    history.upsert_agent_instance(
        agent_id="agent_planner",
        team_id="software_dev_team",
        run_id=run_id,
        profile_id="planner",
        name="Planner",
        role="Planner",
        session_id="session_plan",
        thread_id="thread_plan",
        checkpoint_namespace="team:planner",
        status="idle",
        capabilities=["planning"],
    )
    history.upsert_agent_instance(
        agent_id="agent_coder",
        team_id="software_dev_team",
        run_id=run_id,
        profile_id="coder",
        name="Coder",
        role="Coder",
        session_id="session_code",
        thread_id="thread_code",
        checkpoint_namespace="team:coder",
        status="failed",
        capabilities=["coding"],
    )
    history.insert_artifact(
        artifact_id="artifact_plan",
        run_id=run_id,
        task_id="plan",
        type="markdown",
        relative_path="artifacts/plan.md",
        content_hash="abc123",
        size_bytes=42,
        produced_by="agent_planner",
    )

    start = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    _event(run_id, "RunStarted", start)
    _event(
        run_id,
        "TaskStarted",
        start + timedelta(seconds=1),
        agent_id="agent_planner",
        task_id="plan",
        payload={"objective": "Plan the work"},
    )
    _event(
        run_id,
        "TaskHeartbeat",
        start + timedelta(seconds=2),
        agent_id="agent_planner",
        task_id="plan",
    )
    _event(
        run_id,
        "AfterToolUse",
        start + timedelta(seconds=3),
        agent_id="agent_planner",
        task_id="plan",
        payload={"tool": "read_file"},
    )
    _event(
        run_id,
        "TaskCompleted",
        start + timedelta(seconds=4),
        agent_id="agent_planner",
        task_id="plan",
    )
    _event(
        run_id,
        "TaskStarted",
        start + timedelta(seconds=5),
        agent_id="agent_coder",
        task_id="code",
    )
    _event(
        run_id,
        "TaskFailed",
        start + timedelta(seconds=9),
        agent_id="agent_coder",
        task_id="code",
        payload={"error": "tests failed"},
    )
    _event(
        run_id,
        "TaskRetryScheduled",
        start + timedelta(seconds=10),
        agent_id="agent_coder",
        task_id="code",
    )
    return run_id


def test_execution_projection_explains_agents_critical_path_and_attention():
    run_id = _build_run()

    result = RunExecutionIntelligenceService().inspect(run_id)

    assert result is not None
    assert result["summary"]["tool_call_count"] == 1
    assert result["summary"]["retry_count"] == 1
    assert result["summary"]["critical_path"] == ["code", "test"]
    assert result["summary"]["peak_concurrency"] == 1
    assert result["summary"]["active_time_ms"] == 7_000
    assert result["summary"]["artifact_count"] == 1

    agents = {agent["agent_id"]: agent for agent in result["agents"]}
    assert agents["agent_planner"]["completed_task_ids"] == ["plan"]
    assert agents["agent_planner"]["artifact_ids"] == ["artifact_plan"]
    assert agents["agent_planner"]["tool_call_count"] == 1
    assert all(
        event["event_type"] != "TaskHeartbeat"
        for event in agents["agent_planner"]["recent_events"]
    )
    assert any(
        item["kind"] == "task_blocker" and item["task_id"] == "code"
        for item in result["attention"]
    )


def test_execution_projection_is_available_from_v1_api():
    run_id = _build_run()

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/execution")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["summary"]["critical_path_remaining"] == 2
    assert {agent["name"] for agent in body["agents"]} == {"Planner", "Coder"}
