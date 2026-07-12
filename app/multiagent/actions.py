"""Typed AgentAction Protocol（Pydantic Discriminated Union）。

Replace `dict[str, Any]` action protocol（actions.py §十一）。

每种 Action：
- 有独立字段（不再把一切塞进 content/type）
- 有 Schema 校验（Pydantic）
- 有权限路由（role_allowed）
- 有幂等键（idempotency_key）
- 审计数据（produced_by, produced_at, trace_id）

非法 Action 不能继续产生副作用（ActionGuard 已有的能力 + Pydantic 层面拦截）。

设计目标：
- 向后兼容：提供 to_dict() / from_legacy_dict() 桥接
- 现有 runtime_adapter 可逐步迁移
"""
from __future__ import annotations

import copy as _copy
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class ArtifactRef(BaseModel):
    """产物引用（跨 Action 共享）。"""

    path: str = ""
    artifact_id: str | None = None
    role: str = ""
    version: int = 1
    message_id: str | None = None


class ReviewResultContent(BaseModel):
    """review_result 的审核结果。"""

    passed: bool = False
    issues: list[dict[str, Any]] = Field(default_factory=list)
    required_fix_owner: str | None = None


class EvidenceItem(BaseModel):
    """每条证据引用。"""

    source: str = ""
    content: str = ""


# ===== 各 Action 类型 =====


class SendMessageAction(BaseModel):
    """向另一位 Agent 发送消息。"""

    type: Literal["send_message"] = "send_message"
    to_agent: str = ""
    message_type: str = "observation"
    content: str = ""
    requires_response: bool = False
    reply_to: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)

    # 审计
    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class CreateArtifactAction(BaseModel):
    """创建/更新 Artifact。"""

    type: Literal["create_artifact"] = "create_artifact"
    artifact_path: str = ""
    artifact_role: str = "artifact"
    content: str = ""
    artifact_id: str | None = None
    version: int = 1
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateStateAction(BaseModel):
    """更新 SharedTeamState 的部分字段（patch 模式）。"""

    type: Literal["update_state"] = "update_state"
    patch: dict[str, Any] = Field(default_factory=dict)

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class RequestReviewAction(BaseModel):
    """请求评审。"""

    type: Literal["request_review"] = "request_review"
    to_agent: str = "ReviewerAgent"
    content: str = ""
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class RespondCritiqueAction(BaseModel):
    """响应评审意见（修复后回复）。"""

    type: Literal["respond_critique"] = "respond_critique"
    to_agent: str = "ReviewerAgent"
    content: str = ""
    issue_id: str | None = None
    issue_status: str | None = None

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class HandoffAction(BaseModel):
    """将控制权移交给另一位 Agent。"""

    type: Literal["handoff"] = "handoff"
    to_agent: str = ""
    content: str = ""

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class MarkDoneAction(BaseModel):
    """宣布任务完成（仅 Finalizer 允许）。"""

    type: Literal["mark_done"] = "mark_done"
    content: str = ""
    final_output: str = ""

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


class NoOpAction(BaseModel):
    """本轮无操作（必须附理由）。"""

    type: Literal["no_op"] = "no_op"
    content: str = ""
    # 越权拦截信息（ActionGuard 填入）
    rejected_action_type: str | None = None
    rejected_action: dict[str, Any] | None = None

    produced_by: str = ""
    produced_at: datetime = Field(default_factory=datetime.utcnow)
    idempotency_key: str = ""


# ===== Discriminated Union =====

AgentAction = Annotated[
    SendMessageAction
    | CreateArtifactAction
    | UpdateStateAction
    | RequestReviewAction
    | RespondCritiqueAction
    | HandoffAction
    | MarkDoneAction
    | NoOpAction,
    Field(discriminator="type"),
]


# ===== 转换工具 =====


