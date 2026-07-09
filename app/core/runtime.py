"""运行时上下文：任务执行期间共享的状态容器。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeContext:
    """Agent 执行期间的运行时上下文。"""
    task_id: str = ""
    thread_id: str = "default"
    user_input: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
