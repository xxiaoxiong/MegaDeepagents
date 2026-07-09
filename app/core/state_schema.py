"""自定义 Agent State Schema：扩展 DeepAgentState，纳入任务元数据。"""

from typing import Annotated, Any

from deepagents.graph import DeepAgentState
from langgraph.graph import add_messages


class TaskAgentState(DeepAgentState):
    """任务级 agent state，包含任务元数据与子智能体追踪。"""

    task_id: str
    planner_status: str = "planned"
    subagent_tasks: Annotated[list[dict[str, Any]], add_messages]
