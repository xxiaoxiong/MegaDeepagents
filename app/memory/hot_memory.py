"""热记忆：MEMORY.md / USER.md 读取。写入由 Agent 通过 backend 直接完成。"""

from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import logger


class HotMemory:
    def __init__(self):
        self.memory_path = Path(settings.memory_file)
        self.user_path = Path(settings.user_file)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_path.parent.mkdir(parents=True, exist_ok=True)

        # 初始化默认文件
        if not self.memory_path.exists():
            self.memory_path.write_text(
                "# MEMORY.md\n\n暂无记忆内容。\n", encoding="utf-8"
            )
        if not self.user_path.exists():
            self.user_path.write_text(
                "# USER.md\n\n暂无用户信息。\n", encoding="utf-8"
            )

    def read_memory(self) -> str:
        return self.memory_path.read_text(encoding="utf-8")

    def read_user(self) -> str:
        return self.user_path.read_text(encoding="utf-8")


_hot_memory: Optional[HotMemory] = None


def get_hot_memory() -> HotMemory:
    global _hot_memory
    if _hot_memory is None:
        _hot_memory = HotMemory()
    return _hot_memory
