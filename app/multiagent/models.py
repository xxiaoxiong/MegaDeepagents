"""models.py：核心模型的统一导出面。

设计原则：
- 真正的模型定义分布在 messages.py / state.py / agent_spec.py 中，按职责分文件
- 这里仅作为聚合导出，对应 updatePlan.md 中 models.py 的要求
"""

from app.multiagent.messages import (
    AgentMessage,
    MessageVisibility,
    MessageType,
    make_message_id,
)
from app.multiagent.state import (
    IssueSeverity,
    IssueStatus,
    SharedTeamState,
    TeamArtifactRef,
    TeamDecision,
    TeamIssue,
    TeamPhase,
)
from app.multiagent.agent_spec import (
    AgentSpec,
    AgentSubscription,
    TeamAgentRunResult,
    TeamRunConfig,
    TeamRunResult,
    TeamSpec,
)

__all__ = [
    "AgentMessage",
    "MessageVisibility",
    "MessageType",
    "make_message_id",
    "IssueSeverity",
    "IssueStatus",
    "SharedTeamState",
    "TeamArtifactRef",
    "TeamDecision",
    "TeamIssue",
    "TeamPhase",
    "AgentSpec",
    "AgentSubscription",
    "TeamAgentRunResult",
    "TeamRunConfig",
    "TeamRunResult",
    "TeamSpec",
]
