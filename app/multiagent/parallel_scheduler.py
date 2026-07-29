"""ParallelTeamScheduler — 基于 asyncio + TaskBoard + AgentRegistry 的真实并行调度。

生产并行调度语义：
- 不再用顺序 for 循环遍历 ready_tasks；改用 asyncio 协程池并行执行
- TaskBoard 提供原子认领，多 Agent 可同时抢任务
- AgentRegistry 提供 Agent 生命周期 + 心跳，调度器从空闲池子里挑 worker
- 失败的 task 通过 board.fail() 自动重试到 max_attempts
- 持续工作直到 all_succeeded 或 max_rounds 到达

设计原则：
- 与现有 _run_sync_fallback 并存：
  - TASK_TEAM 默认走 ParallelTeamScheduler（async）
  - 不包含旁路同步调度器
- 优先保证 LLM 工具场景的吞吐：无相互依赖的 task 并行执行
- 单 task 失败不阻塞其他 task
- 调度器和 AgentRegistry 通过心跳互锁：超时的 Agent 被回收，其任务由 timeout
  处理程序 release 回 PENDING 给其他 Agent
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    ClaimResult,
    TaskBoard,
    get_task_board,
)
from app.multiagent.task_graph import capability_timeout
from app.runtime.reliability import RetryDecision, RetryPolicy


class ScheduleStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"


@dataclass
class ParallelRunResult:
    """并行调度的整体结果。"""
    status: str  # ScheduleStatus value; kept as str for API compatibility
    rounds: int
    total_tasks: int
    succeeded: int
    failed: int
    error: str | None = None
    summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "total_tasks": self.total_tasks,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "error": self.error,
            "summary": self.summary,
        }


class ParallelTeamScheduler:
    """真正的并行团队调度器。

    流程（每个 round）：
        1. 通过 board.list_pending() 拿可认领任务（依赖已满足 + capability 匹配）
        2. 给每个 task 在 asyncio.gather 中并行调度：
            - 从 AgentRegistry 取空闲 Agent
            - atomic claim
            - 设 RUNNING
            - 交给 executor 执行
            - complete / fail
        3. round 结束后判断 all_succeeded / max_rounds
    """

    def __init__(
        self,
        run_id: str,
        max_rounds: int = 30,
        max_concurrency: int = 4,
        heartbeat_interval_seconds: float = 3.0,
        lease_timeout_seconds: int = 120,
        task_graph: Any | None = None,
        cancel_event: Any | None = None,
        verifier: Any | None = None,
        worktree_manager: Any | None = None,
        integration_manager: Any | None = None,
        control_plane: Any | None = None,
        permission_broker: Any | None = None,
        task_execution_timeout_seconds: float | None = None,
        retry_policy: RetryPolicy | None = None,
        audit_heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.max_rounds = max_rounds
        self.max_concurrency = max_concurrency
        self.heartbeat_interval = heartbeat_interval_seconds
        self.lease_timeout = lease_timeout_seconds
        self.task_execution_timeout = (
            settings.task_execution_timeout_seconds
            if task_execution_timeout_seconds is None
            else max(0.0, float(task_execution_timeout_seconds))
        )
        self.retry_policy = retry_policy or RetryPolicy(
            base_delay_seconds=settings.retry_base_delay_seconds,
            max_delay_seconds=settings.retry_max_delay_seconds,
            rate_limit_base_delay_seconds=settings.retry_rate_limit_base_delay_seconds,
            rate_limit_max_delay_seconds=settings.retry_rate_limit_max_delay_seconds,
        )
        self.audit_heartbeat_interval = (
            settings.audit_heartbeat_interval_seconds
            if audit_heartbeat_interval_seconds is None
            else max(1.0, float(audit_heartbeat_interval_seconds))
        )
        self.task_graph = task_graph
        self.cancel_event = cancel_event or asyncio.Event()
        self.verifier = verifier
        self.worktree_manager = worktree_manager
        self.integration_manager = integration_manager

        self.board = get_task_board()
        self.registry = get_agent_registry()
        from app.multiagent.agent_runtime_manager import get_agent_runtime_manager
        self.runtime_manager = get_agent_runtime_manager()
        if control_plane is None:
            from app.multiagent.control_plane import TeamControlPlaneService
            control_plane = TeamControlPlaneService()
        self.control_plane = control_plane
        if permission_broker is None:
            from app.multiagent.permission import get_permission_broker
            permission_broker = get_permission_broker()
        self.permission_broker = permission_broker
        self._dispatch_agent_hints: dict[str, str] = {}

    # ===== 主循环 =====

    def _deps_satisfied(self, task: BoardTask) -> bool:
        """Return True only when every dependency of ``task`` is SUCCEEDED.

        ``TaskBoard.list_pending`` returns every PENDING task regardless of
        dependency state.  Dispatching a task whose dependencies are not
        SUCCEEDED is wasteful and dangerous: ``board.claim`` rejects it with
        ``dependency_not_succeeded``, the reserved agent is released
        instantly, and the next round repeats the same dance.  With
        ``max_rounds`` budget this busy-loop burns the entire round budget
        in milliseconds (observed: 80 rounds in 38 ms) and aborts the run
        with ``max_rounds`` before any retry backoff expires.
        """
        for dep_id in task.dependencies:
            dep = self.board.get(dep_id, run_id=self.run_id)
            if dep is None or dep.status != BoardTaskStatus.SUCCEEDED:
                return False
        return True

    async def _run_wide_heartbeat(self, stop_event: asyncio.Event) -> None:
        """Heartbeat every live agent in the run, including IDLE ones.

        Without this loop, only the currently-executing task heartbeats its
        own agent.  IDLE teammates sit with whatever ``last_heartbeat_at``
        they had at registration, so once ``lease_timeout`` elapses
        ``cleanup_expired`` marks them FAILED — killing the whole team
        during any long-running task.  This loop keeps IDLE teammates
        alive while the run is active, and still lets ``cleanup_expired``
        reap truly crashed agents (their per-task heartbeat stops, but the
        scheduler-level heartbeat below is intentionally best-effort and
        skips agents whose status is already terminal).
        """
        from app.multiagent.agent_instance import AgentStatus
        while not stop_event.is_set():
            for agent in self.registry.list_by_run(self.run_id):
                status = getattr(agent.status, "value", agent.status)
                if status in {"stopped", "failed"}:
                    continue
                # Only heartbeat IDLE agents here.  RUNNING agents are
                # heartbeated by their per-task ``_heartbeat_loop``; echoing
                # them from the scheduler would mask a stuck executor thread
                # and defeat the lease-based crash detection.
                if status == AgentStatus.IDLE.value:
                    self.registry.heartbeat(agent.agent_id)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.heartbeat_interval
                )
            except TimeoutError:
                pass

    async def run(self, executor: Any) -> ParallelRunResult:
        """执行并行调度。executor 必须实现 execute_task(dag, task_id, task_input)。"""
        round_n = 0
        self._event("SchedulerStarted", payload={
            "max_rounds": self.max_rounds,
            "max_concurrency": self.max_concurrency,
            "task_execution_timeout_seconds": self.task_execution_timeout,
        })

        # Run-wide heartbeat keeps IDLE teammates alive across long tasks.
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._run_wide_heartbeat(heartbeat_stop))

        try:
            return await self._run_loop(executor, round_n)
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _run_loop(self, executor: Any, round_n: int) -> ParallelRunResult:
        """Event-driven dispatch loop.

        The previous implementation dispatched a batch of tasks then
        ``await asyncio.gather(*coros)`` — waiting for **every** task in the
        batch to finish before re-evaluating the pending queue.  A single
        long-running task (e.g. a 15-minute planning task that eventually
        timed out) blocked re-dispatch of siblings that had already failed
        and were due for retry.  Observed in run_207f813863a04c39: T2
        failed with a 429 at t+6m, its retry became due 2s later, but the
        scheduler could not re-claim it until T1 timed out at t+15m — by
        which point the researcher agent had been reaped by lease expiry.

        This version dispatches new tasks as soon as a concurrency slot
        frees up, using ``asyncio.wait(return_when=FIRST_COMPLETED)`` so a
        slow task never blocks re-dispatch of ready retries.
        """
        from app.infrastructure.database.run_store import get_agent_run_history

        # One semaphore for the whole run so concurrency is honoured across
        # batches, not just within a single gather.
        semaphore = asyncio.Semaphore(self.max_concurrency)
        # future → task_id for the tasks currently dispatched.
        running: dict[asyncio.Future, str] = {}

        while round_n < self.max_rounds:
            self._refresh_task_graph()
            run_record = get_agent_run_history().get_team_run(self.run_id)
            if run_record and run_record.get("status") == "paused":
                await self._cancel_running(running)
                return self._finalize(round_n, status=ScheduleStatus.PAUSED.value,
                                      error="paused")

            if self.cancel_event.is_set():
                await self._cancel_running(running)
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value,
                                      error="cancelled")

            # Reap truly crashed agents (stale heartbeat while
            # RUNNING/CLAIMING).  IDLE agents are kept alive by
            # ``_run_wide_heartbeat`` and will not be reaped.
            self.registry.cleanup_expired()

            # Discover newly-dispatchable tasks (deps satisfied + capability
            # match + not already running).
            new_dispatch = self._discover_dispatchable(running)

            if new_dispatch:
                round_n += 1
                self._event("SchedulerRoundStarted", payload={
                    "round": round_n,
                    "pending_task_ids": [t.task_id for t in new_dispatch],
                    "task_count": len(new_dispatch),
                })
                for task in new_dispatch:
                    fut = asyncio.ensure_future(
                        self._run_one_guarded(task, executor, semaphore)
                    )
                    running[fut] = task.task_id

            # Nothing running and nothing to dispatch → resolve the wait.
            if not running:
                resolved = await self._resolve_idle(round_n)
                if resolved is not None:
                    return resolved
                # _resolve_idle either returned a final result or slept for a
                # deferred retry; loop back to re-evaluate.
                continue

            # Wait for at least one in-flight task to finish, then loop to
            # dispatch newly-ready tasks immediately.  This is the key
            # difference from the old gather(): a slow task no longer blocks
            # re-dispatch of ready retries.
            #
            # BUT: a bare ``FIRST_COMPLETED`` wait with no timeout also blocks
            # re-dispatch of *retry-ready* tasks.  If task A (15-min planning)
            # is running and task B failed with a 15-s backoff, B becomes due
            # while A is still running — but the scheduler won't notice until
            # A finishes.  In run_e8587ea68ac64ff5, T2's retry sat for 10 min
            # behind T1's 900-s run.  Cap the wait at the next deferred retry's
            # due time so the loop wakes up and re-evaluates ``list_pending``.
            retry_wait = self._next_retry_delay()
            # Always wake periodically to observe run cancellation/pause.
            # ``None`` used to block until a non-cooperative model call
            # finished (potentially many minutes), even after the operator
            # had cancelled the run. One lightweight control-plane read per
            # second keeps cancellation bounded without redispatching work.
            wait_timeout = min(retry_wait, 1.0) if retry_wait is not None else 1.0
            done, _ = await asyncio.wait(
                running.keys(),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=wait_timeout,
            )
            if not done:
                # The wait timed out without any task finishing — a deferred
                # retry is likely due now.  Loop back to re-dispatch it
                # without consuming a round.
                continue
            for fut in done:
                running.pop(fut, None)
                exc = fut.exception()
                if exc is not None:
                    logger.error(
                        f"[ParallelSched] run={self.run_id} task raised: {exc}"
                    )

            if self.cancel_event.is_set():
                await self._cancel_running(running)
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value,
                                      error="cancelled")

            run_record = get_agent_run_history().get_team_run(self.run_id)
            if run_record and run_record.get("status") == "paused":
                await self._cancel_running(running)
                return self._finalize(round_n, status=ScheduleStatus.PAUSED.value,
                                      error="paused")

            if self.board.all_succeeded(self.run_id):
                return self._finalize_verified_run(round_n)

        # Exhausted max_rounds — cancel any still-running tasks.
        await self._cancel_running(running)
        return self._finalize(round_n, status="incomplete", error="max_rounds")

    def _discover_dispatchable(
        self, running: dict[asyncio.Future, str],
    ) -> list[BoardTask]:
        """Return tasks that are ready to dispatch right now.

        Filters ``list_pending`` by:
        - dependency satisfaction (every dep must be SUCCEEDED)
        - capability match (at least one IDLE agent can serve the task)
        - not already in-flight (task_id not in ``running``)

        Only IDLE agents are considered for the capability match.  Checking
        all live agents (which includes RUNNING ones) causes a busy-loop:
        the task is dispatched, ``_run_one`` fails to reserve an idle agent,
        the future completes instantly, and ``round_n`` burns through
        ``max_rounds`` before any running task finishes.  Observed in
        ``test_v3_root_graph``: 2 tasks sharing 1 summarization agent
        exhausted 10 rounds in milliseconds, aborting with ``max_rounds``
        before the first task completed.
        """
        from app.multiagent.agent_instance import AgentStatus

        pending = self.board.list_pending(self.run_id)
        ready = [task for task in pending if self._deps_satisfied(task)]

        idle_agents = [
            agent
            for agent in self.registry.list_by_run(self.run_id)
            if getattr(agent.status, "value", agent.status) == AgentStatus.IDLE.value
        ]
        live_agents = [
            agent
            for agent in self.registry.list_by_run(self.run_id)
            if getattr(agent.status, "value", agent.status)
            not in {"stopped", "failed"}
        ]

        serviceable: list[tuple[BoardTask, list[Any]]] = []
        unserviceable: list[BoardTask] = []
        for task in ready:
            candidates = [
                agent
                for agent in idle_agents
                if self._agent_can_serve(agent, task)
            ]
            if candidates:
                serviceable.append((task, candidates))
            else:
                unserviceable.append(task)

        # Only surface tasks that no live agent can ever serve (a true
        # capability gap).  Tasks merely blocked by busy agents are
        # expected — they will be dispatched when an agent returns to IDLE.
        truly_unserviceable = [
            task for task in unserviceable
            if not any(
                self._agent_can_serve(a, task)
                for a in live_agents
            )
        ]
        if truly_unserviceable:
            self._event("TasksWaitingForCapability", payload={
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "required_capabilities": task.required_capabilities,
                    }
                    for task in truly_unserviceable
                ],
                "available_agents": [
                    {
                        "agent_id": agent.agent_id,
                        "capabilities": agent.capabilities,
                    }
                    for agent in live_agents
                ],
            })

        already_running = set(running.values())
        available = {agent.agent_id: agent for agent in idle_agents}
        capacity = max(0, self.max_concurrency - len(running))
        selected: list[BoardTask] = []

        # Build a one-wave bipartite match instead of launching one Future per
        # ready task.  The old implementation could enqueue ten tasks for one
        # idle teammate; nine futures then failed reservation immediately and
        # the same work repeated in the next scheduler round.  Most-constrained
        # tasks are matched first so a generalist does not starve a specialist.
        ordered = sorted(
            (
                (task, candidates)
                for task, candidates in serviceable
                if task.task_id not in already_running
            ),
            key=lambda item: (
                len(item[1]),
                -int(item[0].priority),
                item[0].created_at,
                item[0].task_id,
            ),
        )
        running_has_exclusive = any(
            not self._task_allows_parallel(task_id)
            for task_id in already_running
        )
        if running_has_exclusive:
            return []

        exclusive_ready = [
            item for item in ordered
            if not self._task_allows_parallel(item[0].task_id)
        ]
        if running:
            # An exclusive task waits for the current parallel wave to drain.
            ordered = [
                item for item in ordered
                if self._task_allows_parallel(item[0].task_id)
            ]
        elif exclusive_ready:
            # Start one explicit barrier task before admitting another wave.
            ordered = sorted(
                exclusive_ready,
                key=lambda item: (
                    -int(item[0].priority),
                    item[0].created_at,
                    item[0].task_id,
                ),
            )[:1]
            capacity = min(capacity, 1)

        for task, _ in ordered:
            candidates = [
                agent
                for agent in available.values()
                if self._agent_can_serve(agent, task)
            ]
            if not candidates:
                continue
            # Prefer the narrowest capable worker; preserve broad generalists
            # for tasks that have no specialist alternative.
            chosen = min(
                candidates,
                # ``candidates`` preserves registry order, so equal-width
                # workers retain the established first-registered fairness
                # contract instead of being reordered by random agent IDs.
                key=lambda agent: len(agent.capabilities),
            )
            selected.append(task)
            self._dispatch_agent_hints[task.task_id] = chosen.agent_id
            available.pop(chosen.agent_id, None)
            if len(selected) >= capacity:
                break
        return selected

    def _task_allows_parallel(self, task_id: str) -> bool:
        if self.task_graph is None:
            return True
        node = self.task_graph.nodes.get(task_id)
        if node is None:
            return True
        contract = getattr(node, "output_contract", None)
        return bool(
            True if contract is None else contract.allow_parallel
        )

    @staticmethod
    def _agent_can_serve(agent: Any, task: BoardTask) -> bool:
        """Mirror registry capability fallback during dispatch planning."""
        required = set(task.required_capabilities)
        if not required:
            return True
        available = set(agent.capabilities)
        if required.issubset(available):
            return True
        tool_capabilities = {
            "file_read",
            "file_write",
            "shell_execute",
            "web_research",
            "mcp_access",
            "default",
        }
        primary = required - tool_capabilities
        return bool(primary) and primary.issubset(available)

    def _next_retry_delay(self) -> float | None:
        """Return how long to wait before re-checking for retry-ready tasks.

        Scans every PENDING task with a future ``next_attempt_at`` and returns
        the time until the earliest one becomes due, capped at a small upper
        bound so the scheduler also stays responsive to cancel/pause signals.
        Returns ``None`` when no deferred retries exist, letting
        ``asyncio.wait`` block indefinitely (the old behaviour) — this is
        correct when every pending task is already dispatchable and we're
        purely waiting for a running task to finish.

        Without this cap, ``asyncio.wait(FIRST_COMPLETED)`` blocks until a
        running task finishes.  In run_e8587ea68ac64ff5, T2's 15-s retry sat
        for 10 minutes behind T1's 900-s planning run because the scheduler
        never woke up to re-evaluate ``list_pending``.
        """
        now = datetime.now(UTC)
        soonest: float | None = None
        for task in self.board.list_by_run(self.run_id):
            if task.status != BoardTaskStatus.PENDING:
                continue
            due = task.next_attempt_at
            if due is None:
                continue
            remaining = (due - now).total_seconds()
            if remaining <= 0:
                # Already due — wake immediately.
                return 0.05
            if soonest is None or remaining < soonest:
                soonest = remaining
        if soonest is None:
            return None
        # Cap at 5 s so cancel/pause signals are still picked up promptly
        # even when the next retry is far off.
        return min(soonest, 5.0)

    async def _resolve_idle(self, round_n: int) -> ParallelRunResult | None:
        """Handle the case where nothing is running and nothing is dispatchable.

        Returns a final ``ParallelRunResult`` if the run is over, or ``None``
        after sleeping for a deferred retry / transient gap so the caller can
        loop back and re-evaluate.
        """
        if self.board.all_succeeded(self.run_id):
            logger.info(
                f"[ParallelSched] run={self.run_id}: all succeeded at round={round_n}"
            )
            return self._finalize_verified_run(round_n)

        all_tasks = self.board.list_by_run(self.run_id)
        states = [t.status for t in all_tasks]

        # Deferred retries — wait for the next attempt to become due.
        deferred = [
            task for task in all_tasks
            if task.status == BoardTaskStatus.PENDING
            and task.next_attempt_at is not None
            and task.next_attempt_at > datetime.now(UTC)
        ]
        if deferred:
            due_at = min(task.next_attempt_at for task in deferred)
            wait_seconds = max(
                0.05, (due_at - datetime.now(UTC)).total_seconds()
            )
            self._event("RetryBackoffWaiting", payload={
                "task_ids": [task.task_id for task in deferred],
                "next_attempt_at": due_at.isoformat(),
                "wait_seconds": round(wait_seconds, 3),
            })
            # Cap the sleep so a newly-freed agent or cancel is noticed
            # promptly even when the next retry is far off.
            await asyncio.sleep(min(wait_seconds, 1.0))
            return None

        # Control-plane intervention states.
        if any(s in (BoardTaskStatus.BLOCKED, BoardTaskStatus.REPAIR_REQUIRED,
                     BoardTaskStatus.REPLAN_REQUIRED) for s in states):
            return self._finalize(
                round_n, status=ScheduleStatus.WAITING_HUMAN.value,
                error="control_plane_intervention_required",
            )

        # A task-level cancel may race with a worker completion without setting
        # the run-wide cancel event.  Once every task is terminal, preserve the
        # authoritative cancellation outcome instead of misclassifying the
        # run as a scheduler deadlock.
        terminal_statuses = {
            BoardTaskStatus.SUCCEEDED,
            BoardTaskStatus.FAILED,
            BoardTaskStatus.CANCELLED,
        }
        if states and all(status in terminal_statuses for status in states):
            if BoardTaskStatus.FAILED in states:
                return self._finalize(
                    round_n,
                    status=ScheduleStatus.FAILED.value,
                    error="one_or_more_tasks_failed",
                )
            if BoardTaskStatus.CANCELLED in states:
                return self._finalize(
                    round_n,
                    status=ScheduleStatus.CANCELLED.value,
                    error="one_or_more_tasks_cancelled",
                )

        # Permanent deadlock: a PENDING task depends on a terminal (FAILED /
        # CANCELLED) task.  FAILED→SUCCEEDED is not a legal transition (see
        # _LEGAL_TRANSITIONS in task_graph.py), so these PENDING tasks can
        # never become dispatchable.  This is a hard failure, not "waiting
        # for human" — the previous WAITING_HUMAN / "no_eligible_worker"
        # return was misleading (the issue isn't a missing worker, it's a
        # dead dependency) and prevented the run from routing to _fail.
        # In run_2a438328372441d8, T01 FAILED and 14 PENDING tasks depended
        # on it transitively; the scheduler returned WAITING_HUMAN instead
        # of FAILED, obscuring the real cause.
        terminal_blocker_ids = {
            t.task_id for t in all_tasks
            if t.status in (BoardTaskStatus.FAILED, BoardTaskStatus.CANCELLED)
        }
        if terminal_blocker_ids:
            deadlocked = [
                t for t in all_tasks
                if t.status == BoardTaskStatus.PENDING
                and any(dep in terminal_blocker_ids for dep in t.dependencies)
            ]
            if deadlocked:
                return self._finalize(
                    round_n, status=ScheduleStatus.FAILED.value,
                    error=(
                        "task_deadlock_due_to_failed_dependency: "
                        f"{[t.task_id for t in deadlocked]}"
                    ),
                )

        # No deferred retries, no BLOCKED, nothing running → genuine deadlock
        # or permanent capability gap.
        pending = [t for t in all_tasks if t.status == BoardTaskStatus.PENDING]
        if pending:
            # Pending tasks but none dispatchable → no eligible worker.
            return self._finalize(
                round_n, status=ScheduleStatus.WAITING_HUMAN.value,
                error="no_eligible_worker",
            )

        logger.warning(
            f"[ParallelSched] run={self.run_id} deadlock: states={states}"
        )
        return self._finalize(
            round_n, status="failed", error="scheduler_deadlock",
        )

    async def _cancel_running(self, running: dict[asyncio.Future, str]) -> None:
        """Cancel every in-flight task future and swallow CancelledError."""
        for fut in running:
            fut.cancel()
        if running:
            await asyncio.gather(*running.keys(), return_exceptions=True)
        running.clear()

    # ===== 单任务运行 =====

    async def _run_one_guarded(self, task: BoardTask, executor: Any, semaphore: Any) -> None:
        """Hold the concurrency permit for the complete assignment lifetime."""
        async with semaphore:
            await self._run_one(task, executor)

    async def _run_one(self, task: BoardTask, executor: Any) -> None:
        """认领并执行一个任务。如果 task 仍属 PENDING 且无人占用，则认领并执行。"""
        if self.cancel_event.is_set():
            return
        # Selection and reservation are a single operation.  Do not call
        # find_idle here: a sibling coroutine can otherwise steal the same
        # worker before this task changes its status.
        agent = self.registry.reserve_idle_agent(
            self.run_id,
            set(task.required_capabilities),
            task.task_id,
            preferred_agent_id=self._dispatch_agent_hints.pop(task.task_id, None),
        )
        if agent is None:
            # 没有空闲 worker → 触发 Mailbox.wake_idle_agents 提示正在运行的
            # 同 capability Agent 让出资源（任务书 §12）。这是提示而非阻塞 RPC：
            # 不阻塞调度循环，下一 round 仍会有机会重试。
            logger.info(
                f"[ParallelSched] no idle worker for task={task.task_id} "
                f"required={task.required_capabilities} – 触发 wakeup"
            )
            try:
                from app.multiagent.mailbox import get_mailbox
                from app.multiagent.agent_instance import AgentStatus
                busy = [
                    a.agent_id for a in self.registry.list_by_run(self.run_id)
                    if a.status == AgentStatus.RUNNING
                    and any(c in a.capabilities for c in task.required_capabilities)
                ]
                if busy:
                    get_mailbox().wake_idle_agents(
                        run_id=self.run_id,
                        agent_ids=busy,
                        hint=f"task={task.task_id} 等待空闲 worker，请尽快完成或让出。",
                    )
            except Exception as exc:
                logger.debug(f"[ParallelSched] wake_idle_agents 失败（忽略）: {exc}")
            return

        try:
            from app.multiagent.teammate_session import (
                TeammateLifecycle, get_teammate_supervisor,
            )
            teammate_actor = get_teammate_supervisor().actor_for(agent)
            session = teammate_actor.session
            if session.lifecycle_state == TeammateLifecycle.IDLE:
                session.transition(TeammateLifecycle.CLAIMING)
                get_teammate_supervisor().persist(session)
            claim = self.board.claim(task.task_id, agent.agent_id, run_id=self.run_id)
            if not claim.success:
                # 已被其他协程抢走
                logger.debug(
                    f"[ParallelSched] claim failed for task={task.task_id} "
                    f"agent={agent.agent_id}: {claim.reason}"
                )
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return

            task = claim.task
            from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
            claim_hook = await get_lifecycle_hook_engine().emit_async(
                LifecycleEvent.TASK_CLAIMED,
                {"run_id": self.run_id, "agent_id": agent.agent_id,
                 "task_id": task.task_id},
            )
            if claim_hook.block or not claim_hook.allow:
                self.board.release(task.task_id, agent.agent_id,
                                   claim_hook.feedback or "TaskClaimed hook blocked",
                                   run_id=self.run_id)
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return
            if not self.board.start(task.task_id, agent.agent_id, run_id=self.run_id):
                # 状态机异常，释放并放弃
                self.board.release(task.task_id, agent.agent_id, "start_failed", run_id=self.run_id)
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return
            start_hook = await get_lifecycle_hook_engine().emit_async(
                LifecycleEvent.TASK_STARTED,
                {"run_id": self.run_id, "agent_id": agent.agent_id,
                 "task_id": task.task_id},
            )
            if start_hook.block or not start_hook.allow:
                self.board.release(task.task_id, agent.agent_id,
                                   start_hook.feedback or "TaskStarted hook blocked",
                                   run_id=self.run_id)
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return

            node_for_plan = self.task_graph.nodes.get(task.task_id) if self.task_graph else None
            if node_for_plan is not None and node_for_plan.metadata.get("require_plan_approval"):
                from app.multiagent.plan_approval import (
                    PlanApprovalService, PlanStatus, TeammatePlan,
                )
                existing_plan_id = task.metadata.get("plan_id")
                service = PlanApprovalService()
                existing_plan = service.get(existing_plan_id) if existing_plan_id else None
                if existing_plan is None:
                    session.transition(TeammateLifecycle.PLANNING)
                    plan = service.submit(TeammatePlan(
                        run_id=self.run_id, agent_id=agent.agent_id, task_id=task.task_id,
                        files=list(node_for_plan.metadata.get("plan_files", [])),
                        steps=list(node_for_plan.metadata.get("plan_steps", [node_for_plan.objective])),
                        test_strategy=list(node_for_plan.output_contract.acceptance_criteria or
                                           ["run task-specific verification"]),
                        risks=list(node_for_plan.metadata.get("plan_risks", [])),
                        rollback=node_for_plan.metadata.get("rollback", "revert task commit"),
                    ))
                    task.metadata["plan_id"] = plan.plan_id
                    self.board.add(task)
                    existing_plan = plan
                if existing_plan.status != PlanStatus.PLAN_APPROVED:
                    if session.lifecycle_state == TeammateLifecycle.PLANNING:
                        session.transition(TeammateLifecycle.WAITING_PLAN_APPROVAL)
                    task.status = BoardTaskStatus.BLOCKED
                    task.last_error = existing_plan.feedback or "waiting_plan_approval"
                    self.board.add(task)
                    get_teammate_supervisor().persist(session)
                    self.registry.release_reservation(agent.agent_id, task.task_id)
                    return

            # Agent 状态机：IDLE → RUNNING
            from app.multiagent.agent_instance import AgentStatus
            if not self.registry.transition(agent.agent_id, AgentStatus.RUNNING):
                self.board.release(task.task_id, agent.agent_id, "agent_transition_failed", run_id=self.run_id)
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return
            if session.lifecycle_state == TeammateLifecycle.CLAIMING:
                session.transition(TeammateLifecycle.RUNNING)
            session.current_task_id = task.task_id
            session.cancellation_requested = False
            get_teammate_supervisor().persist(session)
            # 这里的 registry 调用是为了发心跳

            # 心跳任务（执行长时间时记录进度）
            beat_stop = asyncio.Event()
            from app.infrastructure.database.run_store import get_agent_run_history, make_task_run_id
            history = get_agent_run_history()
            task_run_id = make_task_run_id()
            history.insert_task_run(
                task_run_id=task_run_id, task_id=task.task_id, agent_id=agent.agent_id,
                run_id=self.run_id, attempt=task.attempts + 1, status="running",
                metadata={"session_id": agent.session_id, "thread_id": agent.thread_id},
            )
            assignment_started = time.monotonic()

            async def _heartbeat_loop():
                last_audit = -self.audit_heartbeat_interval
                while not beat_stop.is_set():
                    self.registry.heartbeat(agent.agent_id)
                    elapsed = time.monotonic() - assignment_started
                    if elapsed - last_audit >= self.audit_heartbeat_interval:
                        self._event(
                            "TaskHeartbeat",
                            agent_id=agent.agent_id,
                            task_id=task.task_id,
                            payload={
                                "task_run_id": task_run_id,
                                "attempt": task.attempts + 1,
                                "elapsed_seconds": round(elapsed, 1),
                                "message": "Agent is still working",
                            },
                        )
                        last_audit = elapsed
                    try:
                        await asyncio.wait_for(
                            beat_stop.wait(), timeout=self.heartbeat_interval
                        )
                    except TimeoutError:
                        pass

            beat_task = asyncio.create_task(_heartbeat_loop())

            try:
                # 在线程池中跑同步 executor（支持 DeepAgentExecutor / 旧实现）
                task_input = {
                    "run_id": self.run_id,
                    "agent_id": agent.agent_id,
                    "profile_id": agent.profile_id,
                }
                lease = None
                if self.worktree_manager is not None:
                    try:
                        lease = self.worktree_manager.acquire(self.run_id, agent.agent_id)
                    except Exception as exc:
                        from app.multiagent.permission import PermissionRequired
                        if isinstance(exc, PermissionRequired):
                            session.transition(TeammateLifecycle.WAITING_PERMISSION)
                            task.status = BoardTaskStatus.BLOCKED
                            task.last_error = str(exc)
                            task.metadata["permission_request_id"] = exc.request.request_id
                            self.board.add(task)
                            history.update_task_run_status(
                                task_run_id, "waiting_permission", error=str(exc),
                            )
                            return
                        raise
                    task_input["workspace_root"] = lease.worktree_path
                    task_input["worktree_mode"] = True
                    agent.workspace_root = lease.worktree_path
                    agent.worktree_path = lease.worktree_path
                    agent.metadata["worktree_path"] = lease.worktree_path
                    agent.metadata["git_branch"] = lease.branch
                    session.workspace = lease.worktree_path
                    session.worktree = lease.worktree_path
                    get_teammate_supervisor().persist(session)
                task_input.update({
                    "workspace_root": getattr(agent, "workspace_root", ""),
                    "agent_id": agent.agent_id,
                    "session_id": agent.session_id,
                    "thread_id": agent.thread_id,
                })
                artifact_ids, artifact_refs = self._collect_dependency_artifacts(task)
                task_input["input_artifact_ids"] = artifact_ids
                task_input["artifact_refs"] = artifact_refs
                task_input["team_control_plane"] = self.control_plane
                task_input["permission_broker"] = self.permission_broker
                task_input["safety_point"] = teammate_actor.safety_point
                # Mailbox is an execution input, not merely an audit log.
                # Deliver messages atomically before the worker constructs its
                # prompt so user/teammate interventions can affect the task.
                from app.multiagent.mailbox import get_mailbox
                task_input["mailbox_messages"] = [
                    message.model_dump(mode="json")
                    for message in get_mailbox().receive(agent.agent_id, max_count=20)
                ]
                dag = self.task_graph or self._task_graph_from_board()
                assignment_future = asyncio.create_task(
                    self.runtime_manager.execute_assignment(
                        executor=executor,
                        task_graph=dag,
                        task_id=task.task_id,
                        task_input=task_input,
                        cancel_event=self.cancel_event,
                        agent_registry=self.registry,
                    )
                )
                if self.task_execution_timeout > 0:
                    # Per-task timeout resolution order:
                    # 1. node_for_plan.budget.max_seconds (planner-set)
                    # 2. capability_timeout(required_capabilities) — covers
                    #    repair tasks whose ``add_repair_task`` may not have
                    #    inherited the budget (defensive), and any node built
                    #    via ``_task_graph_from_board`` fallback.
                    # 3. scheduler-wide ``task_execution_timeout`` (300s).
                    # The previous code skipped step 2, so repair tasks like
                    # T1__repair_v17 (planning) fell to 300s and were killed
                    # before the LLM could finish (run_e8587ea68ac64ff5).
                    effective_timeout = (
                        node_for_plan.budget.max_seconds
                        if node_for_plan is not None
                        and node_for_plan.budget.max_seconds > 0
                        else capability_timeout(task.required_capabilities)
                        or self.task_execution_timeout
                    )
                    done, _ = await asyncio.wait(
                        {assignment_future},
                        timeout=effective_timeout,
                    )
                    if not done:
                        self.runtime_manager.cancel_agent(
                            self.run_id, agent.agent_id
                        )
                        self._event(
                            "TaskTimedOut",
                            agent_id=agent.agent_id,
                            task_id=task.task_id,
                            payload={
                                "task_run_id": task_run_id,
                                "timeout_seconds": effective_timeout,
                                "attempt": task.attempts + 1,
                            },
                        )
                        assignment_future.cancel()
                        with suppress(asyncio.CancelledError):
                            await assignment_future
                        raise TimeoutError(
                            "task execution timed out after "
                            f"{effective_timeout:g}s"
                        )
                result = await assignment_future

                if task_input["cancel_event"].is_set():
                    if self.cancel_event.is_set():
                        self.board.cancel(task.task_id, "cancelled_during_execution", run_id=self.run_id)
                    else:
                        # A stopped teammate must not turn a late success into
                        # verified completion.  Release its work so another
                        # compatible teammate can claim it.
                        self.board.release(task.task_id, agent.agent_id, "agent_stopped", run_id=self.run_id)
                    history.update_task_run_status(task_run_id, "cancelled", error="cancelled")
                    return

                if result.success:
                    commit_sha = None
                    if lease is not None and self.integration_manager is not None:
                        try:
                            commit_sha = self.integration_manager.commit(
                                lease, f"task {task.task_id}", run_id=self.run_id,
                                agent_id=agent.agent_id,
                            )
                            artifact_store = getattr(self.verifier, "artifact_store", None)
                            if artifact_store is not None:
                                artifact_store.bind_commit(list(result.artifact_ids), commit_sha)
                        except Exception as exc:
                            from app.multiagent.permission import PermissionRequired
                            if isinstance(exc, PermissionRequired):
                                session.transition(TeammateLifecycle.WAITING_PERMISSION)
                                task.status = BoardTaskStatus.BLOCKED
                                task.last_error = str(exc)
                                task.metadata["permission_request_id"] = exc.request.request_id
                                self.board.add(task)
                                history.update_task_run_status(task_run_id, "waiting_permission",
                                                               error=str(exc))
                                return
                            raise
                    # A worker only produces evidence.  It never marks its
                    # own task succeeded; that transition is verifier-owned.
                    if not self.board.mark_produced(
                        task.task_id, agent.agent_id,
                        artifact_ids=list(result.artifact_ids),
                        run_id=self.run_id,
                    ):
                        self._record_task_state_conflict(
                            task=task,
                            agent_id=agent.agent_id,
                            task_run_id=task_run_id,
                            history=history,
                            transition="mark_produced",
                            expected_statuses=[BoardTaskStatus.RUNNING],
                            artifact_ids=list(result.artifact_ids),
                        )
                        return
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.TASK_PRODUCED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id,
                         "artifact_ids": list(result.artifact_ids)},
                    )
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.VERIFICATION_STARTED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id},
                    )
                    # Verification may call an online judge and local checks.
                    # Keep it off the scheduler loop so one slow judge cannot
                    # freeze sibling dispatch, heartbeats, pause/cancel
                    # observation, or SSE-visible progress.
                    if not self.board.mark_verifying(task.task_id, run_id=self.run_id):
                        self._record_task_state_conflict(
                            task=task,
                            agent_id=agent.agent_id,
                            task_run_id=task_run_id,
                            history=history,
                            transition="mark_verifying",
                            expected_statuses=[BoardTaskStatus.PRODUCED],
                            artifact_ids=list(result.artifact_ids),
                        )
                        return
                    if await self._verify_task_async(task):
                        completed_hook = await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.TASK_COMPLETED,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id,
                             "artifact_ids": list(result.artifact_ids)},
                        )
                        if completed_hook.block or not completed_hook.allow:
                            current = self.board.get(task.task_id, run_id=self.run_id)
                            if current is not None:
                                current.metadata["hook_feedback"] = completed_hook.feedback
                                self.board.add(current)
                            if not self.board.mark_repair_required(
                                task.task_id, run_id=self.run_id,
                            ):
                                self._record_task_state_conflict(
                                    task=task,
                                    agent_id=agent.agent_id,
                                    task_run_id=task_run_id,
                                    history=history,
                                    transition="mark_repair_required",
                                    expected_statuses=[BoardTaskStatus.VERIFYING],
                                    artifact_ids=list(result.artifact_ids),
                                )
                                return
                            history.update_task_run_status(
                                task_run_id, "failed",
                                error=completed_hook.feedback or "TaskCompleted hook blocked",
                            )
                            await get_lifecycle_hook_engine().emit_async(
                                LifecycleEvent.VERIFICATION_COMPLETED,
                                {"run_id": self.run_id, "agent_id": agent.agent_id,
                                 "task_id": task.task_id, "verdict": "repair",
                                 "feedback": completed_hook.feedback},
                            )
                            return
                        if commit_sha and self.integration_manager is not None:
                            from app.multiagent.git_workspace import MergeQueueItem
                            integrated = self.integration_manager.integrate(MergeQueueItem(
                                queue_id=f"merge_{task.task_id}_{agent.agent_id}",
                                run_id=self.run_id, agent_id=agent.agent_id,
                                commit_sha=commit_sha, branch=lease.branch,
                            ))
                            if integrated.status == "conflict":
                                current = self.board.get(task.task_id, run_id=self.run_id)
                                current.status = BoardTaskStatus.REPAIR_REQUIRED
                                current.metadata["merge_conflicts"] = integrated.conflicts
                                self.board.add(current)
                                history.update_task_run_status(task_run_id, "failed",
                                                               error="merge_conflict")
                                return
                        artifact_store = getattr(self.verifier, "artifact_store", None)
                        if artifact_store is not None:
                            try:
                                for artifact_id in result.artifact_ids:
                                    if not artifact_store.mark_verified(artifact_id):
                                        raise RuntimeError(
                                            f"artifact status row missing: {artifact_id}"
                                        )
                            except Exception as exc:
                                self._reject_artifacts_fail_closed(
                                    artifact_store, list(result.artifact_ids),
                                )
                                if not self.board.mark_repair_required(
                                    task.task_id, run_id=self.run_id,
                                ):
                                    self._record_task_state_conflict(
                                        task=task,
                                        agent_id=agent.agent_id,
                                        task_run_id=task_run_id,
                                        history=history,
                                        transition="artifact_verification_rollback",
                                        expected_statuses=[BoardTaskStatus.VERIFYING],
                                        artifact_ids=list(result.artifact_ids),
                                    )
                                    return
                                error = f"artifact_verification_commit_failed: {exc}"
                                history.update_task_run_status(
                                    task_run_id, "failed", error=error,
                                )
                                self._event(
                                    "ArtifactVerificationCommitFailed",
                                    agent_id=agent.agent_id,
                                    task_id=task.task_id,
                                    payload={
                                        "task_run_id": task_run_id,
                                        "artifact_ids": list(result.artifact_ids),
                                        "error": str(exc),
                                    },
                                )
                                return
                        # Board success is the final transition and therefore
                        # cannot precede governed Git integration.
                        if not self.board.mark_verified(
                            task.task_id, run_id=self.run_id,
                        ):
                            self._reject_artifacts_fail_closed(
                                artifact_store, list(result.artifact_ids),
                            )
                            self._record_task_state_conflict(
                                task=task,
                                agent_id=agent.agent_id,
                                task_run_id=task_run_id,
                                history=history,
                                transition="mark_verified",
                                expected_statuses=[BoardTaskStatus.VERIFYING],
                                artifact_ids=list(result.artifact_ids),
                            )
                            return
                        history.update_task_run_status(task_run_id, "succeeded")
                        await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.VERIFICATION_COMPLETED,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id, "verdict": "pass"},
                        )
                    else:
                        if not self.board.mark_repair_required(
                            task.task_id, run_id=self.run_id,
                        ):
                            self._record_task_state_conflict(
                                task=task,
                                agent_id=agent.agent_id,
                                task_run_id=task_run_id,
                                history=history,
                                transition="mark_repair_required",
                                expected_statuses=[BoardTaskStatus.VERIFYING],
                                artifact_ids=list(result.artifact_ids),
                            )
                            return
                        history.update_task_run_status(task_run_id, "failed", error="verification_failed")
                        await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.VERIFICATION_COMPLETED,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id, "verdict": "repair"},
                        )
                    # 实时更新 CapabilityRegistry 指标。
                    try:
                        from app.multiagent.agent_profile import get_capability_registry
                        get_capability_registry().record_success(agent.profile_id)
                    except Exception:
                        pass
                    logger.info(
                        f"[ParallelSched] task={task.task_id} agent={agent.agent_id} succeeded"
                    )
                else:
                    error = result.error or "unknown"
                    decision = self._record_task_failure(
                        task=task,
                        agent_id=agent.agent_id,
                        error=error,
                        task_run_id=task_run_id,
                        history=history,
                    )
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.TASK_FAILED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id, "error": error,
                         "retry": decision.to_dict()},
                    )
                    # 实时更新 CapabilityRegistry 指标。
                    try:
                        from app.multiagent.agent_profile import get_capability_registry
                        get_capability_registry().record_failure(agent.profile_id)
                    except Exception:
                        pass
                    last_state = self.board.get(task.task_id, run_id=self.run_id)
                    logger.warning(
                        f"[ParallelSched] task={task.task_id} failed: {result.error} "
                        f"now status={last_state.status.value}"
                    )
            except Exception as exc:
                logger.error(
                    f"[ParallelSched] task={task.task_id} agent={agent.agent_id} "
                    f"raised: {exc}"
                )
                decision = self._record_task_failure(
                    task=task,
                    agent_id=agent.agent_id,
                    error=str(exc),
                    task_run_id=task_run_id,
                    history=history,
                )
                try:
                    from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.TASK_FAILED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id, "error": str(exc),
                         "retry": decision.to_dict()},
                    )
                except Exception:
                    pass
            finally:
                beat_stop.set()
                beat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await beat_task
                # 状态恢复
                self.registry.release_reservation(agent.agent_id, task.task_id)
                session.current_task_id = None
                if session.lifecycle_state not in (
                    TeammateLifecycle.WAITING_PERMISSION, TeammateLifecycle.BLOCKED,
                    TeammateLifecycle.STOPPED, TeammateLifecycle.FAILED,
                ):
                    session.transition(TeammateLifecycle.IDLE)
                get_teammate_supervisor().persist(session)
                if session.lifecycle_state == TeammateLifecycle.IDLE:
                    try:
                        from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
                        idle_hook = await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.TEAMMATE_IDLE,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id},
                        )
                        if idle_hook.request_replan:
                            self.control_plane.team_request_replan(
                                self.run_id, agent.agent_id,
                                idle_hook.feedback or "TeammateIdle hook requested replan",
                                task_id=task.task_id,
                            )
                    except Exception as exc:
                        logger.warning("[ParallelSched] TeammateIdle hook failed: %s", exc)
        except Exception:
            # Reservation occurred before board claim.  Always release it if
            # a cancellation or unexpected error happens in-between.
            self.registry.release_reservation(agent.agent_id, task.task_id)
            raise

    def _record_task_failure(
        self,
        *,
        task: BoardTask,
        agent_id: str,
        error: str,
        task_run_id: str,
        history: Any,
    ) -> RetryDecision:
        """Persist one classified failure and its next recovery action."""
        decision = self.retry_policy.decide(
            error,
            attempt=task.attempts + 1,
            max_attempts=task.max_attempts,
        )
        self.board.fail(
            task.task_id,
            agent_id,
            error,
            run_id=self.run_id,
            retryable=decision.retryable,
            retry_delay_seconds=decision.delay_seconds,
            failure_category=decision.category.value,
        )
        current = self.board.get(task.task_id, run_id=self.run_id)
        history.update_task_run_status(task_run_id, "failed", error=error)
        self._event(
            "TaskRetryScheduled" if decision.retryable else "TaskFailedPermanently",
            agent_id=agent_id,
            task_id=task.task_id,
            payload={
                "task_run_id": task_run_id,
                "attempt": current.attempts if current else task.attempts + 1,
                "max_attempts": current.max_attempts if current else task.max_attempts,
                "next_attempt_at": (
                    current.next_attempt_at.isoformat()
                    if current and current.next_attempt_at
                    else None
                ),
                "error": error,
                **decision.to_dict(),
            },
        )
        return decision

    @staticmethod
    def _reject_artifacts_fail_closed(
        artifact_store: Any,
        artifact_ids: list[str],
        *,
        protected_ids: set[str] | None = None,
    ) -> None:
        """Best-effort rollback for evidence that must not remain verified."""
        if artifact_store is None:
            return
        protected = protected_ids or set()
        for artifact_id in dict.fromkeys(artifact_ids):
            if artifact_id in protected:
                continue
            try:
                artifact_store.mark_rejected(artifact_id)
            except Exception as exc:
                logger.error(
                    "[ParallelSched] failed to reject artifact=%s: %s",
                    artifact_id,
                    exc,
                )

    def _record_task_state_conflict(
        self,
        *,
        task: BoardTask,
        agent_id: str,
        task_run_id: str,
        history: Any,
        transition: str,
        expected_statuses: list[BoardTaskStatus],
        artifact_ids: list[str] | None = None,
    ) -> None:
        """Stop a stale completion path without overwriting newer authority."""
        current = self.board.get(task.task_id, run_id=self.run_id)
        actual_status = current.status if current is not None else None
        protected_ids = (
            set(current.produced_artifact_ids)
            if current is not None and current.status == BoardTaskStatus.SUCCEEDED
            else set()
        )
        self._reject_artifacts_fail_closed(
            getattr(self.verifier, "artifact_store", None),
            artifact_ids or [],
            protected_ids=protected_ids,
        )
        expected = [status.value for status in expected_statuses]
        actual = actual_status.value if actual_status is not None else "missing"
        error = (
            f"task_state_conflict:{transition}:"
            f"expected={','.join(expected)}:actual={actual}"
        )
        task_run_status = (
            "succeeded"
            if actual_status == BoardTaskStatus.SUCCEEDED
            else "cancelled"
            if actual_status == BoardTaskStatus.CANCELLED
            else "failed"
        )
        history.update_task_run_status(
            task_run_id,
            task_run_status,
            error=None if task_run_status == "succeeded" else error,
        )
        self._event(
            "TaskStateConflict",
            agent_id=agent_id,
            task_id=task.task_id,
            payload={
                "task_run_id": task_run_id,
                "transition": transition,
                "expected_statuses": expected,
                "actual_status": actual,
                "artifact_ids": list(artifact_ids or []),
            },
        )
        logger.warning(
            "[ParallelSched] stale completion stopped task=%s transition=%s "
            "expected=%s actual=%s",
            task.task_id,
            transition,
            expected,
            actual,
        )

    def _task_graph_from_board(self) -> Any:
        """Compatibility bridge for legacy callers while never passing None."""
        from app.multiagent.task_graph import TaskGraph, TaskNode
        graph = TaskGraph(root_task_id="task_team")
        for task in self.board.list_by_run(self.run_id):
            graph.add_node(TaskNode(
                id=task.task_id, title=task.title, objective=task.objective,
                dependencies=task.dependencies,
                required_capabilities=task.required_capabilities,
            ))
        self.task_graph = graph
        return graph

    def _refresh_task_graph(self) -> None:
        """Consume durable control-plane graph mutations before dispatch.

        Teammates may create tasks while the scheduler is already running.
        The TaskBoard mutation is durable, but an executor must receive the
        matching latest TaskGraph as well; otherwise it rejects the new task
        as ``task not in dag``.  The persisted snapshot is the versioned
        authority, so refreshing it is safe across both in-process mutations
        and process recovery.
        """
        from app.infrastructure.database.run_store import get_agent_run_history
        from app.multiagent.task_graph import TaskGraph

        payload = get_agent_run_history().load_task_graph(self.run_id)
        if payload is None:
            return
        latest_version = int(payload.get("version", 0))
        current_version = int(getattr(self.task_graph, "version", -1))
        if self.task_graph is not None and latest_version <= current_version:
            return
        graph = TaskGraph.model_validate(payload)
        graph.validate()
        self.task_graph = graph
        self._event("TaskGraphReloaded", payload={
            "previous_version": current_version,
            "version": latest_version,
            "task_count": len(graph.nodes),
        })

    async def _verify_task_async(self, task: BoardTask) -> bool:
        return await asyncio.to_thread(self._verify_task, task)

    def _verify_task(self, task: BoardTask) -> bool:
        """Verifier-owned per-task completion gate.

        Legacy callers without a Verifier retain a compatibility approval
        gate, but the TASK_TEAM facade always injects the real Verifier and
        ArtifactStore, so production never treats executor success as proof.
        """
        if self.verifier is None:
            return True
        store = getattr(self.verifier, "artifact_store", None)
        artifacts: dict[str, dict[str, Any]] = {}
        if store is not None:
            # Recompute the on-disk hash for every produced artifact before
            # handing it to the verifier.  ``_collect_dependency_artifacts``
            # already called ``verify_integrity`` on upstream evidence, but
            # the task's *own* outputs were never re-hashed, so a tampered
            # artifact (post-``mark_produced`` edit, symlink swap, partial
            # write after a crash) would pass verification.  Fail closed: a
            # single integrity failure rejects the whole task.
            tampered: list[str] = []
            for artifact in store.list_by_task(task.task_id):
                if artifact.run_id != self.run_id:
                    continue
                try:
                    if not store.verify_integrity(artifact.id):
                        tampered.append(artifact.id)
                        continue
                except Exception as exc:
                    logger.warning(
                        "[ParallelSched] integrity probe failed task=%s art=%s: %s",
                        task.task_id, artifact.id, exc,
                    )
                    tampered.append(artifact.id)
                    continue
                content = store.read(artifact.id)
                artifacts[artifact.id] = {"artifact_id": artifact.id,
                                          "content": content or "", "path": artifact.path}
            if tampered:
                current = self.board.get(task.task_id, run_id=self.run_id)
                if current is not None:
                    current.metadata["verification"] = {
                        "verdict": "repair",
                        "summary": "artifact integrity check failed",
                        "tampered_artifact_ids": tampered,
                    }
                    self.board.add(current)
                logger.warning(
                    "[ParallelSched] integrity check failed task=%s arts=%s",
                    task.task_id, tampered,
                )
                return False
        node = self.task_graph.nodes.get(task.task_id) if self.task_graph else None
        requires_artifact = bool(node and getattr(node, "output_contract", None)
                                 and getattr(node.output_contract, "artifact_type", "any") != "any")
        if requires_artifact and not artifacts:
            return False
        try:
            checks = None
            if node is not None:
                from app.multiagent.verifier import VerificationPlan
                checks = VerificationPlan.from_output_contract(
                    node.output_contract, workspace_root=(store.root_path if store else None),
                ).to_checks()
            result = self.verifier.validate(goal=task.objective, artifacts=artifacts,
                                            checks=checks)
            from dataclasses import asdict, is_dataclass
            current = self.board.get(task.task_id, run_id=self.run_id)
            if current is not None:
                def dump(value: Any) -> Any:
                    if is_dataclass(value):
                        return asdict(value)
                    if hasattr(value, "model_dump"):
                        return value.model_dump(mode="json")
                    return value
                current.metadata["verification"] = {
                    "verdict": result.verdict.value,
                    "summary": result.summary,
                    "failed_criteria": [dump(item) for item in result.failed_criteria],
                    "evidence": [dump(item) for item in result.evidence],
                    "proposed_tasks": [dump(item) for item in result.proposed_tasks],
                }
                self.board.add(current)
            return result.verdict.value == "pass"
        except Exception as exc:
            logger.warning("[ParallelSched] verifier failed task=%s: %s", task.task_id, exc)
            return False

    def _collect_dependency_artifacts(self, task: BoardTask) -> tuple[list[str], list[dict[str, Any]]]:
        """Resolve verified dependencies plus explicit repair source evidence."""
        store = getattr(self.verifier, "artifact_store", None)
        if store is None:
            return [], []
        ids: list[str] = []
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append_artifact(
            artifact_id: str,
            *,
            purpose: str,
            require_verified: bool,
        ) -> None:
            if artifact_id in seen:
                return
            artifact = store.get(artifact_id)
            if artifact is None:
                raise RuntimeError(f"artifact_not_found:{artifact_id}")
            if artifact.run_id != self.run_id:
                raise RuntimeError(f"artifact_wrong_run:{artifact_id}")
            status = getattr(artifact.status, "value", artifact.status)
            if require_verified and status != "verified":
                raise RuntimeError(f"artifact_not_verified:{artifact_id}")
            if not store.verify_integrity(artifact_id):
                raise RuntimeError(f"artifact_integrity_failed:{artifact_id}")
            seen.add(artifact_id)
            ids.append(artifact_id)
            refs.append({
                "artifact_id": artifact.id,
                "task_id": artifact.task_id,
                "producing_agent_id": artifact.produced_by,
                "type": artifact.type.value,
                "path": artifact.path,
                "content_hash": artifact.content_hash,
                "version": artifact.version,
                "commit_sha": artifact.commit_sha or artifact.metadata.get("commit_sha"),
                "verification_state": status,
                "purpose": purpose,
                "created_at": artifact.created_at.isoformat(),
                "summary": artifact.metadata.get("summary", ""),
            })

        for dependency_id in task.dependencies:
            dependency = self.board.get(dependency_id, run_id=self.run_id)
            if dependency is None or dependency.status != BoardTaskStatus.SUCCEEDED:
                raise RuntimeError(f"dependency_not_verified:{dependency_id}")
            for artifact_id in dependency.produced_artifact_ids:
                append_artifact(
                    artifact_id,
                    purpose="dependency",
                    require_verified=True,
                )
        for artifact_id in task.metadata.get("source_artifact_ids", []):
            append_artifact(
                str(artifact_id),
                purpose="repair_source",
                require_verified=False,
            )
        return ids, refs

    # ===== 工具 =====

    def _event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        from app.infrastructure.database.run_store import (
            get_agent_run_history,
            make_run_event_id,
        )

        get_agent_run_history().record_event(
            event_id=make_run_event_id(),
            run_id=self.run_id,
            event_type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            timestamp=datetime.now(UTC),
            payload=payload or {},
        )

    def _finalize(
        self, rounds: int, status: str, error: str | None = None,
    ) -> ParallelRunResult:
        summarize = self.board.summary(self.run_id)
        result = ParallelRunResult(
            status=status,
            rounds=rounds,
            total_tasks=summarize.get("total", 0),
            # Scheduler completion counts produced tasks; final verified
            # completion remains visible separately in ``summary``.
            succeeded=summarize.get(BoardTaskStatus.SUCCEEDED.value, 0),
            failed=summarize.get(BoardTaskStatus.FAILED.value, 0),
            error=error,
            summary=summarize,
        )
        self._event("SchedulerStopped", payload=result.to_dict())
        return result

    def _finalize_verified_run(self, rounds: int) -> ParallelRunResult:
        """Apply run-level gates after every task is verifier-owned SUCCEEDED."""
        pending_permissions = self.permission_broker.list_pending(self.run_id)
        if pending_permissions:
            return self._finalize(rounds, ScheduleStatus.WAITING_HUMAN.value,
                                  "pending_high_risk_permissions")
        if any(task.metadata.get("merge_conflicts")
               for task in self.board.list_by_run(self.run_id)):
            return self._finalize(rounds, ScheduleStatus.FAILED.value,
                                  "unresolved_merge_conflicts")
        if self.integration_manager is not None:
            root = self.task_graph.nodes.get(self.task_graph.root_task_id) if self.task_graph else None
            plan = self._integration_verification_plan(root)
            for check in plan.checks:
                payload = check.observable_payload()
                self._event("IntegrationVerificationStarted", payload=payload)
                try:
                    result = self.integration_manager.verify_check(check)
                except Exception as exc:
                    self._event("IntegrationVerificationUnavailable", payload={
                        **payload,
                        "error": str(exc),
                    })
                    return self._finalize(
                        rounds,
                        ScheduleStatus.WAITING_HUMAN.value,
                        "integration_verification_unavailable",
                    )
                self._event("IntegrationVerificationCompleted", payload={
                    **payload,
                    "returncode": result.returncode,
                    "cancelled": result.cancelled,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                    "stdout_preview": result.stdout[-2_000:],
                    "stderr_preview": result.stderr[-2_000:],
                })
                if result.returncode != 0 or result.cancelled or result.timed_out:
                    return self._finalize(
                        rounds,
                        ScheduleStatus.FAILED.value,
                        f"integration_verification_failed:{check.label}",
                    )
            if plan.missing_requirements:
                self._event("IntegrationVerificationUnavailable", payload={
                    "missing_requirements": list(plan.missing_requirements),
                    "source": plan.source,
                })
                return self._finalize(
                    rounds,
                    ScheduleStatus.WAITING_HUMAN.value,
                    "integration_verification_requirements_missing",
                )
        return self._finalize(rounds, ScheduleStatus.COMPLETED.value)

    def _integration_verification_plan(self, root: Any) -> Any:
        from app.multiagent.git_workspace import (
            IntegrationVerificationCheck,
            IntegrationVerificationPlan,
        )

        metadata = root.metadata if root is not None else {}
        configured = metadata.get("integration_test_commands")
        if configured is None and metadata.get("integration_test_argv"):
            configured = [metadata["integration_test_argv"]]
        if configured is None:
            return self.integration_manager.discover_verification_plan()
        if not isinstance(configured, list):
            return IntegrationVerificationPlan(
                missing_requirements=("invalid_integration_test_commands",),
                source="configured",
            )
        checks: list[IntegrationVerificationCheck] = []
        missing: list[str] = []
        for index, item in enumerate(configured):
            if isinstance(item, list):
                argv = item
                label = f"Configured check {index + 1}"
                cwd = "."
                timeout = 300.0
            elif isinstance(item, dict):
                argv = item.get("argv")
                label = str(item.get("label") or f"Configured check {index + 1}")
                cwd = str(item.get("cwd") or ".")
                try:
                    timeout = float(item.get("timeout_seconds") or 300.0)
                except (TypeError, ValueError):
                    missing.append(
                        f"configured_check_{index + 1}:invalid_timeout"
                    )
                    continue
            else:
                missing.append(f"configured_check_{index + 1}:invalid_shape")
                continue
            relative_cwd = Path(cwd.replace("\\", "/"))
            if (
                len(cwd) > 512
                or relative_cwd.is_absolute()
                or ".." in relative_cwd.parts
                or (
                    relative_cwd.parts
                    and ":" in relative_cwd.parts[0]
                )
            ):
                missing.append(f"configured_check_{index + 1}:unsafe_cwd")
                continue
            if not math.isfinite(timeout):
                missing.append(f"configured_check_{index + 1}:invalid_timeout")
                continue
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(part, str) and part for part in argv)
            ):
                missing.append(f"configured_check_{index + 1}:invalid_argv")
                continue
            if not self._safe_integration_argv(argv):
                missing.append(f"configured_check_{index + 1}:unsafe_argv")
                continue
            dependency_source = None
            executable = self._integration_executable(argv[0])
            dependency_resolver = getattr(
                self.integration_manager,
                "node_dependency_source",
                None,
            )
            if (
                executable in {"npm", "pnpm", "yarn"}
                and callable(dependency_resolver)
            ):
                dependency_source = dependency_resolver(
                    relative_cwd.as_posix()
                )
                if dependency_source is None:
                    missing.append(
                        f"configured_check_{index + 1}:"
                        "node_dependencies_missing"
                    )
                    continue
            checks.append(IntegrationVerificationCheck(
                label=label,
                argv=tuple(argv),
                cwd_relative=relative_cwd.as_posix(),
                timeout_seconds=max(1.0, min(timeout, 3_600.0)),
                dependency_source=(
                    str(dependency_source)
                    if dependency_source is not None
                    else None
                ),
            ))
        if not checks and not missing:
            missing.append("configured_integration_checks_empty")
        return IntegrationVerificationPlan(
            checks=tuple(checks),
            missing_requirements=tuple(missing),
            source="configured",
        )

    @staticmethod
    def _integration_executable(value: str) -> str:
        executable = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
        for suffix in (".exe", ".cmd", ".bat"):
            executable = executable.removesuffix(suffix)
        return executable

    @classmethod
    def _safe_integration_argv(cls, argv: list[str]) -> bool:
        executable = cls._integration_executable(argv[0])
        args = [part.lower() for part in argv[1:]]
        if executable in {"pytest"}:
            return True
        if executable in {"python", "python3"}:
            return len(args) >= 2 and args[0] == "-m" and args[1] in {
                "pytest",
                "unittest",
                "compileall",
            }
        if executable in {"npm", "pnpm", "yarn"}:
            scripts = {"test", "build", "lint", "typecheck", "check"}
            return bool(args) and (
                args[0] in scripts
                or len(args) >= 2 and args[0] == "run" and args[1] in scripts
            )
        if executable == "cargo":
            return bool(args) and args[0] in {"test", "check", "clippy"}
        if executable == "go":
            return bool(args) and args[0] == "test"
        if executable in {"mvn", "gradle", "gradlew"}:
            return any(arg in {"test", "check", "verify"} for arg in args)
        if executable == "make":
            return bool(args) and args[0] in {"test", "check", "verify", "lint"}
        return False

    # ===== 任务板与 DAG 同步 =====

    @classmethod
    def sync_from_task_graph(
        cls, dag: Any, board: TaskBoard, run_id: str,
    ) -> None:
        """把 TaskGraph 的节点同步到 TaskBoard（仅同步 PENDING 节点）。

        在并行调度开始前调用，让 BoardTask 与 TaskNode 1:1 对应。
        """
        for node_id, node in dag.nodes.items():
            existing = board.get(node_id, run_id=run_id)
            if existing is not None:
                continue
            board.create_task(
                task_id=node_id,
                run_id=run_id,
                title=node.title or node_id,
                objective=node.objective,
                dependencies=list(node.dependencies),
                required_capabilities=list(node.required_capabilities),
                priority=getattr(node, "priority", 0),
                max_attempts=getattr(node, "max_attempts", 3),
            )

    @staticmethod
    def sync_back_to_dag(dag: Any, board: TaskBoard, run_id: str) -> None:
        """把 BoardTask 的最终状态回写到 TaskGraph。

        走合法转换链：PENDING → READY → RUNNING → SUCCEEDED/FAILED。
        """
        from app.multiagent.task_graph import TaskNodeStatus
        for t in board.list_by_run(run_id):
            node = dag.nodes.get(t.task_id)
            if node is None:
                continue
            target = None
            if t.status == BoardTaskStatus.SUCCEEDED:
                target = TaskNodeStatus.SUCCEEDED
            elif t.status == BoardTaskStatus.FAILED:
                target = TaskNodeStatus.FAILED
            elif t.status in (BoardTaskStatus.RUNNING, BoardTaskStatus.CLAIMED):
                target = TaskNodeStatus.RUNNING
            else:
                continue
            # 用 _step_to 推进到 target
            _step_to(dag, t.task_id, target)
            for art in t.produced_artifact_ids:
                node = dag.nodes.get(t.task_id)
                if art not in node.output_artifact_ids:
                    dag.accept_artifact(t.task_id, art)


def _step_to(dag: Any, node_id: str, target: Any) -> None:
    """按合法转换链推进节点状态到 target。

    链：PENDING → READY → RUNNING → SUCCEEDED/FAILED
    """
    from app.multiagent.task_graph import TaskNodeStatus
    node = dag.nodes.get(node_id)
    if node is None or node.status == target:
        return
    # PENDING → READY
    if node.status == TaskNodeStatus.PENDING:
        dag.update_status(node_id, TaskNodeStatus.READY)
    # READY → RUNNING
    node = dag.nodes.get(node_id)
    if node.status == TaskNodeStatus.READY and target != TaskNodeStatus.READY:
        dag.update_status(node_id, TaskNodeStatus.RUNNING)
    node = dag.nodes.get(node_id)
    # RUNNING → target (SUCCEEDED/FAILED)
    if node.status == TaskNodeStatus.RUNNING and target in (
        TaskNodeStatus.SUCCEEDED, TaskNodeStatus.FAILED
    ):
        dag.update_status(node_id, target)
