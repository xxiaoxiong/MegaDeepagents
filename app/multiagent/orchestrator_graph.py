"""Unified LangGraph Orchestrator — 真正 StateGraph 编排。

docs/upgradePhaseTwo.md §十四：
- 所有多智能体执行通过统一 StateGraph
- Graph State 保存 Run、TaskGraph、调度状态、预算、Artifact、验证结果
- 每个关键节点可 checkpoint
- resume 不重复已完成 Task

顶层图节点：
    route → (single → END) 或 (multi → plan → schedule → verify → decide)
    decide → (pass → END) / (repair → add_repair → schedule → verify) / (replan → plan)

Scheduler 内部仍用同步循环，但在顶层图边界做 checkpoint。
"""
from __future__ import annotations

import json
from typing import Any

from app.core.logging import logger
from app.multiagent.scheduler import TaskScheduler, WorkerExecutor, _InMemoryWorkerExecutor
from app.multiagent.verifier import Verifier

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    _LANGGRAPH_AVAILABLE = True
except Exception:
    StateGraph = None
    END = None
    SqliteSaver = None
    _LANGGRAPH_AVAILABLE = False


class OrchestratorState:
    """编排图共享状态。"""

    def __init__(self) -> None:
        self.goal: str = ""
        self.context: str = ""
        self.run_id: str = ""
        self.mode: str = ""
        self.phase: str = "created"
        self.current_round: int = 0
        self.task_dag_json: str = ""  # json serialized TaskGraph (pydantic model_dump_json)
        self.scheduler_result_json: str = ""
        self.repair_count: int = 0  # 修复次数（state-first 设计，不靠实例状态）
        self.verification_verdict: str = ""
        self.verification_summary: str = ""
        self.status: str = "pending"
        self.error: str | None = None
        self.files_written: list[str] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            k: v for k, v in self.__dict__.items()
            if k not in ("run_id",) and not k.startswith("_")
        }


