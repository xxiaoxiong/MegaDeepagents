"""Structured supervisor decisions without direct state mutation."""

from __future__ import annotations

import json
from typing import Any

from app.domain.runs.models import RunMode, SupervisorDecision


class SupervisorAgent:
    """Choose the execution shape and capabilities for one run.

    Explicit user modes are authoritative.  Auto mode asks the configured
    model for structured output and falls back to deterministic complexity
    routing only when the model is unavailable.  This fallback selects a path;
    it never fabricates worker output or marks a run successful.
    """

    def __init__(self, model: Any | None = None) -> None:
        self._model = model

    def decide(self, goal: str, requested_mode: RunMode) -> SupervisorDecision:
        if requested_mode == RunMode.SINGLE:
            return self._explicit_decision("single", goal)
        if requested_mode == RunMode.TEAM:
            return self._explicit_decision("team", goal)
        try:
            return self._model_decision(goal)
        except Exception as exc:
            selected = self._heuristic_mode(goal)
            decision = self._explicit_decision(selected, goal)
            decision.reason_summary = (
                f"Supervisor model unavailable; deterministic routing selected "
                f"{selected}: {type(exc).__name__}"
            )
            decision.payload["degraded_routing"] = True
            return decision

    def _model_decision(self, goal: str) -> SupervisorDecision:
        model = self._model
        if model is None:
            from app.llm_factory import build_aux_model

            model = build_aux_model()
        prompt = (
            "Choose single or team execution for the goal. Use team when work "
            "benefits from separate planning, research, implementation, testing, "
            "or review. Return a structured SupervisorDecision. The decision is "
            "advisory and cannot mutate task state.\n\nGoal:\n" + goal
        )
        structured = getattr(model, "with_structured_output", None)
        if callable(structured):
            result = structured(SupervisorDecision).invoke(prompt)
            return (
                result
                if isinstance(result, SupervisorDecision)
                else SupervisorDecision.model_validate(result)
            )
        response = model.invoke([
            ("system", "Return only valid JSON for SupervisorDecision."),
            ("user", prompt),
        ])
        content = getattr(response, "content", response)
        if isinstance(content, str):
            content = json.loads(content)
        return SupervisorDecision.model_validate(content)

    @staticmethod
    def _heuristic_mode(goal: str) -> str:
        lowered = goal.lower()
        team_signals = (
            "refactor", "架构", "重构", "repository", "仓库", "test", "测试",
            "review", "评审", "deploy", "部署", "research", "调研",
        )
        return "team" if len(goal) > 500 or sum(s in lowered for s in team_signals) >= 2 else "single"

    @staticmethod
    def _capabilities(goal: str) -> list[str]:
        lowered = goal.lower()
        if any(word in lowered for word in ("code", "代码", "实现", "修复", "refactor", "重构")):
            return ["coding"]
        if any(word in lowered for word in ("research", "调研", "分析", "资料")):
            return ["research"]
        return ["summarization"]

    def _explicit_decision(self, mode: str, goal: str) -> SupervisorDecision:
        return SupervisorDecision(
            action="create_tasks" if mode == "team" else "dispatch",
            selected_mode=mode,
            required_capabilities=self._capabilities(goal),
            reason_summary=f"Requested execution mode resolved to {mode}",
        )
