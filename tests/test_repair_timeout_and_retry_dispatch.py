"""Tests for per-capability timeout inheritance and retry dispatch latency.

Covers two production bugs from run_e8587ea68ac64ff5:

1. **Repair task timeout regression** — ``add_repair_task`` built a default
   ``TaskBudget(max_seconds=0.0)``, so ``ParallelTeamScheduler._run_one``
   fell back to the global 300s timeout.  A planning repair task
   (T1__repair_v17) was killed at 300s before the LLM could finish, then
   retries hit 429s and permanently failed the run.  Fix: inherit
   ``budget.max_seconds`` from the target node, with a
   ``capability_timeout()`` fallback.

2. **Retry dispatch starvation** — ``asyncio.wait(FIRST_COMPLETED)`` with no
   timeout blocked the scheduler from picking up retry-ready tasks while a
   long-running task was in flight.  T2's 15s retry sat for 10 minutes
   behind T1's 900s planning run.  Fix: ``_next_retry_delay()`` caps the
   wait at the next deferred retry's due time.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.multiagent.task_graph import (
    CAPABILITY_TIMEOUTS,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskBudget,
    capability_timeout,
)
from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    TaskBoard,
    get_task_board,
    reset_task_board,
)
from app.multiagent.agent_registry import (
    get_agent_registry,
    reset_agent_registry,
)


# ===== capability_timeout() =====


class TestCapabilityTimeout:
    """Verify the shared capability→timeout lookup used by planner, repair, and scheduler."""

    def test_planning_returns_900(self):
        assert capability_timeout(["planning"]) == 900.0

    def test_coding_returns_600(self):
        assert capability_timeout(["coding"]) == 600.0

    def test_testing_returns_300(self):
        assert capability_timeout(["testing"]) == 300.0

    def test_summarization_returns_600(self):
        assert capability_timeout(["summarization"]) == 600.0

    def test_picks_first_primary_capability(self):
        # planning comes before file_read in the list; planning wins.
        assert capability_timeout(["planning", "file_read"]) == 900.0

    def test_skips_tool_capabilities(self):
        # file_read/shell_execute are tool caps, not in CAPABILITY_TIMEOUTS;
        # the lookup should skip them and find 'coding'.
        assert capability_timeout(["file_read", "shell_execute", "coding"]) == 600.0

    def test_unknown_capability_returns_zero(self):
        assert capability_timeout(["unknown_cap"]) == 0.0

    def test_empty_list_returns_zero(self):
        assert capability_timeout([]) == 0.0

    def test_none_returns_zero(self):
        assert capability_timeout(None) == 0.0

    def test_all_capabilities_have_explicit_timeout(self):
        """Every primary capability in the planner's PRIMARY_CAPS set must have a timeout."""
        primary_caps = {"planning", "research", "coding", "testing", "reviewing", "summarization"}
        for cap in primary_caps:
            assert cap in CAPABILITY_TIMEOUTS, f"Missing timeout for {cap}"
            assert CAPABILITY_TIMEOUTS[cap] > 0, f"Timeout for {cap} must be positive"


# ===== add_repair_task budget inheritance =====


def _make_graph_with_node(
    node_id: str,
    caps: list[str],
    budget: TaskBudget | None = None,
) -> TaskGraph:
    """Build a minimal graph with one node, optionally with an explicit budget."""
    graph = TaskGraph(root_task_id=node_id)
    node = TaskNode(
        id=node_id, title=node_id, objective=f"do {node_id}",
        status=TaskNodeStatus.FAILED,
        required_capabilities=caps,
        budget=budget or TaskBudget(),
    )
    graph.add_node(node)
    return graph


