"""结构化 TaskGraph：替代 `state.plan: str` 的伪任务模型。

P2-1（Orchestrator–Worker 升级）核心数据模型。要求（`docs/upgradePhaseTwo.md` 五）：

- TaskNode / TaskGraph 严格 Pydantic 数据模型
- DAG 环检测 / dependency 校验
- Ready Task 计算（依赖全完成 + 满足能力约束）
- 任务状态合法转换（PENDING→READY→RUNNING→SUCCEEDED/FAILED/SKIPPED）
- TaskGraph 版本化（每次突变 +1）
- 动态新增 Repair / 补充调研 / 验证 Task（局部 Replan，不重建整图）

本模块只提供 **数据与图算法**；调度 / 并行由 `scheduler.py` 负责，
执行由 `executor.py` 负责，校验由 `verifier.py` 负责。
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class TaskNodeStatus(str, Enum):
    """任务节点状态机。

    合法转换：
        PENDING → READY                 （依赖满足，可被调度）
        READY → RUNNING                 （被 Scheduler 选中并提交）
        RUNNING → SUCCEEDED             （Worker 正常完成且产出 Artifact）
        RUNNING → FAILED                （Worker 失败 / 超时 / 抛错）
        RUNNING → CANCELLED             （全程 cancel）
        FAILED → PENDING                （重试或 Replan 后重新入队）
        SUCCEEDED → FAILED              （Verifier 反查失败，回退需要修复）
        FAILED → SKIPPED                （超过 max_attempts 不再重试）
        * → SKIPPED                     （手动跳过 / 替代方案）
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# 状态转移表：键 = 当前状态，值 = 允许转到的状态集合
_LEGAL_TRANSITIONS: dict[TaskNodeStatus, set[TaskNodeStatus]] = {
    TaskNodeStatus.PENDING: {
        TaskNodeStatus.READY,
        TaskNodeStatus.RUNNING,  # 允许直调度（READY 跳过）
        TaskNodeStatus.SKIPPED,
        TaskNodeStatus.CANCELLED,
    },
    TaskNodeStatus.READY: {
        TaskNodeStatus.RUNNING,
        TaskNodeStatus.SKIPPED,
        TaskNodeStatus.CANCELLED,
    },
    TaskNodeStatus.RUNNING: {
        TaskNodeStatus.SUCCEEDED,
        TaskNodeStatus.FAILED,
        TaskNodeStatus.CANCELLED,
    },
    TaskNodeStatus.SUCCEEDED: {
        TaskNodeStatus.FAILED,  # Verifier 反查失败回退
    },
    TaskNodeStatus.FAILED: {
        TaskNodeStatus.PENDING,  # 重试 / Replan 重新入队
        TaskNodeStatus.SKIPPED,
        TaskNodeStatus.CANCELLED,
    },
    TaskNodeStatus.SKIPPED: set(),
    TaskNodeStatus.CANCELLED: set(),
}


def is_legal_transition(from_status: TaskNodeStatus, to_status: TaskNodeStatus) -> bool:
    return to_status in _LEGAL_TRANSITIONS.get(from_status, set())


class TaskBudget(BaseModel):
    """任务预算约束。

    Scheduler 应在超过预算时拒绝继续投入资源，并触发 Verifier 的
    `human_required` 或 `replan` verdict。
    """

    max_attempts: int = Field(default=2, ge=1, le=10)
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    max_seconds: float = Field(default=120.0, ge=1.0)
    max_tokens: int | None = Field(default=None, ge=1)


class OutputContract(BaseModel):
    """Task 的输出契约：规定 Worker 必须产出什么。

    Verifier 用 acceptance_criteria 做程序化验证；
    required_artifacts 决定是否产生 ArtifactRef。
    """

    artifact_type: str = Field(default="any")
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(
        default_factory=list,
        description="必须产出的 Artifact role 列表；为空表示无强约束。",
    )
    allow_parallel: bool = Field(
        default=True,
        description="本 Task 是否可与其它独立 Task 并行执行。",
    )


class ExecutionError(BaseModel):
    code: str = ""
    message: str = ""
    tool_name: str | None = None
    attempt: int = 0
    occurred_at: datetime = Field(default_factory=datetime.utcnow)


