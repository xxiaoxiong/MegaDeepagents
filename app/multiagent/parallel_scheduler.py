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
        cancel_event: asyncio.Event | None = None,
        verifier: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self.max_rounds = max_rounds
        self.max_concurrency = max_concurrency
        self.heartbeat_interval = heartbeat_interval_seconds
        self.lease_timeout = lease_timeout_seconds
        self.task_graph = task_graph
        self.cancel_event = cancel_event or asyncio.Event()
        self.verifier = verifier

        self.board = get_task_board()
        self.registry = get_agent_registry()

    # ===== 主循环 =====

    async def run(self, executor: Any) -> ParallelRunResult:
        """执行并行调度。executor 必须实现 execute_task(dag, task_id, task_input)。"""
        round_n = 0
        last_error: str | None = None

        # 申明所有 team 任务的来源（TaskGraph 转化为 BoardTask 由 caller 完成）
        while round_n < self.max_rounds:
            round_n += 1

            if self.cancel_event.is_set():
                return self._finalize(round_n, status=ScheduleStatus.CANCELLED.value, error="cancelled")

            # 租约清理
            self.registry.cleanup_expired()

            pending = self.board.list_pending(self.run_id)
            if not pending:
                if self.board.all_succeeded(self.run_id) or self.board.all_produced(self.run_id):
                    logger.info(f"[ParallelSched] run={self.run_id}: all succeeded at round={round_n}")
                    return self._finalize(round_n, status="completed")
                # 死锁：没有 pending 也没有 all_succeeded
                running = self.board.list_by_run(self.run_id)
                running_states = [t.status for t in running]
                if any(s == BoardTaskStatus.RUNNING for s in running_states):
                    # 等一会再 round
                    logger.info(f"[ParallelSched] run={self.run_id}: 仍有 RUNNING 等待")
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(
                    f"[ParallelSched] run={self.run_id} deadlock: states={running_states}"
                )
                return self._finalize(
                    round_n, status="failed", error="scheduler_deadlock",
                )

            # 用信号量限制并发
            semaphore = asyncio.Semaphore(self.max_concurrency)
            coros = [
                self._run_one(task, executor, semaphore)
                for task in pending
            ]
            await asyncio.gather(*coros, return_exceptions=False)

            if self.board.all_succeeded(self.run_id) or self.board.all_produced(self.run_id):
                return self._finalize(round_n, status="completed")

        # 跑完 max_rounds 还没完成
        return self._finalize(round_n, status="incomplete", error="max_rounds")

    # ===== 单任务运行 =====

    async def _run_one(self, task: BoardTask, executor: Any, semaphore: Any) -> None:
        """认领并执行一个任务。如果 task 仍属 PENDING 且无人占用，则认领并执行。"""
        async with semaphore:
            if self.cancel_event.is_set():
                return
            # Selection and reservation are a single operation.  Do not call
            # find_idle here: a sibling coroutine can otherwise steal the
            # same worker before this task changes its status.
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
            async with semaphore:
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
            if not self.board.start(task.task_id, agent.agent_id, run_id=self.run_id):
                # 状态机异常，释放并放弃
                self.board.release(task.task_id, agent.agent_id, "start_failed", run_id=self.run_id)
                self.registry.release_reservation(agent.agent_id, task.task_id)
                return

            # Agent 状态机：IDLE → RUNNING
            from app.multiagent.agent_instance import AgentStatus
            agent.update_status(AgentStatus.RUNNING)
            from app.multiagent.agent_registry import AgentRegistry
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
                task_input.update({
                    "workspace_root": getattr(agent, "workspace_root", ""),
                    "agent_id": agent.agent_id,
                    "session_id": agent.session_id,
                    "thread_id": agent.thread_id,
                })
                # Mailbox is an execution input, not merely an audit log.
                # Deliver messages atomically before the worker constructs its
                # prompt so user/teammate interventions can affect the task.
                from app.multiagent.mailbox import get_mailbox
                task_input["mailbox_messages"] = [
                    message.model_dump(mode="json")
                    for message in get_mailbox().receive(agent.agent_id, max_count=20)
                ]
                dag = self.task_graph or self._task_graph_from_board()
                result = await asyncio.to_thread(
                    executor.execute_task, dag, task.task_id, task_input,
                )

                if result.success:
                    # A worker only produces evidence.  It never marks its
                    # own task succeeded; that transition is verifier-owned.
                    self.board.mark_produced(
                        task.task_id, agent.agent_id,
                        artifact_ids=list(result.artifact_ids),
                        run_id=self.run_id,
                    )
                    if self._verify_task(task):
                        self.board.mark_verifying(task.task_id, run_id=self.run_id)
                        self.board.mark_verified(task.task_id, run_id=self.run_id)
                        history.update_task_run_status(task_run_id, "succeeded")
                    else:
                        self.board.mark_repair_required(task.task_id, run_id=self.run_id)
                        history.update_task_run_status(task_run_id, "failed", error="verification_failed")
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
            finally:
                beat_stop.set()
                await asyncio.sleep(0)
                # 状态恢复
                self.registry.release_reservation(agent.agent_id, task.task_id)
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
                artifacts[artifact.id] = {"content": content or "", "path": artifact.path}
        node = self.task_graph.nodes.get(task.task_id) if self.task_graph else None
        requires_artifact = bool(node and getattr(node, "output_contract", None)
                                 and getattr(node.output_contract, "artifact_type", "any") != "any")
        if requires_artifact and not artifacts:
            return False
        try:
            result = self.verifier.validate(goal=task.objective, artifacts=artifacts)
            return result.verdict.value == "pass"
        except Exception as exc:
            logger.warning("[ParallelSched] verifier failed task=%s: %s", task.task_id, exc)
            return False

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
            succeeded=(summarize.get(BoardTaskStatus.SUCCEEDED.value, 0)
                       + summarize.get(BoardTaskStatus.PRODUCED.value, 0)),
            failed=summarize.get(BoardTaskStatus.FAILED.value, 0),
            error=error,
            summary=summarize,
        )

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
