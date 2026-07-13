"""Phase G 持久化 + 恢复测试。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings as cfg
from app.multiagent.store import close_connection, _init_multiagent_db, _get_conn


@pytest.fixture(autouse=True)
def _isolate_phase_g(tmp_path):
    """每个测试用独立 db，与 conftest 同理。"""
    close_connection()
    cfg.sqlite_path = str(tmp_path / "test_phase_g.sqlite3")
    from app.multiagent.store import _store as s
    if s is not None:
        from app.multiagent.store import MultiAgentStore
        s.conn = _get_conn()
    yield
    close_connection()


class TestAgentRunHistory:
    def test_upsert_and_get(self):
        from app.multiagent.phase_g_store import get_agent_run_history
        h = get_agent_run_history()
        h.upsert_agent_instance(
            agent_id="a1", team_id="t1", run_id="r1",
            profile_id="p1", name="Worker1", role="coder",
            session_id="s1", thread_id="th1", checkpoint_namespace="ns:r1:a1",
            status="idle", capabilities=["coding"],
        )
        got = h.get_agent_instance("a1")
        assert got is not None
        assert got["name"] == "Worker1"
        assert got["status"] == "idle"

    def test_update_status(self):
        from app.multiagent.phase_g_store import get_agent_run_history
        h = get_agent_run_history()
        h.upsert_agent_instance(
            agent_id="a1", team_id="t", run_id="r",
            profile_id="p", name="W", role="w",
            session_id="s", thread_id="th", checkpoint_namespace="ns",
            status="running",
        )
        h.upsert_agent_instance(
            agent_id="a1", team_id="t", run_id="r",
            profile_id="p", name="W", role="w",
            session_id="s", thread_id="th", checkpoint_namespace="ns",
            status="idle",
        )
        got = h.get_agent_instance("a1")
        assert got["status"] == "idle"

    def test_list_by_run(self):
        from app.multiagent.phase_g_store import get_agent_run_history
        h = get_agent_run_history()
        h.upsert_agent_instance(agent_id="a1", team_id="t", run_id="r1",
            profile_id="p", name="A", role="w",
            session_id="s", thread_id="th", checkpoint_namespace="ns", status="idle",
        )
        h.upsert_agent_instance(agent_id="a2", team_id="t", run_id="r1",
            profile_id="p", name="B", role="w",
            session_id="s", thread_id="th", checkpoint_namespace="ns", status="running",
        )
        h.upsert_agent_instance(agent_id="a3", team_id="t", run_id="r2",
            profile_id="p", name="C", role="w",
            session_id="s", thread_id="th", checkpoint_namespace="ns", status="idle",
        )
        assert len(h.list_by_run("r1")) == 2
        assert len(h.list_alive("r1")) == 2
        assert len(h.list_alive()) == 3  # a1,running + a2,idle + a3,idle


class TestTaskRunPersistence:
    def test_insert_and_query(self):
        from app.multiagent.phase_g_store import get_agent_run_history, make_task_run_id
        h = get_agent_run_history()
        trid = make_task_run_id()
        h.insert_task_run(
            task_run_id=trid, task_id="t1", agent_id="a1", run_id="r1",
            attempt=1, status="running",
        )
        h.update_task_run_status(trid, "completed")
        latest = h.latest_task_run("t1")
        assert latest["status"] == "completed"

    def test_task_run_by_run_id(self):
        from app.multiagent.phase_g_store import get_agent_run_history, make_task_run_id
        h = get_agent_run_history()
        h.insert_task_run(make_task_run_id(), task_id="t1", agent_id="a1", run_id="r1")
        h.insert_task_run(make_task_run_id(), task_id="t2", agent_id="a2", run_id="r1")
        assert len(h.list_task_runs_by_run_id("r1")) == 2


class TestTeamEvents:
    def test_record_and_list(self):
        from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
        h = get_agent_run_history()
        evt_id1 = make_run_event_id()
        h.record_event(evt_id1, "r1", "task_started", agent_id="a1", task_id="t1")
        evt_id2 = make_run_event_id()
        h.record_event(evt_id2, "r1", "task_completed", agent_id="a1", task_id="t1")
        events = h.list_events("r1")
        assert len(events) == 2
        started = h.list_events("r1", event_type="task_started")
        assert len(started) == 1


class TestArtifactPersistence:
    def test_insert_and_list(self):
        from app.multiagent.phase_g_store import get_agent_run_history
        h = get_agent_run_history()
        h.insert_artifact("art_1", "r1", "t1", "code", "tasks/t1/out.py", "sha256:abc")
        h.insert_artifact("art_2", "r1", "t1", "code", "tasks/t1/test.py", "sha256:def")
        h.insert_artifact("art_3", "r2", "t2", "report", "tasks/t2/report.md", "sha256:xyz")
        assert len(h.list_artifacts_by_run("r1")) == 2
        assert len(h.list_artifacts_by_run("r2")) == 1
        assert len(h.list_artifacts_by_task("t1")) == 2
        art = h.list_artifacts_by_task("t1")[0]
        assert art["type"] in ("code", "report")


class TestPermissionRequests:
    def test_insert_and_decide(self):
        from app.multiagent.phase_g_store import (
            get_agent_run_history, make_permission_request_id,
        )
        h = get_agent_run_history()
        pr_id = make_permission_request_id()
        h.insert_permission_request(pr_id, "r1", "a1", "shell:execute", target="rm -rf /", reason="cleanup")
        pending = h.list_pending_permission_requests("r1")
        assert len(pending) == 1
        h.decide_permission_request(pr_id, "human", "rejected")
        pending2 = h.list_pending_permission_requests("r1")
        assert len(pending2) == 0
        # 已裁决
        h.decide_permission_request(pr_id, "human", "approved")  # 不应成功
        # 没有 pending 了


class TestIntegration:
    def test_store_and_load_agent_run(self):
        """完整流程：创建 agent → 记录 task_run → 记录 event → 查询恢复。"""
        from app.multiagent.phase_g_store import (
            get_agent_run_history, make_task_run_id, make_run_event_id,
        )

        h = get_agent_run_history()

        # 创建 Agent
        h.upsert_agent_instance(
            agent_id="agent_planner", team_id="team1", run_id="run_demo",
            profile_id="planner", name="Planner", role="planner",
            session_id="s1", thread_id="th1", checkpoint_namespace="ns",
            status="idle", capabilities=["planning"],
        )

        # 记录 task_run
        tr_id = make_task_run_id()
        h.insert_task_run(
            task_run_id=tr_id, task_id="task_plan", agent_id="agent_planner",
            run_id="run_demo", attempt=1, status="running",
            metadata={"plan_size": 3},
        )
        h.update_task_run_status(tr_id, "succeeded")

        # 记录 event
        h.record_event(
            make_run_event_id(), "run_demo", "task_completed",
            agent_id="agent_planner", task_id="task_plan",
            payload={"result": "ok"},
        )

        # 恢复验证
        agents = h.list_by_run("run_demo")
        assert len(agents) == 1
        assert agents[0]["name"] == "Planner"

        tasks = h.list_task_runs_by_run_id("run_demo")
        assert len(tasks) == 1
        assert tasks[0]["status"] == "succeeded"

        events = h.list_events("run_demo")
        assert len(events) == 1
