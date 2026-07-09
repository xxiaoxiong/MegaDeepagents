"""HITL API 端点测试。

验证：
1. GET /api/team-tasks/{task_id}/hitl-conflicts 返回待裁决冲突清单
2. POST /api/team-tasks/{task_id}/hitl-resolve/{issue_id} 决议后 issue 关闭、decision 记录
3. 不存在的 task_id 返回 404
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_team import router
from app.multiagent.messages import MessageType
from app.multiagent.room import TeamRoom
from app.multiagent.state import (
    IssueSeverity,
    IssueStatus,
    SharedTeamState,
    TeamPhase,
    TeamIssue,
)
from app.multiagent.store import MultiAgentStore, get_multiagent_store


def _bootstrap_room_with_conflict() -> tuple[MultiAgentStore, str, str, str]:
    """构造一个带未决 HITL 冲突的 room，返回 (store, task_id, room_id, issue_id)。"""
    import app.multiagent.store as store_mod
    fresh = MultiAgentStore()
    # 替换全局 store 单例
    store_mod._store = fresh
    room_id = "test_hitl_room"
    task_id = "test_hitl_task"
    issue_id = "conflict_test123"
    from app.multiagent.agent_spec import TeamRunConfig
    from app.multiagent.default_teams import SOFTWARE_DEV_TEAM

    config = TeamRunConfig(goal="goal", team_name="software_dev_team", max_rounds=20)
    state = SharedTeamState(room_id=room_id, task_id=task_id, goal="goal", max_rounds=20)

    room = TeamRoom(
        room_id=room_id,
        task_id=task_id,
        config=config,
        team_spec=SOFTWARE_DEV_TEAM,
        store=fresh,
    )
    # 用 create 替换已有 state
    room_id_actual = task_id
    import uuid
    room = TeamRoom.create(
        task_id=task_id,
        config=config,
        team_spec=SOFTWARE_DEV_TEAM,
        store=fresh,
        room_id=room_id,
    )
    # 在 state 中放一个冲突 issue
    room.state.add_issue(TeamIssue(
        id=issue_id,
        title="路线分歧：A 还是 B",
        description="Coder 与 Tester 对实现路线意见不一致",
        severity=IssueSeverity.HIGH,
        status=IssueStatus.OPEN,
        owner=None,
    ))
    room.state.update_phase(TeamPhase.DISCUSSING)
    room.state.goal = "goal"
    room.state.max_rounds = 20
    fresh.save_state(room.state)
    return fresh, task_id, room_id, issue_id


@pytest.fixture()
def api_client():
    store, task_id, room_id, issue_id = _bootstrap_room_with_conflict()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    yield client, task_id, issue_id, store


def test_get_hitl_conflicts_returns_unresolved(api_client):
    """GET hitl-conflicts 应返回未解决的冲突 issue。"""
    client, task_id, issue_id, store = api_client
    r = client.get(f"/api/team-tasks/{task_id}/hitl-conflicts")
    assert r.status_code == 200
    conflicts = r.json()
    assert len(conflicts) == 1
    assert conflicts[0]["issue_id"] == issue_id
    assert "conflict" in conflicts[0]["conflict_type"].lower() or "路线" in conflicts[0]["description"]


def test_resolve_hitl_conflict_closes_issue(api_client):
    """POST hitl-resolve 决议后 issue 状态变为 resolved。"""
    client, task_id, issue_id, store = api_client
    r = client.post(
        f"/api/team-tasks/{task_id}/hitl-resolve/{issue_id}",
        json={"decision": "采用方案 B", "reason": "Tester 的方案更易测试，权衡之后更优"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"
    # 验证 state 中 issue 已 resolved
    state = store.load_state("test_hitl_room")
    assert state is not None
    target = next((i for i in state.issues if i.id == issue_id), None)
    assert target is not None
    assert target.status == IssueStatus.RESOLVED
    # decision 应被写入
    assert any(d.id == f"hitl_{issue_id}" for d in state.decisions)


def test_get_hitl_conflicts_after_resolve_is_empty(api_client):
    """决议后 GET hitl-conflicts 应为空。"""
    client, task_id, issue_id, store = api_client
    client.post(
        f"/api/team-tasks/{task_id}/hitl-resolve/{issue_id}",
        json={"decision": "A", "reason": "测试"},
    )
    r = client.get(f"/api/team-tasks/{task_id}/hitl-conflicts")
    assert r.status_code == 200
    assert r.json() == []


def test_get_hitl_conflicts_unknown_task_404():
    """task_id 不存在时返回 404。"""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    r = client.get("/api/team-tasks/nonexistent_task/hitl-conflicts")
    assert r.status_code == 404
