"""Conflict Resolver：多 Agent 意见冲突裁决模块。

设计策略（P2-1）：
1. 规则优先：Reviewer 对质量问题有最终否决权（QA veto）
2. Planner 负责流程裁决（遇到并行路线选择时决策）
3. 冲突超出阈值 → 升级到 HITL（Human-in-the-Loop）
4. 高冲突场景可记录为 TeamDecision，供审计

当前版本实现规则引擎 + HITL 升级接口。LLM 裁决为可选 fallback。

冲突类型：
- review_disagreement: Reviewer 不通过，Coder/Planner 认为没问题
- plan_route_conflict: 同一目标有多个实现路线，无法一致
- priority_conflict: 安全/功能/性能优先级不一致
- ownership_disagreement: 修复责任人不一致
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.core.logging import logger
from app.multiagent.state import (
    IssueSeverity,
    IssueStatus,
    SharedTeamState,
    TeamDecision,
    TeamIssue,
)


class ConflictType(str, Enum):
    REVIEW_DISAGREEMENT = "review_disagreement"
    PLAN_ROUTE_CONFLICT = "plan_route_conflict"
    PRIORITY_CONFLICT = "priority_conflict"
    OWNERSHIP_DISAGREEMENT = "ownership_disagreement"
    OTHER = "other"


class ConflictLevel(str, Enum):
    LOW = "low"          # 可规则自动裁决
    MEDIUM = "medium"    # 需 Planner 裁决
    HIGH = "high"        # 需 Supervisor / HITL


class Resolution:
    """单次冲突裁决结果。"""

    __slots__ = (
        "resolved",
        "decision",
        "reason",
        "decided_by",
        "escalate_to_hitl",
        "created_issue_id",
        "created_decision_id",
    )

    def __init__(
        self,
        resolved: bool = False,
        decision: str = "",
        reason: str = "",
        decided_by: str = "",
        escalate_to_hitl: bool = False,
        created_issue_id: str | None = None,
        created_decision_id: str | None = None,
    ):
        self.resolved = resolved
        self.decision = decision
        self.reason = reason
        self.decided_by = decided_by
        self.escalate_to_hitl = escalate_to_hitl
        self.created_issue_id = created_issue_id
        self.created_decision_id = created_decision_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "decision": self.decision,
            "reason": self.reason,
            "decided_by": self.decided_by,
            "escalate_to_hitl": self.escalate_to_hitl,
            "created_issue_id": self.created_issue_id,
            "created_decision_id": self.created_decision_id,
        }


class ConflictResolver:
    """冲突裁决器。规则优先，可升级 HITL。"""

    def __init__(self, state: SharedTeamState | None = None):
        self.state = state

    def set_state(self, state: SharedTeamState) -> None:
        self.state = state

    # ========== 规则引擎 ==========

    def resolve(
        self,
        conflict_type: ConflictType | str,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> Resolution:
        """裁决一次冲突。根据冲突类型走规则引擎，规则兜不住时升级。

        Args:
            conflict_type: 冲突类型
            description: 冲突描述
            positions: 各方立场，例如 [
                {"agent": "ReviewerAgent", "position": "不通过", "reason": "缺少测试"},
                {"agent": "Coder", "position": "功能完整", "reason": "测试已有"},
            ]
            context: 额外上下文（phase, artifacts, 等）
        """
        if isinstance(conflict_type, str):
            try:
                conflict_type = ConflictType(conflict_type)
            except ValueError:
                conflict_type = ConflictType.OTHER

        # === 规则 1：Reviewer 质量否决权 ===
        if conflict_type == ConflictType.REVIEW_DISAGREEMENT:
            return self._resolve_review_disagreement(positions, context or {})

        # === 规则 2：路线冲突由 Planner 裁决 ===
        if conflict_type == ConflictType.PLAN_ROUTE_CONFLICT:
            return self._resolve_plan_route(description, positions, context or {})

        # === 规则 3：优先级冲突 ===
        if conflict_type == ConflictType.PRIORITY_CONFLICT:
            return self._resolve_priority(description, positions, context or {})

        # === 规则 4：责任人冲突 ===
        if conflict_type == ConflictType.OWNERSHIP_DISAGREEMENT:
            return self._resolve_ownership(positions)

        # === 兜底：升级 HITL ===
        return self._escalate(conflict_type, description, positions, context or {})

    def _resolve_review_disagreement(
        self,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> Resolution:
        """Reviewer 对质量问题有最终否决权。

        只要 Reviewer 投不通过，无论其他 Agent 持什么意见，都走返工。
        这避免了"Coder 觉得自己代码没问题就不修"的死锁。
        """
        reviewer_positions = [
            p for p in positions
            if p.get("agent", "").lower() in ("revieweragent", "reviewer")
        ]
        non_reviewer_positions = [
            p for p in positions
            if p.get("agent", "").lower() not in ("revieweragent", "reviewer")
        ]

        # 是否有 Reviewer 明确不通过
        reviewer_veto = any(
            not p.get("position", True) or "不通过" in str(p.get("reason", ""))
            for p in reviewer_positions
        )

        if reviewer_veto:
            logger.info(
                f"[ConflictResolver] Reviewer 否决，冲突按 Reviewer 意见裁决：返工"
            )
            return Resolution(
                resolved=True,
                decision="reviewer_veto",
                reason="Reviewer 对质量有最终否决权：产物未达到评审标准，需要返工修复",
                decided_by="ConflictResolver(rule:reviewer_veto)",
            )

        # 如果 Reviewer 没明确反对，但其他 Agent 有争议：按 Reviewer 意见为准
        if reviewer_positions:
            return Resolution(
                resolved=True,
                decision="reviewer_opinion_binding",
                reason="Reviewer 意见为最终依据，其他 Agent 异议已记录为 issue 供后续参考",
                decided_by="ConflictResolver(rule:reviewer_binding)",
            )

        # 没有 Reviewer 参与：升级为高冲突
        return self._escalate(
            ConflictType.REVIEW_DISAGREEMENT,
            "Reviewer 未参与评审，但存在评审争议",
            positions,
            context,
            forced_level=ConflictLevel.HIGH,
        )

    def _resolve_plan_route(
        self,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> Resolution:
        """Planner 对路线冲突有流程裁决权。"""
        planner_positions = [
            p for p in positions
            if "planner" in p.get("agent", "").lower()
        ]

        if planner_positions:
            # 取 Planner 的最后一条立场
            planner_choice = planner_positions[-1].get("position", "")
            logger.info(
                f"[ConflictResolver] Planner 裁决路线冲突：{planner_choice}"
            )
            return Resolution(
                resolved=True,
                decision=planner_choice,
                reason=f"Planner 按流程裁决路线冲突",
                decided_by=f"ConflictResolver(rule:planner_route)",
            )

        # Planner 未参与 → 升级中级别冲突
        return self._escalate(
            ConflictType.PLAN_ROUTE_CONFLICT,
            description if description else "路线冲突需 Planner 裁决，但 Planner 未表达立场",
            positions,
            context,
            forced_level=ConflictLevel.MEDIUM,
        )

    def _resolve_priority(
        self,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> Resolution:
        """优先级冲突：安全 > 功能 > 性能。"""
        # 简单规则：提到"安全"的立场优先
        for p in positions:
            reason = str(p.get("reason", "")).lower()
            if "安全" in reason or "security" in reason:
                return Resolution(
                    resolved=True,
                    decision=p.get("position", "按涉及安全的立场执行"),
                    reason="优先级规则：安全 > 功能 > 性能",
                    decided_by="ConflictResolver(rule:safety_first)",
                )
        # 没有安全冲突，按功能优先
        return Resolution(
            resolved=True,
            decision="按功能实现方向优先，性能优化可后补",
            reason="优先级规则：功能 > 性能",
            decided_by="ConflictResolver(rule:function_first)",
        )

    def _resolve_ownership(
        self,
        positions: list[dict[str, Any]],
    ) -> Resolution:
        """责任人冲突：按 Who-produced-it 归属逻辑。

        谁产生的 issue，谁负责修复。
        若产生的 Agent 无法修复（如退出/能力不足），升级到 Supervisor。
        """
        # 找谁产生了问题
        for p in positions:
            if p.get("role") in ("producer", "owner", "assignee"):
                return Resolution(
                    resolved=True,
                    decision=f"责任人：{p.get('agent', '未指定')}",
                    reason="谁产生谁负责原则",
                    decided_by="ConflictResolver(rule:producer_responsible)",
                )
        # 无明确 producer：分不清时 Reviewer 指定的 fix_owner 优先
        reviewer_assign = [
            p for p in positions
            if "reviewer" in p.get("agent", "").lower() and p.get("fix_owner")
        ]
        if reviewer_assign:
            return Resolution(
                resolved=True,
                decision=f"责任人：{reviewer_assign[0]['fix_owner']}",
                reason="Reviewer 指定的 fix_owner",
                decided_by="ConflictResolver(rule:reviewer_assign)",
            )
        return self._escalate(
            ConflictType.OWNERSHIP_DISAGREEMENT,
            "无法确定修复责任人，各方分歧",
            positions,
            {},
            forced_level=ConflictLevel.HIGH,
        )

    def _escalate(
        self,
        conflict_type: ConflictType,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
        forced_level: ConflictLevel | None = None,
    ) -> Resolution:
        """升级冲突：无法规则裁决时，建议 HITL。"""
        logger.warning(
            f"[ConflictResolver] 冲突升级 HITL: type={conflict_type.value}, "
            f"desc={description[:100]}"
        )
        resolution = Resolution(
            resolved=False,
            decision="HITL_REQUIRED",
            reason=(
                f"冲突类型 {conflict_type.value} 无法自动裁决：{description[:200]}"
            ),
            decided_by="ConflictResolver(escalation)",
            escalate_to_hitl=True,
        )

        # 在 state 中创建一个 blocking issue
        if self.state:
            import uuid
            issue_id = f"conflict_{uuid.uuid4().hex[:8]}"
            issue = TeamIssue(
                id=issue_id,
                title=f"冲突升级[{conflict_type.value}]：{description[:80]}",
                description=f"冲突详情：{description}\n各方立场：{positions}\n需要人工介入裁决",
                severity=IssueSeverity.HIGH,
                status=IssueStatus.OPEN,
                owner=None,  # HITL，无 owner
            )
            self.state.add_issue(issue)
            resolution.created_issue_id = issue_id

        return resolution
