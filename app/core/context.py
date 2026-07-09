"""运行时上下文：per-run 的 user_id、feature_flags、request_id 等元数据。"""

from dataclasses import dataclass, field


@dataclass
class AgentContext:
    """Agent 运行时的上下文数据，不参与 state 持久化。"""

    user_id: str = "default"
    feature_flags: dict[str, bool] = field(default_factory=dict)
    request_id: str = ""
