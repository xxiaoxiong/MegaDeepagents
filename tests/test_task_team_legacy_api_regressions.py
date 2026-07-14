"""Regression tests for legacy /team-tasks compatibility over TASK_TEAM.

These routes are still public API, but they must operate on the same
TeamRuntimeFacade control plane rather than trying to resurrect TeamRunner.
"""
from __future__ import annotations

import asyncio


def test_legacy_task_routes_control_the_same_task_team_runtime(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.routes_team as routes
    from app.multiagent.agent_registry import get_agent_registry
    from app.multiagent.mailbox import get_mailbox, reset_mailbox
    from app.multiagent.team_runtime import TeamRuntimeFacade

    reset_mailbox()
    runtime = TeamRuntimeFacade()
    ctx = asyncio.run(runtime.create_run(
        goal="implement an isolated worker", team_name="software_dev_team",
        workspace_root=str(tmp_path / "run"),
    ))
    agent = get_agent_registry().create_agent(
        profile_id="coder", name="Coder", role="coder", team_id=ctx.team_id,
        run_id=ctx.run_id, capabilities=["coding"], workspace_root=ctx.workspace_root,
    )
    monkeypatch.setattr(routes, "get_team_runtime", lambda: runtime)
    # A TASK_TEAM compatibility request must never touch the legacy runner.
    monkeypatch.setattr(routes.TeamRunner, "load", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy runner used")))

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    listed = client.get("/team-tasks")
    assert listed.status_code == 200
    assert any(item["task_id"] == ctx.run_id for item in listed.json())

    status = client.get(f"/team-tasks/{ctx.run_id}")
    assert status.status_code == 200
    assert status.json()["task_id"] == ctx.run_id

    agents = client.get(f"/team-tasks/{ctx.run_id}/agents")
    assert agents.status_code == 200
    assert agents.json()[0]["agent_id"] == agent.agent_id

    injected = client.post(
        f"/team-tasks/{ctx.run_id}/messages",
        json={"goal": "Please prioritize the regression test."},
    )
    assert injected.status_code == 200
    assert get_mailbox().peek(agent.agent_id)[0].content == "Please prioritize the regression test."

    cancelled = client.post(f"/team-tasks/{ctx.run_id}/cancel")
    assert cancelled.status_code == 200
    assert asyncio.run(runtime.get_run(ctx.run_id))["status"] == "cancelled"
