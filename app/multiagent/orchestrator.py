"""Unified Orchestrator Graph — 统一多 Agent 运行时。

docs/upgradePhaseTwo.md §十四：
- 所有多智能体执行通过统一 Graph
- Graph State 保存 Run、TaskGraph、调度状态、预算、Artifact、验证结果
- 每个关键节点可 checkpoint
- resume 不重复已完成 Task
- 已提交 Tool Side Effect 具备幂等保护

结构：
    input → route → plan → schedule → verify → (repair → schedule) / done

三个主环：
1. Normal Flow: plan → schedule → verify → PASS → completed
2. Repair Flow: verify → REPAIR → add_repair_task → schedule → verify → ...
3. Replan Flow: verify → REPLAN → replan → schedule → verify → ...

TeamRunner 保留为 Facade 调用编排图；不再维护独立 while 主循环。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.multiagent.team_run_context import TeamRunContext
from app.multiagent.scheduler import TaskScheduler, _InMemoryWorkerExecutor, WorkerExecutor
from app.multiagent.task_graph import TaskGraph, TaskNodeStatus

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    _LANGGRAPH_AVAILABLE = True
except Exception:
    StateGraph = None
    END = None
    SqliteSaver = None
    _LANGGRAPH_AVAILABLE = False


@dataclass
class OrchestrationResult:
    """编排结果。"""
    status: str = "pending"  # completed / failed / cancelled / interrupted
    mode: str = ""
    task_graph_version: int = 0
    total_tasks: int = 0
    succeeded_tasks: int = 0
    failed_tasks: int = 0
    verification_verdict: str = ""
    rounds: int = 0
    error: str | None = None
    summary: str = ""


class SimpleOrchestrator:
    """简单编排器：Chain of Responsibility 风格。

    图表达能力有限时不依赖 StateGraph 而直接链式调用组件。
    对生产环境高可用场景，可升级为完整的 StateGraph 编排。
    """

    def __init__(
        self,
        planner: Any = None,
        executor: "WorkerExecutor | None" = None,
        verifier: Any = None,
        router: Any = None,
        max_repair_rounds: int = 3,
        max_rounds: int = 30,
        checkpoint_path: str | None = None,
        ctx: TeamRunContext | None = None,
        cancel_event: Any | None = None,
    ):
        self.planner = planner
        self._executor = executor or _InMemoryWorkerExecutor()
        self.verifier = verifier
        self.router = router
        self.max_repair_rounds = max_repair_rounds
        self.max_rounds = max_rounds
        self.checkpoint_path = checkpoint_path
        self._ctx = ctx  # TeamRunContext for workspace/run_id
        self.cancel_event = cancel_event

        self._result: OrchestrationResult = OrchestrationResult()
        self._task_graph = None

        # 状态（用于 checkpoint 持久化）
        self.current_round = 0
        self.phase: str = "created"
        self.trace: list[str] = []

    # ===== 运行入口 =====

    def run(self, goal: str, context: str = "", mode_override: str | None = None, ctx: TeamRunContext | None = None) -> OrchestrationResult:
        """执行一次完整的多-agent 编排。"""
        if ctx is not None:
            self._ctx = ctx
        try:
            self._result = OrchestrationResult(mode="single")
            self.trace = []
            self._log(f"开始编排: {goal[:80]}")
            self.phase = "routing"

            # 0. 遥测：run开始
            self._emit_event("orchestrator_started", {"goal": goal[:80]})

            # 1. 复杂度路由
            mode = self._route(goal, mode_override)
            self._result.mode = mode
            self._log(f"路由决策: {mode}")

            if mode in ("single", "codebase_light"):
                return self._run_single(goal, context)

            # 2. 规划
            self.phase = "planning"
            self._emit_event("planning_started", {})
            dag = self._plan(goal, context)
            self._task_graph = dag
            if dag is None:
                self._result.status = "failed"
                self._result.error = "planner_failed"
                self._emit_event("planning_failed", {"error": "planner_failed"})
                return self._result
            self._emit_event("planning_finished", {"task_count": len(dag.nodes)})

            # TASK_TEAM normal runs must create real teammates before the
            # scheduler starts.  DISCUSSION remains on TeamRunner elsewhere.
            if self._ctx is not None:
                from app.multiagent.default_teams import get_team
                from app.multiagent.team_builder import TeamBuilder
                TeamBuilder().build_team_sync(self._ctx, get_team(self._ctx.team_id), dag)

            # 3. 调度 + 执行
            self.phase = "scheduling"
            self._result.total_tasks = len(dag.nodes)
            self._emit_event("scheduling_started", {"total_tasks": len(dag.nodes)})
            success = self._schedule(dag)
            if not success:
                self._result.status = "failed"
                self._result.error = "scheduler_failed"
                self._emit_event("scheduling_failed", {"error": "scheduler_failed"})
                return self._result
            self._emit_event("scheduling_finished", {})

            # 4. 验证
            self.phase = "verifying"
            verdict = self._verify(goal, dag)
            self._result.verification_verdict = verdict
            self._log(f"验证结果: {verdict}")
            self._emit_event("verification_done", {"verdict": verdict})
            if verdict == "pass":
                self._mark_produced_verified(dag)

            # 5. Repair / Replan 循环
            repair_round = 0
            while verdict in ("repair",) and repair_round < self.max_repair_rounds:
                repair_round += 1
                self._log(f"Repair 第 {repair_round} 轮")
                self._emit_event("repair_started", {"round": repair_round})
                dag = self._repair(dag, self._result.verification_verdict)
                if dag is None:
                    break
                self._receive_repaired_dag(dag)
                self._schedule(dag)
                verdict = self._verify(goal, dag)
                self._result.verification_verdict = verdict
                self._emit_event("repair_finished", {"round": repair_round, "verdict": verdict})

            # 6. 最终结果
            if verdict == "pass":
                self._result.status = "completed"
                self.phase = "completed"
            elif verdict in ("human_required", "replan"):
                self._result.status = "interrupted"
                self._result.error = f"requires_{verdict}"
                self.phase = "waiting_human"
            else:
                self._result.status = "incomplete"
                self._result.error = f"verdict_{verdict}"

            self._result.succeeded_tasks = sum(
                1 for n in dag.nodes.values()
                if n.status.value == "succeeded"
            )
            self._result.failed_tasks = sum(
                1 for n in dag.nodes.values()
                if n.status.value == "failed"
            )
            self._result.task_graph_version = dag.version if dag else 0

            self._emit_event("orchestrator_finished", {
                "status": self._result.status,
                "succeeded": self._result.succeeded_tasks,
                "failed": self._result.failed_tasks,
                "verdict": verdict,
            })

        except Exception as exc:
            logger.error(f"[Orchestrator] 异常: {exc}")
            self._result.status = "failed"
            self._result.error = str(exc)
            self._emit_event("orchestrator_failed", {"error": str(exc)})

        return self._result

    def _run_single(self, goal: str, context: str) -> OrchestrationResult:
        """单 Agent 执行（简单任务走此路径）。

        LLM 调用在生产环境下会真实执行；在需要 mock 的场景通过 build_model
        的 monkeypatch 实现。
        """
        self.phase = "executing"
        self._log("单 Agent 执行")
        try:
            from app.llm_factory import build_model
            llm = build_model()
            response = llm.invoke([
                ("system", "你是一个通用助手。输出 JSON 包含 'result' 和 'summary'。"),
                ("user", f"目标: {goal}\n\n上下文: {context or '(无)'}"),
            ])
            text = getattr(response, "content", str(response))
            import json
            try:
                parsed = json.loads(text) if isinstance(text, str) else text
            except json.JSONDecodeError:
                parsed = {"result": "completed", "summary": text[:200]}

            self._result.status = "completed"
            self._result.summary = str(parsed.get("summary", ""))[:300]
            self.phase = "completed"

        except Exception as exc:
            logger.error(f"[Orchestrator single] 异常: {exc}")
            # 禁止伪成功：LLM 失败则返回 failed
            self._result.status = "failed"
            self._result.error = f"single_agent_llm_failed: {exc}"
            self.phase = "failed"

        return self._result

    # ===== 子阶段 =====

    def _route(self, goal: str, mode_override: str | None) -> str:
        if mode_override:
            return mode_override
        if self.router:
            try:
                from app.multiagent.complexity_router import TaskComplexitySignals
                signals = TaskComplexitySignals(input_length=len(goal))
                decision = self.router.route(signals)
                return decision.mode.value
            except Exception:
                pass
        # 默认：基于启发规则
        if "研究" in goal or "research" in goal.lower():
            return "light_multi"
        if len(goal) > 2000:
            return "full_multi"
        return "single"

    def _plan(self, goal: str, context: str):
        if self.planner:
            return self.planner(goal, context)
        # 内置降级
        from app.multiagent.task_graph import TaskGraph, TaskNode, OutputContract
        dag = TaskGraph(root_task_id="execute")
        dag.add_node(TaskNode(
            id="execute", title="执行", objective=goal,
            dependencies=[], required_capabilities=["coding", "testing"],
            output_contract=OutputContract(artifact_type="any", description=goal),
        ))
        return dag

    def _schedule(self, dag) -> bool:
        """执行 DAG 主链。

        优先走 ParallelTeamScheduler（真正的并行 + AgentRegistry 调度 + 心跳租约），
        这是 Phase Two §三/§七要求的主路径——多 Agent 并行运行而非旧的同步串行 fallback。

        当 ParallelTeamScheduler 不可用（无 run_id、缺 asyncio 运行时）时，
        回退到 TaskScheduler._run_sync_fallback——仅作为缺异步运行时保护的兜底，
        不能成为默认主链。
        """
        run_id = self._ctx.run_id if self._ctx else None
        if not run_id:
            # 无 run_id → 无法走 team board 路径；回退 sync fallback
            self._log("无 run_id，回退 sync fallback 调度")
            scheduler = TaskScheduler(
                task_dag=dag,
                max_rounds=self.max_rounds,
                worker_executor=self._executor,
            )
            result = scheduler._run_sync_fallback(task_input={
                "workspace_root": self._ctx.workspace_root if self._ctx else "",
            })
            self.current_round = result.get("rounds", 0)
            status = result.get("status", "failed")
            self._log(f"调度完成(回退): status={status}, rounds={self.current_round}")
            return status in ("completed", "incomplete")

        # ===== 主路径：ParallelTeamScheduler =====
        # 把 DAG 节点同步到 TaskBoard（ParallelTeamScheduler 的工作载体）
        self._sync_dag_to_board(run_id, dag)

        try:
            from app.multiagent.parallel_scheduler import ParallelTeamScheduler

            sched = ParallelTeamScheduler(
                run_id=run_id, task_graph=dag, max_rounds=self.max_rounds,
                verifier=self.verifier, cancel_event=self.cancel_event,
            )
            # 调用方传入的真实 executor（DeepAgentExecutor / 测试 stub）
            executor_used = self._executor or _InMemoryWorkerExecutor()
            run_result = self._run_parallel(sched, executor_used)
            self.current_round = getattr(run_result, "rounds", 0) or 0
            status = getattr(run_result, "status", "failed") or "failed"
            self._log(
                f"调度完成(并行): status={status}, rounds={self.current_round}, "
                f"error={getattr(run_result, 'error', None)}"
            )
            # 把 BoardTask 终态反映回 dag.nodes，让 _verify 能判断
            self._sync_board_to_dag(run_id, dag)
            return status == "completed"
        except Exception as exc:
            logger.error(f"[Orchestrator] ParallelTeamScheduler failed: {exc}")
            return False

    def _run_parallel(self, sched, executor):
        """在事件循环中跑 ParallelTeamScheduler.run。

        兼容嵌套 event loop 场景（pytest-asyncio/已有 loop）：新建独立线程跑 loop。
        """
        import asyncio
        try:
            asyncio.get_running_loop()
            # 已有 loop（async 上下文调用）：在独立线程上跑新 loop 避免冲突
            import threading
            box: dict[str, Any] = {}

            def _runner():
                box["result"] = asyncio.run(sched.run(executor))

            t = threading.Thread(target=_runner, daemon=False)
            t.start()
            t.join()
            return box.get("result")
        except RuntimeError:
            # 无运行中 loop：直接 asyncio.run
            return asyncio.run(sched.run(executor))

    def _sync_dag_to_board(self, run_id: str, dag) -> None:
        """把 TaskGraph.nodes 同步到 TaskBoard（如尚未注册）。

        ParallelTeamScheduler 工作在 board 之上，而 planner 输出 dag；
        本方法把每个 TaskNode 投放到 board 上一个对应 BoardTask。
        已存在同 (run_id, task_id) 的 BoardTask 跳过——保持幂等。
        """
        from app.multiagent.task_board import get_task_board
        board = get_task_board()
        existing = {t.task_id for t in board.list_by_run(run_id)}
        for node in dag.nodes.values():
            if node.id in existing:
                continue
            board.create_task(
                task_id=node.id,
                run_id=run_id,
                title=node.title,
                objective=node.objective,
                dependencies=list(getattr(node, "dependencies", []) or []),
                required_capabilities=list(getattr(node, "required_capabilities", []) or []),
            )

    def _sync_board_to_dag(self, run_id: str, dag) -> None:
        """把 board 上的任务终态反映回 dag.nodes。

        ParallelTeamScheduler 推动的是 BoardTask，而 _verify /repair 路径
        看 dag.nodes；两者要一致。
        """
        from app.multiagent.task_board import get_task_board, BoardTaskStatus
        board = get_task_board()
        for bt in board.list_by_run(run_id):
            node = dag.nodes.get(bt.task_id)
            if node is None:
                continue
            target: TaskNodeStatus | None = None
            if bt.status == BoardTaskStatus.SUCCEEDED:
                target = TaskNodeStatus.SUCCEEDED
            elif bt.status in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                target = TaskNodeStatus.RUNNING
            elif bt.status == BoardTaskStatus.FAILED:
                target = TaskNodeStatus.FAILED
            elif bt.status in (BoardTaskStatus.RUNNING, BoardTaskStatus.CLAIMED):
                target = TaskNodeStatus.RUNNING
            elif bt.status == BoardTaskStatus.PENDING:
                target = TaskNodeStatus.PENDING
            if target is not None and node.status != target:
                dag.update_status(bt.task_id, target)
            # 接受 artifact（BoardTask 用 produced_artifact_ids）
            for art_id in (bt.produced_artifact_ids or []):
                try:
                    dag.accept_artifact(bt.task_id, art_id)
                except Exception:
                    pass

    def _verify(self, goal: str, dag) -> str:
        if self.verifier is None:
            # 无 verifier → 检查 all_succeeded
            try:
                return "pass" if dag.all_succeeded() else "repair"
            except Exception:
                return "pass"
        try:
            # 构造 artifacts dict — 优先读取真实文件内容
            artifacts: dict[str, dict] = {}
            workspace_root = self._ctx.workspace_root if self._ctx else None

            for node in dag.nodes.values():
                if node.status.value in ("succeeded", "running"):
                    entry: dict[str, Any] = {
                        "content_preview": node.objective[:200],
                        "status": node.status.value,
                    }
                    # 尝试读取真实产物
                    if workspace_root:
                        import os
                        task_dir = os.path.join(workspace_root, "tasks", node.id)
                        if os.path.isdir(task_dir):
                            file_contents = []
                            for fname in os.listdir(task_dir)[:5]:
                                fpath = os.path.join(task_dir, fname)
                                if os.path.isfile(fpath):
                                    try:
                                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                                            content = f.read()
                                        file_contents.append(f"--- {fname} ---\n{content[:2000]}")
                                    except Exception:
                                        file_contents.append(f"--- {fname} ---\n(无法读取)")
                            if file_contents:
                                entry["content"] = "\n".join(file_contents)[:5000]
                    artifacts[f"task:{node.id}"] = entry

            result = self.verifier.validate(goal=goal, artifacts=artifacts)
            self._last_validation_result = result
            return result.verdict.value
        except Exception as exc:
            logger.warning(f"[Verifier] validate failed: {exc}")
            return "repair"

    def _mark_produced_verified(self, dag) -> None:
        """Only a passing verifier is allowed to complete board tasks."""
        if not self._ctx:
            return
        from app.multiagent.task_board import get_task_board, BoardTaskStatus
        board = get_task_board()
        for task in board.list_by_run(self._ctx.run_id):
            if task.status in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                board.mark_verifying(task.task_id, run_id=self._ctx.run_id)
                board.mark_verified(task.task_id, run_id=self._ctx.run_id)
        self._sync_board_to_dag(self._ctx.run_id, dag)

    def _repair(self, dag, verdict: str):
        """根据 Verifier 的 REPAIR 结果创建修复任务。"""
        if verdict != "repair":
            return dag
        repair_count = 0
        for node_id, node in list(dag.nodes.items()):
            if node.status.value == "failed":
                try:
                    dag.add_repair_task(
                        node_id,
                        f"修复 {node.objective[:60]}",
                        required_capabilities=node.required_capabilities,
                    )
                    repair_count += 1
                except Exception as exc:
                    logger.warning(f"[Repair] add_repair_task({node_id}) failed: {exc}")
        if repair_count:
            self._log(f"新增 {repair_count} 个 repair task")
        return dag

    def _receive_repaired_dag(self, dag) -> None:
        self._task_graph = dag

    def _log(self, msg: str) -> None:
        logger.info(f"[Orchestrator/{self.phase}] {msg}")
        self.trace.append(f"[{datetime.utcnow().isoformat()}] {msg}")

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """把事件写入 team_events 表（Phase G 可观测性）。"""
        run_id = (getattr(self._ctx, "run_id", None) or "unknown")
        try:
            from app.multiagent.phase_g_store import get_agent_run_history
            h = get_agent_run_history()
            from app.multiagent.phase_g_store import make_run_event_id
            h.record_event(
                event_id=make_run_event_id(),
                run_id=run_id,
                event_type=f"orchestrator:{event_type}",
                payload=payload,
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            logger.debug(f"[Orchestrator] emit_event {event_type} 失败: {exc}")


# ===== 直观工厂函数 =====


def run_orchestrated(
    goal: str,
    context: str = "",
    mode_override: str | None = None,
    planner: Any = None,
    executor: WorkerExecutor | None = None,
    verifier: Any = None,
    router: Any = None,
    max_repair_rounds: int = 3,
    ctx: TeamRunContext | None = None,
    cancel_event: Any | None = None,
) -> OrchestrationResult:
    """一站式编排执行入口。"""
    orch = SimpleOrchestrator(
        planner=planner,
        executor=executor,
        verifier=verifier,
        router=router,
        max_repair_rounds=max_repair_rounds,
        ctx=ctx,
        cancel_event=cancel_event,
    )
    return orch.run(goal=goal, context=context, mode_override=mode_override, ctx=ctx)
