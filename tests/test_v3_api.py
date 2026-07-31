from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.application.runs.service import RunApplicationService, get_run_service
from app.infrastructure.database.run_store import get_agent_run_history, make_run_event_id
from app.main import app
from app.multiagent.artifact import ArtifactStore, ArtifactType


def test_v1_run_contract_events_and_artifact_download(monkeypatch):
    service = get_run_service()

    def do_not_start_model_run(coroutine, *, run_id):
        assert run_id.startswith("run_")
        coroutine.close()

    monkeypatch.setattr(service, "_spawn", do_not_start_model_run)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runs",
            json={
                "goal": "Summarize the repository README",
                "mode": "single",
                "review_required": False,
            },
        )
        assert created.status_code == 202
        run = created.json()
        run_id = run["run_id"]
        assert run["mode"] == "single"
        assert run["status"] == "created"
        assert "workspace_root" not in run

        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
        assert client.get("/api/v1/runs/not-a-run").status_code == 404
        assert client.post(
            "/api/v1/runs",
            json={"goal": "x", "mode": "discussion"},
        ).status_code == 422

        history = get_agent_run_history()
        history.record_event(
            event_id=make_run_event_id(),
            run_id=run_id,
            event_type="test:event",
            payload={"message": "durable"},
        )
        events = client.get(
            f"/api/v1/runs/{run_id}/events?after_sequence=0"
        ).json()
        assert events[-1]["sequence"] >= 1
        assert events[-1]["event_type"] == "test:event"

        internal_run = service.get(run_id)
        assert internal_run is not None
        store = ArtifactStore(internal_run["workspace_root"])
        artifact = store.create(
            run_id=run_id,
            task_id="api-test",
            type=ArtifactType.DOCUMENT,
            relative_path="artifacts/report.txt",
            content="production evidence",
            produced_by="test-worker",
        )
        listed = client.get(f"/api/v1/runs/{run_id}/artifacts").json()
        assert listed[0]["artifact_id"] == artifact.id
        assert listed[0]["path"] == "artifacts/report.txt"
        downloaded = client.get(
            f"/api/v1/runs/{run_id}/artifacts/{artifact.id}/download"
        )
        assert downloaded.status_code == 200
        assert downloaded.text == "production evidence"
        content = client.get(
            f"/api/v1/runs/{run_id}/artifacts/{artifact.id}/content"
        )
        assert content.status_code == 200
        assert content.json()["content"] == "production evidence"
        assert content.json()["complete"] is True

        large_text = (
            ("x" * (64 * 1024 - 1))
            + "🙂"
            + ("0123456789\n" * 50_000)
            + "完整结尾"
        )
        large_artifact = store.create(
            run_id=run_id,
            task_id="api-large-test",
            type=ArtifactType.DOCUMENT,
            relative_path="artifacts/large-report.txt",
            content=large_text,
            produced_by="test-worker",
        )
        chunks: list[str] = []
        offset = 0
        while True:
            page = client.get(
                f"/api/v1/runs/{run_id}/artifacts/{large_artifact.id}/content",
                params={"offset": offset, "limit": 64 * 1024},
            )
            assert page.status_code == 200
            body = page.json()
            assert body["offset"] == offset
            chunks.append(body["content"])
            if body["complete"]:
                assert body["next_offset"] is None
                break
            assert body["next_offset"] > offset
            offset = body["next_offset"]
        assert "".join(chunks) == large_text

        workspace_file = client.get(
            f"/api/v1/runs/{run_id}/files/content",
            params={"path": "artifacts/large-report.txt", "limit": 32},
        )
        assert workspace_file.status_code == 200
        assert workspace_file.json()["content"].startswith("x" * 32)
        assert workspace_file.json()["path"] == "artifacts/large-report.txt"
        assert client.get(
            f"/api/v1/runs/{run_id}/files/content",
            params={"path": "../outside.txt"},
        ).status_code == 403

        settings = client.get("/api/v1/settings").json()
        assert "llm_api_key" not in settings
        assert "llm_api_key_configured" in settings


