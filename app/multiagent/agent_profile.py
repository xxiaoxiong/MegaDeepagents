"""AgentProfile + CapabilityRegistry：Worker 能力隔离与动态团队选择。

requirements（docs/upgradePhaseTwo.md 四、七）：

AgentProfile:
- 独立模型：worker 身份、能力集、各项 policy
- allowed_tools 真正用于过滤传入 DeepAgent 的工具
- 未声明工具默认拒绝（不再默认全开）
- 每种角色有不同工具/动作权限

CapabilityRegistry:
- register(profile)
- find_workers(required_capabilities) -> list[AgentProfile]
- score_worker(profile, task, runtime_metrics) -> float
- 动态根据 TaskNode.required_capabilities 选择 Worker
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ModelPolicy(BaseModel):
    """模型选择与预算策略。"""

    provider: str = Field(default="deepseek", description="模型提供商，如 deepseek / openai / anthropic")
    model_name: str = Field(default="deepseek-reasoner", description="模型名")
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1)
    timeout_seconds: float = Field(default=60.0, ge=1.0)


class ToolPolicy(BaseModel):
    """工具权限策略。"""

    allowed_tools: list[str] = Field(
        default_factory=list,
        description="允许的工具名列表；空 = 拒绝全部",
    )
    deny_all_by_default: bool = Field(
        default=True,
        description="是否默认拒绝所有未声明的工具",
    )
    allow_file_read: bool = Field(default=True)
    allow_file_write: bool = Field(default=False)
    allow_shell: bool = Field(default=False)


class MemoryPolicy(BaseModel):
    """记忆策略。"""

    enabled: bool = Field(default=True)
    private_scope: str | None = Field(default=None)
    max_retrieve: int = Field(default=5, ge=0)
    tiers: list[str] = Field(default_factory=lambda: ["working", "episodic"])


class WorkspacePolicy(BaseModel):
    """工作空间隔离策略。"""

    allow_shared_read: bool = Field(default=True)
    allow_shared_write: bool = Field(default=False)
    max_workspace_size_mb: int = Field(default=50, ge=1)
    allowed_extensions: list[str] = Field(default_factory=list)


class SandboxPolicy(BaseModel):
    """沙箱策略。"""

    enabled: bool = Field(default=False)
    network_access: bool = Field(default=False)
    temp_dir: str | None = Field(default=None)


class ContextPolicy(BaseModel):
    """上下文预算策略。"""

    max_prompt_chars: int = Field(default=8000, ge=100)
    max_history_messages: int = Field(default=20, ge=0)
    include_artifacts_in_context: bool = Field(default=True)


class AgentProfile(BaseModel):
    """Worker 的完整能力与权限配置。

    用于 CapabilityRegistry 动态调度；AgentSpec 继续保持为"团队模板"定义，
    但集成时可以把 AgentSpec 的字段映射成 AgentProfile。
    """

    id: str = Field(..., description="唯一 Worker ID")
    name: str = Field(default="", description="可读名")
    role: str = Field(default="worker", description="主角色")
    description: str = Field(default="")
    capabilities: set[str] = Field(
        default_factory=set,
        description="该 Worker 拥有的能力标签，如 {'coding', 'file_write', 'testing'}",
    )

    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    workspace_policy: WorkspacePolicy = Field(default_factory=WorkspacePolicy)
    sandbox_policy: SandboxPolicy = Field(default_factory=SandboxPolicy)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)

    max_concurrency: int = Field(default=1, ge=1, le=8)

    metadata: dict[str, Any] = Field(default_factory=dict)

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def has_all_capabilities(self, required: set[str]) -> bool:
        return required.issubset(self.capabilities)


@dataclass
class RuntimeMetrics:
    """运行时指标：用于 Worker 评分与动态路由。"""

    avg_response_time: float = 0.0
    success_rate: float = 1.0
    total_tasks: int = 0
    failed_tasks: int = 0
    current_load: int = 0
    last_active: datetime | None = None


class CapabilityRegistry:
    """能力注册中心：动态 Worker 选择。

    线程安全（跨并发调度路径）。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, AgentProfile] = {}
        self._cap_to_profiles: dict[str, set[str]] = defaultdict(set)  # capability → profile_ids
        self._metrics: dict[str, RuntimeMetrics] = defaultdict(RuntimeMetrics)
        self._lock = threading.RLock()

    # ===== 注册与管理 =====

    def register(self, profile: AgentProfile) -> None:
        """注册一个 Worker Profile。已存在则更新。"""
        with self._lock:
            # 清除旧的能力索引
            old: AgentProfile | None = self._profiles.get(profile.id)
            if old:
                for cap in old.capabilities:
                    self._cap_to_profiles[cap].discard(profile.id)
                    if not self._cap_to_profiles[cap]:
                        del self._cap_to_profiles[cap]

            self._profiles[profile.id] = profile
            for cap in profile.capabilities:
                self._cap_to_profiles[cap].add(profile.id)

    def unregister(self, profile_id: str) -> None:
        """移除一个 Worker Profile。"""
        with self._lock:
            profile = self._profiles.pop(profile_id, None)
            if profile:
                for cap in profile.capabilities:
                    self._cap_to_profiles[cap].discard(profile_id)
                    if not self._cap_to_profiles[cap]:
                        del self._cap_to_profiles[cap]
                self._metrics.pop(profile_id, None)

    def get_profile(self, profile_id: str) -> AgentProfile | None:
        with self._lock:
            return self._profiles.get(profile_id)

    def list_profiles(self) -> list[AgentProfile]:
        with self._lock:
            return list(self._profiles.values())

    # ===== 查找（按能力） =====

    def find_workers(self, required_capabilities: set[str]) -> list[AgentProfile]:
        """找到满足所有必需能力的 Worker。

        每个 Worker 必须拥有 required_capabilities 中**全部**能力。
        """
        with self._lock:
            if not required_capabilities:
                return list(self._profiles.values())

            # 从第一个能力的候选开始取交集
            cap_list = list(required_capabilities)
            candidates: set[str] = set(self._cap_to_profiles.get(cap_list[0], set()))
            for cap in cap_list[1:]:
                candidates &= self._cap_to_profiles.get(cap, set())

            return [self._profiles[pid] for pid in candidates if pid in self._profiles]

    def find_best_worker(self, required_capabilities: set[str]) -> AgentProfile | None:
        """返回评分最高的 Worker。"""
        candidates = self.find_workers(required_capabilities)
        if not candidates:
            return None
        # 按评分降序排列；取最高（无需显式 top-k，用 max）
        scored = [(self.score_worker(p), p) for p in candidates]
        scored.sort(key=lambda x: (-x[0], x[1].id))
        return scored[0][1]

    def select_profile(self, required_capabilities) -> "AgentProfile":
        """从必需能力选一个匹配 Worker。

        与 find_best_worker 区别：找不到时返回 default coder profile 而非 None，
        让 Scheduler 路径始终能拿到一个执行器（避免无声空指针）。
        """
        caps = set(required_capabilities or [])
        best = self.find_best_worker(caps)
        if best is not None:
            return best
        # 兜底：返回 default coder profile
        from app.multiagent.executor import _fallback_coder_profile
        return _fallback_coder_profile()

    def score_worker(
        self,
        profile: AgentProfile,
    ) -> float:
        """计算 Worker 的评分（越高越好）。

        权重：
        - 成功率：0～1，x40
        - 空载：当前 load == 0 ? +30 : -10*load
        - 近期活跃：30分钟内活跃过 +20，否则 -10
        - idle_time（可选）：空闲越久加分越高（鼓励轮换）
        """
        with self._lock:
            metrics = self._metrics.get(profile.id, RuntimeMetrics())

            score = 50.0  # 基础分

            # 成功率（重要）
            score += metrics.success_rate * 40.0

            # 负载惩罚
            if metrics.current_load == 0:
                score += 30.0
            else:
                score -= 10.0 * metrics.current_load

            # 近期活跃加分（活跃 = 有负载但不繁忙）
            if metrics.last_active:
                idle_hours = (datetime.utcnow() - metrics.last_active).total_seconds() / 3600
                if idle_hours < 0.5:
                    score += 10.0  # 正在热池里
                elif idle_hours < 24:
                    score += 5.0  # 最近活跃过
                # > 24h 不额外加分

            return score

    # ===== 指标记录 =====

    def record_success(self, profile_id: str) -> None:
        with self._lock:
            m = self._metrics[profile_id]
            m.total_tasks += 1
            m.success_rate = 1.0 if m.total_tasks == 0 else (
                (m.total_tasks - m.failed_tasks) / m.total_tasks
            )
            m.last_active = datetime.utcnow()
            m.current_load = max(0, m.current_load - 1)

    def record_failure(self, profile_id: str) -> None:
        with self._lock:
            m = self._metrics[profile_id]
            m.total_tasks += 1
            m.failed_tasks += 1
            m.success_rate = 1.0 if m.total_tasks == 0 else (
                (m.total_tasks - m.failed_tasks) / m.total_tasks
            )
            m.last_active = datetime.utcnow()
            m.current_load = max(0, m.current_load - 1)

    def increment_load(self, profile_id: str) -> None:
        with self._lock:
            self._metrics[profile_id].current_load += 1

    def get_metrics(self, profile_id: str) -> RuntimeMetrics | None:
        with self._lock:
            return self._metrics.get(profile_id)

    # ===== 预置 =====

    def register_default_profiles(self) -> None:
        """注册默认 Worker Profiles（与 default_teams.py 对标）。"""
        profiles: list[AgentProfile] = [
            AgentProfile(
                id="planner",
                name="Planner",
                role="Planner",
                description="高层计划拆解者",
                capabilities={"planning", "summarization"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.5),
                tool_policy=ToolPolicy(allowed_tools=[], deny_all_by_default=True),
                context_policy=ContextPolicy(max_prompt_chars=12000),
            ),
            AgentProfile(
                id="coder",
                name="Coder",
                role="Coder",
                description="代码实现者",
                capabilities={"coding", "file_read", "file_write", "shell_execute"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.3),
                tool_policy=ToolPolicy(
                    allowed_tools=["create_file", "edit_file", "execute", "read_file", "list_dir"],
                    deny_all_by_default=True,
                    allow_file_read=True,
                    allow_file_write=True,
                    allow_shell=True,
                ),
                workspace_policy=WorkspacePolicy(
                    allow_shared_read=True,
                    allow_shared_write=False,
                ),
                max_concurrency=2,
            ),
            AgentProfile(
                id="tester",
                name="Tester",
                role="Tester",
                description="测试实现者",
                capabilities={"testing", "file_read", "file_write", "shell_execute"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.3),
                tool_policy=ToolPolicy(
                    allowed_tools=["execute", "read_file", "create_file", "list_dir"],
                    deny_all_by_default=True,
                    allow_file_read=True,
                    allow_file_write=False,  # 不能改业务代码
                    allow_shell=True,
                ),
                max_concurrency=3,
            ),
            AgentProfile(
                id="reviewer",
                name="ReviewerAgent",
                role="ReviewerAgent",
                description="代码与产物评审者",
                capabilities={"reviewing", "file_read"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.2),
                tool_policy=ToolPolicy(
                    allowed_tools=["read_file", "list_dir"],
                    deny_all_by_default=True,
                    allow_file_read=True,
                    allow_file_write=False,
                    allow_shell=False,
                ),
                max_concurrency=3,
            ),
            AgentProfile(
                id="researcher",
                name="Researcher",
                role="Researcher",
                description="信息调研者",
                capabilities={"research", "file_read", "web_research"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.7),
                tool_policy=ToolPolicy(
                    allowed_tools=["search", "fetch_url", "read_file", "list_dir"],
                    deny_all_by_default=True,
                    allow_file_read=True,
                    allow_file_write=False,
                ),
                max_concurrency=3,
            ),
            AgentProfile(
                id="finalizer",
                name="Finalizer",
                role="Finalizer",
                description="最终输出集结者",
                capabilities={"summarization", "file_read", "file_write"},
                model_policy=ModelPolicy(model_name="deepseek-chat", temperature=0.2),
                tool_policy=ToolPolicy(
                    allowed_tools=["create_file", "read_file", "list_dir"],
                    deny_all_by_default=True,
                    allow_file_read=True,
                    allow_file_write=True,
                ),
                max_concurrency=1,
            ),
        ]
        for p in profiles:
            self.register(p)


# ===== 全局单例 =====

_global_registry: CapabilityRegistry | None = None
_registry_lock = threading.Lock()


def get_capability_registry() -> CapabilityRegistry:
    global _global_registry
    if _global_registry is None:
        with _registry_lock:
            if _global_registry is None:
                _global_registry = CapabilityRegistry()
                _global_registry.register_default_profiles()
    return _global_registry


def reset_capability_registry() -> None:
    """测试隔离用：重置单例。"""
    global _global_registry
    with _registry_lock:
        _global_registry = None
