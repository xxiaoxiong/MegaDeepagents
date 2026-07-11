"""ConflictResolver 测试。"""

from __future__ import annotations

from app.multiagent.conflict_resolver import (
    ConflictLevel,
    ConflictResolver,
    ConflictType,
)
from app.multiagent.state import SharedTeamState


def test_reviewer_veto_wins():
    """Reviewer 否决 Reviewer 意见为最终。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "ReviewerAgent", "position": False, "reason": "不通过"},
        {"agent": "Coder", "position": True, "reason": "我觉得没问题"},
    ]
    r = resolver.resolve(ConflictType.REVIEW_DISAGREEMENT, "评审不通过", positions)
    assert r.resolved is True
    assert r.decision == "reviewer_veto"


def test_reviewer_binding_when_no_explicit_veto():
    """Reviewer 已表态但未明确否决：按其立场为最终。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "ReviewerAgent", "position": "approved_with_comments", "reason": "建议优化"},
        {"agent": "Coder", "position": "rejected", "reason": "我不改"},
    ]
    r = resolver.resolve(ConflictType.REVIEW_DISAGREEMENT, "争议", positions)
    assert r.resolved is True
    assert r.decision == "reviewer_opinion_binding"


def test_review_absent_escalates_high():
    """Reviewer 没参与但出现评审争议：升级 HITL（无 state 时直接 escalation）。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "ok", "reason": "我自己测过没问题"},
        {"agent": "Tester", "position": "not_ok", "reason": "测试失败"},
    ]
    r = resolver.resolve(ConflictType.REVIEW_DISAGREEMENT, "评审争议", positions)
    assert r.resolved is False
    assert r.escalate_to_hitl is True


def test_planner_decides_route():
    """Planner 在路线冲突中裁决。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "用方案A", "reason": "易实现"},
        {"agent": "Tester", "position": "用方案B", "reason": "易测试"},
        {"agent": "Planner", "position": "用方案A", "reason": "符合进度"},
    ]
    r = resolver.resolve(ConflictType.PLAN_ROUTE_CONFLICT, "路线选择", positions)
    assert r.resolved is True
    assert "方案A" in r.decision or r.decision == "用方案A"


def test_planner_absent_escalates_medium():
    """Planner 未表态，路线冲突升级为中级别。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "A"},
        {"agent": "Tester", "position": "B"},
    ]
    r = resolver.resolve(ConflictType.PLAN_ROUTE_CONFLICT, "路线", positions)
    assert r.resolved is False


def test_priority_safety_first():
    """优先级冲突：涉及安全立场胜出。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "A", "reason": "快"},
        {"agent": "ReviewerAgent", "position": "B", "reason": "B 更安全"},
    ]
    r = resolver.resolve(ConflictType.PRIORITY_CONFLICT, "优先级", positions)
    assert r.resolved is True
    assert r.decision == "B"


def test_priority_function_first():
    """无安全冲突时按功能优先。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "A", "reason": "易实现"},
        {"agent": "Tester", "position": "B", "reason": "易测试"},
    ]
    r = resolver.resolve(ConflictType.PRIORITY_CONFLICT, "优先级", positions)
    assert r.resolved is True
    # 不含"安全"时按功能优先（默认决策 A）
    assert "功能" in r.reason


def test_ownership_producer_responsible():
    """谁产生谁负责。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "role": "producer", "position": "OK"},
        {"agent": "ReviewerAgent", "position": "需修"},
    ]
    r = resolver.resolve(ConflictType.OWNERSHIP_DISAGREEMENT, "谁修", positions)
    assert r.resolved is True
    assert "Coder" in r.decision


def test_ownership_escalates_when_no_producer():
    """没有 producer 信息：升级到高冲突。"""
    resolver = ConflictResolver()
    positions = [
        {"agent": "Coder", "position": "不是我"},
        {"agent": "Tester", "position": "也不是我"},
    ]
    r = resolver.resolve(ConflictType.OWNERSHIP_DISAGREEMENT, "责任不清", positions)
    assert r.resolved is False
    assert r.escalate_to_hitl is True


def test_escalation_creates_blocking_issue_in_state():
    """LLM 不可用时，OTHER 冲突升级 HITL 并在 state 中创建 blocking issue。"""
    from unittest.mock import patch, MagicMock

    state = SharedTeamState(room_id="r1", task_id="t1")
    resolver = ConflictResolver(state=state)
    positions = [
        {"agent": "A", "position": "x"},
        {"agent": "B", "position": "y"},
    ]
    # 强制 LLM 不可用，强制走 HITL 升级路径
    with patch("app.llm_factory.build_model", side_effect=RuntimeError("no key")):
        r = resolver.resolve(ConflictType.OTHER, "无法自动裁决", positions)
    assert r.escalate_to_hitl is True
    assert r.created_issue_id is not None
    # state 中应有一个 issue
    assert len(state.issues) == 1
    assert state.issues[0].id == r.created_issue_id


def test_unknown_conflict_type_treated_as_other():
    """未知 conflict_type 字符串归到 OTHER。LLM 不可用时走升级路径。"""
    from unittest.mock import patch

    resolver = ConflictResolver()
    with patch("app.llm_factory.build_model", side_effect=RuntimeError("no key")):
        r = resolver.resolve("totally_unknown", "x", [{"agent": "A"}])
    # OTHER 走升级路径
    assert r.escalate_to_hitl is True