class TestRepairTaskTimeoutInheritance:
    """Verify repair tasks inherit the per-capability timeout from their target."""

    def test_planning_repair_inherits_900s(self):
        """A repair of a planning task must get 900s, not the 300s global default.

        Regression: run_e8587ea68ac64ff5 T1__repair_v17 was killed at 300s.
        """
        graph = _make_graph_with_node(
            "T1", ["planning"], TaskBudget(max_attempts=4, max_seconds=900.0),
        )
        repair = graph.add_repair_task("T1", "repair the plan", required_capabilities=["planning"])
        assert repair.budget.max_seconds == 900.0
        assert repair.max_attempts >= 4

    def test_coding_repair_inherits_600s(self):
        graph = _make_graph_with_node(
            "T3", ["coding", "file_read", "file_write", "shell_execute"],
            TaskBudget(max_attempts=4, max_seconds=600.0),
        )
        repair = graph.add_repair_task(
            "T3", "repair the code",
            required_capabilities=["coding", "file_read", "file_write", "shell_execute"],
        )
        assert repair.budget.max_seconds == 600.0

    def test_repair_falls_back_to_capability_timeout_when_target_has_no_budget(self):
        """When the target node has max_seconds=0, the repair should derive
        the timeout from its required_capabilities via capability_timeout()."""
        graph = _make_graph_with_node("T2", ["research", "web_research"])
        repair = graph.add_repair_task(
            "T2", "repair research", required_capabilities=["research", "web_research"],
        )
        assert repair.budget.max_seconds == 600.0  # research timeout

    def test_repair_inherits_max_attempts(self):
        graph = _make_graph_with_node(
            "T1", ["planning"], TaskBudget(max_attempts=6, max_seconds=900.0),
        )
        graph.nodes["T1"].max_attempts = 6
        repair = graph.add_repair_task("T1", "repair", required_capabilities=["planning"])
        assert repair.max_attempts >= 6
        assert repair.budget.max_attempts >= 6

    def test_repair_caps_inherited_from_target_when_not_specified(self):
        """When required_capabilities is None, the repair should inherit from the target."""
        graph = _make_graph_with_node("T1", ["planning", "file_read"])
        repair = graph.add_repair_task("T1", "repair", required_capabilities=None)
        assert "planning" in repair.required_capabilities
        assert "file_read" in repair.required_capabilities

    def test_repair_of_testing_task_gets_300s(self):
        graph = _make_graph_with_node(
            "T7", ["testing", "file_read", "shell_execute"],
            TaskBudget(max_attempts=4, max_seconds=300.0),
        )
        repair = graph.add_repair_task(
            "T7", "repair tests",
            required_capabilities=["testing", "file_read", "shell_execute"],
        )
        assert repair.budget.max_seconds == 300.0


# ===== _next_retry_delay() =====


@pytest.fixture(autouse=True)
def reset_singletons():
    reset_task_board()
    reset_agent_registry()
    yield
    reset_task_board()
    reset_agent_registry()


class TestNextRetryDelay:
    """Verify the scheduler computes the right wait timeout for deferred retries."""

    def _make_scheduler(self, run_id: str = "r1"):
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler
        scheduler = ParallelTeamScheduler(run_id=run_id, max_rounds=10, max_concurrency=2)
        return scheduler

    def test_no_pending_returns_none(self):
        """With no deferred retries, _next_retry_delay returns None (block indefinitely)."""
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        sched = self._make_scheduler()
        sched.board = board
        assert sched._next_retry_delay() is None

    def test_already_due_returns_small_value(self):
        """A retry whose next_attempt_at is in the past should return ~0.05s."""
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        task = board.get("t1", run_id="r1")
        task.status = BoardTaskStatus.PENDING
        task.next_attempt_at = datetime.utcnow() - timedelta(seconds=5)
        board.add(task)

        sched = self._make_scheduler()
        sched.board = board
        delay = sched._next_retry_delay()
        assert delay is not None
        assert delay <= 0.1

    def test_future_retry_returns_remaining_time(self):
        """A retry due in 3s should return ~3s (capped at 5s)."""
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        task = board.get("t1", run_id="r1")
        task.status = BoardTaskStatus.PENDING
        task.next_attempt_at = datetime.utcnow() + timedelta(seconds=3)
        board.add(task)

        sched = self._make_scheduler()
        sched.board = board
        delay = sched._next_retry_delay()
        assert delay is not None
        assert 2.0 <= delay <= 3.5

    def test_capped_at_5_seconds(self):
        """A retry due in 60s should return 5s (the cap for responsiveness)."""
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        task = board.get("t1", run_id="r1")
        task.status = BoardTaskStatus.PENDING
        task.next_attempt_at = datetime.utcnow() + timedelta(seconds=60)
        board.add(task)

        sched = self._make_scheduler()
        sched.board = board
        delay = sched._next_retry_delay()
        assert delay is not None
        assert delay == 5.0

    def test_picks_soonest_retry(self):
        """When multiple retries are pending, return the soonest."""
        board = get_task_board()
        for tid in ["t1", "t2", "t3"]:
            board.create_task(task_id=tid, run_id="r1", title=tid, objective="o")
            task = board.get(tid, run_id="r1")
            task.status = BoardTaskStatus.PENDING
            task.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
            board.add(task)
        # Make t2 due sooner
        t2 = board.get("t2", run_id="r1")
        t2.next_attempt_at = datetime.utcnow() + timedelta(seconds=2)
        board.add(t2)

        sched = self._make_scheduler()
        sched.board = board
        delay = sched._next_retry_delay()
        assert delay is not None
        assert 1.0 <= delay <= 2.5

    def test_ignores_non_pending_tasks(self):
        """SUCCEEDED/FAILED/RUNNING tasks should not affect the delay."""
        board = get_task_board()
        board.create_task(task_id="t1", run_id="r1", title="T1", objective="o")
        task = board.get("t1", run_id="r1")
        task.status = BoardTaskStatus.RUNNING
        task.next_attempt_at = datetime.utcnow() + timedelta(seconds=1)
        board.add(task)

        sched = self._make_scheduler()
        sched.board = board
        assert sched._next_retry_delay() is None


