"""多智能体运行时：在现有单 Agent 框架基础上扩展的支持多智能体通信协作的 TeamRuntime。

设计参考 MetaGPT 的 Environment / Role / Action / Message / watch / msg_buffer 思路，
但适配到当前项目的 DeepAgents + LangGraph + SQLite 技术栈。

核心组件（已接入主链）：
- TeamRoom：多智能体任务环境
- MessageBus：结构化消息路由（无子串模糊匹配，确定性别名 + dead-letter 拒绝）
- AgentInbox：每个 Agent 的私有收件箱
- SharedTeamState：团队共享状态
- SpeakerSelector：发言者选择（rule-first + LLM fallback）
- TerminationChecker：终止条件（max_rounds→INCOMPLETE、review_passed→COMPLETED）
- ReviewRepair：评审-返工循环（闭环 critique 发布到 MessageBus）
- AgentRuntimeAdapter：复用现有 DeepAgents 作为执行内核
- TeamRunner：核心同步主循环（复用 TeamRoundExecutor）
- TeamRoundExecutor：统一单轮执行组件（TeamRunner + TeamGraph 共用）
- EffectiveRunPolicy：决策 review_required / max_rounds / max_review_cycles

实验性组件（未接入 API/CLI，checkpoint 恢复未生产验证）：
- TeamGraphRunner：LangGraph 状态图 + SqliteSaver checkpoint
- LayeredMemory：分层记忆（Working + Episodic + Semantic + Procedural）
- ConflictResolver：多 Agent 意见冲突裁决
- ParallelRunner：异步并行执行
"""

from app.multiagent.messages import AgentMessage, MessageVisibility, MessageType
from app.multiagent.state import (
    SharedTeamState,
    TeamDecision,
    TeamIssue,
    TeamArtifactRef,
    TeamPhase,
    IssueSeverity,
    IssueStatus,
)
from app.multiagent.agent_spec import (
    AgentSpec,
    AgentSubscription,
    TeamSpec,
    TeamRunConfig,
    TeamRunResult,
    TeamAgentRunResult,
)

__all__ = [
    "AgentMessage",
    "MessageVisibility",
    "MessageType",
    "SharedTeamState",
    "TeamDecision",
    "TeamIssue",
    "TeamArtifactRef",
    "TeamPhase",
    "IssueSeverity",
    "IssueStatus",
    "AgentSpec",
    "AgentSubscription",
    "TeamSpec",
    "TeamRunConfig",
    "TeamRunResult",
    "TeamAgentRunResult",
]
