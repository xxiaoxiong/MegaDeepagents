"""ParallelTeamScheduler — 基于 asyncio + TaskBoard + AgentRegistry 的真实并行调度。

Phase E（docs/MegaDeepagents_Agent_Teams_改造任务书.md §10）：
- 不再用顺序 for 循环遍历 ready_tasks；改用 asyncio 协程池并行执行
- TaskBoard 提供原子认领，多 Agent 可同时抢任务
- AgentRegistry 提供 Agent 生命周期 + 心跳，调度器从空闲池子里挑 worker
- 失败的 task 通过 board.fail() 自动重试到 max_attempts
- 持续工作直到 all_succeeded 或 max_rounds 到达

设计原则：
- 与现有 _run_sync_fallback 并存：
  - TASK_TEAM 默认走 ParallelTeamScheduler（async）
  - 旧的 Sync scheduler 保留作为已知回退
- 优先保证 LLM 工具场景的吞吐：无相互依赖的 task 并行执行
- 单 task 失败不阻塞其他 task
- 调度器和 AgentRegistry 通过心跳互锁：超时的 Agent 被回收，其任务由 timeout
  处理程序 release 回 PENDING 给其他 Agent
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from app.core.logging import logger
from app.multiagent.agent_registry import AgentRegistry, get_agent_registry
from app.multiagent.task_board import (
    BoardTask,
    BoardTaskStatus,
    ClaimResult,
    TaskBoard,
    get_task_board,
)


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
    ) -> None:
        self.run_id = run_id
        self.max_rounds = max_rounds
        self.max_concurrency = max_concurrency
        self.heartbeat_interval = heartbeat_interval_seconds
        self.lease_timeout = lease_timeout_seconds
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

    # ===== 主循环 =====

    async def run(self, executor: Any) -> ParallelRunResult:
        """执行并行调度。executor 必须实现 execute_task(dag, task_id, task_input)。"""
        round_n = 0
        last_error: str | None = None

        # 申明所有 team 任务的来源（TaskGraph 转化为 BoardTask 由 caller 完成）
        while round_n < self.max_rounds:
            round_n += 1

            from app.multiagent.phase_g_store import get_agent_run_history
            run_record = get_agent_run_history().get_team_run(self.run_id)
            if run_record and run_record.get("status") == "paused":
                return self._finalize(round_n, status=ScheduleStatus.PAUSED.value,
                                      error="paused")

            if self.cancel_event.is_set():
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value, error="cancelled")

            # 租约清理
            self.registry.cleanup_expired()

            pending = self.board.list_pending(self.run_id)
            if not pending:
                if self.board.all_succeeded(self.run_id):
                    logger.info(f"[ParallelSched] run={self.run_id}: all succeeded at round={round_n}")
                    return self._finalize_verified_run(round_n)
                # 死锁：没有 pending 也没有 all_succeeded
                running = self.board.list_by_run(self.run_id)
                running_states = [t.status for t in running]
                if any(s == BoardTaskStatus.RUNNING for s in running_states):
                    # 等一会再 round
                    logger.info(f"[ParallelSched] run={self.run_id}: 仍有 RUNNING 等待")
                    await asyncio.sleep(0.5)
                    continue
                if any(s in (BoardTaskStatus.BLOCKED, BoardTaskStatus.REPAIR_REQUIRED,
                             BoardTaskStatus.REPLAN_REQUIRED) for s in running_states):
                    return self._finalize(
                        round_n, status=ScheduleStatus.WAITING_HUMAN.value,
                        error="control_plane_intervention_required",
                    )
                logger.warning(
                    f"[ParallelSched] run={self.run_id} deadlock: states={running_states}"
                )
                return self._finalize(
                    round_n, status="failed", error="scheduler_deadlock",
                )

            # 用信号量限制并发
            semaphore = asyncio.Semaphore(self.max_concurrency)
            coros = [
                self._run_one_guarded(task, executor, semaphore)
                for task in pending
            ]
            await asyncio.gather(*coros, return_exceptions=False)

            if self.cancel_event.is_set():
                self.board.cancel_run(self.run_id)
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value, error="cancelled")

            run_record = get_agent_run_history().get_team_run(self.run_id)
            if run_record and run_record.get("status") == "paused":
                return self._finalize(round_n, status=ScheduleStatus.PAUSED.value,
                                      error="paused")

            if self.board.all_succeeded(self.run_id):
                return self._finalize_verified_run(round_n)

        # 跑完 max_rounds 还没完成
        return self._finalize(round_n, status="incomplete", error="max_rounds")

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
            self.run_id, set(task.required_capabilities), task.task_id,
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
            from app.multiagent.phase_g_store import get_agent_run_history, make_task_run_id
            history = get_agent_run_history()
            task_run_id = make_task_run_id()
            history.insert_task_run(
                task_run_id=task_run_id, task_id=task.task_id, agent_id=agent.agent_id,
                run_id=self.run_id, attempt=task.attempts + 1, status="running",
                metadata={"session_id": agent.session_id, "thread_id": agent.thread_id},
            )

            async def _heartbeat_loop():
                while not beat_stop.is_set():
                    self.registry.heartbeat(agent.agent_id)
                    await asyncio.sleep(self.heartbeat_interval)

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
                result = await self.runtime_manager.execute_assignment(
                    executor=executor, task_graph=dag, task_id=task.task_id,
                    task_input=task_input, cancel_event=self.cancel_event,
                    agent_registry=self.registry,
                )

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
                    self.board.mark_produced(
                        task.task_id, agent.agent_id,
                        artifact_ids=list(result.artifact_ids),
                        run_id=self.run_id,
                    )
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
                    if self._verify_task(task):
                        self.board.mark_verifying(task.task_id, run_id=self.run_id)
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
                            self.board.mark_repair_required(task.task_id, run_id=self.run_id)
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
                        artifact_store = getattr(self.verifier, "artifact_store", None)
                        if artifact_store is not None:
                            for artifact_id in result.artifact_ids:
                                artifact_store.mark_verified(artifact_id)
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
                        # Board success is the final transition and therefore
                        # cannot precede governed Git integration.
                        self.board.mark_verified(task.task_id, run_id=self.run_id)
                        history.update_task_run_status(task_run_id, "succeeded")
                        await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.VERIFICATION_COMPLETED,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id, "verdict": "pass"},
                        )
                    else:
                        self.board.mark_repair_required(task.task_id, run_id=self.run_id)
                        history.update_task_run_status(task_run_id, "failed", error="verification_failed")
                        await get_lifecycle_hook_engine().emit_async(
                            LifecycleEvent.VERIFICATION_COMPLETED,
                            {"run_id": self.run_id, "agent_id": agent.agent_id,
                             "task_id": task.task_id, "verdict": "repair"},
                        )
                    # Phase Two #19: 实时更新 CapabilityRegistry 指标
                    try:
                        from app.multiagent.agent_profile import get_capability_registry
                        get_capability_registry().record_success(agent.profile_id)
                    except Exception:
                        pass
                    logger.info(
                        f"[ParallelSched] task={task.task_id} agent={agent.agent_id} succeeded"
                    )
                else:
                    self.board.fail(task.task_id, agent.agent_id, result.error or "unknown", run_id=self.run_id)
                    history.update_task_run_status(task_run_id, "failed", error=result.error or "unknown")
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.TASK_FAILED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id, "error": result.error or "unknown"},
                    )
                    # Phase Two #19: 实时更新 CapabilityRegistry 指标
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
                self.board.fail(task.task_id, agent.agent_id, str(exc), run_id=self.run_id)
                history.update_task_run_status(task_run_id, "failed", error=str(exc))
                try:
                    from app.multiagent.lifecycle_hooks import LifecycleEvent, get_lifecycle_hook_engine
                    await get_lifecycle_hook_engine().emit_async(
                        LifecycleEvent.TASK_FAILED,
                        {"run_id": self.run_id, "agent_id": agent.agent_id,
                         "task_id": task.task_id, "error": str(exc)},
                    )
                except Exception:
                    pass
            finally:
                beat_stop.set()
                await asyncio.sleep(0)
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
                            )
                    except Exception as exc:
                        logger.warning("[ParallelSched] TeammateIdle hook failed: %s", exc)
        except Exception:
            # Reservation occurred before board claim.  Always release it if
            # a cancellation or unexpected error happens in-between.
            self.registry.release_reservation(agent.agent_id, task.task_id)
            raise

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
            for artifact in store.list_by_task(task.task_id):
                if artifact.run_id != self.run_id:
                    continue
                content = store.read(artifact.id)
                artifacts[artifact.id] = {"artifact_id": artifact.id,
                                          "content": content or "", "path": artifact.path}
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
        """Resolve only direct, verified, same-run dependency artifacts."""
        store = getattr(self.verifier, "artifact_store", None)
        if not task.dependencies or store is None:
            return [], []
        ids: list[str] = []
        refs: list[dict[str, Any]] = []
        for dependency_id in task.dependencies:
            dependency = self.board.get(dependency_id, run_id=self.run_id)
            if dependency is None or dependency.status != BoardTaskStatus.SUCCEEDED:
                raise RuntimeError(f"dependency_not_verified:{dependency_id}")
            for artifact_id in dependency.produced_artifact_ids:
                artifact = store.get(artifact_id)
                if artifact is None:
                    raise RuntimeError(f"artifact_not_found:{artifact_id}")
                if artifact.run_id != self.run_id:
                    raise RuntimeError(f"artifact_wrong_run:{artifact_id}")
                if getattr(artifact.status, "value", artifact.status) != "verified":
                    raise RuntimeError(f"artifact_not_verified:{artifact_id}")
                if not store.verify_integrity(artifact_id):
                    raise RuntimeError(f"artifact_integrity_failed:{artifact_id}")
                ids.append(artifact_id)
                refs.append({
                    "artifact_id": artifact.id, "task_id": artifact.task_id,
                    "producing_agent_id": artifact.produced_by,
                    "type": artifact.type.value, "path": artifact.path,
                    "content_hash": artifact.content_hash, "version": artifact.version,
                    "commit_sha": artifact.commit_sha or artifact.metadata.get("commit_sha"),
                    "verification_state": artifact.status.value,
                    "created_at": artifact.created_at.isoformat(),
                    "summary": artifact.metadata.get("summary", ""),
                })
        return ids, refs

    # ===== 工具 =====

    def _finalize(
        self, rounds: int, status: str, error: str | None = None,
    ) -> ParallelRunResult:
        summarize = self.board.summary(self.run_id)
        return ParallelRunResult(
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
            argv = (root.metadata.get("integration_test_argv") if root else None)
            if not argv:
                return self._finalize(rounds, ScheduleStatus.FAILED.value,
                                      "integration_verification_missing")
            result = self.integration_manager.verify_integration(list(argv))
            if result.returncode != 0 or result.cancelled or result.timed_out:
                return self._finalize(rounds, ScheduleStatus.FAILED.value,
                                      "integration_verification_failed")
        return self._finalize(rounds, ScheduleStatus.COMPLETED.value)

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


def _node_transition(from_status: Any, to_status: Any) -> bool:
    """判断 TaskNode 状态转换是否合法（含中间补步）。"""
    from app.multiagent.task_graph import is_legal_task_transition, TaskNodeStatus
    if from_status == to_status:
        return True
    if is_legal_task_transition(from_status, to_status):
        return True
    chain_path = [TaskNodeStatus.READY, TaskNodeStatus.RUNNING]
    current = from_status
    for step in chain_path:
        if is_legal_task_transition(current, step):
            current = step
    return is_legal_task_transition(current, to_status)