class TaskNode(BaseModel):
    """结构化任务节点。"""

    id: str
    title: str = ""
    objective: str = ""
    description: str = ""

    status: TaskNodeStatus = TaskNodeStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_agent_profile: str | None = None
    assigned_agent_id: str | None = None

    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    output_contract: OutputContract = Field(default_factory=OutputContract)

    priority: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    budget: TaskBudget = Field(default_factory=TaskBudget)

    error: ExecutionError | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "TaskNode":
        if self.id in self.dependencies:
            raise ValueError(f"TaskNode {self.id} 不能依赖自己")
        return self

    def is_terminal(self) -> bool:
        return self.status in (
            TaskNodeStatus.SUCCEEDED,
            TaskNodeStatus.FAILED,
            TaskNodeStatus.SKIPPED,
            TaskNodeStatus.CANCELLED,
        )

    def can_be_ready(self) -> bool:
        return self.status in (TaskNodeStatus.PENDING, TaskNodeStatus.READY)


class TaskGraph(BaseModel):
    """结构化任务图：DAG + 节点表 + 版本化。"""

    root_task_id: str
    nodes: dict[str, TaskNode] = Field(default_factory=dict)
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def _root_exists(self) -> "TaskGraph":
        # root 存在性只在 nodes 非空时检查：允许先构造空图再逐个 add_node
        if self.nodes and self.root_task_id and self.root_task_id not in self.nodes:
            raise ValueError(
                f"TaskGraph.root_task_id='{self.root_task_id}' 不在 nodes 中"
            )
        return self

    # ===== 不可变校验 =====

    def validate(self) -> None:
        """全图校验：所有 dependencies 指向存在节点 + 无环。"""
        self._check_all_dependencies_exist()
        if self.has_cycle():
            raise ValueError("TaskGraph 检测到环")

    def _check_all_dependencies_exist(self) -> None:
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep not in self.nodes:
                    raise ValueError(
                        f"TaskNode {node.id} 依赖不存在的节点 {dep!r}"
                    )

    # ===== 图算法 =====

    def has_cycle(self) -> bool:
        """基于 Kahn 算法的环检测（不依赖 NetworkX，纯标准库）。"""
        indeg: dict[str, int] = {nid: 0 for nid in self.nodes}
        adj: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes.values():
            for dep in node.dependencies:
                adj[dep].append(node.id)
                indeg[node.id] += 1
        # 注意：依赖图中 dep → node 表示节点依赖于 dep；入度统计的是被依赖关系反向
        # 但环检测只要任一方向有环即可，这里标准 Kahn：
        queue: deque[str] = deque(nid for nid, d in indeg.items() if d == 0)
        visited = 0
        # Kahn 复制版
        local_indeg = dict(indeg)
        while queue:
            nid = queue.popleft()
            visited += 1
            for succ in adj[nid]:
                local_indeg[succ] -= 1
                if local_indeg[succ] == 0:
                    queue.append(succ)
        return visited != len(self.nodes)

    def topological_order(self) -> list[str]:
        """返回拓扑顺序。若有环则抛 ValueError。"""
        self._check_all_dependencies_exist()
        if self.has_cycle():
            raise ValueError("TaskGraph 有环，无法得到拓扑序")
        indeg: dict[str, int] = {nid: 0 for nid in self.nodes}
        adj: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes.values():
            for dep in node.dependencies:
                adj[dep].append(node.id)
                indeg[node.id] += 1
        # 优先按 priority 高 → 低，同优先级按 id 字典序，保证确定性
        queue = sorted(
            (nid for nid, d in indeg.items() if d == 0),
            key=lambda nid: (-self.nodes[nid].priority, nid),
        )
        result: list[str] = []
        local_indeg = dict(indeg)
        # 简化的确定性 Kahn：每次取队列首元素，再重新排序加入的新节点
        from heapq import heappush, heappop
        heap: list[tuple[int, str]] = []
        for nid in queue:
            heappush(heap, (-self.nodes[nid].priority, nid))
        while heap:
            _, nid = heappop(heap)
            result.append(nid)
            for succ in adj[nid]:
                local_indeg[succ] -= 1
                if local_indeg[succ] == 0:
                    heappush(heap, (-self.nodes[succ].priority, succ))
        return result

    def ready_tasks(self) -> list[TaskNode]:
        """计算所有 Ready Task：未结束 + 依赖全部 SUCCEEDED。"""
        ready: list[TaskNode] = []
        for node in self.nodes.values():
            if not node.can_be_ready():
                continue
            deps_ok = all(
                self.nodes[dep].status == TaskNodeStatus.SUCCEEDED
                for dep in node.dependencies
            )
            if deps_ok:
                ready.append(node)
        # 确定性排队：priority 降序 + id 升序
        ready.sort(key=lambda n: (-n.priority, n.id))
        return ready

    def descendants(self, node_id: str) -> set[str]:
        """返回 node_id 的所有直接/间接后继（含传递依赖）。"""
        if node_id not in self.nodes:
            return set()
        # 构建 child 映射
        children: dict[str, list[str]] = defaultdict(list)
        for n in self.nodes.values():
            for dep in n.dependencies:
                children[dep].append(n.id)
        seen: set[str] = set()
        stack = list(children.get(node_id, []))
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            stack.extend(children.get(nid, []))
        return seen

    # ===== 突变操作（每次自增 version） =====

    def add_node(self, node: TaskNode) -> None:
        if node.id in self.nodes:
            raise ValueError(f"TaskNode {node.id} 已存在")
        self.nodes[node.id] = node
        self._touch()

    def update_status(self, node_id: str, status: TaskNodeStatus) -> bool:
        """合法转换 → 修改 + 自增 version。非法转换记 WARNING 返回 False。"""
        node = self.nodes.get(node_id)
        if node is None:
            return False
        if not is_legal_transition(node.status, status):
            return False
        node.status = status
        if status == TaskNodeStatus.RUNNING and node.started_at is None:
            node.started_at = datetime.utcnow()
        if status in (TaskNodeStatus.SUCCEEDED, TaskNodeStatus.FAILED,
                      TaskNodeStatus.SKIPPED, TaskNodeStatus.CANCELLED):
            node.completed_at = datetime.utcnow()
        self._touch()
        return True

    def assign_agent(self, node_id: str, agent_id: str | None) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        node.assigned_agent_id = agent_id
        self._touch()

    def record_attempt(self, node_id: str, error: ExecutionError | None = None) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        node.attempts += 1
        if error is not None:
            node.error = error
        self._touch()

    def add_repair_task(
        self,
        target_node_id: str,
        repair_objective: str,
        required_capabilities: list[str] | None = None,
    ) -> TaskNode:
        """动态新增 Repair Task（局部 Replan）。

        修复节点**替换**出问题的目标 Task（而非依赖它）：
        - 继承依赖：repair 的 dependencies = 原 target 的 upstream dependencies
          （如果 repair 依赖已 FAILED 的 target，它永远无法 ready）
        - 不重建整图，只追加一个修复节点
        - 原 target 保留为 FAILED 用于审计；下游新节点改为依赖 repair
        """
        if target_node_id not in self.nodes:
            raise ValueError(f"target_node_id={target_node_id!r} 不存在")
        target = self.nodes[target_node_id]
        repair_id = f"{target_node_id}__repair_v{self.version}"
        # 避免重复
        i = 0
        while repair_id in self.nodes:
            i += 1
            repair_id = f"{target_node_id}__repair_v{self.version}_{i}"
        node = TaskNode(
            id=repair_id,
            title=f"Repair {target_node_id}",
            objective=repair_objective,
            description=f"在 {target_node_id} 基础上的修复任务",
            status=TaskNodeStatus.PENDING,
            # 继承 target 的上游依赖（不依赖已 FAILED 的 target）
            dependencies=list(target.dependencies),
            required_capabilities=required_capabilities or ["coding", "file_write"],
            output_contract=OutputContract(
                artifact_type="patch",
                description="修复产物",
                acceptance_criteria=["修复后必须通过 Verifier"],
                required_artifacts=["repair_patch"],
            ),
            priority=10,
        )
        self.add_node(node)
        # 将下游节点从依赖 target 改为依赖 repair
        for n in self.nodes.values():
            if target_node_id in n.dependencies:
                n.dependencies = [
                    repair_id if d == target_node_id else d
                    for d in n.dependencies
                ]
        # 原 target 进入 SKIPPED：repair 已顶替它，原 FAILED 节点不再阻塞
        # all_succeeded()，也不会被 ready_tasks() 调度（terminal）。FAILED → SKIPPED
        # 必须是合法转换，否则只记 WARNING，调度行为仍能正确（all_succeeded 仅看
        # 是否全 SUCCEEDED，FAILED 节点天然使 all_succeeded=False；本步是 best-effort）。
        self.update_status(target_node_id, TaskNodeStatus.SKIPPED)
        return node

    def accept_artifact(self, node_id: str, artifact_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        if artifact_id not in node.output_artifact_ids:
            node.output_artifact_ids.append(artifact_id)
            self._touch()

    # ===== 整体状态 =====

    def all_succeeded(self) -> bool:
        if not self.nodes:
            return True
        # SKIPPED 节点视为已解决（被 repair task 顶替的 FAILED 原 task）
        return all(
            n.status in (TaskNodeStatus.SUCCEEDED, TaskNodeStatus.SKIPPED)
            for n in self.nodes.values()
        )

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            counts[n.status.value] += 1
        return {
            "version": self.version,
            "total": len(self.nodes),
            "by_status": dict(counts),
            "ready": [n.id for n in self.ready_tasks()],
        }

    # ===== 私有 =====

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.utcnow()
