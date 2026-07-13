"""Phase A/B/C/D 新增模块测试：TeamRunContext / TeamRuntimeFacade /
AgentRegistry / TaskBoard / AgentInstance 状态机。"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from app.multiagent.team_run_context import TeamRunContext, TeamRunMode
from app.multiagent.agent_instance import (
    AgentInstance,
    AgentStatus,
    is_legal_agent_transition,
)
from app.multiagent.agent_registry import (
    AgentRegistry,
    get_agent_registry,
    reset_agent_registry,
)
from app.multiagent.task_board import TaskBoard, BoardTask, BoardTaskStatus, get_task_board, reset_task_board


# ===== TeamRunContext =====

class TestTeamRunContext:
    def test_create_context_with_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = TeamRunContext.create(
                goal="测试目标",
                team_name="software_dev_team",
                mode=TeamRunMode.TASK_TEAM,
                workspace_root=os.path.join(tmp, "run1"),
            )
            assert ctx.run_id.startswith("run_")
            assert os.path.isdir(ctx.workspace_root)
            assert os.path.isdir(ctx.artifacts_dir())
            assert os.path.isdir(ctx.checkpoints_dir())
            assert ctx.mode == TeamRunMode.TASK_TEAM

    def test_task_workspace_auto_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = TeamRunContext.create(
                goal="x", workspace_root=os.path.join(tmp, "r2"),
            )
            tw = ctx.task_workspace("task_1")
            assert os.path.isdir(tw)
            assert tw.endswith(os.path.join("tasks", "task_1"))

    def test_relative_path(self):
        ctx = TeamRunContext.create(goal="x", workspace_root="/tmp/abc")
        rel = ctx.task_relative_path("task_1", "output.py")
        assert rel == "tasks/task_1/output.py"

    def test_mode_from_legacy(self):
        assert TeamRunMode.from_legacy("controlled_group_chat") == TeamRunMode.DISCUSSION
        assert TeamRunMode.from_legacy("full_multi") == TeamRunMode.TASK_TEAM
        assert TeamRunMode.from_legacy("unknown") == TeamRunMode.TASK_TEAM


# ===== AgentInstance 状态机 =====

class TestAgentInstanceStateMachine:
    def test_legal_transition(self):
        assert is_legal_agent_transition(AgentStatus.IDLE, AgentStatus.RUNNING)
        assert is_legal_agent_transition(AgentStatus.RUNNING, AgentStatus.IDLE)
        assert is_legal_agent_transition(AgentStatus.CREATED, AgentStatus.SPAWNING)

    def test_illegal_transition(self):
        # STOPPED 终态，不能再转移
        assert not is_legal_agent_transition(AgentStatus.STOPPED, AgentStatus.RUNNING)
        # SPAWNING 不能直接到 RUNNING（必须经 IDLE）
        assert not is_legal_agent_transition(AgentStatus.SPAWNING, AgentStatus.RUNNING)

    def test_update_status_records_timestamp(self):
        a = AgentInstance(
            agent_id="a1", team_id="t1", run_id="r1",
            profile_id="p1", name="A", role="coder",
            session_id="s1", thread_id="th1", checkpoint_namespace="ns",
        )
        a.update_status(AgentStatus.IDLE)
        assert a.status == AgentStatus.IDLE
        assert a.is_idle()

        # illegal transition 被拒绝
        a.update_status(AgentStatus.CREATED)
        assert a.status == AgentStatus.IDLE  # 仍是 IDLE

    def test_heartbeat(self):
        a = AgentInstance(
            agent_id="a1", team_id="t1", run_id="r1",
            profile_id="p1", name="A", role="coder",
            session_id="s1", thread_id="th1", checkpoint_namespace="ns",
        )
        assert a.last_heartbeat_at is None
        a.heartbeat()
        assert a.last_heartbeat_at is not None


# ===== AgentRegistry =====

class TestAgentRegistry:
    def setup_method(self, _):
        reset_agent_registry()

    def test_create_and_query(self):
        reg = get_agent_registry()
        a = reg.create_agent(
            profile_id="coder", name="CoderBot", role="coder",
            team_id="team1", run_id="run1",
            capabilities=["coding", "testing"],
        )
        assert a.agent_id.startswith("agent_")
        assert a.status == AgentStatus.IDLE
        # 查询
        assert reg.get(a.agent_id) is a
        assert len(reg.list_by_run("run1")) == 1
        assert len(reg.list_by_team("team1")) == 1
        assert len(reg.list_by_status(AgentStatus.IDLE)) == 1

    def test_find_idle(self):
        reg = get_agent_registry()
        a = reg.create_agent(
            profile_id="coder", name="CoderBot", role="coder",
            team_id="team1", run_id="run1",
            capabilities=["coding"],
        )
        # 按 capability 找
        found = reg.find_idle("run1", capabilities=["coding"])
        assert found is not None and found.agent_id == a.agent_id
        # 不匹配的能力
        assert reg.find_idle("run1", capabilities=["research"]) is None
        # 占用后不应找到
        a.update_status(AgentStatus.RUNNING)
        assert reg.find_idle("run1") is None

    def test_lease_cleanup(self):
        reg = AgentRegistry(lease_timeout_seconds=0)  # 0 秒即过期
        a = reg.create_agent(
            profile_id="p", name="A", role="r",
            team_id="t", run_id="r",
        )
        # 立即过期
        import time
        time.sleep(0.01)
        expired = reg.cleanup_expired()
        assert a.agent_id in expired
        assert a.status == AgentStatus.FAILED

    def test_stop_and_remove(self):
        reg = get_agent_registry()
        a = reg.create_agent(
            profile_id="p", name="A", role="r",
            team_id="t", run_id="r",
        )
        assert reg.stop(a.agent_id)
        assert a.status == AgentStatus.STOPPED
        assert reg.remove(a.agent_id)
        assert reg.get(a.agent_id) is None


# ===== TaskBoard 原子认领 =====

class TestTaskBoardAtomicClaim:
    def setup_method(self, _):
        reset_task_board()

    def test_add_and_claim(self):
        board = get_task_board()
        board.create_task(
            task_id="t1", run_id="r1", title="T1", objective="o",
            required_capabilities=["coding"],
        )
        result = board.claim("t1", "agent_a")
        assert result.success
        assert result.task.status == BoardTaskStatus.CLAIMED
        assert result.task.claimed_by == "agent_a"

    def test_double_claim_rejected(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        assert board.claim("t1", "agent_a").success
        result = board.claim("t1", "agent_b")
        assert not result.success
        assert "task_not_pending" in result.reason

    def test_dependency_blocks_claim(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        board.create_task(
            task_id="t2", run_id="r1", title="T2", objective="o",
            dependencies=["t1"],
        )
        # t1 还没完成，t2 不能认领
        result = board.claim("t2", "agent_a")
        assert not result.success
        assert "dependency" in result.reason

    def test_dependency_satisfied_then_claimable(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        board.create_task(
            task_id="t2", run_id="r1", title="T2", objective="o",
            dependencies=["t1"],
        )
        # 完成 t1
        assert board.claim("t1", "agent_a").success
        assert board.start("t1", "agent_a")
        assert board.complete("t1", "agent_a", artifact_ids=["art1"])
        # 现在 t2 可认领
        result = board.claim("t2", "agent_b")
        assert result.success

    def test_release_resets_to_pending(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        board.claim("t1", "agent_a")
        assert board.release("t1", "agent_a", reason="error")
        t = board.get("t1")
        assert t.status == BoardTaskStatus.PENDING
        assert t.claimed_by is None
        assert t.attempts == 1
        assert t.last_error == "error"

    def test_fail_attempt_then_retry(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o", )
        board.get("t1").max_attempts = 3
        board.claim("t1", "agent_a")
        board.fail("t1", "agent_a", "boom")
        t = board.get("t1")
        assert t.status == BoardTaskStatus.PENDING
        assert t.attempts == 1

        # 多次失败到 max → FAILED
        board.claim("t1", "agent_a")
        board.fail("t1", "agent_a", "boom2")
        board.claim("t1", "agent_a")
        board.fail("t1", "agent_a", "boom3")
        t = board.get("t1")
        assert t.status == BoardTaskStatus.FAILED

    def test_summary(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        s = board.summary("r1")
        assert s["pending"] == 1
        assert s["total"] == 1

    def test_list_claimable_by_capability(self):
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o", required_capabilities=["coding"])
        board.create_task(task_id="t2", run_id="r1", title="T2", objective="o", required_capabilities=["research"])
        # coder 只能 claim t1
        claimable = board.list_claimable("r1", "agent_a", capabilities=["coding"])
        assert [t.task_id for t in claimable] == ["t1"]


# ===== TeamRuntimeFacade 烟雾测试 =====

class TestTeamRuntimeFacade:
    def teardown_method(self, _):
        # 重置 runtime 单例避免状态残留
        from app.multiagent.team_runtime import reset_team_runtime
        reset_team_runtime()

    def test_create_run_returns_context(self):
        from app.multiagent.team_runtime import get_team_runtime
        runtime = get_team_runtime()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = asyncio.run(runtime.create_run(
                goal="test", workspace_root=os.path.join(tmp, "r1"),
            ))
            assert ctx.run_id.startswith("run_")
            assert os.path.isdir(ctx.workspace_root)
            run_info = asyncio.run(runtime.get_run(ctx.run_id))
            assert run_info is not None
            assert run_info["status"] == "created"

    def test_list_runs(self):
        from app.multiagent.team_runtime import get_team_runtime
        runtime = get_team_runtime()
        asyncio.run(runtime.create_run(
            goal="t1", workspace_root="/tmp/r1",
        ))
        asyncio.run(runtime.create_run(
            goal="t2", workspace_root="/tmp/r2",
        ))
        runs = runtime.list_runs()
        assert len(runs) == 2
