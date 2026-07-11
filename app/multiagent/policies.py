"""运行策略：多 Agent 任务的模式选择与有效配置计算。

mode 类型：
- controlled_group_chat（默认）：完整 SpeakerSelector + TerminationChecker 控制
- round_robin：简单轮替（不考虑优先级，测试用）
- free_form：Agent 自由输出，不做 SpeakerSelector（不推荐，可能穷举）

每个模式对应不同的 TeamRunner 调用方式。

EffectiveRunPolicy：根据 TeamSpec 默认值和 TeamRunConfig 运行时覆盖，
计算出真正生效的运行策略，确保在 TerminationChecker / ReviewRepairLoop /
TeamRunner / TeamGraph / API 返回值中看到一致的配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TeamRunMode(str, Enum):
    CONTROLLED_GROUP_CHAT = "controlled_group_chat"
    ROUND_ROBIN = "round_robin"
    FREE_FORM = "free_form"


@dataclass
class EffectiveRunPolicy:
    """根据 TeamSpec 默认值和 TeamRunConfig 运行时覆盖计算出的有效策略。

    构造原则：
    - RunConfig 中不为 None / 不含默认值的字段，覆盖 TeamSpec 对应值。
    - RunConfig 中保持默认值的字段，取 TeamSpec 对应值。
    - 最终结果确保 review_required/max_rounds/max_review_cycles 在所有组件中一致。
    """

    review_required: bool = True
    max_rounds: int = 20
    max_review_cycles: int = 3
    termination_policy: str = "review_passed_or_max_rounds"

    @classmethod
    def from_team_and_run_config(
        cls,
        team_spec: Any,
        run_config: Any | None = None,
    ) -> "EffectiveRunPolicy":
        """从 TeamSpec 和 TeamRunConfig 计算有效运行策略。

        Args:
            team_spec: TeamSpec 实例（含默认值）
            run_config: TeamRunConfig 实例（运行时的覆盖配置，可为 None）

        Returns:
            EffectiveRunPolicy: 真正生效的策略值
        """
        # 从 TeamSpec 读取默认值
        review_required = getattr(team_spec, "review_required", True)
        max_rounds = getattr(team_spec, "max_rounds", 20)
        max_review_cycles = getattr(team_spec, "max_review_cycles", 3)
        termination_policy = getattr(team_spec, "termination_policy", "review_passed_or_max_rounds")

        # RunConfig 覆盖：检查显式设置的字段
        if run_config is not None:
            # max_rounds：若 RunConfig 设置了非 spec 默认值则覆盖
            rc_max_rounds = getattr(run_config, "max_rounds", None)
            if rc_max_rounds is not None and rc_max_rounds != 20:
                max_rounds = rc_max_rounds

            # review_required：若 RunConfig 显式设置了则覆盖
            rc_review = getattr(run_config, "review_required", None)
            if rc_review is not None:
                review_required = rc_review

        return cls(
            review_required=review_required,
            max_rounds=max_rounds,
            max_review_cycles=max_review_cycles,
            termination_policy=termination_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_required": self.review_required,
            "max_rounds": self.max_rounds,
            "max_review_cycles": self.max_review_cycles,
            "termination_policy": self.termination_policy,
        }
