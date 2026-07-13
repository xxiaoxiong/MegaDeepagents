"""ResumeCoordinator 测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.core.config import settings as cfg


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    from app.multiagent.store import close_connection
    close_connection()
    cfg.sqlite_path = str(tmp_path / "test_resume.sqlite3")
    from app.multiagent.agent_registry import reset_agent_registry
    from app.multiagent.task_board import reset_task_board
    from app.multiagent.phase_g_store import reset_agent_run_history
    reset_agent_registry()
    reset_task_board()
    reset_agent_run_history()
    yield
    reset_agent_registry()
    reset_task_board()
    reset_agent_run_history()
    close_connection()


class TestResumeCoordinator:
    def test_resume_no_history_safe(self):
        """没有持久化历史的恢复应该是 no-op。"""
        from app.multiagent.resume_coordinator import ResumeCoordinator
        c = ResumeCoordinator()
        result = c.resume("nonexistent_run")
        assert result.resumed_agents == 0
        assert result.skipped_tasks == 0
        assert result.errors == []

    def test_resume_recovers_persisted_agent(self):
        """持久化有 Agent → 重建为 IDLE Agent。"""
        from app.multiagent.resume_coordinator import ResumeCoordinator
        from app.multiagent.phase_g_store import get_agent_run_history
        from app.multiagent.agent_registry import get_agent_registry

        h = get_agent_run_history()
        h.upsert_agent_instance(
            agent_id="agent_a", team_id="t", run_id="r_test",
            profile_id="p", name="Alice", role="coder",
            session_id="s1", thread_id="th", checkpoint_namespace="ns",
            status="idle", capabilities=["coding"],
        )

        c = ResumeCoordinator()
        result = c.resume("r_test")
        assert result.resumed_agents == 1
        assert result.skipped_tasks == 0
        assert len(result.errors) == 0

        reg = get_agent_registry()
        agent = reg.get("agent_a")
        assert agent is not None
        assert agent.name == "Alice"
        assert agent.role == "coder"

    def test_resume_skips_succeeded_task(self):
        """已 succeeded 的 task 应该被跳过（直接置 SUCCEEDED）。"""
        from app.multiagent.resume_coordinator import ResumeCoordinator
        from app.multiagent.phase_g_store import get_agent_run_history
        from app.multiagent.task_board import get_task_board, BoardTaskStatus

        board = get_task_board()
        board.create_task(task_id="t_done", run_id="r_done", title="Done", objective="o")
        # 这个 task 已经 PENDING（默认）

        h = get_agent_run_history()
        h.insert_task_run(
            task_run_id="tr1", task_id="t_done", agent_id="agent_a", run_id="r_done",
            attempt=1, status="succeeded",
        )

        c = ResumeCoordinator()
        result = c.resume("r_done")
        assert result.skipped_tasks == 1, f"应跳过 1 个 task，实际 {result.skipped_tasks}"
        t = board.get("t_done")
        assert t.status == BoardTaskStatus.SUCCEEDED

    def test_resume_does_not_touch_failed_task(self):
        """Failure 状态的 task 不应被置为成功（应留给主链重试）。"""
        from app.multiagent.resume_coordinator import ResumeCoordinator
        from app.multiagent.phase_g_store import get_agent_run_history
        from app.multiagent.task_board import get_task_board, BoardTaskStatus

        board = get_task_board()
        board.create_task(task_id="t_failed", run_id="r_failed", title="Failed", objective="o", max_attempts=1)

        h = get_agent_run_history()
        h.insert_task_run(
            task_run_id="tr1", task_id="t_failed", agent_id="agent_a", run_id="r_failed",
            attempt=1, status="failed", error="boom",
        )

        # 先模拟 fail —— 在 board 上手动 failed
        board.claim("t_failed", "agent_a")
        board.start("t_failed", "agent_a")
        board.fail("t_failed", "agent_a", "boom")
        # 然后 fail 后 board 不会自动回到 PENDING（需要外部重试逻辑）

        c = ResumeCoordinator()
        c.resume("r_failed")
        t = board.get("t_failed")
        # 状态仍然是 FAILED（不被 resume 动）
        assert t.status == BoardTaskStatus.FAILED

    def test_resume_skips_stopped_agent(self):
        """停止/失败的 Agent 不被重建。"""
        from app.multiagent.resume_coordinator import ResumeCoordinator
        from app.multiagent.phase_g_store import get_agent_run_history
        from app.multiagent.agent_registry import get_agent_registry

        h = get_agent_run_history()
        h.upsert_agent_instance(
            agent_id="agent_dead", team_id="t", run_id="r",
            profile_id="p", name="Dead", role="coder",
            session_id="s", thread_id="th", checkpoint_namespace="ns",
            status="stopped",
        )
        h.upsert_agent_instance(
            agent_id="agent_alive", team_id="t", run_id="r",
            profile_id="p", name="Alive", role="coder",
            session_id="s", thread_id="th", checkpoint_namespace="ns",
            status="idle",
        )

        c = ResumeCoordinator()
        result = c.resume("r")
        assert result.resumed_agents == 1
        reg = get_agent_registry()
        assert reg.get("agent_dead") is None
        assert reg.get("agent_alive") is not None