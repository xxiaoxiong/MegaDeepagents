"""工具集注册中心：按 toolset 注册工具。"""

from typing import Any

from app.core.config import settings
from app.memory.tools import build_memory_tools
from app.skills.tools import build_skill_tools
from app.tools.task_tools import build_task_tools
from app.tools.web_tools import build_web_tools


class ToolRegistry:
    def __init__(self, task_runner: Any | None = None):
        self.task_runner = task_runner
        self._toolsets: dict[str, list[Any]] = {}

    def register_all(self) -> None:
        self._toolsets = {
            "file": [],  # 由智能体内置
            "memory": build_memory_tools(),
            "skills": build_skill_tools(),
            "task": build_task_tools(self.task_runner),
        }
        if settings.enable_web_tools:
            self._toolsets["web"] = build_web_tools()
        if settings.enable_mcp_tools:
            from app.tools.mcp_loader import load_mcp_tools
            self._toolsets["mcp"] = load_mcp_tools()

    def get_toolset(self, name: str) -> list[Any]:
        return self._toolsets.get(name, [])

    def enabled_tools(self) -> list[Any]:
        tools = []
        for toolset_name, tool_list in self._toolsets.items():
            tools.extend(tool_list)
        return tools

    def list_toolsets(self) -> dict[str, dict[str, Any]]:
        all_names = ["file", "memory", "skills", "task", "web", "shell_safe", "mcp"]
        result = {}
        for name in all_names:
            enabled = name in self._toolsets
            tools = self._toolsets.get(name, [])
            result[name] = {
                "enabled": enabled,
                "count": len(tools),
                "tools": [getattr(t, "name", str(t)) for t in tools],
            }
        return result
