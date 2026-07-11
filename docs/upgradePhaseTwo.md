你现在继续改造 MegaDeepagents。第一阶段已经完成现有多智能体主链修复。本阶段目标是：

**将项目从“固定角色轮流对话”升级为真正的 Orchestrator–Worker + Task Graph + 真实工具型 Worker 架构。**

这是本项目最关键的一次架构升级。必须实际修改代码、运行测试并完成端到端验证。不要只写设计文档，不要保留空壳接口，不要在遇到复杂问题时退回固定流程或把业务步骤写死。

# 一、目标架构

最终主链应接近：

```text
User Goal
   ↓
Complexity Router
   ├─ Simple → Single DeepAgent
   └─ Complex → Orchestrator
                    ↓
              Structured Task Graph
                    ↓
              Durable Scheduler
         ┌──────────┼──────────┐
         ↓          ↓          ↓
     Worker A    Worker B    Worker C
    Research     Coding       Testing
         └──────────┼──────────┘
                    ↓
               Artifact Store
                    ↓
             Evaluator / Verifier
             ├─ PASS
             ├─ REPAIR
             ├─ REPLAN
             └─ HUMAN_REQUIRED
```

多智能体不能再主要依赖“谁下一轮发言”，而应主要依赖：

```text
哪些 TaskNode 已满足依赖
→ 哪些 Worker 具备所需能力
→ 哪些任务可并行
→ 哪些结果需要验证
```

# 二、先做架构审查

开始修改前先确认第一阶段代码现状，并阅读：

- 单 Agent 的 `build_agent()` 和 DeepAgents 执行链
- ToolRegistry
- Backend
- Permissions
- TaskRunner
- TeamRunner / TeamGraph
- AgentSpec / TeamSpec
- SharedTeamState
- Store
- API
- 现有测试

先形成内部改造计划，然后直接实施，不需要向用户请求确认。

# 三、建立统一 AgentExecutor

当前单 Agent 路径已经具备真实 DeepAgents 工具执行能力，多 Agent Worker 必须复用这套能力。

设计统一接口，例如：

```python
class AgentExecutor(Protocol):
    async def execute(
        self,
        assignment: TaskAssignment,
        profile: AgentProfile,
        context: ExecutionContext,
    ) -> AgentExecutionResult:
        ...
```

实现至少两类 Executor：

1. `DeepAgentExecutor`
   - 用于 Coder、Tester、Researcher 等真实 Worker。
   - 能调用受限工具、文件系统和沙箱。
   - 能产生真实 Artifact。
   - 支持 checkpoint、timeout、cancel 和 HITL。
2. `ModelDecisionExecutor`
   - 用于 Planner、Router、轻量 Evaluator 等只需要结构化决策的节点。
   - 不默认获得写文件或 Shell 权限。

禁止所有 Agent 都继续使用同一个裸 `build_model().invoke(prompt)` 逻辑。

# 四、实现真实的 Worker 能力隔离

每个 Worker 必须有独立配置：

```python
class AgentProfile:
    id: str
    role: str
    description: str
    capabilities: set[str]
    model_policy: ModelPolicy
    tool_policy: ToolPolicy
    memory_policy: MemoryPolicy
    workspace_policy: WorkspacePolicy
    sandbox_policy: SandboxPolicy
    context_policy: ContextPolicy
    max_concurrency: int
```

要求：

- `allowed_tools` 必须真正用于过滤传入 DeepAgent 的工具。
- 未声明工具默认拒绝，不再默认全开。
- Coder 可以读写代码和执行构建。
- Tester 可以读代码、写测试和执行测试，但不能无授权修改业务实现。
- Reviewer 默认只读。
- Planner 默认不能直接修改 Artifact。
- Finalizer 不能绕过 Verifier 宣布成功。
- 每个 Worker 的执行记录必须包含实际调用过的工具。

# 五、引入结构化 Task Graph

不要继续使用：

```python
plan: str
completed_steps: list[str]
```

作为主要任务模型。

实现严格数据模型：

