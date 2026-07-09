"""多智能体运行时：在现有单 Agent 框架基础上扩展的支持多智能体通信协作的 TeamRuntime。

设计参考 MetaGPT 的 Environment / Role / Action / Message / watch / msg_buffer 思路，
但适配到当前项目的 DeepAgents + LangGraph + SQLite 技术栈。

核心组件：
- TeamRoom：多智能体任务环境
- MessageBus：结构化消息路由
- AgentInbox：每个 Agent 的私有收件箱
- SharedTeamState：团队共享状态
- SpeakerSelector：发言者选择
- TerminationChecker：终止条件
- ReviewRepair：评审-返工循环
- AgentRuntimeAdapter：复用现有 DeepAgents
- TeamRunner：核心循环
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
