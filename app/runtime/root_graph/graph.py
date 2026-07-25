"""The production LangGraph root graph for both single and team runs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.core.config import settings
from app.core.logging import logger
from app.domain.runs.models import RunMode, SupervisorDecision
from app.infrastructure.database.connection import open_connection
from app.infrastructure.database.run_store import get_agent_run_history, make_run_event_id
from app.multiagent.task_graph import OutputContract, TaskGraph, TaskNode, TaskNodeStatus
from app.multiagent.team_run_context import TeamRunContext
from app.runtime.root_graph.state import AgentRunState
from app.runtime.supervisor.agent import SupervisorAgent


@dataclass
class OrchestrationResult:
    status: str = "pending"
    mode: str = ""
    task_graph_version: int = 0
    total_tasks: int = 0
    succeeded_tasks: int = 0
    failed_tasks: int = 0
    verification_verdict: str = ""
    rounds: int = 0
    error: str | None = None
    summary: str = ""


class GovernedRunGraph:
    """LangGraph is the authority for routing, repair, HITL, and finalization."""

    def __init__(
        self,
        *,
        ctx: TeamRunContext,
        planner: Any,
        executor: Any,
        verifier: Any,
        supervisor: SupervisorAgent | None = None,
        cancel_event: Any | None = None,
        max_rounds: int = 30,
        max_repair_rounds: int | None = None,
        task_graph: TaskGraph | None = None,
    ) -> None:
        self.ctx = ctx
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.supervisor = supervisor or SupervisorAgent()
        self.cancel_event = cancel_event
        self.max_rounds = max_rounds
        self.max_repair_rounds = max_repair_rounds or settings.max_repair_rounds
        self.resume_graph = task_graph
        self._checkpoint_connection = open_connection()
        self._compiled = self._compile()

    def _compile(self):
        builder = StateGraph(AgentRunState)
        builder.add_node("intake", self._intake)
        builder.add_node("complexity_router", self._complexity_router)
        builder.add_node("single_plan", self._single_plan)
        builder.add_node("team_supervisor", self._team_supervisor)
        builder.add_node("build_team", self._build_team)
        builder.add_node("dispatch", self._dispatch)
        builder.add_node("collect", self._collect)
        builder.add_node("verify", self._verify)
        builder.add_node("repair", self._repair)
        builder.add_node("replan", self._replan)
        builder.add_node("human_interrupt", self._human_interrupt)
        builder.add_node("finalize", self._finalize)
        builder.add_node("fail", self._fail)

        builder.set_entry_point("intake")
        builder.add_edge("intake", "complexity_router")
        builder.add_conditional_edges(
            "complexity_router",
            lambda state: state.get("mode", "team"),
            {"single": "single_plan", "team": "team_supervisor"},
        )
        builder.add_edge("single_plan", "build_team")
        builder.add_edge("team_supervisor", "build_team")
        builder.add_conditional_edges(
            "build_team",
            lambda state: "fail" if state.get("error") else "dispatch",
            {"dispatch": "dispatch", "fail": "fail"},
        )
        builder.add_conditional_edges(
            "dispatch",
            self._route_after_dispatch,
            {
                "collect": "collect",
                "repair": "repair",
                "replan": "replan",
                "human": "human_interrupt",
                "fail": "fail",
            },
        )
        builder.add_edge("collect", "verify")
        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {
                "finalize": "finalize",
                "repair": "repair",
                "replan": "replan",
                "human": "human_interrupt",
                "fail": "fail",
            },
        )
        builder.add_edge("repair", "build_team")
        builder.add_edge("replan", "team_supervisor")
        builder.add_conditional_edges(
            "human_interrupt",
            lambda state: "fail" if state.get("error") else "dispatch",
            {"dispatch": "dispatch", "fail": "fail"},
        )
        builder.add_edge("finalize", END)
        builder.add_edge("fail", END)
        return builder.compile(checkpointer=SqliteSaver(self._checkpoint_connection))

    def invoke(self, *, goal: str, requested_mode: str, resume: bool = False) -> OrchestrationResult:
        config = {
            "configurable": {
                "thread_id": self.ctx.run_id,
                "checkpoint_ns": self.ctx.checkpoint_namespace,
            },
            "recursion_limit": 100,
        }
        try:
            if resume:
                final = self._compiled.invoke(None, config=config)
            else:
                initial: AgentRunState = {
                "run_id": self.ctx.run_id,
                "goal": goal,
                "requested_mode": requested_mode,
                "mode": "",
                "phase": "created",
                "task_graph_version": 0,
                "task_graph_json": (
                    self.resume_graph.model_dump_json() if self.resume_graph else ""
                ),
                "active_task_ids": [],
                "completed_task_ids": [],
                "blocked_task_ids": [],
                "pending_permission_ids": [],
                "pending_plan_ids": [],
                "verification_summary": {},
                "supervisor_decision": None,
                "dispatch_status": "",
                "dispatch_rounds": 0,
                "repair_round": 0,
                "final_output": None,
                "error": None,
                "status": "created",
            }
                final = self._compiled.invoke(initial, config=config)
            if isinstance(final, dict) and final.get("__interrupt__"):
                final = {
                    **final,
                    "status": "waiting_human",
                    "phase": "human_interrupt",
                    "error": final.get("error") or "human input required",
                }
            return self._to_result(final)
        finally:
            self._checkpoint_connection.close()

    def _intake(self, state: AgentRunState) -> dict[str, Any]:
        goal = (state.get("goal") or "").strip()
        if not goal:
            return {"phase": "intake", "status": "failed", "error": "goal is required"}
        self._event("intake_completed", {"requested_mode": state.get("requested_mode")})
        return {"phase": "intake", "status": "running", "goal": goal}

    def _complexity_router(self, state: AgentRunState) -> dict[str, Any]:
        if state.get("error"):
            return {"mode": "team"}
        try:
            requested = RunMode(state.get("requested_mode") or RunMode.AUTO.value)
        except ValueError:
            requested = RunMode.AUTO
        decision = self.supervisor.decide(state["goal"], requested)
        self._event("supervisor_decision", decision.model_dump(mode="json"))
        return {
            "phase": "routed",
            "mode": decision.selected_mode,
            "supervisor_decision": decision.model_dump(mode="json"),
        }

    def _single_plan(self, state: AgentRunState) -> dict[str, Any]:
        decision = SupervisorDecision.model_validate(state["supervisor_decision"])
        capabilities = decision.required_capabilities or ["summarization"]
        graph = TaskGraph(root_task_id="execute")
        graph.add_node(TaskNode(
            id="execute",
            title="Execute goal",
            objective=state["goal"],
            required_capabilities=capabilities,
            output_contract=OutputContract(
                artifact_type="any",
                description=state["goal"],
                acceptance_criteria=[],
            ),
        ))
        self._persist_graph(graph)
        return {
            "phase": "planned",
            "task_graph_json": graph.model_dump_json(),
            "task_graph_version": graph.version,
        }

    def _team_supervisor(self, state: AgentRunState) -> dict[str, Any]:
        if state.get("task_graph_json") and self.resume_graph is not None:
            graph = self._load_graph(state)
        else:
            try:
                graph = self.planner(state["goal"], "")
            except Exception as exc:
                return {
                    "phase": "planning",
                    "status": "failed",
                    "error": f"planner_failed: {exc}",
                }
        self._persist_graph(graph)
        self._event("planning_completed", {"task_count": len(graph.nodes)})
        return {
            "phase": "planned",
            "task_graph_json": graph.model_dump_json(),
            "task_graph_version": graph.version,
            "error": None,
        }

    def _build_team(self, state: AgentRunState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        try:
            graph = self._load_graph(state)
            from app.multiagent.default_teams import get_team
            from app.multiagent.team_builder import TeamBuilder

            agents = TeamBuilder().build_team_sync(
                self.ctx, get_team(self.ctx.team_id), graph
            )
            return {
                "phase": "team_ready",
                "active_task_ids": [node.id for node in graph.ready_tasks()],
                "status": "running",
                "error": None,
                "verification_summary": {
                    **state.get("verification_summary", {}),
                    "agent_count": len(agents),
                },
            }
        except Exception as exc:
            return {"phase": "team_build_failed", "status": "failed", "error": str(exc)}

    def _dispatch(self, state: AgentRunState) -> dict[str, Any]:
        graph = self._load_graph(state)
        try:
            from app.multiagent.parallel_scheduler import ParallelTeamScheduler
            from app.multiagent.transactional_task_service import TransactionalTaskService

            TransactionalTaskService().register_initial_graph(self.ctx.run_id, graph)
            scheduler = ParallelTeamScheduler(
                run_id=self.ctx.run_id,
                task_graph=graph,
                max_rounds=self.max_rounds,
                max_concurrency=settings.max_concurrency,
                verifier=self.verifier,
                cancel_event=self.cancel_event,
                **self._workspace_components(),
            )
            result = asyncio.run(scheduler.run(self.executor))
            self._sync_board_to_graph(graph)
            self._persist_graph(graph)
            self._event("dispatch_completed", result.to_dict())
            return {
                "phase": "dispatched",
                "dispatch_status": result.status,
                "dispatch_rounds": result.rounds,
                "task_graph_json": graph.model_dump_json(),
                "task_graph_version": graph.version,
                "error": result.error,
            }
        except Exception as exc:
            logger.exception("[RootGraph] dispatch failed run=%s", self.ctx.run_id)
            return {
                "phase": "dispatch_failed",
                "dispatch_status": "failed",
                "status": "failed",
                "error": str(exc),
            }

    def _collect(self, state: AgentRunState) -> dict[str, Any]:
        graph = self._load_graph(state)
        from app.multiagent.task_board import BoardTaskStatus, get_task_board

        tasks = get_task_board().list_by_run(self.ctx.run_id)
        return {
            "phase": "collected",
            "active_task_ids": [
                task.task_id for task in tasks
                if task.status in (BoardTaskStatus.CLAIMED, BoardTaskStatus.RUNNING)
            ],
            "completed_task_ids": [
                task.task_id for task in tasks if task.status == BoardTaskStatus.SUCCEEDED
            ],
            "blocked_task_ids": [
                task.task_id for task in tasks
                if task.status in (
                    BoardTaskStatus.BLOCKED,
                    BoardTaskStatus.REPAIR_REQUIRED,
                    BoardTaskStatus.REPLAN_REQUIRED,
                )
            ],
            "task_graph_json": graph.model_dump_json(),
        }

    def _verify(self, state: AgentRunState) -> dict[str, Any]:
        graph = self._load_graph(state)
        artifacts = self._verification_artifacts(graph)
        try:
            result = self.verifier.validate(goal=state["goal"], artifacts=artifacts)
            summary = {
                "verdict": result.verdict.value,
                "summary": result.summary,
                "scores": result.scores,
                "failed_criteria": [
                    {
                        "criterion": item.criterion,
                        "detail": item.detail,
                        "severity": item.severity,
                    }
                    for item in result.failed_criteria
                ],
                "proposed_tasks": [
                    {
                        "title": item.title,
                        "objective": item.objective,
                        "required_capabilities": list(item.required_capabilities),
                        "dependencies": list(item.dependencies),
                        "priority": item.priority,
                    }
                    for item in result.proposed_tasks
                ],
            }
            self._event("verification_completed", summary)
            return {
                "phase": "verified",
                "verification_summary": summary,
                "error": None,
            }
        except Exception as exc:
            return {
                "phase": "verification_failed",
                "verification_summary": {
                    "verdict": "repair",
                    "summary": str(exc),
                },
                "error": str(exc),
            }

    def _repair(self, state: AgentRunState) -> dict[str, Any]:
        graph = self._load_graph(state)
        from app.multiagent.task_board import BoardTaskStatus, get_task_board
        from app.multiagent.transactional_task_service import TransactionalTaskService

        created: list[str] = []
        board = get_task_board()
        candidates = [
            task for task in board.list_by_run(self.ctx.run_id)
            if task.status == BoardTaskStatus.REPAIR_REQUIRED
            and not task.metadata.get("superseded_by_repair")
        ]
        if not candidates:
            # Whole-run verification happens after every worker task passed.
            # Repair the successful leaf outputs that contributed to the final
            # result, not every task in the run.
            depended_on = {
                dependency
                for node in graph.nodes.values()
                for dependency in node.dependencies
            }
            candidates = [
                task for task in board.list_by_run(self.ctx.run_id)
                if task.status == BoardTaskStatus.SUCCEEDED
                and task.task_id not in depended_on
                and not task.metadata.get("superseded_by_repair")
            ]
        for task in candidates:
            before = set(graph.nodes)
            mutation = TransactionalTaskService().create_repair(
                self.ctx.run_id,
                task.task_id,
                objective=f"Repair {task.objective}",
                required_capabilities=list(task.required_capabilities),
                source_artifact_ids=list(task.produced_artifact_ids),
                verification_feedback=state.get("verification_summary", {}),
            )
            graph = mutation.graph
            created.extend(sorted(set(graph.nodes) - before))
        if not created:
            return {"status": "failed", "error": "repair_requested_without_repairable_tasks"}
        self._persist_graph(graph)
        return {
            "phase": "repair_planned",
            "repair_round": int(state.get("repair_round", 0)) + 1,
            "task_graph_json": graph.model_dump_json(),
            "task_graph_version": graph.version,
            "error": None,
        }

    def _replan(self, state: AgentRunState) -> dict[str, Any]:
        return {
            "phase": "replanning",
            "task_graph_json": "",
            "repair_round": int(state.get("repair_round", 0)) + 1,
            "error": None,
        }

    def _human_interrupt(self, state: AgentRunState) -> dict[str, Any]:
        response = interrupt({
            "run_id": self.ctx.run_id,
            "reason": state.get("error") or "human approval required",
            "pending_permission_ids": state.get("pending_permission_ids", []),
            "pending_plan_ids": state.get("pending_plan_ids", []),
        })
        if isinstance(response, dict) and response.get("decision") in {"deny", "cancel"}:
            return {
                "phase": "human_denied",
                "status": "failed",
                "error": response.get("feedback") or "human denied continuation",
            }
        return {
            "phase": "resumed",
            "status": "running",
            "dispatch_status": "",
            "error": None,
        }

    def _finalize(self, state: AgentRunState) -> dict[str, Any]:
        graph = self._load_graph(state)
        from app.multiagent.task_board import BoardTaskStatus, get_task_board

        board = get_task_board()
        for task in board.list_by_run(self.ctx.run_id):
            if task.status in (BoardTaskStatus.PRODUCED, BoardTaskStatus.VERIFYING):
                board.mark_verifying(task.task_id, run_id=self.ctx.run_id)
                board.mark_verified(task.task_id, run_id=self.ctx.run_id)
        self._sync_board_to_graph(graph)
        self._persist_graph(graph)
        summary = state.get("verification_summary", {}).get("summary") or (
            f"Run completed with {len(state.get('completed_task_ids', []))} verified tasks"
        )
        self._event("run_finalized", {"summary": summary})
        return {
            "phase": "finalized",
            "status": "succeeded",
            "final_output": summary,
            "task_graph_json": graph.model_dump_json(),
            "error": None,
        }

    def _fail(self, state: AgentRunState) -> dict[str, Any]:
        self._event("run_failed", {"error": state.get("error")})
        return {"phase": "failed", "status": "failed"}

    def _route_after_dispatch(self, state: AgentRunState) -> str:
        status = state.get("dispatch_status")
        if status == "waiting_human":
            from app.multiagent.task_board import BoardTaskStatus, get_task_board

            statuses = {
                task.status for task in get_task_board().list_by_run(self.ctx.run_id)
            }
            if BoardTaskStatus.REPAIR_REQUIRED in statuses:
                return "repair"
            if BoardTaskStatus.REPLAN_REQUIRED in statuses:
                return "replan"
            return "human"
        if status == "paused":
            return "human"
        if status in {"completed", "incomplete"}:
            return "collect"
        return "fail"

    def _route_after_verify(self, state: AgentRunState) -> str:
        verdict = state.get("verification_summary", {}).get("verdict", "fail")
        if verdict == "pass":
            return "finalize"
        if verdict == "repair":
            if int(state.get("repair_round", 0)) < self.max_repair_rounds:
                return "repair"
            return "fail"
        if verdict == "replan":
            if int(state.get("repair_round", 0)) < self.max_repair_rounds:
                return "replan"
            return "fail"
        if verdict == "human_required":
            return "human"
        return "fail"

    def _persist_graph(self, graph: TaskGraph) -> None:
        get_agent_run_history().save_task_graph(
            self.ctx.run_id, graph.model_dump(mode="json")
        )

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        get_agent_run_history().record_event(
            event_id=make_run_event_id(),
            run_id=self.ctx.run_id,
            event_type=f"root_graph:{event_type}",
            payload=payload,
            timestamp=datetime.now(UTC),
        )

    @staticmethod
    def _load_graph(state: AgentRunState) -> TaskGraph:
        graph_json = state.get("task_graph_json") or ""
        if not graph_json:
            raise RuntimeError("task_graph_missing")
        return TaskGraph.model_validate_json(graph_json)

    def _sync_board_to_graph(self, graph: TaskGraph) -> None:
        from app.multiagent.task_board import BoardTaskStatus, get_task_board

        mapping = {
            BoardTaskStatus.PENDING: TaskNodeStatus.PENDING,
            BoardTaskStatus.CLAIMED: TaskNodeStatus.RUNNING,
            BoardTaskStatus.RUNNING: TaskNodeStatus.RUNNING,
            BoardTaskStatus.PRODUCED: TaskNodeStatus.RUNNING,
            BoardTaskStatus.VERIFYING: TaskNodeStatus.RUNNING,
            BoardTaskStatus.SUCCEEDED: TaskNodeStatus.SUCCEEDED,
            BoardTaskStatus.REPAIR_REQUIRED: TaskNodeStatus.FAILED,
            BoardTaskStatus.REPLAN_REQUIRED: TaskNodeStatus.FAILED,
            BoardTaskStatus.FAILED: TaskNodeStatus.FAILED,
            BoardTaskStatus.CANCELLED: TaskNodeStatus.CANCELLED,
        }
        for task in get_task_board().list_by_run(self.ctx.run_id):
            node = graph.nodes.get(task.task_id)
            if node is None:
                continue
            target = mapping.get(task.status)
            if (
                task.status == BoardTaskStatus.REPAIR_REQUIRED
                and task.metadata.get("superseded_by_repair")
            ):
                target = TaskNodeStatus.SKIPPED
            if target is not None and node.status != target:
                # TaskBoard is the execution authority.  Replay the smallest
                # legal TaskGraph transition path when synchronizing its
                # terminal state instead of attempting PENDING -> SUCCEEDED.
                if (
                    target in {
                        TaskNodeStatus.SUCCEEDED,
                        TaskNodeStatus.FAILED,
                        TaskNodeStatus.SKIPPED,
                    }
                    and node.status in {TaskNodeStatus.PENDING, TaskNodeStatus.READY}
                ):
                    graph.update_status(task.task_id, TaskNodeStatus.RUNNING)
                if target == TaskNodeStatus.SKIPPED and node.status == TaskNodeStatus.SUCCEEDED:
                    graph.update_status(task.task_id, TaskNodeStatus.FAILED)
                graph.update_status(task.task_id, target)
            for artifact_id in task.produced_artifact_ids:
                if artifact_id not in node.output_artifact_ids:
                    node.output_artifact_ids.append(artifact_id)

    def _verification_artifacts(self, graph: TaskGraph) -> dict[str, dict[str, Any]]:
        store = getattr(self.verifier, "artifact_store", None)
        artifacts: dict[str, dict[str, Any]] = {}
        if store is not None:
            for artifact in store.list_by_run(self.ctx.run_id):
                artifacts[f"artifact:{artifact.id}"] = {
                    "artifact_id": artifact.id,
                    "path": artifact.path,
                    "content_hash": artifact.content_hash,
                    "status": artifact.status.value,
                }
        if artifacts:
            return artifacts
        for node in graph.nodes.values():
            for artifact_id in node.output_artifact_ids:
                artifacts[f"artifact:{artifact_id}"] = {
                    "artifact_id": artifact_id,
                    "status": node.status.value,
                }
        return artifacts

    def _workspace_components(self) -> dict[str, Any]:
        repository_meta = self.ctx.metadata.get("repository") or {}
        source = repository_meta.get("source_repository_path")
        if not source:
            return {}
        from app.multiagent.git_workspace import (
            AgentWorktreeManager,
            GitIntegrationManager,
            RepositoryWorkspaceManager,
        )
        from app.multiagent.permission import get_permission_broker

        repository = RepositoryWorkspaceManager(
            source,
            self.ctx.workspace_root,
            base_branch=repository_meta.get("base_branch"),
            base_commit_sha=repository_meta.get("base_commit_sha"),
        )
        broker = get_permission_broker()
        return {
            "worktree_manager": AgentWorktreeManager(
                repository,
                permission_broker=broker,
                environment_file_allowlist=repository_meta.get(
                    "environment_file_allowlist"
                ) or [],
            ),
            "integration_manager": GitIntegrationManager(
                repository, permission_broker=broker
            ),
            "permission_broker": broker,
        }

    @staticmethod
    def _to_result(state: AgentRunState) -> OrchestrationResult:
        graph = None
        if state.get("task_graph_json"):
            try:
                graph = TaskGraph.model_validate_json(state["task_graph_json"])
            except Exception:
                graph = None
        nodes = list(graph.nodes.values()) if graph else []
        verification = state.get("verification_summary", {})
        return OrchestrationResult(
            status=(
                "completed" if state.get("status") == "succeeded"
                else "interrupted" if state.get("status") in {"paused", "waiting_human"}
                else state.get("status", "failed")
            ),
            mode=state.get("mode", ""),
            task_graph_version=state.get("task_graph_version", 0),
            total_tasks=len(nodes),
            succeeded_tasks=sum(n.status == TaskNodeStatus.SUCCEEDED for n in nodes),
            failed_tasks=sum(n.status == TaskNodeStatus.FAILED for n in nodes),
            verification_verdict=verification.get("verdict", ""),
            rounds=state.get("dispatch_rounds", 0),
            error=state.get("error"),
            summary=state.get("final_output") or verification.get("summary", ""),
        )


def run_governed(
    *,
    goal: str,
    requested_mode: str,
    planner: Any,
    executor: Any,
    verifier: Any,
    ctx: TeamRunContext,
    cancel_event: Any | None = None,
    task_graph: TaskGraph | None = None,
    resume: bool = False,
    max_rounds: int = 30,
) -> OrchestrationResult:
    return GovernedRunGraph(
        ctx=ctx,
        planner=planner,
        executor=executor,
        verifier=verifier,
        cancel_event=cancel_event,
        task_graph=task_graph,
        max_rounds=max_rounds,
    ).invoke(goal=goal, requested_mode=requested_mode, resume=resume)