```python
class TaskNode(BaseModel):
    id: str
    title: str
    objective: str
    description: str
    status: TaskNodeStatus
    dependencies: list[str]
    required_capabilities: list[str]
    preferred_agent_profile: str | None
    assigned_agent_id: str | None
    input_artifact_ids: list[str]
    output_contract: OutputContract
    priority: int
    attempts: int
    max_attempts: int
    budget: TaskBudget
    error: ExecutionError | None
```

以及：

```python
class TaskGraph(BaseModel):
    root_task_id: str
    nodes: dict[str, TaskNode]
    version: int
    created_at: datetime
    updated_at: datetime
```

需要实现：

- DAG 环检测。
- dependency 校验。
- Ready Task 计算。
- Task 状态合法转换。
- TaskGraph 版本化。
- 动态新增 Repair Task。
- 动态新增补充调研或验证 Task。
- 局部 Replan，而不是每次重建整个计划。

# 六、Planner 必须输出结构化计划

Planner 输出不能再只是自然语言 plan。

Planner 必须返回符合 Schema 的任务图建议，包括：

- Task ID
- 目标
- 依赖
- 所需能力
- 输入
- 输出契约
- 验收条件
- 预算建议
- 是否允许并行

对 Planner 输出执行：

1. Pydantic 校验。
2. DAG 校验。
3. 能力是否存在校验。
4. 输出契约校验。
5. 失败时结构化重试。
6. 多次失败后进入人工或降级策略。

# 七、实现 Capability Registry 和动态团队

固定 `software_dev_team`、`research_team` 可以保留为 Preset，但不再是运行时的唯一方式。

实现：

```python
class CapabilityRegistry:
    def register(profile)
    def find_workers(required_capabilities)
    def score_worker(profile, task, runtime_metrics)
```

Orchestrator 根据 TaskNode 动态选择 Worker，而不是根据：

```text
planning → Planner
executing → Coder
reviewing → Reviewer
```

硬编码路由。

至少支持能力：

- planning
- research
- coding
- testing
- reviewing
- summarization
- file_read
- file_write
- shell_execute
- web_research
- mcp_access

# 八、实现可控并行调度

Scheduler 应能够：

- 找出所有 Ready Task。
- 对无依赖任务并行执行。
- 对同一 Workspace 的冲突写入进行串行化或锁控制。
- 设置全局并发上限。
- 设置每个 Agent Profile 并发上限。
- 并发执行失败时不影响其他独立任务提交结果。
- 在 checkpoint 中保存每个 TaskNode 状态。

不要用“多个线程随意启动”的方式实现并行。

优先使用当前 LangGraph 版本支持的并行节点、Send、fan-out/fan-in 或等价可靠机制。实现前检查实际依赖版本和 API，不要根据旧版文档猜测。

# 九、实现 Run 级 Workspace 隔离

当前全局共享 Workspace 必须改造。

至少做到：

```text
runtime/workspaces/{run_id}/
runtime/workspaces/{run_id}/tasks/{task_id}/
runtime/workspaces/{run_id}/shared/
```

规则：

- Worker 默认只写自己的 Task Workspace。
- 明确批准后才能修改 shared Artifact。
- 读其他 Task 输出应通过 ArtifactRef。
- 禁止跨 Run 访问。
- 单 Agent 旧路径保留兼容适配，但新多 Agent 路径必须隔离。
- Artifact 自动扫描不能误收其他任务文件。

# 十、实现 Artifact-first 协作

Agent 之间不应通过超长消息传递代码和报告。

实现真实 Artifact 模型：

```python
class Artifact:
    id: str
    run_id: str
    task_id: str
    type: str
    path: str
    content_hash: str
    size_bytes: int
    version: int
    produced_by: str
    status: str
    metadata: dict
```

要求：

- Artifact 内容真实存在。
- 计算 Hash。
- 支持版本。
- 支持 Producer、Reviewer、ValidationResult。
- Agent 消息只传 Artifact ID、摘要和关键证据。
- Verifier 直接读取 Artifact，不依赖 Agent 转述。
- 修复产物生成新版本，不覆盖审计记录。

# 十一、实现严格 Action Protocol

禁止继续以任意 `dict[str, Any]` 作为核心协议。

使用 Pydantic Discriminated Union，例如：

