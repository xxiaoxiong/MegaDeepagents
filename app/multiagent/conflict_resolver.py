"""Conflict Resolver：多 Agent 意见冲突裁决模块（实验性，未接入主链）。

设计策略：
1. 规则优先：Reviewer 对质量问题有最终否决权（QA veto）
2. Planner 负责流程裁决（遇到并行路线选择时决策）
3. LLM 裁决 fallback：规则引擎兜不住时，用 Planner 角色的 LLM 做自动仲裁
4. 冲突超出阈值 → 升级到 HITL（Human-in-the-Loop）
5. 高冲突场景可记录为 TeamDecision，供审计

冲突类型：
- review_disagreement: Reviewer 不通过，Coder/Planner 认为没问题
- plan_route_conflict: 同一目标有多个实现路线，无法一致
- priority_conflict: 安全/功能/性能优先级不一致
- ownership_disagreement: 修复责任人不一致

注意（Req 10）：本模块尚未接入 TeamRunner / API / CLI 主链，仅供参考性展示
B5 增强的冲突裁决设计思路。生产路径上 Reviewer 通过 ReviewRepairLoop 影响 state.review_status，
本身即等价于一种隐式冲突裁决。本模块的"显式 ConflictResolution 消息"机制未生效，
仅单元测试覆盖。在 TeamRoster 内已实现 disagreements 计数（state.has_open_blocking_issues）。
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
    MEDIUM = "medium"    # 需 Planner 裁决（B5: 优先 LLM 自动仲裁）
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
    """冲突裁决器。规则优先 -> LLM 仲裁 fallback -> HITL 升级。"""

    def __init__(self, state: SharedTeamState | None = None):
        self.state = state
        self._llm_available = True  # 乐观可用；构造 LLM 失败时 self-disable
        self._fallback_notified = False

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
        """裁决一次冲突。三阶段：规则 → LLM 仲裁 → HITL 升级。

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
            result = self._resolve_review_disagreement(positions, context or {})
            if result.resolved:
                return result
            # 规则 1 兜不住（无 Reviewer 参与）→ 进入 LLM 仲裁

        # === 规则 2：路线冲突由 Planner 裁决 ===
        if conflict_type == ConflictType.PLAN_ROUTE_CONFLICT:
            result = self._resolve_plan_route(description, positions, context or {})
            if result.resolved:
                return result

        # === 规则 3：优先级冲突 ===
        if conflict_type == ConflictType.PRIORITY_CONFLICT:
            result = self._resolve_priority(description, positions, context or {})
            if result.resolved:
                return result

        # === 规则 4：责任人冲突 ===
        if conflict_type == ConflictType.OWNERSHIP_DISAGREEMENT:
            result = self._resolve_ownership(positions)
            if result.resolved:
                return result

        # === B5: LLM 自动仲裁（如果规则引擎兜不住）===
        llm_resolution = self._try_llm_arbitration(conflict_type, description, positions, context or {})
        if llm_resolution is not None:
            return llm_resolution

        # === 兜底：升级 HITL ===
        return self._escalate(conflict_type, description, positions, context or {})

    def _try_llm_arbitration(
        self,
        conflict_type: ConflictType,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> Resolution | None:
        """B5: LLM 自动仲裁。以 Planner 视角给出绑定裁决。

        触发条件：
        - self.state 非 None：LLM 仲裁需要 state 来记录 TeamDecision
        - self._llm_available：上次未 self-disable
        失败（LLM 不可用/超时/回复不可用）返回 None，由调用方决定升级 HITL。
        成功时记录一条 TeamDecision。
        """
        if self.state is None:
            # 无 state → LLM 仲裁无意义（无地方写 decision），由调用方升级
            return None
        if not self._llm_available:
            return None
        try:
            from app.llm_factory import build_model

            llm = build_model()
            prompt = self._build_arbitration_prompt(conflict_type, description, positions, context)
            response = llm.invoke([("system", "你是团队中的 Planner，负责裁决成员之间的分歧。"), ("user", prompt)])
            text = response.content if hasattr(response, "content") else str(response)
            if not text or len(text.strip()) < 10:
                logger.warning("[ConflictResolver] LLM 仲裁返回空响应，视为不可用")
                return None
            import uuid
            import json

            # 尝试截取 JSON decision
            decision_text = text.strip()
            parsed = None
            first_brace = decision_text.find("{")
            last_brace = decision_text.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                candidate = decision_text[first_brace : last_brace + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
            if parsed and isinstance(parsed, dict) and parsed.get("decision"):
                decision = parsed["decision"]
                reason = parsed.get("reason", str(decision)[:200])
            else:
                # fallback：截取前 200 字作为裁决理由
                decision = "planner_llm_arbitration"
                reason = decision_text[:200]

            logger.info(f"[ConflictResolver] LLM 仲裁完成：decision={decision}, reason={reason[:80]}...")

            if self.state:
                decision_record = TeamDecision(
                    id=f"arb_{uuid.uuid4().hex[:8]}",
                    title=f"LLM 仲裁：{conflict_type.value}",
                    rationale=reason[:500],
                    decided_by="Planner(LLM arbitration)",
                )
                self.state.add_decision(decision_record)
                # 尝试持久化 store（如果 state 关联了 store）
                try:
                    from app.multiagent.store import get_multiagent_store
                    store = get_multiagent_store()
                    store.save_state(self.state)
                except Exception:
                    pass

            return Resolution(
                resolved=True,
                decision=str(decision)[:200],
                reason=reason[:500],
                decided_by="ConflictResolver(llm_planner_arbitration)",
            )
        except Exception as exc:
            logger.warning(f"[ConflictResolver] LLM 仲裁异常，降级 HITL：{exc}")
            self._llm_available = False
            return None

    @staticmethod
    def _build_arbitration_prompt(
        conflict_type: ConflictType,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        """构造 LLM 仲裁 prompt。"""
        lines = [
            f"# 冲突类型：{conflict_type.value}",
            f"# 冲突描述：{description[:500]}",
            "\n## 各方立场",
        ]
        for p in positions:
            agent = p.get("agent", "?")
            pos = p.get("position", "?")
            reason = p.get("reason", "")
            lines.append(f"- {agent}：{pos}（理由：{reason[:200]}）")
        phase = context.get("phase", "?")
        artifacts = context.get("artifacts", [])
        if artifacts:
            lines.append(f"\n## 相关产物\n" + "\n".join(f"- {a}" for a in artifacts[:5]))
        lines.append(f"\n## 当前阶段\n{phase}")
        lines.append(
            "\n---\n"
            "请作为 Planner 给出最终裁决。输出 JSON 格式：\n"
            '{"decision": "你的裁决结论", "reason": "简洁理由（不超过 200 字）"}\n'
            "你的裁决是绑定性的，团队会据此执行。"
        )
        return "\n".join(lines)

    def _resolve_review_disagreement(
        self,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> Resolution:
        """Reviewer 对质量问题有最终否决权。

        只要 Reviewer 投不通过，无论其他 Agent 持什么意见，都走返工。
        这避免了"Coder 觉得自己代码没问题就不修"的死锁。

        Returns:
            resolved=True 时表示规则已裁决；resolved=False 时表示规则无法决定，
            调用方应继续走 LLM 仲裁或 HITL 升级。
        """
        # 兼容旧测试：无 state 时走直接 escalation（不进入 LLM 路径）
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

        # 没有 Reviewer 参与：返回 not_resolved → 调用方继续走 LLM/HITL
        return Resolution(
            resolved=False,
            decision="",
            reason="Reviewer 未参与评审，但存在评审争议",
            decided_by="ConflictResolver(rule:reviewer_abscent)",
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

        # Planner 未参与 → not resolved → 走 LLM/HITL
        return Resolution(
            resolved=False,
            decision="",
            reason="路线冲突需 Planner 裁决，但 Planner 未表达立场",
            decided_by="ConflictResolver(rule:planner_route_waiting)",
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
        return Resolution(
            resolved=False,
            decision="",
            reason="无法确定修复责任人，各方分歧",
            decided_by="ConflictResolver(rule:unclear_ownership)",
        )

    def _escalate(
        self,
        conflict_type: ConflictType,
        description: str,
        positions: list[dict[str, Any]],
        context: dict[str, Any],
        forced_level: ConflictLevel | None = None,
    ) -> Resolution:
        """升级冲突：无法规则/LLM 裁决时，建议 HITL。"""
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
