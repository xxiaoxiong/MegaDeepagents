"""预置团队模板：software_dev_team / research_team。

注意：AgentSpec.role 是 prompt 模板 key，将配合 prompts.ROLE_PROMPTS 渲染。
watched_message_types 决定 broadcast 投递。
"""

from __future__ import annotations

from app.multiagent.agent_spec import AgentSpec, AgentSubscription, TeamSpec
from app.multiagent.messages import MessageType


RESEARCHER_SPEC = AgentSpec(
    name="Researcher",
    role="Researcher",
    goal="收集事实、验证假设，提供可核对证据",
    watched_message_types=[
        MessageType.USER_REQUEST,
        MessageType.RESEARCH_REQUEST,
        MessageType.QUESTION,
    ],
    allowed_tools=["search", "fetch_url", "read_file", "list_dir"],
)

PLANNNER_SPEC = AgentSpec(
    name="Planner",
    role="Planner",
    goal="把高层目标拆解为有序步骤，并指派负责人",
    watched_message_types=[
        MessageType.USER_REQUEST,
        MessageType.QUESTION,
        MessageType.HANDOFF,
        MessageType.CRITIQUE,
        MessageType.REVIEW_RESULT,
    ],
    allowed_tools=[],
    subscription=AgentSubscription(
        message_types=[
            MessageType.USER_REQUEST,
            MessageType.QUESTION,
            MessageType.CRITIQUE,
            MessageType.REVIEW_RESULT,
        ],
        from_agents=[],  # 接受任何来源，避免团队其他成员的紧急消息传不到 Planner
    ),
)

CODER_SPEC = AgentSpec(
    name="Coder",
    role="Coder",
    goal="把计划落地为可运行代码并通过验证",
    watched_message_types=[
        MessageType.PLAN,
        MessageType.DELEGATION,
        MessageType.REVISION_PLAN,
        MessageType.CRITIQUE,
        MessageType.TEST_RESULT,
    ],
    allowed_tools=["create_file", "edit_file", "execute", "read_file", "list_dir"],
    subscription=AgentSubscription(
        message_types=[
            MessageType.PLAN,
            MessageType.DELEGATION,
            MessageType.REVISION_PLAN,
            MessageType.CRITIQUE,
            MessageType.TEST_RESULT,
            MessageType.DECISION,
        ],
        from_agents=["Planner", "ReviewerAgent", "Tester", "Finalizer"],
    ),
)

TESTER_SPEC = AgentSpec(
    name="Tester",
    role="Tester",
    goal="编写并运行测试，向团队汇报测试结果",
    watched_message_types=[
        MessageType.ARTIFACT_CREATED,
        MessageType.HANDOFF,
        MessageType.DECISION,
        MessageType.TEST_REQUEST,
        MessageType.PLAN,
        MessageType.DELEGATION,
    ],
    allowed_tools=["execute", "read_file", "create_file"],
    subscription=AgentSubscription(
        message_types=[
            MessageType.ARTIFACT_CREATED,
            MessageType.TEST_REQUEST,
            MessageType.DELEGATION,
        ],
    ),
)

REVIEWER_SPEC = AgentSpec(
    name="ReviewerAgent",
    role="ReviewerAgent",
    goal="审查产物，给出基于证据的通过 / 修复决策",
    watched_message_types=[
        MessageType.REVIEW_REQUEST,
        MessageType.TEST_RESULT,
        MessageType.ARTIFACT_CREATED,
        MessageType.REVISION_DONE,
        MessageType.HANDOFF,
    ],
    allowed_tools=["read_file", "list_dir"],
    subscription=AgentSubscription(
        message_types=[MessageType.REVIEW_REQUEST, MessageType.REVISION_DONE, MessageType.ARTIFACT_CREATED],
    ),
)

FINALIZER_SPEC = AgentSpec(
    name="Finalizer",
    role="Finalizer",
    goal="把工作收拢为最终交付物，向用户输出最终结果",
    watched_message_types=[
        MessageType.REVIEW_RESULT,
        MessageType.DECISION,
        MessageType.HANDOFF,
        MessageType.FINAL,
    ],
    allowed_tools=["create_file", "read_file"],
    subscription=AgentSubscription(
        message_types=[MessageType.REVIEW_RESULT, MessageType.DECISION, MessageType.HANDOFF],
    ),
)


# ========== 预置团队 ==========


SOFTWARE_DEV_TEAM = TeamSpec(
    name="software_dev_team",
    description="软件开发团队：规划 → 实现 → 测试 → 评审 → 收尾",
    agents=[PLANNNER_SPEC, CODER_SPEC, TESTER_SPEC, REVIEWER_SPEC, FINALIZER_SPEC],
    max_rounds=20,
    termination_policy="review_passed_or_max_rounds",
    review_required=True,
    max_review_cycles=3,
)

RESEARCH_TEAM = TeamSpec(
    name="research_team",
    description="研究团队：规划 → 调研 → 综合报告",
    agents=[PLANNNER_SPEC.model_copy(update={"name": "ResearchPlanner"}), RESEARCHER_SPEC, FINALIZER_SPEC],
    max_rounds=12,
    termination_policy="final_message_produced",
    review_required=False,
    max_review_cycles=1,
)


DEFAULT_TEAMS: dict[str, TeamSpec] = {
    "software_dev_team": SOFTWARE_DEV_TEAM,
    "research_team": RESEARCH_TEAM,
}


def get_team(name: str) -> TeamSpec | None:
    return DEFAULT_TEAMS.get(name)


def list_teams() -> list[str]:
    return list(DEFAULT_TEAMS.keys())
