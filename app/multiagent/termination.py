"""TerminationChecker：判断多 Agent 任务的终止条件。

支持策略：
1. manual_only：仅人工终止
2. all_steps_completed：所有完成步骤 → 终止
3. review_passed_or_max_rounds（默认）：评审通过 OR 达到最大轮次 OR traceback
4. final_message_produced：有 FINAL 消息 → 终止
5. max_rounds：直接按轮次限制
6. stale：连续 N 轮无消息或全是 no_op → 终止

终止后给出 termination_reason 供上层日志 / 前端展示。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.multiagent.agent_spec import TeamSpec
from app.multiagent.messages import AgentMessage, MessageType
from app.multiagent.state import SharedTeamState, TeamPhase


class TerminationDecision:
    __slots__ = ("should_terminate", "reason", "final_phase")

    def __init__(self, should_terminate: bool, reason: str = "", final_phase: TeamPhase | None = None):
        self.should_terminate = should_terminate
        self.reason = reason
        self.final_phase = final_phase


class TerminationChecker:
    """多 Agent 任务终止检查器。"""

    def __init__(self, team_spec: TeamSpec, max_stale_rounds: int = 4):
        self.team_spec = team_spec
        self.policy = team_spec.termination_policy
        self.max_stale_rounds = max_stale_rounds
        self._no_op_count = 0
        # 新增：基于"是否产出有效投递"的 stale 检测，替代易被 reset 的 no_op 计数
        self._unproductive_count = 0
        self._last_signature: tuple[str, MessageType] | None = None

    def check(
        self,
        state: SharedTeamState,
        recent_messages: list[AgentMessage],
        round_count: int,
        productive_delivery: bool | None = None,
    ) -> TerminationDecision:
        # 显式终止
        if state.phase in (TeamPhase.COMPLETED, TeamPhase.FAILED, TeamPhase.CANCELLED):
            return TerminationDecision(
                True,
                reason=f"phase_already_{state.phase.value}",
                final_phase=state.phase,
            )

        # cancel requested
        if state.metadata.get("cancel_requested"):
            return TerminationDecision(True, reason="cancel_requested", final_phase=TeamPhase.CANCELLED)

        if self.policy == "manual_only":
            return TerminationDecision(False, reason="manual_only")

        # 最大轮次
        if round_count >= state.max_rounds:
            return TerminationDecision(True, reason="max_rounds", final_phase=TeamPhase.COMPLETED)

        # 修复方案完成 / final message
        if state.final_output:
            return TerminationDecision(True, reason="final_output_set", final_phase=TeamPhase.COMPLETED)
        for m in recent_messages:
            if m.message_type == MessageType.FINAL:
                return TerminationDecision(True, reason="final_message_produced", final_phase=TeamPhase.COMPLETED)

        # all_steps_completed
        if self.policy == "all_steps_completed":
            if state.plan and not state.open_questions and not state.open_issues():
                if not any(s not in state.completed_steps for s in _extract_steps_from_plan(state.plan)):
                    return TerminationDecision(
                        True, reason="all_steps_completed", final_phase=TeamPhase.COMPLETED
                    )

        # 评审通过 + 必要评审
        if self.team_spec.review_required:
            if state.review_status == "passed" and state.phase in (TeamPhase.REVIEWING, TeamPhase.FINALIZING):
                return TerminationDecision(
                    True, reason="review_passed", final_phase=TeamPhase.COMPLETED
                )
            if state.review_cycles > self.team_spec.max_review_cycles:
                return TerminationDecision(
                    True,
                    reason=f"max_review_cycles_exceeded ({state.review_cycles})",
                    final_phase=TeamPhase.FAILED,
                )

        # ============ 双层 stale 检测 ============
        # 1) 基于投递有效性的 stale（核心：路由黑洞 / 全 no_op 都会被这一层抓住）
        if productive_delivery is not None:
            if not productive_delivery and not state.final_output:
                self._unproductive_count += 1
                if self._unproductive_count >= self.max_stale_rounds:
                    return TerminationDecision(
                        True, reason="stale_no_progress", final_phase=TeamPhase.FAILED
                    )
            else:
                self._unproductive_count = 0

        # 2) 原有 no_op 全空检测（保留作 fall-through）
        if recent_messages and all(m.message_type == MessageType.NO_OP for m in recent_messages):
            self._no_op_count += 1
            if self._no_op_count >= self.max_stale_rounds:
                return TerminationDecision(True, reason="stale_no_op", final_phase=TeamPhase.FAILED)
        else:
            self._no_op_count = 0

        # 报错立即终止
        for m in recent_messages:
            if m.message_type == MessageType.ERROR:
                return TerminationDecision(True, reason="error_message", final_phase=TeamPhase.FAILED)

        # 严重阻塞 issue 未解决且超过最大评审返工
        if state.has_open_blocking_issues() and state.review_cycles > self.team_spec.max_review_cycles:
            return TerminationDecision(True, reason="blocking_issue_unresolved", final_phase=TeamPhase.FAILED)

        return TerminationDecision(False, reason="continue")


def _extract_steps_from_plan(plan_text: str) -> list[str]:
    """从 plan 文本中提取步骤（简单按行）。"""
    if not plan_text:
        return []
    lines: list[str] = []
    for line in plan_text.splitlines():
        s = line.strip()
        if s:
            lines.append(s.lstrip("-*0123456789. ").strip())
    return lines