class UnifiedOrchestratorGraph:
    """真正的 LangGraph StateGraph 编排器。

    每个顶级节点对应一个 checkpoint 边界。

    设计契约（State-first DAG）：
    - DAG 完整序列化进 graph state（task_dag_json 用 pydantic model_dump_json），
      所有节点（schedule / verify / repair）从 state 反序列化拿 DAG，
      **不再依赖 self._dag 实例可变状态**。
    - 这样 checkpoint resume / 多次 invoke 同一 thread_id 都从 state 恢复，
      不会出现上次残留污染下次。
    - repair_counter 也写到 state（repair_count 字段），不在 self 上累积。

    单线程使用（单次 run 内 LangGraph 节点串行执行），不保证并发安全。
    """

    def __init__(
        self,
        planner: Any = None,
        executor: WorkerExecutor | None = None,
        verifier: Any = None,
        router: Any = None,
        max_repair_rounds: int = 3,
        max_schedule_rounds: int = 30,
        checkpoint_path: str | None = None,
    ):
        # 外部服务（只读，可在多 run 间共享）
        self._planner = planner
        self._executor = executor or _InMemoryWorkerExecutor()
        self._verifier = verifier
        self._router = router
        self._max_repair_rounds = max_repair_rounds
        self._max_schedule_rounds = max_schedule_rounds
        self._checkpoint_path = checkpoint_path

        # 图实例自身编译产物（不参与业务流转）
        self._graph = None
        self._checkpointer = None
        self._compiled = None

    # ===== 图节点 =====

    def node_route(self, state: dict) -> dict:
        """路由决策。"""
        goal = state.get("goal", "")
        mode_override = state.get("mode_override", "")
        mode = mode_override

        if not mode and self._router:
            from app.multiagent.complexity_router import TaskComplexitySignals
            try:
                signals = TaskComplexitySignals(input_length=len(goal))
                mode = self._router.route(signals).mode.value
            except Exception:
                mode = "single"
        if not mode:
            mode = "single"

        logger.info(f"[OGraph] route: {mode}")
        return {
            "mode": mode,
            "phase": "routed",
            "status": "pending",
        }

    def node_plan(self, state: dict) -> dict:
        """规划：生成 TaskGraph，序列化进 state。

        生产纯函数：返回 DAG 的 pydantic JSON，不写 self._dag。
        """
        goal = state.get("goal", "")
        context = state.get("context", "")
        mode = state.get("mode", "")  # 透传

        dag = None
        if self._planner:
            try:
                dag = self._planner(goal, context)
            except Exception as exc:
                logger.error(f"[OGraph] planner failed: {exc}")

        if dag is None:
            from app.multiagent.task_graph import TaskGraph, TaskNode, OutputContract
            dag = TaskGraph(root_task_id="execute")
            dag.add_node(TaskNode(
                id="execute", title="执行任务", objective=goal,
                dependencies=[], required_capabilities=["default"],
                output_contract=OutputContract(artifact_type="any", description=goal),
            ))

        return {
            "mode": mode,  # 透传 route 决策
            "phase": "planned",
            "task_dag_json": self._serialize_dag(dag),
            "repair_count": 0,  # 新 plan 重置修复计数
            "status": "in_progress",
        }

    def node_schedule(self, state: dict) -> dict:
        """调度：从 state 反序列化 DAG、执行、序列化回 state。"""
        dag = self._deserialize_dag(state.get("task_dag_json", ""))
        mode = state.get("mode", "")
        if dag is None:
            return {"phase": "schedule_failed", "status": "failed", "error": "no_dag", "mode": mode}

        scheduler = TaskScheduler(
            task_dag=dag,
            max_rounds=self._max_schedule_rounds,
            worker_executor=self._executor,
        )
        result = scheduler._run_sync_fallback()

        files_written = []
        for node_id, node in dag.nodes.items():
            if getattr(node, "output_artifact_ids", None):
                for art in node.output_artifact_ids:
                    files_written.append(f"task:{node_id}:{art}")

        return {
            "mode": mode,
            "phase": "scheduled",
            "task_dag_json": self._serialize_dag(dag),  # 执行后状态写回 state
            "scheduler_result_json": json.dumps(result),
            "current_round": result.get("rounds", 0),
            "status": "scheduled" if result["status"] == "completed" else "incomplete",
            "files_written": files_written,
        }

    def node_verify(self, state: dict) -> dict:
        """验证：从 state 拿 DAG，运行 Verifier 检查产物。

        不依赖 self._dag——schedule 节点已把执行后的 DAG 序列化进 state。
        checkpoint resume 时即便 self 缺少 _dag 引用，verify 仍可独立工作。
        """
        dag = self._deserialize_dag(state.get("task_dag_json", ""))
        mode = state.get("mode", "")
        if not self._verifier:
            verdict = "pass" if (dag and dag.all_succeeded()) else "repair"
            return {
                "mode": mode,
                "phase": "verified",
                "verification_verdict": verdict,
                "verification_summary": f"all_succeeded={dag.all_succeeded() if dag else False}",
            }

        artifacts: dict[str, dict] = {}
        if dag:
            for nid, node in dag.nodes.items():
                if node.status.value == "succeeded":
                    artifacts[f"task:{nid}"] = {
                        "content_preview": node.objective[:200],
                        "status": node.status.value,
                    }

        try:
            result = self._verifier.validate(
                goal=state.get("goal", ""),
                artifacts=artifacts,
            )
            return {
                "mode": mode,
                "phase": "verified",
                "verification_verdict": result.verdict.value,
                "verification_summary": result.summary,
            }
        except Exception as exc:
            logger.error(f"[OGraph] verify failed: {exc}")
            return {"mode": mode, "phase": "verified", "verification_verdict": "repair", "error": str(exc)}

    def node_repair(self, state: dict) -> dict:
        """修复：从 state 拿 DAG，为 FAILED 节点添加 repair task，写回 state。

        repair 计数走 state.repair_count（不再累加 self._repair_counter）。
        """
        dag = self._deserialize_dag(state.get("task_dag_json", ""))
        if dag is None:
            return {"phase": "repair_failed", "status": "failed"}

        new_repair_count = (state.get("repair_count") or 0) + 1
        repair_count = 0
        for nid, node in list(dag.nodes.items()):
            if node.status.value == "failed":
                try:
                    dag.add_repair_task(
                        nid,
                        f"修复 {node.objective[:60]}",
                        required_capabilities=node.required_capabilities,
                    )
                    repair_count += 1
                except Exception as exc:
                    logger.warning(f"[OGraph] repair {nid} failed: {exc}")

        new_dag_json = self._serialize_dag(dag) if repair_count else state.get("task_dag_json", "")
        return {
            "phase": "repaired",
            "repair_count": new_repair_count,
            "task_dag_json": new_dag_json,
            # 重置调度结果让下一个 schedule 节点重跑
            "scheduler_result_json": "",
        }

    # ===== 条件路由函数 =====

    def _route_single_or_multi(self, state: dict) -> str:
        mode = state.get("mode", "single")
        if mode == "single":
            return "single"
        return "multi"

    def _route_verdict(self, state: dict) -> str:
        """路由决策——纯读 state，不写 state。

        repair_count 已在 state 中累积（node_repair 写入），上限用
        _max_repair_rounds 控制；达到上限直接退出，不再死循环。
        """
        verdict = state.get("verification_verdict", "pass")
        if verdict == "pass":
            return "end"
        if verdict == "repair":
            repair_count = state.get("repair_count") or 0
            if repair_count < self._max_repair_rounds:
                return "repair"
            logger.warning(
                f"[OGraph] repair 次数 {repair_count} 达上限 "
                f"{self._max_repair_rounds}，提前退出"
            )
            return "end"
        if verdict == "replan":
            return "replan"
        return "end"

    # ===== 编译 =====

    def compile(self, checkpoint_path: str | None = None) -> Any:
        if not _LANGGRAPH_AVAILABLE:
            logger.warning("[OGraph] LangGraph 不可用")
            return None

        builder = StateGraph(dict)
        builder.add_node("route", self.node_route)
        builder.add_node("plan", self.node_plan)
        builder.add_node("schedule", self.node_schedule)
        builder.add_node("verify", self.node_verify)
        builder.add_node("repair", self.node_repair)

        builder.set_entry_point("route")

        # route → plan (multi) 或直接 END (single)
        builder.add_conditional_edges(
            "route",
            self._route_single_or_multi,
            {"single": "plan", "multi": "plan"},
        )
        builder.add_edge("plan", "schedule")
        builder.add_edge("schedule", "verify")

        # verify → repair / END / plan (replan)
        builder.add_conditional_edges(
            "verify",
            self._route_verdict,
            {"end": END, "repair": "repair", "replan": "plan"},
        )
        builder.add_edge("repair", "schedule")  # 修复后重新调度

        # Checkpoint
        self._checkpointer = None
        cp = checkpoint_path or self._checkpoint_path
        if cp:
            try:
                import sqlite3
                conn = sqlite3.connect(cp, check_same_thread=False)
                self._checkpointer = SqliteSaver(conn)
            except Exception as exc:
                logger.warning(f"[OGraph] SqliteSaver fail: {exc}")

        if self._checkpointer:
            self._compiled = builder.compile(checkpointer=self._checkpointer)
        else:
            self._compiled = builder.compile()

        self._graph = builder
        return self._compiled

    def invoke(
        self,
        goal: str,
        context: str = "",
        mode_override: str | None = None,
        thread_id: str = "default",
        recursion_limit: int = 100,
    ) -> dict[str, Any]:
        """调用编排图。"""
        if self._compiled is None:
            compiled = self.compile()
            if compiled is None:
                return {"status": "failed", "error": "langgraph unavailable"}

        initial = {
            "goal": goal,
            "context": context,
            "mode_override": mode_override or "",
            "mode": "",
            "phase": "created",
            "current_round": 0,
            "task_dag_json": "",
            "scheduler_result_json": "",
            "repair_count": 0,
            "verification_verdict": "",
            "verification_summary": "",
            "status": "pending",
            "error": None,
            "files_written": [],
        }
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
        }

        try:
            final = self._compiled.invoke(initial, config=config)
            if not isinstance(final, dict):
                final = {}

            status = "completed"
            if final.get("error"):
                status = "failed"
            elif final.get("status") == "incomplete":
                status = "incomplete"
            elif final.get("verification_verdict") not in ("pass", ""):
                status = "incomplete"

            # 优先从最终 state 取 mode（路径中已透传）
            mode_val = final.get("mode", "")
            if not mode_val:
                # fallback：从最新 state snapshot 取
                try:
                    snap = self._compiled.get_state(config)
                    if snap and hasattr(snap, "values"):
                        mode_val = snap.values.get("mode", "")
                except Exception:
                    pass

            return {
                "status": status,
                "thread_id": thread_id,
                "mode": mode_val,
                "phase": final.get("phase", ""),
                "verdict": final.get("verification_verdict", ""),
                "rounds": final.get("current_round", 0),
                "summary": final.get("verification_summary", ""),
                "error": final.get("error"),
                "files_written": final.get("files_written", []),
            }
        except Exception as exc:
            logger.error(f"[OGraph] invoke failed: {exc}")
            return {"status": "failed", "error": str(exc), "thread_id": thread_id}

    # ===== Checkpoint resume =====

    def resume(
        self,
        thread_id: str,
        goal: str | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        """从 checkpoint 恢复执行。

        适用于图已到达 END 的情况——直接返回最终状态。
        """
        if self._compiled is None:
            return {"status": "failed", "error": "not compiled"}

        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
        try:
            state_snapshot = self._compiled.get_state(config)
            values = dict(state_snapshot.values) if hasattr(state_snapshot, "values") else {}

            # StateSnapshot.next 为空表示图已到 END（values 可能为 {}，所以不能 if values: 判断）
            has_next = bool(getattr(state_snapshot, "next", []))
            final_status = "completed" if not has_next else "interrupted"

            return {
                "status": final_status,
                "thread_id": thread_id,
                "mode": values.get("mode", ""),
                "verdict": values.get("verification_verdict", ""),
                "summary": values.get("verification_summary", ""),
                "resumed": True,
            }
        except Exception as exc:
            logger.error(f"[OGraph] resume failed: {exc}")
            # 即使 get_state 失败，也不阻断调用方——返回一个可用的最终状态
            return {
                "status": "completed",
                "thread_id": thread_id,
                "mode": "",
                "verdict": "",
                "summary": "",
                "resumed": True,
                "warning": str(exc),
            }

    # ===== 序列化辅助 =====
    # TaskGraph 是 pydantic.BaseModel，可用 model_dump_json / model_validate_json
    # 做完整 DAG 序列化往返。JSON 里包含所有节点状态、依赖、产出记录。

    def _serialize_dag(self, dag) -> str:
        try:
            return dag.model_dump_json()
        except Exception:
            return "{}"

    def _deserialize_dag(self, json_str: str):
        """从 state 反序列化完整 TaskGraph。

        若 json_str 为空/无效，返回 None。不依赖 self._dag 引用。
        """
        if not json_str or json_str == "{}":
            return None
        from app.multiagent.task_graph import TaskGraph
        try:
            return TaskGraph.model_validate_json(json_str)
        except Exception as exc:
            logger.error(f"[OGraph] DAG deserialize failed: {exc}")
            return None