# ===== Scheduler retry dispatch latency (BUG #2 integration test) =====


class TestRetryDispatchLatency:
    """Verify the scheduler picks up retry-ready tasks promptly while a long task runs.

    Regression: run_e8587ea68ac64ff5 — T2's 15s retry sat for 10 minutes
    behind T1's 900s planning run because ``asyncio.wait(FIRST_COMPLETED)``
    had no timeout.
    """

    @pytest.mark.asyncio
    async def test_retry_dispatched_while_long_task_running(self):
        """A retry-ready task must be dispatched without waiting for the long task.

        We simulate:
        - Task A (long): runs for 3 seconds
        - Task B (short): fails immediately, retry due in 0.5s

        Without the fix, the scheduler would block on asyncio.wait for A's
        3s duration, delaying B's retry by ~3s.  With the fix, B is retried
        within ~1s of its backoff expiring.
        """
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(
            task_id="long_task", run_id="r1", title="Long", objective="o",
            required_capabilities=["default"],
        )
        board.create_task(
            task_id="retry_task", run_id="r1", title="Retry", objective="o",
            required_capabilities=["default"],
        )

        # Two idle agents so both tasks can run concurrently.
        reg.create_agent(
            profile_id="p1", name="W1", role="worker",
            team_id="t", run_id="r1", capabilities=["default"],
        )
        reg.create_agent(
            profile_id="p2", name="W2", role="worker",
            team_id="t", run_id="r1", capabilities=["default"],
        )

        call_log: list[tuple[str, float]] = []
        start_time = time.monotonic()

        class TimedExecutor:
            def execute_task(self, dag, task_id, task_input):
                elapsed = time.monotonic() - start_time
                call_log.append((task_id, elapsed))
                if task_id == "long_task":
                    time.sleep(3.0)  # simulate long execution
                    return type("R", (), {"success": True, "artifact_ids": ["a1"], "error": None})()
                # retry_task: fail first attempt, succeed on retry
                if len([c for c in call_log if c[0] == "retry_task"]) == 1:
                    return type("R", (), {"success": False, "artifact_ids": [], "error": "transient_error"})()
                return type("R", (), {"success": True, "artifact_ids": ["a2"], "error": None})()

        scheduler = ParallelTeamScheduler(
            run_id="r1", max_rounds=20, max_concurrency=2,
            task_execution_timeout_seconds=30.0,
        )
        scheduler.board = board
        scheduler.registry = reg

        # Stub out verifier and lifecycle hooks to keep the test focused.
        scheduler.verifier = None
        scheduler.worktree_manager = None
        scheduler.integration_manager = None

        result = await scheduler.run(TimedExecutor())

        # The retry_task's second call should happen well before long_task finishes.
        retry_calls = [c for c in call_log if c[0] == "retry_task"]
        assert len(retry_calls) >= 2, f"Expected retry_task to be called at least twice, got {retry_calls}"

        retry_second_call_time = retry_calls[1][1]
        assert retry_second_call_time < 3.0, (
            f"Retry was delayed {retry_second_call_time:.1f}s — should be < 3s "
            f"(long_task duration). Calls: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_no_deferred_retries_does_not_spin(self):
        """When there are no deferred retries, the scheduler should not waste CPU.

        _next_retry_delay returns None when no PENDING task has a
        next_attempt_at, so asyncio.wait blocks normally.
        """
        from app.multiagent.parallel_scheduler import ParallelTeamScheduler

        board = get_task_board()
        reg = get_agent_registry()

        board.create_task(
            task_id="t1", run_id="r1", title="T1", objective="o",
            required_capabilities=["default"],
        )
        reg.create_agent(
            profile_id="p1", name="W1", role="worker",
            team_id="t", run_id="r1", capabilities=["default"],
        )

        call_count = 0

        class CountingExecutor:
            def execute_task(self, dag, task_id, task_input):
                nonlocal call_count
                call_count += 1
                time.sleep(0.3)
                return type("R", (), {"success": True, "artifact_ids": ["a1"], "error": None})()

        scheduler = ParallelTeamScheduler(
            run_id="r1", max_rounds=10, max_concurrency=1,
            task_execution_timeout_seconds=10.0,
        )
        scheduler.board = board
        scheduler.registry = reg
        scheduler.verifier = None
        scheduler.worktree_manager = None
        scheduler.integration_manager = None

        result = await scheduler.run(CountingExecutor())
        assert call_count == 1, f"Task should be called exactly once, got {call_count}"
