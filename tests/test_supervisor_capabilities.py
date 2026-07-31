"""Tests for SupervisorAgent capability sanitization.

Regression: run_a88956be58554c3c failed with
``no_matching_worker for task=execute capabilities=['api_interaction']``
because the LLM returned ``api_interaction`` as a capability, which matches
no registered agent profile.  The supervisor now sanitizes capabilities
to known values before returning the decision.
"""
from __future__ import annotations

from app.domain.runs.models import RunMode, SupervisorDecision
from app.runtime.supervisor.agent import (
    SupervisorAgent,
    _sanitize_capabilities,
)


# ===== _sanitize_capabilities =====


def test_sanitize_drops_unknown_capability():
    """Unknown capabilities with no synonym are dropped."""
    result = _sanitize_capabilities(["api_interaction"])
    # api_interaction has a synonym → mapped to coding
    assert result == ["coding"]


def test_sanitize_maps_synonyms():
    """Common LLM-hallucinated names are mapped to valid capabilities."""
    assert _sanitize_capabilities(["frontend"]) == ["coding"]
    assert _sanitize_capabilities(["backend"]) == ["coding"]
    assert _sanitize_capabilities(["architecture"]) == ["planning"]
    assert _sanitize_capabilities(["debug"]) == ["testing"]
    assert _sanitize_capabilities(["documentation"]) == ["summarization"]


def test_sanitize_keeps_known_caps():
    """Known capabilities pass through unchanged."""
    result = _sanitize_capabilities(["coding", "file_read"])
    assert result == ["coding", "file_read"]


def test_sanitize_deduplicates():
    """Duplicate capabilities after mapping are removed."""
    result = _sanitize_capabilities(["coding", "frontend", "backend"])
    assert result == ["coding"]


def test_sanitize_empty_returns_coding():
    """Empty capability list defaults to coding."""
    assert _sanitize_capabilities([]) == ["coding"]


def test_sanitize_ensures_primary_cap():
    """If only tool caps remain, a primary cap is added."""
    result = _sanitize_capabilities(["file_read", "shell_execute"])
    assert "coding" in result
    assert "file_read" in result
    assert "shell_execute" in result


def test_sanitize_truly_unknown_dropped():
    """Capabilities with no synonym and not known are dropped."""
    result = _sanitize_capabilities(["totally_unknown", "coding"])
    assert "totally_unknown" not in result
    assert "coding" in result


def test_sanitize_all_unknown_maps_to_coding():
    """All-unknown capabilities still produce a valid primary cap."""
    result = _sanitize_capabilities(["frontend", "backend", "api"])
    assert result == ["coding"]


# ===== SupervisorAgent.decide sanitizes LLM output =====


class _StubModel:
    """Returns a fixed SupervisorDecision to simulate LLM output."""

    def __init__(self, decision: SupervisorDecision):
        self._decision = decision

    def with_structured_output(self, _schema):
        class _Inner:
            def invoke(_self, _prompt):
                return self._decision
        return _Inner()


def test_supervisor_decide_sanitizes_llm_capabilities():
    """SupervisorAgent.decide should sanitize capabilities from LLM output.

    Regression: run_a88956be58554c3c — LLM returned ``api_interaction``
    which caused ``no_matching_worker`` failure.  The decide() method
    must map it to a known capability before returning.
    """
    bad_decision = SupervisorDecision(
        action="create_tasks",
        selected_mode="team",
        required_capabilities=["api_interaction"],
        reason_summary="LLM returned bad capability",
    )
    agent = SupervisorAgent(model=_StubModel(bad_decision))
    result = agent.decide("构建一个前后端项目", RunMode.AUTO)
    assert "api_interaction" not in result.required_capabilities
    assert "coding" in result.required_capabilities


def test_supervisor_decide_keeps_valid_capabilities():
    """Valid capabilities from LLM should pass through unchanged."""
    good_decision = SupervisorDecision(
        action="create_tasks",
        selected_mode="team",
        required_capabilities=["coding", "testing"],
        reason_summary="Valid capabilities",
    )
    agent = SupervisorAgent(model=_StubModel(good_decision))
    result = agent.decide("构建项目", RunMode.AUTO)
    assert "coding" in result.required_capabilities
    assert "testing" in result.required_capabilities


def test_supervisor_heuristic_team_mode_for_project():
    """'构建一个前后端项目' should trigger team mode (前后端 + 项目 signals)."""
    agent = SupervisorAgent(model=None)
    mode = agent._heuristic_mode("构建一个前后端项目")
    assert mode == "team"


def test_supervisor_heuristic_coding_for_project():
    """'构建一个前后端项目' should map to coding capability."""
    caps = SupervisorAgent._capabilities("构建一个前后端项目")
    assert "coding" in caps