```python
AgentAction = Annotated[
    SendMessageAction
    | CreateArtifactAction
    | UpdateTaskAction
    | RequestReviewAction
    | HandoffAction
    | RequestHumanAction
    | NoOpAction,
    Field(discriminator="type"),
]
```

每种 Action：

- 有独立字段。
- 有明确权限。
- 有 Schema 校验。
- 有审计数据。
- 有幂等键。
- 禁止未知字段静默通过。
- 非法 Action 不得继续产生副作用。

# 十二、实现 Verifier，而非让 Finalizer 自我宣布完成

增加统一验证结果：

```python
class ValidationResult(BaseModel):
    verdict: Literal[
        "pass",
        "repair",
        "replan",
        "human_required",
        "fail",
    ]
    scores: dict[str, float]
    failed_criteria: list[CriterionFailure]
    evidence: list[EvidenceRef]
    proposed_tasks: list[TaskProposal]
```

Verifier 至少支持：

1. 程序化验证：
   - 文件存在
   - JSON Schema
   - 测试命令
   - 构建命令
   - Lint
   - 输出格式
2. LLM Rubric 验证：
   - 完整性
   - 正确性
   - 与目标一致性
   - 证据充分度
3. 人工审批：
   - 高风险写操作
   - 无法自动判断
   - 预算超限
   - 冲突无法处理

只有 Verifier 返回 pass，Run 才能进入 COMPLETED。

# 十三、实现复杂度路由

不是所有任务都应进入多智能体。

实现可解释的 Complexity Router：

- 简单问答、单文件生成、低风险任务：单 Agent。
- 多模块、可并行、需要研究+实现+测试的任务：多 Agent。
- 高风险或边界不明确任务：人工确认或限制模式。

Router 结果必须记录原因，允许 API 调用方强制指定模式：

```text
auto
single
multi
```

# 十四、LangGraph 成为唯一多智能体编排运行时

本阶段结束后：

- TeamRunner 只能是 Facade。
- 不再维护独立 while 主循环。
- 所有多智能体执行通过统一 Graph。
- Graph State 保存 Run、TaskGraph、调度状态、预算、Artifact 和验证结果。
- 每个关键节点可 checkpoint。
- resume 不重复已完成 Task。
- 已提交 Tool Side Effect 必须具备幂等保护。

# 十五、测试要求

必须增加：

1. Planner 生成合法 DAG。
2. 非法环形依赖被拒绝。
3. 两个无依赖 Task 真正并行。
4. 有依赖 Task 不会提前执行。
5. Coder 真实写入文件。
6. Tester 真实执行测试。
7. Reviewer 无法修改代码。
8. 未授权工具调用被硬拒绝。
9. Worker 产出 ArtifactRef 和真实文件。
10. Repair 产生新 Artifact 版本。
11. Verifier 未通过时不能 Completed。
12. Replan 只修改受影响子图。
13. 进程恢复后不重复已完成 Worker。
14. single/multi/auto 路由正确。
15. 使用确定性 Fake Model 完成完整 E2E。
16. live model 测试独立标记。

# 十六、本阶段验收任务

实现一个真实端到端测试任务：

```text
创建一个小型 Python REST 服务：
- Planner 拆分架构、实现、测试、评审任务
- Coder 真实生成代码
- Tester 真实运行测试
- Reviewer 读取代码和测试结果
- Verifier 执行 pytest
- 若失败则创建 Repair Task
- 修复后再次测试
- 通过后输出最终 Artifact
```

验收要求：

- 至少两个可并行任务确实并行执行。
- 文件真实存在。
- pytest 真实通过。
- 至少产生一次 Artifact。
- 最终状态由 Verifier 决定。
- Run 可从 checkpoint 恢复。
- Trace 中能看到 Planner、TaskNode、Worker、Tool、Artifact、Verifier。

# 十七、最终交付

完成后输出：

1. 新架构图。
2. 核心数据模型。
3. 旧架构到新架构的迁移说明。
4. 修改文件列表。
5. E2E 任务运行记录。
6. 测试命令和结果。
7. 当前完成度。
8. 进入生产化阶段前仍缺少的能力。

不得以“接口已预留”“后续可实现”代替本阶段要求的核心功能。