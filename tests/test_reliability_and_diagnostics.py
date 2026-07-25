"""Regression coverage for retry policy, durable audit, and Run diagnostics."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.application.runs.service import get_run_service
from app.application.runs.diagnostics import RunDiagnosticsService
from app.infrastructure.database.run_store import get_agent_run_history
from app.main import app
from app.multiagent.lifecycle_hooks import LifecycleEvent, LifecycleHookEngine
from app.multiagent.task_board import BoardTaskStatus, TaskBoard, get_task_board
from app.runtime.reliability import FailureCategory, RetryPolicy


def test_retry_policy_classifies_failures_and_bounds_backoff():
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=5)

    rate_limited = policy.decide(
        "HTTP 429: too many requests",
        attempt=2,
        max_attempts=4,
    )
    assert rate_limited.category == FailureCategory.RATE_LIMITED
    assert rate_limited.retryable is True
    assert rate_limited.delay_seconds == 4

    capped = policy.decide(
        "gateway timeout",
        attempt=3,
        max_attempts=5,
    )
    assert capped.category == FailureCategory.TIMEOUT
    assert capped.delay_seconds == 5

    authentication = policy.decide(
        "401 invalid API key",
        attempt=1,
        max_attempts=5,
    )
    assert authentication.category == FailureCategory.AUTHENTICATION
    assert authentication.retryable is False
    assert authentication.reason == "authentication_requires_intervention"


def test_task_board_defers_retry_and_supports_manual_recovery():
    board = TaskBoard()
    task = board.create_task(
        task_id="research",
        run_id="run_recovery",
        title="Research",
        objective="Collect evidence",
        max_attempts=2,
    )
    assert board.claim(task.task_id, "agent_1", run_id=task.run_id).success
    assert board.start(task.task_id, "agent_1", run_id=task.run_id)

    assert board.fail(
        task.task_id,
        "agent_1",
        "service unavailable",
        run_id=task.run_id,
        retryable=True,
        retry_delay_seconds=30,
        failure_category="transient",
    )
    deferred = board.get(task.task_id, run_id=task.run_id)
    assert deferred is not None
    assert deferred.status == BoardTaskStatus.PENDING
    assert deferred.next_attempt_at is not None
    assert board.list_pending(task.run_id) == []
    assert deferred.metadata["error_history"][0]["category"] == "transient"

    # Exhaust the budget, then verify that an operator can explicitly requeue
    # the same durable task without inventing a second scheduler.
    deferred.next_attempt_at = None
    assert board.claim(task.task_id, "agent_1", run_id=task.run_id).success
    assert board.start(task.task_id, "agent_1", run_id=task.run_id)
    assert board.fail(
        task.task_id,
        "agent_1",
        "service unavailable",
        run_id=task.run_id,
        retryable=True,
        failure_category="transient",
    )
    assert board.get(task.task_id, run_id=task.run_id).status == BoardTaskStatus.FAILED

    assert board.retry(
        task.task_id,
        run_id=task.run_id,
        reason="operator confirmed upstream recovery",
    )
    recovered = board.get(task.task_id, run_id=task.run_id)
    assert recovered is not None
    assert recovered.status == BoardTaskStatus.PENDING
    assert recovered.max_attempts == 3
    assert recovered.metadata["manual_retries"][-1]["previous_status"] == "failed"


def test_lifecycle_is_durable_without_registered_hooks():
    history = get_agent_run_history()
    history.save_team_run(
        run_id="run_audit",
        goal="Make execution visible",
        team_id="team_audit",
        mode="team",
        workspace_root=".",
        status="running",
        max_rounds=3,
        review_required=False,
    )

    result = LifecycleHookEngine().emit(
        LifecycleEvent.TASK_STARTED,
        {
            "run_id": "run_audit",
            "task_id": "task_1",
            "agent_id": "agent_1",
            "objective": "Inspect repository",
        },
    )

    assert result.allow is True
    events = history.list_event_envelopes("run_audit")
    assert len(events) == 1
    assert events[0]["event_type"] == "TaskStarted"
    assert events[0]["task_id"] == "task_1"
    assert events[0]["payload"]["objective"] == "Inspect repository"


def test_diagnostics_projects_failures_into_operator_guidance():
    run_id = "run_diagnostics"
    history = get_agent_run_history()
    history.save_team_run(
        run_id=run_id,
        goal="Diagnose a failed task",
        team_id="team_diagnostics",
        mode="team",
        workspace_root=".",
        status="running",
        max_rounds=3,
        review_required=False,
    )
    board = TaskBoard(persist=True)
    board.create_task(
        task_id="task_failed",
        run_id=run_id,
        title="Unstable upstream",
        objective="Call service",
        max_attempts=1,
    )
    assert board.claim("task_failed", "agent_1", run_id=run_id).success
    assert board.start("task_failed", "agent_1", run_id=run_id)
    assert board.fail(
        "task_failed",
        "agent_1",
        "HTTP 401 invalid API key",
        run_id=run_id,
        retryable=False,
        failure_category="authentication",
    )
    LifecycleHookEngine().emit(
        LifecycleEvent.TASK_FAILED,
        {
            "run_id": run_id,
            "task_id": "task_failed",
            "agent_id": "agent_1",
            "error": "HTTP 401 invalid API key",
        },
    )

    diagnostics = RunDiagnosticsService().inspect(run_id)
    assert diagnostics is not None
    assert diagnostics["health"] == "attention"
    assert diagnostics["event_count"] == 1
    assert diagnostics["task_counts"]["failed"] == 1
    assert diagnostics["retryable_task_ids"] == ["task_failed"]
    assert diagnostics["blockers"][0]["message"] == "HTTP 401 invalid API key"


def test_v1_diagnostics_and_manual_retry_contract(monkeypatch):
    service = get_run_service()

    def do_not_start(coroutine, *, run_id):
        assert run_id.startswith("run_")
        coroutine.close()

    monkeypatch.setattr(service, "_spawn", do_not_start)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={
                "goal": "Recover a failed durable task",
                "mode": "team",
                "review_required": False,
            },
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]

        board = get_task_board()
        board.create_task(
            task_id="unstable_api",
            run_id=run_id,
            title="Unstable API",
            objective="Call upstream",
            max_attempts=1,
        )
        assert board.claim("unstable_api", "agent_1", run_id=run_id).success
        assert board.start("unstable_api", "agent_1", run_id=run_id)
        assert board.fail(
            "unstable_api",
            "agent_1",
            "HTTP 503 service unavailable",
            run_id=run_id,
            retryable=False,
            failure_category="transient",
        )

        diagnostics = client.get(f"/api/v1/runs/{run_id}/diagnostics")
        assert diagnostics.status_code == 200
        assert diagnostics.json()["health"] == "attention"
        assert diagnostics.json()["retryable_task_ids"] == ["unstable_api"]

        retried = client.post(
            f"/api/v1/runs/{run_id}/retry",
            json={
                "task_id": "unstable_api",
                "reason": "upstream recovered",
            },
        )
        assert retried.status_code == 202
        assert retried.json()["retried_task_ids"] == ["unstable_api"]
        assert retried.json()["recovery_generation"] == 1
        assert (
            board.get("unstable_api", run_id=run_id).status
            == BoardTaskStatus.PENDING
        )
        event_types = [
            event["event_type"]
            for event in get_agent_run_history().list_event_envelopes(run_id)
        ]
        assert "ManualRetryRequested" in event_types
