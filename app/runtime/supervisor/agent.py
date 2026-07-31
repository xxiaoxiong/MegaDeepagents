"""Structured supervisor decisions without direct state mutation."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.runs.models import RunMode, SupervisorDecision

logger = logging.getLogger(__name__)

# Known capabilities that agent profiles actually possess.
# The LLM sometimes returns fabricated capability names (e.g.
# ``api_interaction``, ``frontend``, ``backend``) that match no registered
# agent profile, causing ``no_matching_worker`` failures at dispatch time.
# This set mirrors ``planner.py``'s PRIMARY_CAPS | TOOL_CAPS.
_KNOWN_PRIMARY_CAPS = frozenset({
    "planning", "research", "coding", "testing",
    "reviewing", "summarization",
})
_KNOWN_TOOL_CAPS = frozenset({
    "file_read", "file_write", "shell_execute",
    "web_research", "mcp_access", "default",
})
_KNOWN_CAPS = _KNOWN_PRIMARY_CAPS | _KNOWN_TOOL_CAPS

# Common LLM-hallucinated capability names → valid equivalent.
_CAP_SYNONYMS: dict[str, str] = {
    "api_interaction": "coding",
    "api": "coding",
    "frontend": "coding",
    "backend": "coding",
    "fullstack": "coding",
    "full_stack": "coding",
    "implementation": "coding",
    "implement": "coding",
    "develop": "coding",
    "development": "coding",
    "design": "planning",
    "architecture": "planning",
    "architect": "planning",
    "deploy": "coding",
    "deployment": "coding",
    "devops": "coding",
    "debug": "testing",
    "debugging": "testing",
    "qa": "testing",
    "document": "summarization",
    "documentation": "summarization",
    "writing": "summarization",
}


def _sanitize_capabilities(caps: list[str]) -> list[str]:
    """Filter and map LLM-returned capabilities to known ones.

    Unknown capabilities are mapped via ``_CAP_SYNONYMS``; if no synonym
    exists they are dropped.  Ensures at least one primary capability
    remains so the scheduler can find a matching worker.
    """
    if not caps:
        return ["coding"]

    sanitized: list[str] = []
    for cap in caps:
        cap_lower = cap.lower().strip()
        if cap_lower in _KNOWN_CAPS:
            sanitized.append(cap_lower)
        elif cap_lower in _CAP_SYNONYMS:
            mapped = _CAP_SYNONYMS[cap_lower]
            if mapped not in sanitized:
                sanitized.append(mapped)
                logger.warning(
                    "[Supervisor] mapped unknown capability %r → %r",
                    cap, mapped,
                )
        else:
            logger.warning(
                "[Supervisor] dropped unknown capability %r (not in known set)",
                cap,
            )

    # Ensure at least one primary capability
    has_primary = any(c in _KNOWN_PRIMARY_CAPS for c in sanitized)
    if not has_primary:
        sanitized.insert(0, "coding")

    # Deduplicate while preserving order
    seen: set[str] = set()
    result = [c for c in sanitized if not (c in seen or seen.add(c))]
    return result


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
            decision = self._model_decision(goal)
        except Exception as exc:
            selected = self._heuristic_mode(goal)
            decision = self._explicit_decision(selected, goal)
            decision.reason_summary = (
                f"Supervisor model unavailable; deterministic routing selected "
                f"{selected}: {type(exc).__name__}"
            )
            decision.payload["degraded_routing"] = True
            return decision

        # Sanitize LLM-returned capabilities to prevent no_matching_worker
        # failures.  The LLM occasionally invents capability names like
        # "api_interaction" that match no registered agent profile.
        original_caps = list(decision.required_capabilities)
        decision.required_capabilities = _sanitize_capabilities(
            decision.required_capabilities
        )
        if decision.required_capabilities != original_caps:
            logger.info(
                "[Supervisor] capabilities sanitized: %s → %s",
                original_caps, decision.required_capabilities,
            )
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
            "advisory and cannot mutate task state.\n\n"
            "IMPORTANT: required_capabilities must be one of: "
            "planning, research, coding, testing, reviewing, summarization, "
            "file_read, file_write, shell_execute, web_research, mcp_access. "
            "Do not use other capability names.\n\nGoal:\n" + goal
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
            "前后端", "fullstack", "full stack", "项目",
        )
        return "team" if len(goal) > 500 or sum(s in lowered for s in team_signals) >= 2 else "single"

    @staticmethod
    def _capabilities(goal: str) -> list[str]:
        lowered = goal.lower()
        if any(word in lowered for word in ("code", "代码", "实现", "修复", "refactor", "重构", "项目", "前后端")):
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