def action_from_dict(d: dict[str, Any]) -> AgentAction:
    """从遗留 dict 转换为 typed Action。

    根据 'type' 字段分发到具体类的构造器。
    未知 type → 抛出 ValueError（不让未知字段静默通过）。
    """
    action_type = d.get("type", "")
    raw = dict(d)  # 拷贝，不修改原始

    # 所有 action 都可以有审计字段
    produced_by = raw.pop("produced_by", "")
    idempotency_key = raw.pop("idempotency_key", "")
    produced_at = raw.pop("produced_at", None)

    # 清理已知在运行时注入但不在构造器中的额外字段
    raw.pop("langsmith_trace", None)

    if action_type == "send_message":
        evidence_raw = raw.pop("evidence", [])
        evidence = [
            EvidenceItem(**e) if isinstance(e, dict) else EvidenceItem(source=str(e))
            for e in evidence_raw
        ]
        action = SendMessageAction(
            to_agent=raw.get("to_agent", ""),
            message_type=raw.get("message_type", "observation"),
            content=raw.get("content", ""),
            requires_response=raw.get("requires_response", False),
            reply_to=raw.get("reply_to"),
            evidence=evidence,
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "create_artifact":
        refs_raw = raw.pop("artifact_refs", [])
        refs = [
            ArtifactRef(**r) if isinstance(r, dict) else ArtifactRef(path=str(r))
            for r in refs_raw
        ]
        action = CreateArtifactAction(
            artifact_path=raw.get("artifact_path", raw.get("content", "")),
            artifact_role=raw.get("artifact_role", "artifact"),
            content=raw.get("content", ""),
            artifact_id=raw.get("artifact_id"),
            version=raw.get("version", 1),
            artifact_refs=refs,
            produced_by=produced_by,
            idempotency_key=idempotency_key,
            metadata=raw.get("metadata", {}),
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "update_state":
        action = UpdateStateAction(
            patch=raw.get("patch", {}),
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "request_review":
        refs_raw = raw.pop("artifact_refs", [])
        refs = [
            ArtifactRef(**r) if isinstance(r, dict) else ArtifactRef(path=str(r))
            for r in refs_raw
        ]
        action = RequestReviewAction(
            to_agent=raw.get("to_agent", "ReviewerAgent"),
            content=raw.get("content", ""),
            artifact_refs=refs,
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "respond_critique":
        action = RespondCritiqueAction(
            to_agent=raw.get("to_agent", "ReviewerAgent"),
            content=raw.get("content", ""),
            issue_id=raw.get("issue_id"),
            issue_status=raw.get("issue_status"),
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "handoff":
        action = HandoffAction(
            to_agent=raw.get("to_agent", ""),
            content=raw.get("content", ""),
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "mark_done":
        action = MarkDoneAction(
            content=raw.get("content", ""),
            final_output=raw.get("final_output", raw.get("content", "")),
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    elif action_type == "no_op":
        action = NoOpAction(
            content=raw.get("content", ""),
            rejected_action_type=raw.get("rejected_action_type"),
            rejected_action=raw.get("rejected_action"),
            produced_by=produced_by,
            idempotency_key=idempotency_key,
        )
        if produced_at:
            action.produced_at = produced_at
        return action

    else:
        raise ValueError(f"未知 action type: {action_type!r}")


def actions_from_legacy_list(actions: list[dict[str, Any]]) -> list[AgentAction]:
    """批量转换遗留 dict 列表到 typed Actions。"""
    return [action_from_dict(a) for a in actions]


def action_to_dict(action: AgentAction) -> dict[str, Any]:
    """将 typed Action 转回 dict（向后兼容序列化）。"""
    return action.model_dump(exclude_none=False)


def actions_to_legacy_list(actions: list[AgentAction]) -> list[dict[str, Any]]:
    """批量转换 typed Actions 到 dict 列表。"""
    return [action_to_dict(a) for a in actions]


# ===== 角色权限定义（typed 版本，与 action_guard.py 互补） =====

# 每个角色允许的 action type 列表
ROLE_ALLOWED_ACTION_TYPES: dict[str, set[str]] = {
    "Planner": {"send_message", "update_state", "handoff", "no_op"},
    "Coder": {"send_message", "create_artifact", "request_review", "handoff", "no_op"},
    "Tester": {"send_message", "create_artifact", "handoff", "no_op"},
    "ReviewerAgent": {"send_message", "request_review", "respond_critique", "no_op"},
    "Reviewer": {"send_message", "request_review", "respond_critique", "no_op"},
    "Finalizer": {"send_message", "update_state", "respond_critique", "mark_done", "no_op"},
    "Researcher": {"send_message", "create_artifact", "handoff", "no_op"},
}


def get_role_allowed_action_types(role: str) -> set[str]:
    """取得角色允许的 action type 集合（空 = 全开）。"""
    return ROLE_ALLOWED_ACTION_TYPES.get(role, set())


def is_action_type_allowed_for_role(role: str, action_type: str) -> bool:
    allowed = get_role_allowed_action_types(role)
    if not allowed:
        return True  # 未定义 = 默认允许（向后兼容）
    return action_type in allowed
