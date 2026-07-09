"""Task 工具：当前任务状态、产物记录工具。"""

from typing import Any

from langchain.tools import tool


def build_task_tools(task_runner: Any | None = None) -> list[Any]:
    tools = []

    @tool
    def record_artifact(path: str, name: str, size_bytes: int = 0) -> str:
        """记录一个产物到当前任务。path 为 workspace 相对路径。"""
        if task_runner and task_runner.task_id:
            from app.task.service import get_task_service
            get_task_service().add_artifact(task_runner.task_id, path, name, size_bytes)
            return f"产物已记录: {path}"
        return "当前无活跃任务，无法记录产物。"

    tools.append(record_artifact)
    return tools