@pytest.mark.asyncio
async def test_background_failure_is_persisted(monkeypatch):
    recorded: list[tuple[str, object]] = []

    class History:
        def update_team_run_status(self, run_id, status):
            recorded.append(("status", (run_id, status)))

        def record_event(self, **event):
            recorded.append(("event", event))

    monkeypatch.setattr(
        "app.application.runs.service.get_agent_run_history",
        lambda: History(),
    )

    async def fail():
        raise RuntimeError("worker crashed")

    service = RunApplicationService()
    service._spawn(fail(), run_id="run_failure")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert ("status", ("run_failure", "failed")) in recorded
    event = next(item for kind, item in recorded if kind == "event")
    assert event["event_type"] == "RunFailed"
    assert event["payload"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_run_controls_reject_illegal_or_duplicate_transitions(monkeypatch):
    """Public controls cannot regress terminal state or start a second runner."""
    calls: list[tuple[str, str]] = []

    class Runtime:
        async def pause_run(self, run_id):
            calls.append(("pause", run_id))
            return True

        async def cancel_run(self, run_id):
            calls.append(("cancel", run_id))
            return True

        async def resume_run(self, run_id, *, resume_decision=None):
            calls.append(("resume", run_id))
            return True

    monkeypatch.setattr(
        "app.application.runs.service.get_team_runtime",
        lambda: Runtime(),
    )
    service = RunApplicationService()
    spawned: list[str] = []

    def capture_spawn(coroutine, *, run_id):
        spawned.append(run_id)
        coroutine.close()

    monkeypatch.setattr(service, "_spawn", capture_spawn)

    monkeypatch.setattr(service, "get", lambda _run_id: {"status": "succeeded"})
    assert await service.pause("run_terminal") is False
    assert await service.cancel("run_terminal") is False
    assert await service.resume("run_terminal") is False

    monkeypatch.setattr(service, "get", lambda _run_id: {"status": "running"})
    assert await service.resume("run_live") is False
    assert await service.pause("run_live") is True

    monkeypatch.setattr(service, "get", lambda _run_id: {"status": "paused"})
    assert await service.pause("run_paused") is False
    assert await service.resume("run_paused") is True
    assert await service.cancel("run_paused") is True

    assert calls == [("pause", "run_live"), ("cancel", "run_paused")]
    assert spawned == ["run_paused"]


@pytest.mark.asyncio
async def test_resume_forwards_an_explicit_human_decision(monkeypatch):
    calls: list[tuple[str, dict[str, str] | None]] = []

    class Runtime:
        async def resume_run(self, run_id, *, resume_decision=None):
            calls.append((run_id, resume_decision))
            return True

    monkeypatch.setattr(
        "app.application.runs.service.get_team_runtime",
        lambda: Runtime(),
    )
    service = RunApplicationService()
    monkeypatch.setattr(
        service,
        "get",
        lambda _run_id: {"status": "waiting_human"},
    )
    spawned = []

    def capture_spawn(coroutine, *, run_id):
        spawned.append((run_id, coroutine))

    monkeypatch.setattr(service, "_spawn", capture_spawn)

    assert await service.resume(
        "run_waiting",
        decision="deny",
        feedback="The proposed plan is unsafe.",
    )
    assert spawned[0][0] == "run_waiting"
    await spawned[0][1]
    assert calls == [(
        "run_waiting",
        {
            "decision": "deny",
            "feedback": "The proposed plan is unsafe.",
        },
    )]


def test_resume_api_accepts_decision_payload(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    async def resume(run_id, *, decision="continue", feedback=""):
        calls.append((run_id, decision, feedback))
        return True

    service = get_run_service()
    monkeypatch.setattr(service, "get", lambda _run_id: {"run_id": "run_waiting"})
    monkeypatch.setattr(service, "resume", resume)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs/run_waiting/resume",
            json={"decision": "deny", "feedback": "Revise the plan."},
        )

    assert response.status_code == 202
    assert calls == [("run_waiting", "deny", "Revise the plan.")]


def test_startup_recovery_never_auto_approves_waiting_human(monkeypatch):
    class History:
        def list_team_runs(self, _limit):
            return [
                {"run_id": "run_created", "status": "created"},
                {"run_id": "run_running", "status": "running"},
                {"run_id": "run_waiting", "status": "waiting_human"},
                {"run_id": "run_paused", "status": "paused"},
            ]

    class Runtime:
        async def resume_run(self, run_id):
            return run_id

    monkeypatch.setattr(
        "app.application.runs.service.get_agent_run_history",
        lambda: History(),
    )
    monkeypatch.setattr(
        "app.application.runs.service.get_team_runtime",
        lambda: Runtime(),
    )
    service = RunApplicationService()
    spawned: list[str] = []

    def capture_spawn(coroutine, *, run_id):
        spawned.append(run_id)
        coroutine.close()

    monkeypatch.setattr(service, "_spawn", capture_spawn)

    assert service.recover_incomplete() == 2
    assert spawned == ["run_created", "run_running"]
