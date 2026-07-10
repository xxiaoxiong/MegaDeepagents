"""ReviewRepairLoop：评审-返工循环。

流程：
1. Agent 产生产物 → 向 ReviewerAgent 发 review_request
2. ReviewerAgent 输出 review_result（含 passed / issues / required_fix_owner）
3. passed → 更新 shared_state.review_status = passed
4. failed → critique 消息 → 责任 Agent 响应 revision_plan → 执行修复 → 再次 review
5. 最大返工次数可配置（TeamSpec.max_review_cycles）

与 SpeakerSelector 的协作：
- review_request 消息会触发 ReviewerAgent 被选择（SpeakerSelector 规则 2）
- critique 消息触发目标 Agent 被选择
- revision_done 触发 ReviewerAgent 被再次选择

设计要点：
- 评审周期计数和 pass 决定在 review_repair.py 中管理
- 实际评审动作由 ReviewerAgent 在 TeamRunner 循环中执行
- 本模块负责：收到 review_result 后的决策逻辑
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.logging import logger
from app.core.observability import traceable
from app.multiagent.messages import AgentMessage, MessageType, make_message_id
from app.multiagent.state import IssueSeverity, IssueStatus, SharedTeamState, TeamIssue, TeamPhase


class ReviewResult:
    """解析后的评审结果。"""

    __slots__ = ("passed", "issues", "required_fix_owner", "raw")

    def __init__(
        self,
        passed: bool = False,
        issues: list[dict[str, Any]] | None = None,
        required_fix_owner: str | None = None,
        raw: str = "",
    ):
        self.passed = passed
        self.issues = issues or []
        self.required_fix_owner = required_fix_owner


class ReviewRepairLoop:
    """管理评审与修复循环的决策逻辑。"""

    def __init__(self, max_cycles: int = 3):
        self.max_cycles = max_cycles
        self.cycle_count = 0

    @traceable(name="review_repair", run_type="chain")
    def process_review_result(
        self,
        result: ReviewResult,
        state: SharedTeamState,
        room,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> list[AgentMessage]:
        """处理一条 review_result 消息，更新状态并产生后续消息。

        被 @traceable 装饰：评审决策每轮都上报到 LangSmith。
        call-time 可传 langsmith_extra={"metadata": {...}} 注入 cycle/passed 等动态字段。
        """
        messages: list[AgentMessage] = []
        state.review_cycles = self.cycle_count

        if result.passed:
            state.review_status = "passed"
            state.update_phase(TeamPhase.FINALIZING)
            logger.info(f"[ReviewRepair] review PASSED (round={self.cycle_count})")
            return messages

        # failed
        self.cycle_count += 1
        state.review_cycles = self.cycle_count
        state.review_status = "failed"
        state.update_phase(TeamPhase.REPAIRING)

        if self.cycle_count > self.max_cycles:
            state.review_status = "max_retries_exceeded"
            logger.warning(f"[ReviewRepair] max cycles {self.max_cycles} exceeded")
            return messages

        # 将 issues 注册为 SharedTeamState.issues
        for issue_data in result.issues:
            issue_id = issue_data.get("id", f"issue_{uuid.uuid4().hex[:8]}")
            raw_evidence = issue_data.get("evidence", [])
            # 容错：把 evidence 中的非 dict 元素包装成 {"detail": value}
            normalized_evidence: list[dict[str, Any]] = []
            for ev in raw_evidence:
                if isinstance(ev, dict):
                    normalized_evidence.append(ev)
                else:
                    normalized_evidence.append({"detail": str(ev)})
            issue = TeamIssue(
                id=issue_id,
                title=issue_data.get("problem", issue_data.get("title", "unknown issue")),
                description=issue_data.get("description", ""),
                severity=IssueSeverity(issue_data.get("severity", "medium")),
                status=IssueStatus.OPEN,
                owner=result.required_fix_owner or issue_data.get("owner"),
                evidence=normalized_evidence,
            )
            state.add_issue(issue)

            # 发 critique 消息给责任 Agent
            ev_str = "; ".join(
                f"{e.get('detail', str(e)) if isinstance(e, dict) else str(e)}"
                for e in raw_evidence
            )
            critique = AgentMessage(
                id=make_message_id(),
                task_id=state.task_id,
                room_id=state.room_id,
                from_agent="ReviewerAgent",
                to_agent=result.required_fix_owner,
                message_type=MessageType.CRITIQUE,
                content=f"问题：{issue.title}\n证据：{ev_str}",
                requires_response=True,
                expected_response_type="observation",
                cause_by="review_result",
                artifact_refs=issue_data.get("artifact_refs", []),
                evidence=normalized_evidence,
            )
            messages.append(critique)

        logger.info(
            f"[ReviewRepair] review FAILED (round={self.cycle_count}), "
            f"issues={len(result.issues)}, "
            f"owner={result.required_fix_owner}"
        )
        return messages

    @staticmethod
    def parse_review_result(message: AgentMessage) -> ReviewResult:
        """从 AgentMessage（review_result 类型）解析出结构化结果。"""
        content = message.content.strip()
        # 尝试 JSON 解析
        import json
        try:
            parsed = json.loads(content)
            return ReviewResult(
                passed=parsed.get("passed", False),
                issues=parsed.get("issues", []),
                required_fix_owner=parsed.get("required_fix_owner", message.metadata.get("fix_owner")),
                raw=content,
            )
        except (json.JSONDecodeError, ValueError):
            pass

        # 基于文本启发判定
        lower = content.lower()
        passed = any(kw in lower for kw in ["pass", "approved", "合格", "通过"])
        return ReviewResult(
            passed=passed,
            issues=[{"problem": content[:200], "severity": "medium"}] if not passed else [],
            required_fix_owner=message.metadata.get("fix_owner"),
            raw=content,
        )
