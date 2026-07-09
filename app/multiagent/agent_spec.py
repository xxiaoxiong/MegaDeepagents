"""AgentSpec / TeamSpec / TeamRunConfig：多智能体团队的配置与运行参数。

每个 Agent 有清晰的：
- name / role / goal
- watched_message_types（类似 MetaGPT Role.watch）
- allowed_tools / permissions
- private_memory_scope（可选的私有记忆域）

TeamSpec 定义一组 Agent 如何组成团队，以及团队策略。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.multiagent.messages import MessageType


class AgentSubscription(BaseModel):
    """Agent 订阅规则。

    当 MessageBus 处理 broadcast 消息时，根据订阅规则判断投递地址。
    类似 MetaGPT Role.watch(cause_by)。
    """

    message_types: list[MessageType] = Field(
        default_factory=list,
        description="关注的消息类型：匹配 message_type",
    )
    cause_by: list[str] = Field(
        default_factory=list,
        description="关注的消息来源动作",
    )
    from_agents: list[str] = Field(
        default_factory=list,
        description="关注的消息来自哪些 Agent（空=所有）",
    )

    def matches(self, msg_type: MessageType, cause_by: str | None = None, from_agent: str | None = None) -> bool:
        """判断一条消息是否命中此订阅规则。"""
        if self.message_types and msg_type in self.message_types:
            if self.from_agents and from_agent and from_agent not in self.from_agents:
                return False
            return True
        if self.cause_by and cause_by and cause_by in self.cause_by:
            if self.from_agents and from_agent and from_agent not in self.from_agents:
                return False
            return True
        if not self.message_types and not self.cause_by:
            # 空的 subscription = 不匹配任何消息，Agent 不自动收广播
            return False
        return False


class AgentSpec(BaseModel):
    """多智能体团队中的单个 Agent 定义。"""

    name: str = Field(..., description="唯一标识名")
    role: str = Field(..., description="可读角色名称，如'任务规划者'")
    goal: str = Field(..., description="Agent 目标，用于构造 system prompt")

    system_prompt: str | None = Field(
        default=None,
        description="system prompt 模板，若不提供则在 prompts.py 中按 role 查找",
    )
    watched_message_types: list[MessageType] = Field(
        default_factory=list,
        description="订阅的消息类型（简化方式）",
    )
    subscription: AgentSubscription | None = Field(
        default=None,
        description="订阅规则（完整方式，优先级高于 watched_message_types）",
    )

    allowed_tools: list[str] = Field(
        default_factory=list,
        description="允许使用的工具名列表",
    )
    permissions: list[str] = Field(
        default_factory=list,
        description="权限列表",
    )
    allowed_actions: list[str] = Field(
        default_factory=list,
        description=(
            "允许该 Agent 产出的 action type 白名单（运行时强制）。"
            "空列表 = 不限制。常见值：send_message / update_state / create_artifact / "
            "request_review / respond_critique / mark_done / handoff / no_op。"
            "作用：阻止 Reviewer 越权改代码、阻止 Coder 自评通过等角色越界。"
        ),
    )

    runtime_type: str = Field(
        default="deepagents",
        description="运行时类型，目前仅支持 deepagents",
    )
    private_memory_scope: str | None = Field(
        default=None,
        description="私有记忆域路径，可选的每位 Agent 专属记忆",
    )

    metadata: dict[str, Any] = Field(default_factory=dict)

    def get_subscription(self) -> AgentSubscription:
        if self.subscription:
            return self.subscription
        return AgentSubscription(message_types=self.watched_message_types)


class TeamSpec(BaseModel):
    """多智能体团队模板。与当前项目中"团队"定义对应。

    每套 TeamSpec 可创建多个 TeamRoom 实例。
    """

    name: str = Field(..., description="团队模板名称，如 software_dev_team")
    description: str = Field(default="", description="描述")
    agents: list[AgentSpec] = Field(..., description="Agent 定义列表")
    max_rounds: int = Field(default=20, ge=1, le=200, description="最大轮次")
    termination_policy: str = Field(
        default="review_passed_or_max_rounds",
        description="终止策略：review_passed_or_max_rounds / all_steps_completed / manual_only / final_message_produced",
    )
    review_required: bool = Field(default=True, description="是否需要评审环节")
    max_review_cycles: int = Field(default=3, ge=0, le=20, description="最大评审返工次数")

    def get_agent_names(self) -> list[str]:
        return [a.name for a in self.agents]

    def get_agent(self, name: str) -> AgentSpec | None:
        for a in self.agents:
            if a.name == name:
                return a
        return None


class TeamRunConfig(BaseModel):
    """单次多 Agent 任务的运行时配置。"""

    goal: str = Field(..., description="任务目标")
    team_name: str = Field(default="software_dev_team", description="团队模板名")
    max_rounds: int = Field(default=20, ge=1, le=200)
    review_required: bool = True
    auto_start: bool = Field(default=True, description="创建后自动开始运行")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TeamAgentRunResult(BaseModel):
    """单个 Agent 在单轮中的执行结果。"""

    agent_name: str = ""
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    action_count: int = 0
    error: str | None = None


class TeamRunResult(BaseModel):
    """一次多 Agent 任务的最终运行结果。"""

    task_id: str = ""
    room_id: str = ""
    status: str = ""
    agent_results: list[TeamAgentRunResult] = Field(default_factory=list)
    final_output: str | None = None
    phase: str = ""
    total_rounds: int = 0
    termination_reason: str | None = None
    error: str | None = None
    completed_at: datetime | None = None
