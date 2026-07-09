"""运行策略：多 Agent 任务的模式选择。

mode 类型：
- controlled_group_chat（默认）：完整 SpeakerSelector + TerminationChecker 控制
- round_robin：简单轮替（不考虑优先级，测试用）
- free_form：Agent 自由输出，不做 SpeakerSelector（不推荐，可能穷举）

每个模式对应不同的 TeamRunner 调用方式。
"""

from __future__ import annotations

from enum import Enum


class TeamRunMode(str, Enum):
    CONTROLLED_GROUP_CHAT = "controlled_group_chat"
    ROUND_ROBIN = "round_robin"
    FREE_FORM = "free_form"
