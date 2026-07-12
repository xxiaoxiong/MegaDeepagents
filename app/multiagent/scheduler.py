"""DAG-based Scheduler：基于 TaskGraph 的调度器，使用 LangGraph `Send` 实现并行 fan-out。

requirements（docs/upgradePhaseTwo.md 八）：

- 维护 `task_dag` 而非依赖 `state.plan: str`
- 每轮读取 `task_dag.ready_tasks()`，为每个 ready Task 通过 `langgraph.types.Send`
  dispatch 一个独立 task node
- Phase 与 DAG 联动：`task_dag.all_succeeded()` → 进入 finalizing
- Worker 失败 → Scheduler 触发 Verifier verdict
- 节点+边：build_dag() → entry → dispatch → run_task → join → decide → END

设计原则：
- Scheduler 是无状态图编排；Worker 是 `task_runner` 工具的 future；
- `Send(item={"task_id": ..., "agent_id": ...})` 让每个 task node 自己的输入
  独立，不共享 mutable 状态；
- 通过 verifying → repairing 阶段闭环：Verifier 反查失败回写 task.status=FAILED，
  Scheduler 在下一轮 emit repair Task。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.core.logging import logger
from app.multiagent.task_graph import TaskGraph, TaskNode, TaskNodeStatus

try:
    from langgraph.graph import StateGraph, END
    from langgraph.types import Send
    _LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    StateGraph = None  # type: ignore
    END = None  # type: ignore
    Send = None  # type: ignore
    _LANGGRAPH_AVAILABLE = False


# ===== Scheduler 状态 =====


@dataclass
class SchedulerState:
    """Scheduler 调度图的共享状态。"""

    task_dag: TaskGraph
    current_round: int = 0
    max_rounds: int = 30
    inflight_task_ids: list[str] = field(default_factory=list)
    completed_task_ids: list[str] = field(default_factory=list)
    failed_task_ids: list[str] = field(default_factory=list)
    phase: str = "executing"  # planning / executing / repairing / verifying / finalizing
    should_stop: bool = False
    termination_reason: str | None = None
    last_error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    """单个 Task 执行后的结果回写。"""

    task_id: str
    success: bool
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    attempted: bool = False


class SchedulerError(Exception):
    pass


class TaskScheduler:
    """基于 DAG 的调度器。

    Workflow：
        build_graph() → 编译出可执行 LangGraph
        run(graph) → 按 ready_tasks fan-out + join
    """

    def __init__(
        self,
        task_dag: TaskGraph,
        max_rounds: int = 30,
        worker_executor: "WorkerExecutor | None" = None,
    ) -> None:
        self.task_dag = task_dag
        self.max_rounds = max_rounds
        self.worker_executor = worker_executor or _InMemoryWorkerExecutor()
        self._compiled = None

    # ===== 节点定义 =====

    def node_dispatch(self, state: dict[str, Any]) -> list[Send]:
        """每轮读 DAG ready_tasks，fan-out 多个 Send 给 run_task 节点。"""
        dag = self._dag_from_state(state)
        # 递增轮次
        current_round = state.get("current_round", 0) + 1

        ready: list[TaskNode] = dag.ready_tasks()
        if not ready:
            return [Send(node="join", arg={"no_dispatch": True, "round": current_round})]

        # 标记为 RUNNING 并附到 inflight
        inflight: list[str] = []
        sends: list[Send] = []
        for task in ready:
            if not dag.update_status(task.id, TaskNodeStatus.READY):
                # 可能已是终态（如并发的 SUCCEEDED→FAILED）→ 跳过
                continue
            if not dag.update_status(task.id, TaskNodeStatus.RUNNING):
                logger.warning(f"[Scheduler] task {task.id} 无法转 RUNNING")
                continue
            inflight.append(task.id)
            sends.append(
                Send(
                    node="run_task",
                    arg={
                        "task_id": task.id,
                        "round": current_round,
                        "inflight": inflight,
                        "started_at": datetime.utcnow().isoformat(),
                    },
                )
            )
        return sends

    def node_run_task(self, task_input: dict[str, Any]) -> dict[str, Any]:
        """单个 Task 的执行入口。

        worker_executor 负责实际调用 LLM / 工具，并返回 TaskResult（序列化为 dict）。
        Scheduler 在此把结果回写到 self.task_dag。

        幂等保护：若 task 已处于终态（SUCCEEDED/FAILED/SKIPPED/CANCELLED），
        跳过重新执行，直接返回已保存的结果。这防止 checkpoint resume 时重复执行。
        """
        task_id = task_input.get("task_id")
        if not task_id:
            return {"task_id": None, "success": False, "error": "no task_id"}

        # 幂等保护：已终态的 task 跳过执行
        node = self.task_dag.nodes.get(task_id)
        if node and node.is_terminal():
            return {
                "task_id": task_id,
                "success": node.status == TaskNodeStatus.SUCCEEDED,
                "artifact_ids": list(node.output_artifact_ids),
                "error": None,
                "idempotent_skip": True,
            }

        result = self.worker_executor.execute_task(self.task_dag, task_id, task_input)

        if result.success:
            self.task_dag.update_status(task_id, TaskNodeStatus.SUCCEEDED)
            for art in result.artifact_ids:
                self.task_dag.accept_artifact(task_id, art)
        else:
            # 失败仍可重试：record_attempt + 标记 FAILED
            self.task_dag.record_attempt(task_id)
            self.task_dag.update_status(task_id, TaskNodeStatus.FAILED)
            logger.info(
                f"[Scheduler] task {task_id} failed (attempt recorded) err={result.error}"
            )
        return {
            "task_id": task_id,
            "success": result.success,
            "artifact_ids": result.artifact_ids,
            "error": result.error,
        }

    def node_join(self, state: dict[str, Any] | list[Any]) -> dict[str, Any]:
        """合并所有 run_task 的结果 + 决定下一轮 / 终止。"""
        # LangGraph 把 Send fan-out 的结果都累积成 list，需要兼容两种形态
        if isinstance(state, list):
            results: list[dict[str, Any]] = []
            for item in state:
                if isinstance(item, dict) and "task_id" in item:
                    results.append(item)
            current_round = max((r.get("round", 0) for r in results), default=0)
        else:
            results = state.get("results", []) if isinstance(state, dict) else []
            current_round = state.get("round", 0) if isinstance(state, dict) else 0

        # 终止判断
        dag = self.task_dag
        if dag.all_succeeded():
            return {
                "should_stop": True,
                "termination_reason": "all_tasks_succeeded",
                "current_round": current_round,
                "phase": "finalizing",
            }

        if current_round >= self.max_rounds:
            return {
                "should_stop": True,
                "termination_reason": "max_rounds",
                "current_round": current_round,
            }

        # 否则继续下一轮
        return {
            "should_stop": False,
            "current_round": current_round,
            "phase": "executing",
        }

    def node_decide(self, state: dict[str, Any]) -> dict[str, Any]:
        """根据 join 结果决定继续或终止。"""
        if state.get("should_stop"):
            return {"continue": False, "termination_reason": state.get("termination_reason")}
        return {"continue": True, "current_round": state.get("current_round", 0)}

    # ===== 图构建 =====

    def build_graph(self):
        """编译并返回 LangGraph。langgraph 不可用时返回 None。"""
        if not _LANGGRAPH_AVAILABLE:
            logger.warning("[Scheduler] langgraph 不可用")
            return None

        builder = StateGraph(dict)
        builder.add_node("dispatch", self.node_dispatch)
        builder.add_node("run_task", self.node_run_task)
        builder.add_node("join", self.node_join)
        builder.add_node("decide", self.node_decide)

        builder.set_entry_point("dispatch")
        # dispatch → fan-out Sends 到 run_task，或 fallback Send 到 join
        builder.add_edge("run_task", "join")
        builder.add_edge("join", "decide")
        builder.add_conditional_edges(
            "decide",
            lambda s: "continue" if s.get("continue") else "end",
            {"continue": "dispatch", "end": END},
        )

        self._compiled = builder.compile()
        return self._compiled

    # ===== 运行 =====

    def run(self) -> dict[str, Any]:
        """执行调度图。langgraph 不可用时回退到同步循环。"""
        graph = self.build_graph() if self._compiled is None else self._compiled
        if graph is None:
            return self._run_sync_fallback()

        try:
            final = graph.invoke(
                {"current_round": 0},
                config={"recursion_limit": max(self.max_rounds * 8 + 10, 50)},
            )
            return {
                "status": "completed",
                "termination_reason": final.get("termination_reason") if isinstance(final, dict) else None,
                "task_dag_version": self.task_dag.version,
                "summary": self.task_dag.summary(),
            }
        except Exception as exc:
            logger.error(f"[Scheduler] graph invoke 失败：{exc}")
            return {"status": "failed", "error": str(exc)}

    def _run_sync_fallback(self) -> dict[str, Any]:
        """无 langgraph 时的同步调度循环。"""
        round_n = 0
        termination_reason = None
        while round_n < self.max_rounds:
            round_n += 1
            ready = self.task_dag.ready_tasks()
            if not ready:
                if self.task_dag.all_succeeded():
                    termination_reason = "all_tasks_succeeded"
                    break
                # 没有 ready 但还有非 SUCCEEDED → 死锁，跳出
                non_terminal = [
                    n for n in self.task_dag.nodes.values()
                    if not n.is_terminal()
                ]
                if not non_terminal:
                    # 所有节点都已结束但不是全 SUCCEEDED → 部分失败
                    termination_reason = "partial_failure"
                    break
                logger.warning(
                    f"[Scheduler fallback] round {round_n} 无 ready 但有非终结任务，"
                    f"可能是依赖环或 failed——退出"
                )
                termination_reason = "no_ready_with_pending"
                break

            for task in ready:
                self.task_dag.update_status(task.id, TaskNodeStatus.READY)
                self.task_dag.update_status(task.id, TaskNodeStatus.RUNNING)
                result = self.worker_executor.execute_task(self.task_dag, task.id, {})
                if result.success:
                    self.task_dag.update_status(task.id, TaskNodeStatus.SUCCEEDED)
                    for art in result.artifact_ids:
                        self.task_dag.accept_artifact(task.id, art)
                else:
                    self.task_dag.record_attempt(task.id)
                    self.task_dag.update_status(task.id, TaskNodeStatus.FAILED)

            if self.task_dag.all_succeeded():
                termination_reason = "all_tasks_succeeded"
                break

        return {
            "status": "completed" if termination_reason == "all_tasks_succeeded" else "incomplete",
            "termination_reason": termination_reason or "max_rounds",
            "task_dag_version": self.task_dag.version,
            "rounds": round_n,
            "summary": self.task_dag.summary(),
        }

    # ===== 私有 =====

    def _dag_from_state(self, state: dict[str, Any]) -> TaskGraph:
        # 我们始终使用 self.task_dag（LangGraph StateGraph(dict) 默认 reducer 会替换
        # 整状态，因此我们把 DAG 作为外部可变结构而非塞进 state）。
        return self.task_dag


# ===== Worker Executor 接口 =====


class WorkerExecutor:
    """Worker 执行接口。具体执行实现放到 executor.py。"""

    def execute_task(
        self, dag: TaskGraph, task_id: str, task_input: dict[str, Any]
    ) -> TaskResult:
        raise NotImplementedError


class _InMemoryWorkerExecutor(WorkerExecutor):
    """测试 / 默认 stub：永远成功，无 Artifact。

    完成一个任务 → 标记 SUCCEEDED（Scheduler 已通过 record_attempt 完成）。这里仅
    返回 TaskResult；状态转换由 Scheduler.node_run_task 完成。
    """

    def execute_task(self, dag, task_id, task_input):
        from app.multiagent.task_graph import TaskNodeStatus  # local import for safety
        # auto-succeed
        return TaskResult(
            task_id=task_id,
            success=True,
            artifact_ids=[f"art:{task_id}"],
            attempted=True,
        )


# ===== 简单工厂：用脚本驱动 Worker（用于测试） =====


class ScriptedWorkerExecutor(WorkerExecutor):
    """对每个 (task_id) 返回预设结果的脚本执行器。

    用法：
        ScriptedWorkerExecutor({"A": TaskResult(task_id="A", success=True, ...)})

    失败 task 通过 attempt 后维持 FAILED，触发 Scheduler 下一轮重新调度
    （需要外部把 FAILED 重置回 PENDING 才能再 ready）。
    """

    def __init__(
        self,
        script_success: dict[str, bool],
        artifacts: dict[str, list[str]] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self._ok = script_success
        self._artifacts = artifacts or {}
        self._errors = errors or {}

    def execute_task(self, dag, task_id, task_input):
        ok = self._ok.get(task_id, True)
        return TaskResult(
            task_id=task_id,
            success=bool(ok),
            artifact_ids=self._artifacts.get(task_id, [f"art:{task_id}"]) if ok else [],
            error=self._errors.get(task_id) if not ok else None,
            attempted=True,
        )
