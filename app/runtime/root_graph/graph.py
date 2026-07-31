"""The production LangGraph root graph for both single and team runs."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from app.core.config import settings
from app.core.logging import logger
from app.domain.runs.models import RunMode, SupervisorDecision
from app.infrastructure.database.connection import open_connection
from app.infrastructure.database.run_store import get_agent_run_history, make_run_event_id
from app.multiagent.task_graph import OutputContract, TaskGraph, TaskNode, TaskNodeStatus
from app.multiagent.team_run_context import TeamRunContext
from app.runtime.root_graph.state import AgentRunState
from app.runtime.reliability import RetryPolicy
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
        self.retry_policy = RetryPolicy(
            base_delay_seconds=settings.retry_base_delay_seconds,
            max_delay_seconds=settings.retry_max_delay_seconds,
        )
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

    def invoke(
        self,
        *,
        goal: str,
        requested_mode: str,
        resume: bool = False,
        resume_decision: dict[str, Any] | None = None,
    ) -> OrchestrationResult:
        # æ¯ä¸ª root graph èŠ‚ç‚¹åˆ‡æ¢ä¼šæ¶ˆè€— 2 ä¸ª recursion stepï¼ˆsuper-step boundaryï¼‰
        # ä¸€è½®æ­£å¸¸æµæ°´çº¿ intakeâ†’routerâ†’supervisorâ†’build_teamâ†’dispatchâ†’collectâ†’
        # verifyâ†’finalize è‡³å°‘ ~16 æ­¥ï¼›æ¯å¤šä¸€è½® repair å¤š ~8 æ­¥ã€‚
        # åŽŸæ¥ 100 çš„ä¸Šé™åœ¨ max_repair_rounds=3 + å¤šæ¬¡ dispatch retry ä¸‹ä¼šè§¦å‘
        # GraphRecursionErrorï¼ˆrun agent_7dc8e85d9ec6 / task_1__repair_v8ï¼‰ã€‚
        # æŠ¬åˆ° 200 è®©æœ‰é™è½®æ¬¡ repair / é‡ dispatch éƒ½èƒ½å®Œæˆï¼›ä¸Šé™ä»ç„¶å­˜åœ¨
        # æ˜¯ä¸ºäº†é˜²æ­¢çœŸæ­£çš„æ­»å¾ªçŽ¯æŠŠè¿›ç¨‹å¡æ­»ã€‚
        config = {
            "configurable": {
                "thread_id": self.ctx.run_id,
                "checkpoint_ns": self.ctx.checkpoint_namespace,
            },
            "recursion_limit": 200,
        }
        try:
            if resume:
                continuation: Any = (
                    Command(resume=resume_decision)
                    if resume_decision is not None
                    else None
                )
                final = self._compiled.invoke(continuation, config=config)
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
                "repair_rounds_by_task": {},
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
        self._apply_integration_verification_metadata(graph)
        return {
            "phase": "planned",
            "task_graph_json": graph.model_dump_json(),
            "task_graph_version": graph.version,
        }

    def _team_supervisor(self, state: AgentRunState) -> dict[str, Any]:
        if state.get("task_graph_json") and self.resume_graph is not None:
            graph = self._load_graph(state)
        else:
            graph = None
            last_error: Exception | None = None
            for attempt in range(1, 4):
                self._event("planning_started", {"attempt": attempt})
                try:
                    graph = self.planner(state["goal"], "")
                    break
                except Exception as exc:
                    last_error = exc
                    decision = self.retry_policy.decide(
                        str(exc), attempt=attempt, max_attempts=3
                    )
                    self._event("planning_attempt_failed", {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        **decision.to_dict(),
                    })
                    if not decision.retryable:
                        break
                    time.sleep(decision.delay_seconds)
            if graph is None:
                # Degradation path: when the LLM Planner exhausts all retries,
                # fall back to a minimal role-safe plan (plan â†’ execute â†’ verify) instead
                # of killing the Run.  This gives the runtime a chance to make
                # progress even when the model produces unparseable output.
                from app.multiagent.planner import build_fallback_plan
                try:
                    graph = build_fallback_plan(state["goal"])
                    self._event("planning_degraded", {
                        "reason": "fallback_plan_after_retries",
                        "last_error": str(last_error) if last_error else "",
                    })
                except Exception:
                    return {
                        "phase": "planning",
                        "status": "failed",
                        "error": f"planner_failed: {last_error}",
                    }
        self._apply_integration_verification_metadata(graph)
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
        self._event("team_build_started", {
            "task_graph_version": state.get("task_graph_version", 0),
        })
        try:
            graph = self._load_graph(state)
            from app.multiagent.default_teams import get_team
            from app.multiagent.team_builder import TeamBuilder

            agents = TeamBuilder().build_team_sync(
                self.ctx, get_team(self.ctx.team_id), graph
            )
            self._event("team_build_completed", {
                "agent_count": len(agents),
                "ready_task_ids": [node.id for node in graph.ready_tasks()],
            })
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
        self._event("dispatch_started", {
            "task_count": len(graph.nodes),
            "graph_version": graph.version,
        })
        try:
            from app.multiagent.parallel_scheduler import ParallelTeamScheduler
            from app.multiagent.transactional_task_service import TransactionalTaskService

            registration = TransactionalTaskService().register_initial_graph(
                self.ctx.run_id, graph,
            )
            graph = registration.graph
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
            graph = scheduler.task_graph or graph
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
            logger.exception("[RãÞz¶‰žËkºwµçE¥È½¹ÑÉ…Ð°‰ÕÐ¥ÐµÕÍÐ¹½Ð‰”•±¥¥‰±”™½ÈÑ¡”(€€€€€€€€€€€€€€€€€€€€Œ¹•áÐÝ¡½±”µÉÕ¸AML‘•¥Í¥½¸¸(€€€€€€€€€€€€€€€€€€€…ÉÑ¥™…Ñ}ÍÑ½É”¹µ…É­}É•©•Ñ•¡…ÉÑ¥™…Ñ}¥¤(€€€€€€€¥˜¹½ÐÉ•…Ñ•è(€€€€€€€€€€€€Œ•±¥¥‰±”ƒ¦v{ž¦ë’öšÊ‡šr'–"o–îë–ë’îï’öTÉ•Á…¥ÈÑ…Í¬ƒŠSŠPƒžB¢ºë’â+’â7–êS–>GžR|(€€€€€€€€€€€€Œƒ¾ò!É•…Ñ•}É•Á…¥Èƒ–¦£–ò–âãš&7’òk¢ÖÃ–"Ã¢þg¦3¾ò'¾ò3žîg–ë¢¾+šZ·’þ‡š¿’úÿ’ê;š:Kš~—Ž(€€€€€€€€€€€‰½…É‘}Í¹…ÁÍ¡½Ð€ôl(€€€€€€€€€€€€€€€ì‰Ñ…Í­}¥ˆèÐ¹Ñ…Í­}¥°€‰ÍÑ…ÑÕÌˆèÐ¹ÍÑ…ÑÕÌ¹Ù…±Õ”°(€€€€€€€€€€€€€€€€€‰ÍÕÁ•ÉÍ•‘•ˆè‰½½°¡Ð¹µ•Ñ…‘…Ñ„¹•Ð ‰ÍÕÁ•ÉÍ•‘•‘}‰å}É•Á…¥Èˆ¤¥ô(€€€€€€€€€€€€€€€™½ÈÐ¥¸‰½…É¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤(€€€€€€€€€€€t(€€€€€€€€€€€Í•±˜¹}•Ù•¹Ð ‰É•Á…¥É}¹½}…¹‘¥‘…Ñ•Ìˆ°ì‰‰½…Éˆè‰½…É‘}Í¹…ÁÍ¡½Ñô¤(€€€€€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ°(€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè€ (€€€€€€€€€€€€€€€€€€€€‰É•Á…¥É}É•…Ñ•‘}¹½}Ñ…Í­Ìè€ˆ(€€€€€€€€€€€€€€€€€€€˜‰•±¥¥‰±”õímÐ¹Ñ…Í­}¥™½ÈÐ¥¸•±¥¥‰±•uô°€ˆ(€€€€€€€€€€€€€€€€€€€˜‰‰½…Éõí‰½…É‘}Í¹…ÁÍ¡½Ñôˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô(€€€€€€€Í•±˜¹}Á•ÉÍ¥ÍÑ}É…Á ¡É…Á ¤(€€€€€€€Í•±˜¹}•Ù•¹Ð ‰É•Á…¥É}Á±…¹¹•ˆ°ì(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹ˆè¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€¬€Ä°(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹‘Í}‰å}Ñ…Í¬ˆèÉ½Õ¹‘Í}‰å}Ñ…Í¬°(€€€€€€€€€€€€‰É•…Ñ•‘}Ñ…Í­}¥‘ÌˆèÉ•…Ñ•°(€€€€€€€€€€€€‰Ù•É¥™¥…Ñ¥½¸ˆèÍÑ…Ñ”¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÕµµ…Éäˆ°íô¤°(€€€€€€€ô¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰Á¡…Í”ˆè€‰É•Á…¥É}Á±…¹¹•ˆ°(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹ˆè¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€¬€Ä°(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹‘Í}‰å}Ñ…Í¬ˆèÉ½Õ¹‘Í}‰å}Ñ…Í¬°(€€€€€€€€€€€€‰Ñ…Í­}É…Á¡}©Í½¸ˆèÉ…Á ¹µ½‘•±}‘ÕµÁ}©Í½¸ ¤°(€€€€€€€€€€€€‰Ñ…Í­}É…Á¡}Ù•ÉÍ¥½¸ˆèÉ…Á ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”°(€€€€€€€ô((€€€‘•˜}É•Á±…¸¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€…ÉÑ¥™…Ñ}ÍÑ½É”€ô•Ñ…ÑÑÈ¡Í•±˜¹Ù•É¥™¥•È°€‰…ÉÑ¥™…Ñ}ÍÑ½É”ˆ°9½¹”¤(€€€€€€€¥˜…ÉÑ¥™…Ñ}ÍÑ½É”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€™½È…ÉÑ¥™…Ð¥¸…ÉÑ¥™…Ñ}ÍÑ½É”¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€€€€€¥˜…ÉÑ¥™…Ð¹ÍÑ…ÑÕÌ¹Ù…±Õ”¥¸ì‰ÁÕ‰±¥Í¡•ˆ°€‰Ù•É¥™¥•‰ôè(€€€€€€€€€€€€€€€€€€€…ÉÑ¥™…Ñ}ÍÑ½É”¹µ…É­}É•©•Ñ•¡…ÉÑ¥™…Ð¹¥¤(€€€€€€€Í•±˜¹}•Ù•¹Ð ‰É•Á±…¹}É•ÅÕ•ÍÑ•ˆ°ì(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹ˆè¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€¬€Ä°(€€€€€€€€€€€€‰Ù•É¥™¥…Ñ¥½¸ˆèÍÑ…Ñ”¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÕµµ…Éäˆ°íô¤°(€€€€€€€ô¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰Á¡…Í”ˆè€‰É•Á±…¹¹¥¹œˆ°(€€€€€€€€€€€€‰Ñ…Í­}É…Á¡}©Í½¸ˆè€ˆˆ°(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹ˆè¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€¬€Ä°(€€€€€€€€€€€€ŒI•Á±…¸ƒ¦7žö¸Á•ÈµÑ…Í¬ƒ¢º‡šVÃ–f£¾òkšZÀÁ±…¸ƒ’êŸžR–£šZÃžjÑ…Í¬¥“¾ò0(€€€€€€€€€€€€Œƒš^Ÿ¢º‡šVÃ–f£’â7–7¦žR£Ž–£–Æ É•Á…¥É}É½Õ¹ƒžîŸžî·žÒ¿žž¿’ös’âë–º'–£žöGŽ(€€€€€€€€€€€€‰É•Á…¥É}É½Õ¹‘Í}‰å}Ñ…Í¬ˆèíô°(€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”°(€€€€€€€ô((€€€‘•˜}¡Õµ…¹}¥¹Ñ•ÉÉÕÁÐ¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€Í•±˜¹}•Ù•¹Ð ‰¡Õµ…¹}¥¹ÁÕÑ}É•ÅÕ•ÍÑ•ˆ°ì(€€€€€€€€€€€€‰É•…Í½¸ˆèÍÑ…Ñ”¹•Ð ‰•ÉÉ½Èˆ¤½È€‰¡Õµ…¸…ÁÁÉ½Ù…°É•ÅÕ¥É•ˆ°(€€€€€€€€€€€€‰Á•¹‘¥¹}Á•Éµ¥ÍÍ¥½¹}¥‘ÌˆèÍÑ…Ñ”¹•Ð ‰Á•¹‘¥¹}Á•Éµ¥ÍÍ¥½¹}¥‘Ìˆ°mt¤°(€€€€€€€€€€€€‰Á•¹‘¥¹}Á±…¹}¥‘ÌˆèÍÑ…Ñ”¹•Ð ‰Á•¹‘¥¹}Á±…¹}¥‘Ìˆ°mt¤°(€€€€€€€ô¤(€€€€€€€É•ÍÁ½¹Í”€ô¥¹Ñ•ÉÉÕÁÐ¡ì(€€€€€€€€€€€€‰ÉÕ¹}¥ˆèÍ•±˜¹Ñà¹ÉÕ¹}¥°(€€€€€€€€€€€€‰É•…Í½¸ˆèÍÑ…Ñ”¹•Ð ‰•ÉÉ½Èˆ¤½È€‰¡Õµ…¸…ÁÁÉ½Ù…°É•ÅÕ¥É•ˆ°(€€€€€€€€€€€€‰Á•¹‘¥¹}Á•Éµ¥ÍÍ¥½¹}¥‘ÌˆèÍÑ…Ñ”¹•Ð ‰Á•¹‘¥¹}Á•Éµ¥ÍÍ¥½¹}¥‘Ìˆ°mt¤°(€€€€€€€€€€€€‰Á•¹‘¥¹}Á±…¹}¥‘ÌˆèÍÑ…Ñ”¹•Ð ‰Á•¹‘¥¹}Á±…¹}¥‘Ìˆ°mt¤°(€€€€€€€ô¤(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É•ÍÁ½¹Í”°‘¥Ð¤…¹É•ÍÁ½¹Í”¹•Ð ‰‘•¥Í¥½¸ˆ¤¥¸ì‰‘•¹äˆ°€‰…¹•°‰ôè(€€€€€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€€€€€‰Á¡…Í”ˆè€‰¡Õµ…¹}‘•¹¥•ˆ°(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ°(€€€€€€€€€€€€€€€€‰•ÉÉ½ÈˆèÉ•ÍÁ½¹Í”¹•Ð ‰™••‘‰…¬ˆ¤½È€‰¡Õµ…¸‘•¹¥•½¹Ñ¥¹Õ…Ñ¥½¸ˆ°(€€€€€€€€€€€ô(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰Á¡…Í”ˆè€‰É•ÍÕµ•ˆ°(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰ÉÕ¹¹¥¹œˆ°(€€€€€€€€€€€€‰‘¥ÍÁ…Ñ¡}ÍÑ…ÑÕÌˆè€ˆˆ°(€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”°(€€€€€€€ô((€€€‘•˜}™¥¹…±¥é”¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€É…Á €ôÍ•±˜¹}±½…‘}É…Á ¡ÍÑ…Ñ”¤(€€€€€€€™É½´…ÁÀ¹µÕ±Ñ¥…•¹Ð¹Ñ…Í­}‰½…É¥µÁ½ÉÐ	½…É‘Q…Í­MÑ…ÑÕÌ°•Ñ}Ñ…Í­}‰½…É((€€€€€€€‰½…É€ô•Ñ}Ñ…Í­}‰½…É ¤(€€€€€€€™½ÈÑ…Í¬¥¸‰½…É¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€¥˜Ñ…Í¬¹ÍÑ…ÑÕÌ€ôô	½…É‘Q…Í­MÑ…ÑÕÌ¹AI=Uè(€€€€€€€€€€€€€€€¥˜¹½Ð‰½…É¹µ…É­}Ù•É¥™å¥¹œ¡Ñ…Í¬¹Ñ…Í­}¥°ÉÕ¹}¥õÍ•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È (€€€€€€€€€€€€€€€€€€€€€€€˜‰™¥¹…±¥é•}ÍÑ…Ñ•}½¹™±¥ÐéíÑ…Í¬¹Ñ…Í­}¥‘ôéµ…É­}Ù•É¥™å¥¹œˆ(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€ÕÉÉ•¹Ð€ô‰½…É¹•Ð¡Ñ…Í¬¹Ñ…Í­}¥°ÉÕ¹}¥õÍ•±˜¹Ñà¹ÉÕ¹}¥¤(€€€€€€€€€€€¥˜ÕÉÉ•¹Ð¥Ì¹½Ð9½¹”…¹ÕÉÉ•¹Ð¹ÍÑ…ÑÕÌ€ôô	½…É‘Q…Í­MÑ…ÑÕÌ¹YI%e%9è(€€€€€€€€€€€€€€€¥˜¹½Ð‰½…É¹µ…É­}Ù•É¥™¥•¡Ñ…Í¬¹Ñ…Í­}¥°ÉÕ¹}¥õÍ•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È (€€€€€€€€€€€€€€€€€€€€€€€˜‰™¥¹…±¥é•}ÍÑ…Ñ•}½¹™±¥ÐéíÑ…Í¬¹Ñ…Í­}¥‘ôéµ…É­}Ù•É¥™¥•ˆ(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹}Íå¹}‰½…É‘}Ñ½}É…Á ¡É…Á ¤(€€€€€€€Í•±˜¹}Á•ÉÍ¥ÍÑ}É…Á ¡É…Á ¤(€€€€€€€ÍÕµµ…Éä€ôÍÑ…Ñ”¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÕµµ…Éäˆ°íô¤¹•Ð ‰ÍÕµµ…Éäˆ¤½È€ (€€€€€€€€€€€˜‰IÕ¸½µÁ±•Ñ•Ý¥Ñ í±•¸¡ÍÑ…Ñ”¹•Ð ½µÁ±•Ñ•‘}Ñ…Í­}¥‘Ìœ°mt¤¥ôÙ•É¥™¥•Ñ…Í­Ìˆ(€€€€€€€€¤(€€€€€€€Í•±˜¹}•Ù•¹Ð ‰ÉÕ¹}™¥¹…±¥é•ˆ°ì‰ÍÕµµ…ÉäˆèÍÕµµ…Éåô¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰Á¡…Í”ˆè€‰™¥¹…±¥é•ˆ°(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰ÍÕ••‘•ˆ°(€€€€€€€€€€€€‰™¥¹…±}½ÕÑÁÕÐˆèÍÕµµ…Éä°(€€€€€€€€€€€€‰Ñ…Í­}É…Á¡}©Í½¸ˆèÉ…Á ¹µ½‘•±}‘ÕµÁ}©Í½¸ ¤°(€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”°(€€€€€€€ô((€€€‘•˜}™…¥°¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€Í•±˜¹}•Ù•¹Ð ‰ÉÕ¹}™…¥±•ˆ°ì‰•ÉÉ½ÈˆèÍÑ…Ñ”¹•Ð ‰•ÉÉ½Èˆ¥ô¤(€€€€€€€É•ÑÕÉ¸ì‰Á¡…Í”ˆè€‰™…¥±•ˆ°€‰ÍÑ…ÑÕÌˆè€‰™…¥±•‰ô((€€€‘•˜}É½ÕÑ•}…™Ñ•É}‘¥ÍÁ…Ñ ¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´øÍÑÈè(€€€€€€€ÍÑ…ÑÕÌ€ôÍÑ…Ñ”¹•Ð ‰‘¥ÍÁ…Ñ¡}ÍÑ…ÑÕÌˆ¤(€€€€€€€¥˜ÍÑ…ÑÕÌ€ôô€‰Ý…¥Ñ¥¹}¡Õµ…¸ˆè(€€€€€€€€€€€™É½´…ÁÀ¹µÕ±Ñ¥…•¹Ð¹Ñ…Í­}‰½…É¥µÁ½ÉÐ	½…É‘Q…Í­MÑ…ÑÕÌ°•Ñ}Ñ…Í­}‰½…É((€€€€€€€€€€€ÍÑ…ÑÕÍ•Ì€ôì(€€€€€€€€€€€€€€€Ñ…Í¬¹ÍÑ…ÑÕÌ™½ÈÑ…Í¬¥¸•Ñ}Ñ…Í­}‰½…É ¤¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤(€€€€€€€€€€€ô(€€€€€€€€€€€¥˜	½…É‘Q…Í­MÑ…ÑÕÌ¹IA%I}IEU%I¥¸ÍÑ…ÑÕÍ•Ìè(€€€€€€€€€€€€€€€É•ÑÕÉ¸€‰É•Á…¥Èˆ(€€€€€€€€€€€¥˜	½…É‘Q…Í­MÑ…ÑÕÌ¹IA19}IEU%I¥¸ÍÑ…ÑÕÍ•Ìè(€€€€€€€€€€€€€€€É•ÑÕÉ¸€‰É•Á±…¸ˆ(€€€€€€€€€€€É•ÑÕÉ¸€‰¡Õµ…¸ˆ(€€€€€€€¥˜ÍÑ…ÑÕÌ€ôô€‰Á…ÕÍ•ˆè(€€€€€€€€€€€É•ÑÕÉ¸€‰¡Õµ…¸ˆ(€€€€€€€¥˜ÍÑ…ÑÕÌ€ôô€‰½µÁ±•Ñ•ˆè(€€€€€€€€€€€É•ÑÕÉ¸€‰½±±•Ðˆ(€€€€€€€É•ÑÕÉ¸€‰™…¥°ˆ((€€€‘•˜}É½ÕÑ•}…™Ñ•É}Ù•É¥™ä¡Í•±˜°ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´øÍÑÈè(€€€€€€€Ù•É‘¥Ð€ôÍÑ…Ñ”¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÕµµ…Éäˆ°íô¤¹•Ð ‰Ù•É‘¥Ðˆ°€‰™…¥°ˆ¤(€€€€€€€¥˜Ù•É‘¥Ð€ôô€‰Á…ÍÌˆè(€€€€€€€€€€€É•ÑÕÉ¸€‰™¥¹…±¥é”ˆ(€€€€€€€€Œƒ–£–Æ–º'–£žöG¾òkšï’þ»–’7¢ö»š²‡¢úû–"Àµ…á}É•Á…¥É}É½Õ¹‘Ì€¨€Ìƒš^Û’â7–7¢Þ¿žRÇ–"À(€€€€€€€€ŒÉ•Á…¥È½É•Á±…»¾ò3žnÓš:”™…¥³Ž	Á•ÈµÑ…Í¬ƒžj’þ»–’7¦Šžº_šŽš~—–r }É•Á…¥È€¼}É•Á±…¸(€€€€€€€€Œƒ¢*ž
ç–¦£š&Ÿ¢†3¾ò3¦
¦3’òkžÊûž†»š:Ÿ–"Ûš¾?šv‡’îï–*‡¦Nûžj’þ»–’7’â+¦fCŽ¢þg¦3–>«–kžÊ_žÊK–ê›žj(€€€€€€€€Œƒ–£–Æ–s–êW¾ò3¦bËš¶ˆÁ•ÈµÑ…Í¬ƒ¦ï¢úGšr$‰Õœƒš^Ûš^ƒ¦fC–ú«ž:¿Ž(€€€€€€€±½‰…±}±¥µ¥Ð€ôÍ•±˜¹µ…á}É•Á…¥É}É½Õ¹‘Ì€¨€Ì(€€€€€€€¥˜Ù•É‘¥Ð€ôô€‰É•Á…¥Èˆè(€€€€€€€€€€€¥˜¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€ð±½‰…±}±¥µ¥Ðè(€€€€€€€€€€€€€€€É•ÑÕÉ¸€‰É•Á…¥Èˆ(€€€€€€€€€€€É•ÑÕÉ¸€‰™…¥°ˆ(€€€€€€€¥˜Ù•É‘¥Ð€ôô€‰É•Á±…¸ˆè(€€€€€€€€€€€¥˜¥¹Ð¡ÍÑ…Ñ”¹•Ð ‰É•Á…¥É}É½Õ¹ˆ°€À¤¤€ð±½‰…±}±¥µ¥Ðè(€€€€€€€€€€€€€€€É•ÑÕÉ¸€‰É•Á±…¸ˆ(€€€€€€€€€€€É•ÑÕÉ¸€‰™…¥°ˆ(€€€€€€€¥˜Ù•É‘¥Ð€ôô€‰¡Õµ…¹}É•ÅÕ¥É•ˆè(€€€€€€€€€€€É•ÑÕÉ¸€‰¡Õµ…¸ˆ(€€€€€€€É•ÑÕÉ¸€‰™…¥°ˆ((€€€‘•˜}Á•ÉÍ¥ÍÑ}É…Á ¡Í•±˜°É…Á èQ…Í­É…Á ¤€´ø9½¹”è(€€€€€€€•Ñ}…•¹Ñ}ÉÕ¹}¡¥ÍÑ½Éä ¤¹Í…Ù•}Ñ…Í­}É…Á  (€€€€€€€€€€€Í•±˜¹Ñà¹ÉÕ¹}¥°É…Á ¹µ½‘•±}‘ÕµÀ¡µ½‘”ô‰©Í½¸ˆ¤(€€€€€€€€¤((€€€‘•˜}•Ù•¹Ð¡Í•±˜°•Ù•¹Ñ}ÑåÁ”èÍÑÈ°Á…å±½…è‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è(€€€€€€€•Ñ}…•¹Ñ}ÉÕ¹}¡¥ÍÑ½Éä ¤¹É•½É‘}•Ù•¹Ð (€€€€€€€€€€€•Ù•¹Ñ}¥õµ…­•}ÉÕ¹}•Ù•¹Ñ}¥ ¤°(€€€€€€€€€€€ÉÕ¹}¥õÍ•±˜¹Ñà¹ÉÕ¹}¥°(€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”õ˜‰É½½Ñ}É…Á éí•Ù•¹Ñ}ÑåÁ•ôˆ°(€€€€€€€€€€€Á…å±½…õÁ…å±½…°(€€€€€€€€€€€Ñ¥µ•ÍÑ…µÀõ‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤°(€€€€€€€€¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}±½…‘}É…Á ¡ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´øQ…Í­É…Á è(€€€€€€€É…Á¡}©Í½¸€ôÍÑ…Ñ”¹•Ð ‰Ñ…Í­}É…Á¡}©Í½¸ˆ¤½È€ˆˆ(€€€€€€€¥˜¹½ÐÉ…Á¡}©Í½¸è(€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‰Ñ…Í­}É…Á¡}µ¥ÍÍ¥¹œˆ¤(€€€€€€€É•ÑÕÉ¸Q…Í­É…Á ¹µ½‘•±}Ù…±¥‘…Ñ•}©Í½¸¡É…Á¡}©Í½¸¤((€€€‘•˜}Íå¹}‰½…É‘}Ñ½}É…Á ¡Í•±˜°É…Á èQ…Í­É…Á ¤€´ø9½¹”è(€€€€€€€™É½´…ÁÀ¹µÕ±Ñ¥…•¹Ð¹Ñ…Í­}‰½…É¥µÁ½ÉÐ	½…É‘Q…Í­MÑ…ÑÕÌ°•Ñ}Ñ…Í­}‰½…É((€€€€€€€µ…ÁÁ¥¹œ€ôì(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹A9%9èQ…Í­9½‘•MÑ…ÑÕÌ¹A9%9°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹1%5èQ…Í­9½‘•MÑ…ÑÕÌ¹IU99%9°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹IU99%9èQ…Í­9½‘•MÑ…ÑÕÌ¹IU99%9°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹AI=UèQ…Í­9½‘•MÑ…ÑÕÌ¹IU99%9°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹YI%e%9èQ…Í­9½‘•MÑ…ÑÕÌ¹IU99%9°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹MUèQ…Í­9½‘•MÑ…ÑÕÌ¹MU°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹IA%I}IEU%IèQ…Í­9½‘•MÑ…ÑÕÌ¹%1°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹IA19}IEU%IèQ…Í­9½‘•MÑ…ÑÕÌ¹%1°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹%1èQ…Í­9½‘•MÑ…ÑÕÌ¹%1°(€€€€€€€€€€€	½…É‘Q…Í­MÑ…ÑÕÌ¹911èQ…Í­9½‘•MÑ…ÑÕÌ¹911°(€€€€€€€ô(€€€€€€€™½ÈÑ…Í¬¥¸•Ñ}Ñ…Í­}‰½…É ¤¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€¹½‘”€ôÉ…Á ¹¹½‘•Ì¹•Ð¡Ñ…Í¬¹Ñ…Í­}¥¤(€€€€€€€€€€€¥˜¹½‘”¥Ì9½¹”è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€Ñ…É•Ð€ôµ…ÁÁ¥¹œ¹•Ð¡Ñ…Í¬¹ÍÑ…ÑÕÌ¤(€€€€€€€€€€€¥˜€ (€€€€€€€€€€€€€€€Ñ…Í¬¹ÍÑ…ÑÕÌ€ôô	½…É‘Q…Í­MÑ…ÑÕÌ¹IA%I}IEU%I(€€€€€€€€€€€€€€€…¹Ñ…Í¬¹µ•Ñ…‘…Ñ„¹•Ð ‰ÍÕÁ•ÉÍ•‘•‘}‰å}É•Á…¥Èˆ¤(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€Ñ…É•Ð€ôQ…Í­9½‘•MÑ…ÑÕÌ¹M-%AA(€€€€€€€€€€€¥˜Ñ…É•Ð¥Ì¹½Ð9½¹”…¹¹½‘”¹ÍÑ…ÑÕÌ€„ôÑ…É•Ðè(€€€€€€€€€€€€€€€€ŒQ…Í­	½…É¥ÌÑ¡”•á•ÕÑ¥½¸…ÕÑ¡½É¥Ñä¸€I•Á±…äÑ¡”Íµ…±±•ÍÐ(€€€€€€€€€€€€€€€€Œ±•…°Q…Í­É…Á ÑÉ…¹Í¥Ñ¥½¸Á…Ñ Ý¡•¸Íå¹¡É½¹¥é¥¹œ¥ÑÌ(€€€€€€€€€€€€€€€€ŒÑ•Éµ¥¹…°ÍÑ…Ñ”¥¹ÍÑ•…½˜…ÑÑ•µÁÑ¥¹œA9%9€´øMU¸(€€€€€€€€€€€€€€€¥˜€ (€€€€€€€€€€€€€€€€€€€Ñ…É•Ð¥¸ì(€€€€€€€€€€€€€€€€€€€€€€€Q…Í­9½‘•MÑ…ÑÕÌ¹MU°(€€€€€€€€€€€€€€€€€€€€€€€Q…Í­9½‘•MÑ…ÑÕÌ¹%1°(€€€€€€€€€€€€€€€€€€€€€€€Q…Í­9½‘•MÑ…ÑÕÌ¹M-%AA°(€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€…¹¹½‘”¹ÍÑ…ÑÕÌ¥¸íQ…Í­9½‘•MÑ…ÑÕÌ¹A9%9°Q…Í­9½‘•MÑ…ÑÕÌ¹Ieô(€€€€€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€€€€€É…Á ¹ÕÁ‘…Ñ•}ÍÑ…ÑÕÌ¡Ñ…Í¬¹Ñ…Í­}¥°Q…Í­9½‘•MÑ…ÑÕÌ¹IU99%9¤(€€€€€€€€€€€€€€€¥˜Ñ…É•Ð€ôôQ…Í­9½‘•MÑ…ÑÕÌ¹M-%AA…¹¹½‘”¹ÍÑ…ÑÕÌ€ôôQ…Í­9½‘•MÑ…ÑÕÌ¹MUè(€€€€€€€€€€€€€€€€€€€É…Á ¹ÕÁ‘…Ñ•}ÍÑ…ÑÕÌ¡Ñ…Í¬¹Ñ…Í­}¥°Q…Í­9½‘•MÑ…ÑÕÌ¹%1¤(€€€€€€€€€€€€€€€É…Á ¹ÕÁ‘…Ñ•}ÍÑ…ÑÕÌ¡Ñ…Í¬¹Ñ…Í­}¥°Ñ…É•Ð¤(€€€€€€€€€€€™½È…ÉÑ¥™…Ñ}¥¥¸Ñ…Í¬¹ÁÉ½‘Õ•‘}…ÉÑ¥™…Ñ}¥‘Ìè(€€€€€€€€€€€€€€€¥˜…ÉÑ¥™…Ñ}¥¹½Ð¥¸¹½‘”¹½ÕÑÁÕÑ}…ÉÑ¥™…Ñ}¥‘Ìè(€€€€€€€€€€€€€€€€€€€¹½‘”¹½ÕÑÁÕÑ}…ÉÑ¥™…Ñ}¥‘Ì¹…ÁÁ•¹¡…ÉÑ¥™…Ñ}¥¤((€€€‘•˜}Ù•É¥™¥…Ñ¥½¹}…ÉÑ¥™…ÑÌ¡Í•±˜°É…Á èQ…Í­É…Á ¤€´ø‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åutè(€€€€€€€ÍÑ½É”€ô•Ñ…ÑÑÈ¡Í•±˜¹Ù•É¥™¥•È°€‰…ÉÑ¥™…Ñ}ÍÑ½É”ˆ°9½¹”¤(€€€€€€€…ÉÑ¥™…ÑÌè‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åut€ôíô(€€€€€€€¥˜ÍÑ½É”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€™½È…ÉÑ¥™…Ð¥¸ÍÑ½É”¹±¥ÍÑ}‰å}ÉÕ¸¡Í•±˜¹Ñà¹ÉÕ¹}¥¤è(€€€€€€€€€€€€€€€…ÉÑ¥™…ÑÍm˜‰…ÉÑ¥™…Ðéí…ÉÑ¥™…Ð¹¥‘ô‰t€ôì(€€€€€€€€€€€€€€€€€€€€‰…ÉÑ¥™…Ñ}¥ˆè…ÉÑ¥™…Ð¹¥°(€€€€€€€€€€€€€€€€€€€€‰Á…Ñ ˆè…ÉÑ¥™…Ð¹Á…Ñ °(€€€€€€€€€€€€€€€€€€€€‰½¹Ñ•¹Ñ}¡…Í ˆè…ÉÑ¥™…Ð¹½¹Ñ•¹Ñ}¡…Í °(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè…ÉÑ¥™…Ð¹ÍÑ…ÑÕÌ¹Ù…±Õ”°(€€€€€€€€€€€€€€€ô(€€€€€€€¥˜…ÉÑ¥™…ÑÌè(€€€€€€€€€€€É•ÑÕÉ¸…ÉÑ¥™…ÑÌ(€€€€€€€™½È¹½‘”¥¸É…Á ¹¹½‘•Ì¹Ù…±Õ•Ì ¤è(€€€€€€€€€€€™½È…ÉÑ¥™…Ñ}¥¥¸¹½‘”¹½ÕÑÁÕÑ}…ÉÑ¥™…Ñ}¥‘Ìè(€€€€€€€€€€€€€€€…ÉÑ¥™…ÑÍm˜‰…ÉÑ¥™…Ðéí…ÉÑ¥™…Ñ}¥‘ô‰t€ôì(€€€€€€€€€€€€€€€€€€€€‰…ÉÑ¥™…Ñ}¥ˆè…ÉÑ¥™…Ñ}¥°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè¹½‘”¹ÍÑ…ÑÕÌ¹Ù…±Õ”°(€€€€€€€€€€€€€€€ô(€€€€€€€É•ÑÕÉ¸…ÉÑ¥™…ÑÌ((€€€‘•˜}…ÁÁ±å}¥¹Ñ•É…Ñ¥½¹}Ù•É¥™¥…Ñ¥½¹}µ•Ñ…‘…Ñ„¡Í•±˜°É…Á èQ…Í­É…Á ¤€´ø9½¹”è(€€€€€€€€ˆˆ‰…ÉÉäÉÕ¸µ±•Ù•°É•Á½Í¥Ñ½Éä¡•­Ì¥¹Ñ¼Ñ¡”‘ÕÉ…‰±”É½½Ð½¹ÑÉ…Ð¸ˆˆˆ(€€€€€€€É½½Ð€ôÉ…Á ¹¹½‘•Ì¹•Ð¡É…Á ¹É½½Ñ}Ñ…Í­}¥¤(€€€€€€€¥˜É½½Ð¥Ì9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€É•Á½Í¥Ñ½Éä€ôÍ•±˜¹Ñà¹µ•Ñ…‘…Ñ„¹•Ð ‰É•Á½Í¥Ñ½Éäˆ¤½Èíô(€€€€€€€™½È­•ä¥¸€ ‰¥¹Ñ•É…Ñ¥½¹}Ñ•ÍÑ}…ÉØˆ°€‰¥¹Ñ•É…Ñ¥½¹}Ñ•ÍÑ}½µµ…¹‘Ìˆ¤è(€€€€€€€€€€€Ù…±Õ”€ôÍ•±˜¹Ñà¹µ•Ñ…‘…Ñ„¹•Ð¡­•ä¤(€€€€€€€€€€€¥˜Ù…±Õ”¥Ì9½¹”…¹¥Í¥¹ÍÑ…¹”¡É•Á½Í¥Ñ½Éä°‘¥Ð¤è(€€€€€€€€€€€€€€€Ù…±Õ”€ôÉ•Á½Í¥Ñ½Éä¹•Ð¡­•ä¤(€€€€€€€€€€€¥˜Ù…±Õ”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€É½½Ð¹µ•Ñ…‘…Ñ…m­•åt€ôÙ…±Õ”((€€€‘•˜}Ý½É­ÍÁ…•}½µÁ½¹•¹ÑÌ¡Í•±˜¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€É•Á½Í¥Ñ½Éå}µ•Ñ„€ôÍ•±˜¹Ñà¹µ•Ñ…‘…Ñ„¹•Ð ‰É•Á½Í¥Ñ½Éäˆ¤½Èíô(€€€€€€€Í½ÕÉ”€ôÉ•Á½Í¥Ñ½Éå}µ•Ñ„¹•Ð ‰Í½ÕÉ•}É•Á½Í¥Ñ½Éå}Á…Ñ ˆ¤(€€€€€€€¥˜¹½ÐÍ½ÕÉ”è(€€€€€€€€€€€É•ÑÕÉ¸íô(€€€€€€€™É½´…ÁÀ¹µÕ±Ñ¥…•¹Ð¹¥Ñ}Ý½É­ÍÁ…”¥µÁ½ÉÐ€ (€€€€€€€€€€€•¹Ñ]½É­ÑÉ••5…¹…•È°(€€€€€€€€€€€¥Ñ%¹Ñ•É…Ñ¥½¹5…¹…•È°(€€€€€€€€€€€I•Á½Í¥Ñ½Éå]½É­ÍÁ…•5…¹…•È°(€€€€€€€€¤(€€€€€€€™É½´…ÁÀ¹µÕ±Ñ¥…•¹Ð¹Á•Éµ¥ÍÍ¥½¸¥µÁ½ÉÐ•Ñ}Á•Éµ¥ÍÍ¥½¹}‰É½­•È((€€€€€€€É•Á½Í¥Ñ½Éä€ôI•Á½Í¥Ñ½Éå]½É­ÍÁ…•5…¹…•È (€€€€€€€€€€€Í½ÕÉ”°(€€€€€€€€€€€Í•±˜¹Ñà¹Ý½É­ÍÁ…•}É½½Ð°(€€€€€€€€€€€‰…Í•}‰É…¹ õÉ•Á½Í¥Ñ½Éå}µ•Ñ„¹•Ð ‰‰…Í•}‰É…¹ ˆ¤°(€€€€€€€€€€€‰…Í•}½µµ¥Ñ}Í¡„õÉ•Á½Í¥Ñ½Éå}µ•Ñ„¹•Ð ‰‰…Í•}½µµ¥Ñ}Í¡„ˆ¤°(€€€€€€€€¤(€€€€€€€‰É½­•È€ô•Ñ}Á•Éµ¥ÍÍ¥½¹}‰É½­•È ¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰Ý½É­ÑÉ••}µ…¹…•Èˆè•¹Ñ]½É­ÑÉ••5…¹…•È (€€€€€€€€€€€€€€€É•Á½Í¥Ñ½Éä°(€€€€€€€€€€€€€€€Á•Éµ¥ÍÍ¥½¹}‰É½­•Èõ‰É½­•È°(€€€€€€€€€€€€€€€•¹Ù¥É½¹µ•¹Ñ}™¥±•}…±±½Ý±¥ÍÐõÉ•Á½Í¥Ñ½Éå}µ•Ñ„¹•Ð (€€€€€€€€€€€€€€€€€€€€‰•¹Ù¥É½¹µ•¹Ñ}™¥±•}…±±½Ý±¥ÍÐˆ(€€€€€€€€€€€€€€€€¤½Èmt°(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰¥¹Ñ•É…Ñ¥½¹}µ…¹…•Èˆè¥Ñ%¹Ñ•É…Ñ¥½¹5…¹…•È (€€€€€€€€€€€€€€€É•Á½Í¥Ñ½Éä°Á•Éµ¥ÍÍ¥½¹}‰É½­•Èõ‰É½­•È(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰Á•Éµ¥ÍÍ¥½¹}‰É½­•Èˆè‰É½­•È°(€€€€€€€ô((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}Ñ½}É•ÍÕ±Ð¡ÍÑ…Ñ”è•¹ÑIÕ¹MÑ…Ñ”¤€´ø=É¡•ÍÑÉ…Ñ¥½¹I•ÍÕ±Ðè(€€€€€€€É…Á €ô9½¹”(€€€€€€€¥˜ÍÑ…Ñ”¹•Ð ‰Ñ…Í­}É…Á¡}©Í½¸ˆ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É…Á €ôQ…Í­É…Á ¹µ½‘•±}Ù…±¥‘…Ñ•}©Í½¸¡ÍÑ…Ñ•l‰Ñ…Í­}É…Á¡}©Í½¸‰t¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€É…Á €ô9½¹”(€€€€€€€¹½‘•Ì€ô±¥ÍÐ¡É…Á ¹¹½‘•Ì¹Ù…±Õ•Ì ¤¤¥˜É…Á •±Í”mt(€€€€€€€Ù•É¥™¥…Ñ¥½¸€ôÍÑ…Ñ”¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÕµµ…Éäˆ°íô¤(€€€€€€€É•ÑÕÉ¸=É¡•ÍÑÉ…Ñ¥½¹I•ÍÕ±Ð (€€€€€€€€€€€ÍÑ…ÑÕÌô (€€€€€€€€€€€€€€€€‰½µÁ±•Ñ•ˆ¥˜ÍÑ…Ñ”¹•Ð ‰ÍÑ…ÑÕÌˆ¤€ôô€‰ÍÕ••‘•ˆ(€€€€€€€€€€€€€€€•±Í”€‰¥¹Ñ•ÉÉÕÁÑ•ˆ¥˜ÍÑ…Ñ”¹•Ð ‰ÍÑ…ÑÕÌˆ¤¥¸ì‰Á…ÕÍ•ˆ°€‰Ý…¥Ñ¥¹}¡Õµ…¸‰ô(€€€€€€€€€€€€€€€•±Í”ÍÑ…Ñ”¹•Ð ‰ÍÑ…ÑÕÌˆ°€‰™…¥±•ˆ¤(€€€€€€€€€€€€¤°(€€€€€€€€€€€µ½‘”õÍÑ…Ñ”¹•Ð ‰µ½‘”ˆ°€ˆˆ¤°(€€€€€€€€€€€Ñ…Í­}É…Á¡}Ù•ÉÍ¥½¸õÍÑ…Ñ”¹•Ð ‰Ñ…Í­}É…Á¡}Ù•ÉÍ¥½¸ˆ°€À¤°(€€€€€€€€€€€Ñ½Ñ…±}Ñ…Í­Ìõ±•¸¡¹½‘•Ì¤°(€€€€€€€€€€€ÍÕ••‘•‘}Ñ…Í­ÌõÍÕ´¡¸¹ÍÑ…ÑÕÌ€ôôQ…Í­9½‘•MÑ…ÑÕÌ¹MU™½È¸¥¸¹½‘•Ì¤°(€€€€€€€€€€€™…¥±•‘}Ñ…Í­ÌõÍÕ´¡¸¹ÍÑ…ÑÕÌ€ôôQ…Í­9½‘•MÑ…ÑÕÌ¹%1™½È¸¥¸¹½‘•Ì¤°(€€€€€€€€€€€Ù•É¥™¥…Ñ¥½¹}Ù•É‘¥ÐõÙ•É¥™¥…Ñ¥½¸¹•Ð ‰Ù•É‘¥Ðˆ°€ˆˆ¤°(€€€€€€€€€€€É½Õ¹‘ÌõÍÑ…Ñ”¹•Ð ‰‘¥ÍÁ…Ñ¡}É½Õ¹‘Ìˆ°€À¤°(€€€€€€€€€€€•ÉÉ½ÈõÍÑ…Ñ”¹•Ð ‰•ÉÉ½Èˆ¤°(€€€€€€€€€€€ÍÕµµ…ÉäõÍÑ…Ñ”¹•Ð ‰™¥¹…±}½ÕÑÁÕÐˆ¤½ÈÙ•É¥™¥…Ñ¥½¸¹•Ð ‰ÍÕµµ…Éäˆ°€ˆˆ¤°(€€€€€€€€¤(()‘•˜ÉÕ¹}½Ù•É¹• (€€€€¨°(€€€½…°èÍÑÈ°(€€€É•ÅÕ•ÍÑ•‘}µ½‘”èÍÑÈ°(€€€Á±…¹¹•Èè¹ä°(€€€•á•ÕÑ½Èè¹ä°(€€€Ù•É¥™¥•Èè¹ä°(€€€ÑàèQ•…µIÕ¹½¹Ñ•áÐ°(€€€…¹•±}•Ù•¹Ðè¹äð9½¹”€ô9½¹”°(€€€Ñ…Í­}É…Á èQ…Í­É…Á ð9½¹”€ô9½¹”°(€€€É•ÍÕµ”è‰½½°€ô…±Í”°(€€€É•ÍÕµ•}‘•¥Í¥½¸è‘¥ÑmÍÑÈ°¹åtð9½¹”€ô9½¹”°(€€€µ…á}É½Õ¹‘Ìè¥¹Ð€ô€ÌÀ°(€€€µ…á}É•Á…¥É}É½Õ¹‘Ìè¥¹Ðð9½¹”€ô9½¹”°(¤€´ø=É¡•ÍÑÉ…Ñ¥½¹I•ÍÕ±Ðè(€€€É•ÑÕÉ¸½Ù•É¹•‘IÕ¹É…Á  (€€€€€€€ÑàõÑà°(€€€€€€€Á±…¹¹•ÈõÁ±…¹¹•È°(€€€€€€€•á•ÕÑ½Èõ•á•ÕÑ½È°(€€€€€€€Ù•É¥™¥•ÈõÙ•É¥™¥•È°(€€€€€€€…¹•±}•Ù•¹Ðõ…¹•±}•Ù•¹Ð°(€€€€€€€Ñ…Í­}É…Á õÑ…Í­}É…Á °(€€€€€€€µ…á}É½Õ¹‘Ìõµ…á}É½Õ¹‘Ì°(€€€€€€€µ…á}É•Á…¥É}É½Õ¹‘Ìõµ…á}É•Á…¥É}É½Õ¹‘Ì°(€€€€¤¹¥¹Ù½­” (€€€€€€€½…°õ½…°°(€€€€€€€É•ÅÕ•ÍÑ•‘}µ½‘”õÉ•ÅÕ•ÍÑ•‘}µ½‘”°(€€€€€€€É•ÍÕµ”õÉ•ÍÕµ”°(€€€€€€€É•ÍÕµ•}‘•¥Í¥½¸õÉ•ÍÕµ•}‘•¥Í¥½¸°(€€€€¤