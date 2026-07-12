"""Typed AgentAction protocol 单元测试。"""
from __future__ import annotations

import pytest

from app.multiagent.actions import (
    actions_from_legacy_list,
    action_from_dict,
    action_to_dict,
    get_role_allowed_action_types,
    is_action_type_allowed_for_role,
    SendMessageAction,
    CreateArtifactAction,
    UpdateStateAction,
    RequestReviewAction,
    RespondCritiqueAction,
    HandoffAction,
    MarkDoneAction,
    NoOpAction,
    ArtifactRef,
    EvidenceItem,
)


# ===== 区分联合构造 =====


def test_send_message_construction():
    a = SendMessageAction(
        to_agent="Coder",
        message_type="delegation",
        content="实现 X 模块",
        requires_response=True,
        produced_by="Planner",
        idempotency_key="k1",
    )
    assert a.type == "send_message"
    assert a.to_agent == "Coder"
    assert a.requires_response is True


def test_discriminator_routes_correct_type():
    a = action_from_dict({
        "type": "send_message",
        "to_agent": "Coder",
        "message_type": "plan",
        "content": "hi",
    })
    assert isinstance(a, SendMessageAction)

    b = action_from_dict({
        "type": "create_artifact",
        "artifact_path": "/tmp/x.py",
        "artifact_role": "code",
        "content": "print(1)",
    })
    assert isinstance(b, CreateArtifactAction)
    assert b.artifact_role == "code"


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        action_from_dict({"type": "totally_unknown", "content": "?"})


# ===== 各类型字段 =====


def test_update_state_action():
    a = UpdateStateAction(patch={"phase": "executing", "plan": "step 1"}, produced_by="Planner")
    assert a.type == "update_state"
    assert a.patch["phase"] == "executing"


def test_request_review_action():
    a = RequestReviewAction(
        to_agent="ReviewerAgent",
        content="请评审",
        artifact_refs=[ArtifactRef(path="/tmp/x.py")],
    )
    assert a.type == "request_review"
    assert len(a.artifact_refs) == 1


def test_respond_critique_action():
    a = RespondCritiqueAction(
        content="已修复",
        issue_id="iss-1",
        issue_status="resolved",
    )
    assert a.type == "respond_critique"
    assert a.issue_status == "resolved"


def test_handoff_action():
    a = HandoffAction(to_agent="Finalizer", content="请你收尾")
    assert a.type == "handoff"


def test_mark_done_action():
    a = MarkDoneAction(content="done", final_output="final answer")
    assert a.type == "mark_done"
    assert a.final_output == "final answer"


def test_noop_action_with_rejected_info():
    a = NoOpAction(content="blocked", rejected_action_type="mark_done")
    assert a.type == "no_op"
    assert a.rejected_action_type == "mark_done"


# ===== 证据/引用子模型 =====


def test_evidence_and_artifact_refs():
    a = SendMessageAction(
        to_agent="Reviewer",
        content="参考实验",
        evidence=[EvidenceItem(source="log", content="x=1")],
    )
    assert len(a.evidence) == 1
    assert a.evidence[0].source == "log"

    b = CreateArtifactAction(
        artifact_path="/tmp/y.py",
        artifact_role="code",
        artifact_refs=[ArtifactRef(path="/tmp/y.py", role="code", version=1)],
    )
    assert len(b.artifact_refs) == 1
    assert b.artifact_refs[0].version == 1


# ===== 序列化往返 =====


def test_round_trip_to_dict_and_back():
    original = SendMessageAction(
        to_agent="Coder", message_type="plan", content="hello",
        produced_by="Planner", idempotency_key="k1",
    )
    d = action_to_dict(original)
    assert d["type"] == "send_message"
    assert d["to_agent"] == "Coder"

    restored = action_from_dict(d)
    assert isinstance(restored, SendMessageAction)
    assert restored.to_agent == "Coder"
    assert restored.idempotency_key == "k1"


def test_batch_conversion():
    legacy = [
        {"type": "send_message", "to_agent": "Coder", "content": "hi"},
        {"type": "create_artifact", "artifact_path": "p", "artifact_role": "code"},
        {"type": "no_op", "content": "skip"},
    ]
    typed = actions_from_legacy_list(legacy)
    assert len(typed) == 3
    assert isinstance(typed[0], SendMessageAction)
    assert isinstance(typed[1], CreateArtifactAction)
    assert isinstance(typed[2], NoOpAction)

    # 回归
    back = [action_to_dict(a) for a in typed]
    assert back[0]["to_agent"] == "Coder"


# ===== 角色权限 =====


def test_role_allowed_action_types():
    assert "send_message" in get_role_allowed_action_types("Planner")
    assert "coding_skill" not in get_role_allowed_action_types("Planner")


def test_action_type_allowed_for_role():
    assert is_action_type_allowed_for_role("Planner", "send_message")
    # Planner 不允许 mark_done
    assert not is_action_type_allowed_for_role("Planner", "mark_done")
    # Finalizer 允许 mark_done
    assert is_action_type_allowed_for_role("Finalizer", "mark_done")
    # Reviewer 不允许 create_artifact
    assert not is_action_type_allowed_for_role("ReviewerAgent", "create_artifact")


def test_unknown_role_default_allow():
    """未定义角色的 action 默认 all-allow（向后兼容）。"""
    assert is_action_type_allowed_for_role("Custom", "send_message")
