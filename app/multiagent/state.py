"""SharedTeamState：多智能体团队共享状态。

任务进度不再只存于聊天文本中，而是结构化维护在共享状态里：
- phase：当前阶段
- plan：计划文本
- open_questions：尚未解决的问题
- issues：阻塞问题（带 severity / status）
- decisions：已做出的决策
- artifacts：产物引用
- review_status：评审状态

参考 MetaGPT Environment 的共享上下文思路。这里把它做成持久化友好的 Pydantic 模型。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from app.core.logging import logger
from pydantic import BaseModel, Field


class TeamPhase(str, Enum):
    """团队任务的阶段机。"""

    CREATED = "created"
    PLANNING = "planning"
    DISCUSSING = "discussing"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 新增：区分"达到上限但未成功"与真正成功
    INCOMPLETE = "incomplete"  # 达到最大轮次但未满足成功条件
    TIMED_OUT = "timed_out"  # 超时
    WAITING_HUMAN = "waiting_human"  # 等待人工处理


class IssueSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"


class IssueStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class TeamIssue(BaseModel):
    """团队开放问题 / 阻塞项。"""

    id: str
    title: str
    description: str = ""
    severity: IssueSeverity = IssueSeverity.MEDIUM
    status: IssueStatus = IssueStatus.OPEN
    owner: str | None = Field(default=None, description="负责解决的 Agent 名")
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None


class TeamDecision(BaseModel):
    """团队决策记录。"""

    id: str
    title: str
    rationale: str = ""
    decided_by: str
    alternatives: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TeamArtifactRef(BaseModel):
    """产物引用：每个 Agent 输出的文件 / 报告等。

    扩展字段（P0-2 Artifact Ownership）：
    - version: 递增版本号，追踪更新
    - updated_by: 最后修改 Agent
    - reviewed_by: 评审 Agent（可选，评审后填入）
    - reviewed_at: 评审时间
    - message_id: 关联消息 ID
    - artifact_id: 产物的稳定 ID（不同 version 可共享同一 artifact_id）
    - status: 产物状态（created / reviewing / approved / rejected）
    """

    path: str
    name: str = ""
    role: str = Field(default="", description="该产物的角色，如 plan / code / review / test")
    produced_by: str = ""
    version: int = Field(default=1, ge=1, description="版本号，递增")
    updated_by: str | None = Field(default=None, description="最后修改 Agent")
    reviewed_by: str | None = Field(default=None, description="评审 Agent")
    reviewed_at: datetime | None = Field(default=None)
    message_id: str | None = Field(default=None, description="关联消息 ID")
    artifact_id: str | None = Field(default=None, description="稳定产物 ID")
    status: str = Field(default="created", description="created / reviewing / approved / rejected")
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SharedTeamState(BaseModel):
    """团队共享状态。

    所有 Agent 都能看到这份状态（通过 to_prompt_context() 投射到 prompt），
    但只有对应 Agent 在本轮行动中才能修改对应字段。
    """

    room_id: str
    task_id: str
    goal: str = ""
    phase: TeamPhase = TeamPhase.CREATED
    plan: str = ""
    current_round: int = 0
    max_rounds: int = 20

    open_questions: list[str] = Field(default_factory=list)
    issues: list[TeamIssue] = Field(default_factory=list)
    decisions: list[TeamDecision] = Field(default_factory=list)
    artifacts: list[TeamArtifactRef] = Field(default_factory=list)

    completed_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[str] = Field(default_factory=list)

    review_status: str | None = Field(
        default=None,
        description="none / pending / passed / failed / partial",
    )
    review_cycles: int = 0

    final_output: str | None = None
    final_artifact_refs: list[TeamArtifactRef] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ========== 合法阶段转换 ==========

    # 终态：进入后不可再变
    _TERMINAL: set["TeamPhase"] = {
        TeamPhase.COMPLETED, TeamPhase.FAILED, TeamPhase.CANCELLED,
        TeamPhase.INCOMPLETE, TeamPhase.TIMED_OUT,
    }
    # 工作中阶段：相互之间可自由跳转
    _WORKING: set["TeamPhase"] = {
        TeamPhase.PLANNING,
        TeamPhase.DISCUSSING,
        TeamPhase.EXECUTING,
        TeamPhase.REVIEWING,
        TeamPhase.REPAIRING,
        TeamPhase.FINALIZING,
    }

    def update_phase(self, phase: TeamPhase) -> bool:
        """更新阶段。拒绝非法转换（终态→任何、跳过 FINALIZING 直接到 COMPLETED），
        非法时记 WARNING 但返回 False。"""
        if phase == self.phase:
            return False
        # 终态后不可改
        if self.phase in self._TERMINAL:
            logger.warning(
                f"[SharedTeamState] 已是终态 {self.phase.value}，忽略转 {phase.value}"
            )
            return False
        # 任何非终态 → 终态都允许
        if phase in self._TERMINAL:
            # CREATED 直接到终态被认为是从未启动 → 拒绝
            if self.phase == TeamPhase.CREATED and phase in (TeamPhase.COMPLETED,):
                logger.warning(
                    f"[SharedTeamState] 非法阶段转换：{self.phase.value} → {phase.value}，需先经 FINALIZING"
                )
                return False
            self.phase = phase
            return True
        # CREATED → 任意工作阶段都允许（LLM 可能跳过 PLANNING 直接 EXECUTING）
        # 工作阶段之间互通
        if self.phase == TeamPhase.CREATED or self.phase in self._WORKING:
            if phase in self._WORKING:
                self.phase = phase
                return True
        logger.warning(
            f"[SharedTeamState] 非法阶段转换：{self.phase.value} → {phase.value}，忽略"
        )
        return False

    def add_issue(self, issue: TeamIssue) -> None:
        # id 唯一去重
        if any(i.id == issue.id for i in self.issues):
            return
        self.issues.append(issue)

    def resolve_issue(self, issue_id: str, status: IssueStatus = IssueStatus.RESOLVED) -> bool:
        for i in self.issues:
            if i.id == issue_id:
                i.status = status
                if status in (IssueStatus.RESOLVED, IssueStatus.WONT_FIX):
                    i.resolved_at = datetime.utcnow()
                return True
        return False

    def add_decision(self, decision: TeamDecision) -> None:
        # id 唯一去重
        if any(d.id == decision.id for d in self.decisions):
            return
        self.decisions.append(decision)

    def add_artifact(self, artifact: TeamArtifactRef) -> TeamArtifactRef:
        """添加或更新产物。同 path 时更新版本和归属字段，不同 path 时新增。

        版本控制：
        - 新增：version=1
        - 更新已有 path：version+1，保留 produced_by（首创建者），更新 updated_by
        """
        for i, a in enumerate(self.artifacts):
            if a.path == artifact.path:
                # 更新已有产物：版本 +1，保留原始 produced_by
                updated = artifact.model_copy(deep=True)
                updated.version = a.version + 1 if not artifact.version or artifact.version <= a.version else artifact.version
                updated.produced_by = a.produced_by  # 首创建者不变
                if not updated.updated_by:
                    updated.updated_by = artifact.produced_by or a.produced_by
                # 合并 message_id（若新传入）
                if artifact.message_id and artifact.message_id != a.message_id:
                    updated.message_id = artifact.message_id
                self.artifacts[i] = updated
                return updated
        # 新增
        if not artifact.version or artifact.version < 1:
            artifact.version = 1
        self.artifacts.append(artifact)
        return artifact

    def mark_artifact_reviewed(
        self,
        path: str,
        reviewed_by: str,
        status: str = "approved",
        message_id: str | None = None,
    ) -> bool:
        """评审 Agent 标注产物评审状态。reviewed_by 记录评审者。

        用于"谁评审了谁的产物"的可审计链路。
        """
        for a in self.artifacts:
            if a.path == path:
                a.reviewed_by = reviewed_by
                a.reviewed_at = datetime.utcnow()
                a.status = status
                if message_id:
                    a.message_id = message_id
                return True
        logger.warning(
            f"[SharedTeamState] mark_artifact_reviewed: artifact {path} 不存在"
        )
        return False

    def mark_step_done(self, step: str) -> None:
        if step not in self.completed_steps:
            self.completed_steps.append(step)
        if step in self.blocked_steps:
            self.blocked_steps.remove(step)

    def add_blocking_step(self, step: str) -> None:
        if step not in self.blocked_steps:
            self.blocked_steps.append(step)

    def add_open_question(self, question: str) -> None:
        if question and question not in self.open_questions:
            self.open_questions.append(question)

    def resolve_open_question(self, question: str) -> None:
        if question in self.open_questions:
            self.open_questions.remove(question)

    def has_open_blocking_issues(self) -> bool:
        return any(
            i.status == IssueStatus.OPEN and i.severity in (IssueSeverity.HIGH, IssueSeverity.BLOCKER)
            for i in self.issues
        )

    def open_issues(self) -> list[TeamIssue]:
        return [i for i in self.issues if i.status == IssueStatus.OPEN]

    # ========== 投射到 Prompt ==========

    def to_prompt_context(self, max_items: int = 12) -> str:
        """生成本状态的一段 Markdown 摘要，供 Agent 的 system prompt 使用。"""
        lines: list[str] = []
        lines.append(f"# 团队目标\n{self.goal or '(未设置)'}")
        lines.append(f"# 当前阶段\n{self.phase.value}（第 {self.current_round}/{self.max_rounds} 轮）")
        if self.plan:
            lines.append(f"# 计划\n{self.plan}")
        if self.completed_steps:
            lines.append("# 已完成步骤\n" + "\n".join(f"- {s}" for s in self.completed_steps[-max_items:]))
        if self.blocked_steps:
            lines.append("# 阻塞步骤\n" + "\n".join(f"- {s}" for s in self.blocked_steps))
        if self.open_questions:
            lines.append("# 开放问题\n" + "\n".join(f"- {q}" for q in self.open_questions[:max_items]))
        if self.issues:
            line = "\n".join(
                f"- [{i.severity.value}/{i.status.value}] {i.title}（owner={i.owner or 'unassigned'}）"
                for i in self.issues[:max_items]
            )
            lines.append(f"# Issues\n{line}")
        if self.decisions:
            line = "\n".join(f"- {d.title}（by {d.decided_by}）" for d in self.decisions[:max_items])
            lines.append(f"# 已做决策\n{line}")
        if self.artifacts:
            line = "\n".join(
                f"- {a.path}（role={a.role}, by={a.produced_by}）" for a in self.artifacts[:max_items]
            )
            lines.append(f"# 已有产物\n{line}")
        if self.review_status:
            lines.append(f"# 评审状态\n{self.review_status}（第 {self.review_cycles} 次返工）")
        if self.final_output:
            lines.append(f"# 最终输出（草稿）\n{self.final_output[:800]}")
        return "\n\n".join(lines)
