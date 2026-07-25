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
