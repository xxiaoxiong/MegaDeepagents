# MegaDeepagents 对齐 Claude Code Agent Teams：二次深度审查与 Codex 改造任务书

> 仓库：`https://github.com/xxiaoxiong/MegaDeepagents.git`  
> 审查基线：`main` 分支，提交 `07e1ae30c000fe2791403f39e0f57bcca3abc0c7`  
> 目标：把当前“已搭出 Agent Team 模块但主链仍未完全闭环”的实现，改造成真正可运行、可验证、可恢复的 Agent Team Runtime。  
> 执行者：Codex / Claude Code / 其他具备代码编辑和测试能力的 AI 编程智能体

---

## 0. 你的执行身份

你现在是本项目的首席多智能体架构师、Python 运行时工程师、并发与持久化工程师、安全工程师和测试负责人。

这不是一次文档整理任务，而是一次真实代码修复和架构收敛任务。

你必须：

1. 阅读当前仓库真实代码，而不是只相信 `docs/MegaDeepagents_改造进度总结.md`。
2. 先复现主链问题，再修改代码。
3. 不允许用 Mock、FakeExecutor、固定返回值或“默认成功”掩盖生产路径失败。
4. 不允许只新增接口、数据模型和注释而不接入生产主链。
5. 所有关键能力必须通过真实端到端测试证明。
6. CLI、FastAPI 和后续 Web UI 必须使用同一套 Team Runtime。
7. 不能为了“测试通过”削弱断言或绕开真实实现。
8. 不能把本次范围扩展为分布式集群、Kafka 或 Kubernetes；先把单机 Agent Team Runtime 做正确。

---

# 1. 当前架构判断

当前项目已经具备以下正确基础：

- `TeamRuntimeFacade`
- `TeamRunContext`
- `AgentProfile`
- `AgentInstance`
- `AgentRegistry`
- `TaskGraph`
- `TaskBoard`
- `ParallelTeamScheduler`
- `Mailbox`
- `DeepAgentExecutor`
- `ArtifactStore`
- `Verifier`
- `ResumeCoordinator`
- SQLite 持久化表
- `TASK_TEAM` / `DISCUSSION` 双模式
- LangSmith / TeamEvent 可观测性基础

方向总体正确，但当前更接近：

```text
“Agent Team 所需模块已创建”
```

还没有达到：

```text
“多个持续存在的独立 Teammate 在统一控制面下可靠协作”
```

当前最核心的问题不是继续添加更多模块，而是：

```text
生产主链没有把现有模块正确、完整、可靠地串起来。
```

---

# 2. 目标架构

最终 TASK_TEAM 主链应为：

```text
User Goal
   ↓
TeamRuntimeFacade
   ↓
TeamRunContext（持久化）
   ↓
Team Lead / Orchestrator
   ├─ 规划 TaskGraph
   ├─ 根据 TaskGraph 与团队模板创建 Teammate
   ├─ 创建共享 TaskBoard
   └─ 启动 AgentRuntimeManager
           ↓
┌───────────────────────────────────────────────┐
│              Team Control Plane               │
│                                               │
│ TeamRunStore                                  │
│ AgentRuntimeManager                           │
│ TaskBoard / TaskGraph                         │
│ Mailbox                                       │
│ Capability Registry                           │
│ Permission Broker                             │
│ Hook Gate                                     │
│ Artifact Store                                │
│ Checkpoint / Resume                           │
└───────────────┬───────────────┬───────────────┘
                ↓               ↓
        Teammate A         Teammate B
        独立 agent_id       独立 agent_id
        独立 session_id     独立 session_id
        独立 thread_id      独立 thread_id
        独立 Inbox          独立 Inbox
        持续 Agent Loop     持续 Agent Loop
                └───────┬───────┘
                        ↓
              Workspace / Artifact
                        ↓
                    Verifier
          PASS / REPAIR / REPLAN / HITL
                        ↓
                     Team Lead
                        ↓
                    Final Result
```

第一版约束：

- 每个 Team 只有一个 Lead。
- 默认 3–5 个 Teammate。
- 不允许嵌套 Team。
- 单进程 + `asyncio` + SQLite。
- `DISCUSSION` 保留旧轮流发言模式。
- `TASK_TEAM` 必须是真实 Agent Team 主链。

---

# 3. P0：当前必须先修复的阻断性 Bug

以下问题会直接导致真实 TASK_TEAM 运行失败、假成功或状态错误。必须最先修复。

---

## P0-1：正常运行没有创建任何 Teammate

### 现状

`AgentRegistry.create_agent()` 的生产调用主要出现在恢复逻辑，正常：

```text
create_run
→ start_run
→ orchestrator
→ scheduler
```

链路中没有明确的 Team Builder / Spawn Teammate 阶段。

因此正常 Run 启动后，`AgentRegistry` 很可能为空，`ParallelTeamScheduler.find_idle()` 永远找不到 Worker，最终消耗 `max_rounds` 后失败。

### 修复要求

新增明确的团队构建阶段：

```text
app/multiagent/team_builder.py
```

实现：

```python
class TeamBuilder:
    async def build_team(
        self,
        ctx: TeamRunContext,
        team_spec: TeamSpec,
        task_graph: TaskGraph,
    ) -> list[AgentInstance]:
        ...
```

规则：

1. 从 `TeamSpec` / `AgentProfile` 创建 AgentInstance。
2. 为每个成员生成稳定：
   - `agent_id`
   - `session_id`
   - `thread_id`
   - `checkpoint_namespace`
3. 立即持久化到 `agent_instances`。
4. 注册到 `AgentRegistry`。
5. 创建 Mailbox Inbox。
6. 写入 `agent_spawned` TeamEvent。
7. 每个 Profile 的实际能力、工具、模型策略必须同步到 AgentInstance。
8. 默认不超过 5 个 Teammate。
9. TASK_TEAM 启动后必须至少有一个可执行 Worker，否则立即失败，不能空转。

建议软件团队第一版成员：

```text
Lead / Planner
Coder
Tester
Reviewer
Finalizer
```

复杂任务可按 TaskGraph 能力需求选择性创建，不能每次无脑启动全部成员。

---

## P0-2：ParallelTeamScheduler 向真实 Executor 传入 `task_dag=None`

### 现状

当前调度器调用类似：

```python
executor.execute_task(None, task.task_id, task_input)
```

但 `DeepAgentExecutor.execute_task()` 立即访问：

```python
task_dag.nodes.get(task_id)
```

真实生产路径会失败。现有 FakeExecutor 测试忽略了 DAG，因此没有发现问题。

### 修复要求

不要继续把 `None` 传给 Executor。

选择一种统一设计：

### 推荐方案

让 Scheduler 持有 TaskGraph：

```python
class ParallelTeamScheduler:
    def __init__(
        self,
        run_id: str,
        task_graph: TaskGraph,
        ...
    ):
        self.task_graph = task_graph
```

调用：

```python
result = await asyncio.to_thread(
    executor.execute_task,
    self.task_graph,
    task.task_id,
    task_input,
)
```

或者将 `TaskAssignment` 在 Scheduler 中完整构造，彻底移除 Executor 对 TaskGraph 的依赖。

必须增加真实 `DeepAgentExecutor` 协议测试，禁止只用忽略参数的 FakeExecutor。

---

## P0-3：Artifact 注册代码存在未定义变量和缺失循环

### 现状

`DeepAgentExecutor.execute()` 已经得到：

```python
produced_files = [...]
```

但后续注册 Artifact 时没有：

```python
for f in produced_files:
```

却直接使用 `f`，同时使用未定义的 `relative_path`。

只要真实 Worker 产出文件并启用 ArtifactStore，就可能抛异常。

### 修复要求

正确实现：

```python
for file_path in produced_files:
    relative_path = file_path.relative_to(context.workspace_root).as_posix()

    content = file_path.read_bytes()

    artifact = artifact_store.create(
        run_id=context.run_id,
        task_id=assignment.task_id,
        type=infer_artifact_type(file_path),
        relative_path=relative_path,
        content=content,
        produced_by=agent_instance.agent_id,
        metadata={
            "profile_id": profile.id,
            "original_name": file_path.name,
        },
    )
```

额外要求：

- 支持文本和二进制。
- 递归扫描。
- 忽略 `.git`、`__pycache__`、缓存和临时文件。
- 没有真实文件时，不得伪造 Artifact ID。
- OutputContract 要求 Artifact 时，没有 Artifact 必须判失败。
- Artifact 注册失败必须让 TaskRun 失败。
- 添加真实 Artifact 端到端测试。

---

## P0-4：调度器可能把同一个 Idle Agent 同时分配给多个任务

### 现状

多个 `_run_one()` 协程会先执行：

```text
find_idle()
```

然后才进入全局 Semaphore 和任务 Claim。

多个协程可能同时拿到同一个仍为 IDLE 的 Agent。

### 修复要求

Agent 选择与预留必须是原子的。

新增：

```python
class AgentRegistry:
    def reserve_idle_agent(
        self,
        run_id: str,
        required_capabilities: set[str],
        task_id: str,
    ) -> AgentInstance | None:
        ...
```

在同一个锁内完成：

1. 查找满足全部能力的 IDLE Agent。
2. 检查当前负载与 `max_concurrency`。
3. 将 Agent 转为 `CLAIMING`。
4. 设置 `current_task_id` 或增加 active task 集。
5. 更新心跳。
6. 持久化。

任务 Claim 失败时必须释放 Agent Reservation。

不能先选 Agent，再在锁外修改状态。

---

## P0-5：Agent/Profile 级 `max_concurrency` 没有真实生效

### 现状

当前只有全局：

```python
asyncio.Semaphore(max_concurrency)
```

`AgentInstance.max_concurrency` 和 `AgentProfile.max_concurrency` 没有真正限制生产调度。

### 修复要求

实现三级限制：

```text
全局 Run 并发
Profile 并发
AgentInstance 并发
```

建议：

```python
global_sem: asyncio.Semaphore
profile_sems: dict[str, asyncio.Semaphore]
agent_sems: dict[str, asyncio.Semaphore]
```

或通过 Registry 的原子负载计数完成。

必须有真实 Scheduler 测试：

- 两个 Agent 可并行。
- 同一个 Agent `max_concurrency=1` 不得并发。
- 同一 Profile `max_concurrency=2` 最多两个任务并行。
- 测试必须调用 `ParallelTeamScheduler.run()`，不能在测试中手写串行循环。

---

## P0-6：Worker success 被直接标记为 Task `SUCCEEDED`

### 现状

Executor 返回 `success=True` 后，TaskBoard 直接进入 `SUCCEEDED`。

这违背：

```text
执行没有抛异常 ≠ 产物通过验收
```

### 修复要求

扩展任务状态：

```python
class BoardTaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    PRODUCED = "produced"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    REPAIR_REQUIRED = "repair_required"
    REPLAN_REQUIRED = "replan_required"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

正确流程：

```text
Worker success
→ PRODUCED
→ Verifier
→ PASS
→ SUCCEEDED
```

只有 Verifier 可以将 Task 变为 `SUCCEEDED`。

TaskBoard 的 `complete()` 应拆分成：

```python
mark_produced(...)
mark_verifying(...)
mark_verified(...)
mark_repair_required(...)
```

---

## P0-7：Verifier 没有真正接入本次 Run 的 ArtifactStore

### 现状

`TeamRuntimeFacade` 创建了 ArtifactStore，并注入 Executor，但创建 Verifier 时没有：

```python
artifact_store=artifact_store
```

因此 Verifier 的 ArtifactStore enrichment 没有生效。

Orchestrator 仍然自行扫描 Task 目录并用 Task objective 作为 preview，容易造成假验证。

### 修复要求

创建：

```python
verifier = Verifier(
    artifact_store=artifact_store,
    llm_rubric=...,
)
```

Verifier 必须以 ArtifactStore 为事实源。

Orchestrator 不应重复自行扫描目录；改为传：

```python
artifact_store.list_by_run(ctx.run_id)
```

每个任务的 OutputContract 必须转换为验证项。

编码任务至少验证：

- 必需文件存在。
- 文件非空。
- 测试命令返回 0。
- 构建 / Lint（若配置）。
- Artifact Hash 一致。
- 验收标准满足。

---

## P0-8：Verifier 返回 REPAIR 时，Repair Loop 可能什么也不做

### 现状

当前 Repair 逻辑主要给 `FAILED` 节点添加修复任务。

但常见情况是：

```text
所有 Worker 都返回 success
DAG 节点全部 SUCCEEDED
Verifier 判断质量不足 → REPAIR
```

此时没有 FAILED 节点，系统可能重复验证同一批产物，直到达到修复轮次上限。

### 修复要求

Verifier 的 `ValidationResult.proposed_tasks` 和 `failed_criteria` 必须真正驱动 Repair。

实现：

```python
def build_repair_tasks(
    result: ValidationResult,
    dag: TaskGraph,
) -> list[TaskNode]:
    ...
```

规则：

- 对每个高优先级失败条件创建 Repair Task。
- Repair Task 依赖原产物任务。
- 指定 `parent_artifact_id`。
- 通过能力路由到 Coder / Tester / Reviewer。
- 新 Artifact 生成新版本。
- 原 Artifact 标记 rejected/superseded。
- 修复完成后重新验证。
- 禁止重复创建同一 Repair Task，使用幂等键。

---

## P0-9：API 只在创建任务时走新 Runtime，查询、消息和取消仍走旧 TeamRunner

### 现状

`POST /team-tasks` 使用 TeamRuntimeFacade。

但以下接口仍查询旧 `MultiAgentStore` / `TeamRoom` / `TeamRunner`：

- GET 任务状态
- GET 消息
- GET state
- GET agents
- POST 注入消息
- POST cancel

新 TASK_TEAM Run 很可能无法被这些接口正确查询或控制。

### 修复要求

建立统一新 API：

```text
POST   /team-runs
GET    /team-runs/{run_id}
POST   /team-runs/{run_id}/cancel
POST   /team-runs/{run_id}/resume

GET    /team-runs/{run_id}/agents
POST   /team-runs/{run_id}/agents/{agent_id}/messages
POST   /team-runs/{run_id}/agents/{agent_id}/pause
POST   /team-runs/{run_id}/agents/{agent_id}/resume
POST   /team-runs/{run_id}/agents/{agent_id}/stop

GET    /team-runs/{run_id}/tasks
GET    /team-runs/{run_id}/messages
GET    /team-runs/{run_id}/artifacts
GET    /team-runs/{run_id}/events
```

旧 `/team-tasks` 保持兼容，但内部必须调用 TeamRuntimeFacade，不能再加载旧 Runner。

---

## P0-10：`cancel_run()` 只是修改内存字符串，不会停止任务

### 修复要求

每个 Run 建立：

```python
cancel_event: asyncio.Event
active_tasks: dict[task_run_id, asyncio.Task]
```

取消时：

1. 设置 Cancel Event。
2. Scheduler 停止调度新任务。
3. 对可取消 Task 调用 `task.cancel()`。
4. Agent 转为 STOPPING / IDLE。
5. TaskRun 标记 CANCELLED。
6. 写 TeamEvent。
7. 持久化 Run 状态。
8. 不允许取消接口只修改 `_active_runs["status"]`。

---

## P0-11：正常 Scheduler 状态无论成功失败都被 Orchestrator 当作成功

### 现状

Parallel Scheduler 返回后，Orchestrator 路径可能直接 `return True`，没有严格检查：

```text
completed
failed
incomplete
cancelled
```

同步 fallback 还可能把 `incomplete` 当作成功。

### 修复要求

使用强类型结果：

```python
class ScheduleStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    WAITING_HUMAN = "waiting_human"
```

Orchestrator 必须按状态处理，禁止 Boolean 模糊化。

只有：

```text
ScheduleStatus.COMPLETED
```

才能进入整体 Verifier。

---

## P0-12：TaskBoard 以 `task_id` 为全局键，跨 Run 会相互覆盖

### 现状

TaskBoard 内部：

```python
_tasks: dict[str, BoardTask]
```

如果两个 Run 都有 `task_1`，后创建的会覆盖前一个。

### 修复要求

所有 Task 存储必须使用复合标识：

```python
TaskKey = tuple[run_id, task_id]
```

或使用全局唯一 Task ID：

```text
{run_id}:{local_task_id}
```

推荐数据库使用：

```sql
PRIMARY KEY (run_id, task_id)
```

所有查询必须显式带 `run_id`。

`latest_task_run()`、Artifact by task、Task Claim 等也必须按 Run 隔离。

---

## P0-13：TaskBoard 的依赖和能力判断使用“任意匹配”且会忽略缺失依赖

### 修复要求

能力要求必须是全部满足：

```python
required_capabilities <= agent_capabilities
```

缺失依赖必须视为错误或不可认领：

```python
for dep in dependencies:
    if dep not in task_store:
        return not_claimable("missing_dependency")
```

禁止使用会忽略不存在依赖的生成式过滤。

---

## P0-14：真实端到端测试仍在测试旧 TeamRunner

### 现状

标记为真实 LLM 的复杂团队测试仍直接执行旧：

```python
TeamRunner.create(...)
runner.run()
```

它只能证明 DISCUSSION 旧路径可运行，不能证明新 TASK_TEAM 可运行。

### 修复要求

新增真实新主链测试：

```python
ctx = await runtime.create_run(...)
result = await runtime.start_run(ctx, ...)
```

至少验证：

- 正常 Spawn Teammate。
- AgentRegistry 非空。
- TaskGraph 真实创建。
- 多任务进入 Scheduler。
- DeepAgentExecutor 收到真实 DAG。
- Worker 产生真实文件。
- ArtifactStore 注册成功。
- Verifier 读取真实 Artifact。
- Run 最终状态正确。
- API 可以查询同一个 Run。

旧 TeamRunner 的 live test 可以保留为 DISCUSSION 回归测试，但不得再作为 Agent Team 完成证明。

---

# 4. P1：Agent Teams 核心能力仍未真正完成

---

## P1-1：AgentInstance 只是元数据，不是持续运行的 Teammate

当前 `DeepAgentExecutor` 每个 Task 都重新：

```python
create_deep_agent(...)
```

并使用：

```text
thread_id = run_id:task_id
```

这意味着 Session 属于 Task，不属于 Agent。

### 目标

实现：

```text
AgentProfile = 静态模板
AgentInstance = 团队成员身份
AgentSession = 可持续恢复的 DeepAgent 会话
```

新增：

```text
app/multiagent/agent_runtime_manager.py
app/multiagent/teammate_loop.py
```

```python
class AgentRuntimeManager:
    async def spawn(...)
    async def get_or_create_session(...)
    async def execute_assignment(...)
    async def wake(...)
    async def pause(...)
    async def stop(...)
    async def restore(...)
```

DeepAgent Thread ID 必须使用：

```python
agent_instance.thread_id
```

不能使用 Task ID 代替 Agent 会话 ID。

Task 作为新消息送入该 Agent 的既有 Session。

---

## P1-2：恢复时没有保留原 session_id/thread_id

当前 ResumeCoordinator 重新调用 `create_agent()`，会生成新的 Session/Thread，只保留 Agent ID。

### 修复要求

`create_agent()` 增加：

```python
session_id_override
thread_id_override
checkpoint_namespace_override
created_at_override
workspace_root
```

恢复时完整还原。

Checkpoint 不仅要“读出来并记录日志”，还要实际用于恢复 Agent Graph / Thread。

---

## P1-3：`resume_run()` 没有真正续跑

当前恢复协调器完成后，没有重新启动 Scheduler。

### 修复要求

跨进程恢复流程：

```text
加载 TeamRun
→ 加载 TaskGraph / TaskBoard
→ 加载 AgentInstances
→ 加载 Mailbox
→ 加载 Artifacts
→ 恢复 Checkpoint
→ 将旧 RUNNING Task 标记 RECOVERING
→ 重建 AgentRuntime
→ 启动 Scheduler
→ 继续执行
```

必须从“全局单例均为空”的状态完成恢复测试。

禁止测试预先在内存 TaskBoard 中创建 Task。

---

## P1-4：TaskBoard 和 AgentRegistry 的持久化接口没有接入生产写路径

虽然 SQLite CRUD 已存在，但正常：

- Agent Spawn
- Agent 状态变化
- Task Claim
- Task Start
- Task Produced
- Task Verified
- Task Failed

未全部自动落库。

### 修复要求

不要依靠调用者记得手动写库。

推荐 Repository Pattern：

```python
class AgentRepository:
    def save(instance)
    def load(...)
```

```python
class TaskRepository:
    def create(...)
    def claim_atomic(...)
    def update_status(...)
```

Registry/Board 的所有状态突变必须通过 Repository。

---

## P1-5：Mailbox 不是可等待的实时邮箱

当前 wakeup 只是向 deque 放一条消息，没有真正唤醒长期等待的 Agent Loop。

### 修复要求

每个 Agent 建立：

```python
asyncio.Queue[MailboxMessage]
asyncio.Event
```

SQLite 是事实源，Queue 是实时通知。

提供：

```python
async def wait_for_message(agent_id, timeout=None)
```

消息到达后真正唤醒 Teammate Loop。

---

## P1-6：Mailbox 没有接入 Agent Prompt / Context

当前 Executor 的 Prompt 只包含任务目标和描述，没有读取：

- Agent Inbox
- Lead 消息
- 其他 Teammate 消息
- ArtifactRef
- 项目上下文
- 自己的历史摘要

### 修复要求

构建独立 ContextBuilder：

```python
class AgentContextBuilder:
    def build(
        agent: AgentInstance,
        assignment: TaskAssignment,
        inbox_messages: list[MailboxMessage],
        artifact_refs: list[ArtifactRef],
        project_context: ProjectContext,
    ) -> AgentInvocationContext:
        ...
```

不要复制 Lead 的完整历史。

---

## P1-7：没有 Team Lead 控制工具

实现 Team Lead 工具：

```text
create_task
update_task
assign_task
unassign_task
spawn_teammate
list_teammates
send_message
request_plan
approve_plan
reject_plan
request_review
pause_agent
resume_agent
shutdown_agent
request_human
get_team_state
get_task_board
```

Lead 不能直接改数据库，也不能绕过 Verifier。

---

## P1-8：没有 Plan Approval

实现可选计划审批：

```text
Teammate 进入 Plan Mode
→ 提交 PLAN_SUBMITTED
→ Lead / User 审批
→ APPROVED 后获得写工具
```

高风险任务默认先规划后执行。

---

## P1-9：没有 TaskCreated / TaskCompleted / TeammateIdle 等 Hook Gate

实现确定性 Hook：

```text
TaskCreated
TaskClaimed
TaskStarted
PreToolUse
PostToolUse
TaskProduced
TaskCompleted
TeammateIdle
AgentFailed
PlanSubmitted
PermissionRequested
RunCompleted
```

关键规则：

- 无 Artifact 不允许 TaskProduced。
- 测试失败不允许 TaskCompleted。
- 未审批危险操作不允许 PreToolUse。
- Verifier 未 PASS 不允许 RunCompleted。
- Agent 有未处理任务/消息时不允许 Idle。

---

# 5. P1：安全问题

---

## P1-10：read_file / list_dir 没有路径隔离

它们目前可以读取任意路径。

必须统一通过：

```python
safe_resolve(base, requested)
```

限制在允许目录。

---

## P1-11：路径前缀判断存在绕过风险

禁止：

```python
abs_path.startswith(workspace_root)
```

使用：

```python
Path(abs_path).resolve().is_relative_to(Path(root).resolve())
```

并处理符号链接越界。

---

## P1-12：ArtifactStore 的 relative_path 未做安全验证

`os.path.join(root, artifact.path)` 可能被 `../` 或绝对路径逃逸。

所有 Artifact read/write 必须使用同一安全路径解析器。

---

## P1-13：Shell 黑名单不足且仍使用任意 `shell=True`

仅检查命令前缀无法防止：

- `echo x && rm ...`
- PowerShell 组合命令
- Python 子进程间接执行
- 编码/转义绕过

第一版至少做到：

1. 结构化 Command Policy。
2. 默认允许明确命令族：
   - pytest
   - python
   - npm test
   - lint/build
   - git status/diff
3. 危险命令进入 HITL。
4. 不允许任意网络下载。
5. 记录返回码。
6. 非 0 返回码让工具结果显式失败。
7. 支持取消与超时。

---

## P1-14：ToolPolicy 字段之间不一致

例如 Tester 可能：

```text
allowed_tools 包含 create_file
但 allow_file_write=False
```

Executor 当前主要看 allowed_tools，可能仍允许写文件。

### 修复要求

最终权限必须取交集：

```python
effective_tools = declared_tools ∩ boolean_policy ∩ workspace_policy ∩ runtime_permission
```

出现配置冲突时默认拒绝，并记录告警。

---

## P1-15：Capability 找不到时回退到高权限 DefaultCoder

这会造成权限升级。

### 修复要求

找不到匹配能力时：

```text
返回 no_matching_worker
→ Lead 重新规划 / Spawn 合适 Agent / 请求人工
```

不能自动回退到拥有 Shell 和写权限的 Coder。

---

# 6. P1：模型、预算和上下文

---

## P1-16：model_policy 没有真正切换 provider/model

当前只绑定 temperature/max_tokens/timeout，忽略 Profile 的 provider/model_name。

修复：

```python
build_model_for_policy(policy)
```

必须根据：

```text
provider
model_name
base_url
api_key reference
temperature
max_tokens
timeout
```

真正构建不同模型。

---

## P1-17：Task Budget 没有执行

落实：

- max_attempts
- max_tool_calls
- max_seconds
- max_tokens
- Run 总预算
- Agent 总预算

超预算进入：

```text
REPLAN
HUMAN_REQUIRED
FAILED
```

不能无限轮询。

---

## P1-18：输入 Artifact 没有传递给下游 Agent

Planner 已定义 `input_artifact_ids`，但 Executor Prompt 没有加载内容。

修复：

- 只读取 Task 声明的 Artifact。
- 验证 run_id。
- 加入摘要和必要片段。
- 大文件按需读取，避免完整塞进上下文。
- 记录消费关系。

---

# 7. P1：数据库和一致性

---

## P1-19：缺少持久化 TeamRun / TeamTask / TaskDependency / Claim / Lease

新增或完善：

```text
team_runs
team_tasks
task_dependencies
task_claims
agent_leases
validation_results
artifact_relations
human_decisions
```

关键约束：

```sql
PRIMARY KEY (run_id, task_id)
UNIQUE (run_id, agent_id)
```

---

## P1-20：SQLite 并发策略不足

配置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

关键状态更新使用事务和 CAS。

不要每个小字段更新都无条件 commit，适当使用 Unit of Work。

---

## P1-21：当前 schema_version 不是实际迁移系统

不要只把版本号直接设为 3。

实现显式 migration：

```text
v1_to_v2
v2_to_v3
v3_to_v4
```

每次升级在事务中执行。

---

## P1-22：查询没有 Run 范围

修复：

- latest_task_run(task_id, run_id)
- list_artifacts_by_task(run_id, task_id)
- checkpoint by run/agent
- message by run/agent
- agent ID 冲突

---

# 8. P2：产品和工程质量优化

1. README 改为新 TASK_TEAM 架构，旧 Controlled Group Chat 标记为 DISCUSSION。
2. 删除误提交的 `.claude/worktrees/*` Git worktree/gitlink，并加入 `.gitignore`。
3. `runtime/workspaces/`、临时输出和测试生成文件全部忽略。
4. 增加 GitHub Actions：
   - lint
   - type check
   - unit tests
   - integration tests
   - security tests
5. 增加 Ruff / MyPy 或 Pyright。
6. 清理未使用 import、重复状态模型和双 Store。
7. 统一时间为 timezone-aware UTC。
8. 为 Team Runtime 增加 OpenTelemetry / LangSmith 层级 Trace。
9. 增加用户可查看的 Agent Transcript 和 Task Timeline。
10. 后续再考虑 Git Worktree 文件隔离；第一版至少实现文件 Ownership/Write Set 冲突检测。

---

# 9. 测试重构要求

当前“468 passed”不能作为完成依据，因为部分关键测试没有覆盖真实生产主链。

---

## 9.1 必须先增加失败测试

在修复前先写能复现以下问题的测试：

1. 正常 `start_run()` 后 Registry 为空。
2. Scheduler 给真实 Executor 传 `None DAG`。
3. Worker 创建文件后 Artifact 注册抛未定义变量。
4. 两个任务同时拿到同一个 Agent。
5. 两个 Run 均有 `task_1` 时相互覆盖。
6. API POST 后 GET 返回 404 或旧状态。
7. cancel 不会停止 Scheduler。
8. Resume 在清空全局单例后无法恢复 Task。
9. Tester `allow_file_write=False` 仍获得 create_file。
10. Artifact 路径 `../outside` 越界。
11. `find_idle` 只满足部分能力却被选中。
12. Verifier REPAIR 后没有创建修复任务。

修复前测试必须失败，修复后通过。

---

## 9.2 真正的并行测试

必须调用生产 Scheduler：

```python
result = await scheduler.run(real_or_protocol_correct_executor)
```

记录每个 Task：

```text
started_at
finished_at
agent_id
```

断言：

- 两个不同 Agent 的独立任务区间重叠。
- 同一个 Agent 不会被重复分配。
- Profile Semaphore 生效。

禁止测试代码自己 `asyncio.gather()` 两个自定义函数来证明 Scheduler 并行。

---

## 9.3 新 TASK_TEAM 真实 E2E

新增：

```text
tests/e2e/test_task_team_runtime.py
```

场景：

```text
目标：实现一个简单 Python 温度转换模块并写测试
```

要求：

1. 使用 `TeamRuntimeFacade`。
2. 创建至少 Coder、Tester、Reviewer。
3. TaskGraph 至少包含两个可执行任务。
4. Worker 使用真实 DeepAgent Tool Loop。
5. 产生真实 `.py` 文件。
6. ArtifactStore 中可读取。
7. pytest 返回 0。
8. Verifier PASS。
9. API 可查询 Agent、Task、Artifact、Event。
10. 最终 Run COMPLETED。

真实模型测试可用 marker，但必须另有本地 deterministic integration test 覆盖完整协议链。

---

## 9.4 恢复测试

流程：

```text
启动 Run
→ 完成第一个任务
→ 模拟进程退出
→ reset 所有全局单例
→ 从 SQLite resume
→ 已完成任务不重跑
→ 原 agent_id/session_id/thread_id 恢复
→ 剩余任务继续执行
→ Run 完成
```

禁止预先在恢复后的内存 Board 中手动创建 Task。

---

## 9.5 API 一致性测试

```text
POST /team-runs
GET /team-runs/{id}
GET /agents
GET /tasks
POST /agents/{agent}/messages
POST /cancel
POST /resume
```

全部必须操作同一个新 Runtime。

---

# 10. 推荐实施顺序

---

## Phase 1：让新主链真正跑通

1. 修复 Executor Artifact 循环。
2. Scheduler 传真实 DAG/Assignment。
3. TeamBuilder 正常 Spawn Agent。
4. 原子 Reserve Agent。
5. Scheduler 严格处理结果状态。
6. Verifier 注入 ArtifactStore。
7. 增加新 TASK_TEAM E2E。

完成标准：

```text
一个真实任务可以通过新主链完成。
```

---

## Phase 2：修复任务状态与验证闭环

1. 增加 PRODUCED / VERIFYING / REPAIR_REQUIRED。
2. Worker 不再直接 SUCCEEDED。
3. Verifier 按任务验收。
4. Repair 根据 failed criteria 创建真实修复任务。
5. Artifact 版本链。

完成标准：

```text
失败测试能够自动生成 Repair Task，修复后重新验证。
```

---

## Phase 3：持续 Teammate Runtime

1. AgentRuntimeManager。
2. AgentSession 归属于 AgentInstance。
3. 稳定 thread_id。
4. Teammate Loop。
5. Mailbox async wait/wake。
6. 上下文构建和 Inbox 消费。
7. Agent-to-Agent 直接消息。

完成标准：

```text
Agent 完成 Task A 后，以同一身份和 Session 执行 Task B。
```

---

## Phase 4：统一 API、取消和恢复

1. 所有团队 API 改走 TeamRuntime。
2. TeamRun 持久化。
3. TaskBoard/Registry 自动落库。
4. 真 Cancel。
5. 真 Resume。
6. 空内存重启恢复测试。

---

## Phase 5：权限、Hook 和计划审批

1. Safe Path。
2. Shell Policy。
3. Permission Broker。
4. Plan Approval。
5. Hook Gate。
6. 防止 Agent 转述用户权限。

---

## Phase 6：文档、CI 和仓库清理

1. README 更新。
2. 删除误提交 worktree/runtime 文件。
3. GitHub Actions。
4. 架构图。
5. 已知限制。

---

# 11. 禁止事项

- 不得继续以 FakeExecutor 证明生产 Executor 正常。
- 不得把异常捕获后返回 completed。
- 不得在没有 Worker 时反复空转到 max_rounds。
- 不得使用高权限 DefaultCoder 作为能力缺失兜底。
- 不得让 Worker 自己宣布 Task 成功。
- 不得把 Task objective 当作真实 Artifact。
- 不得只读取文件名，不执行测试。
- 不得只恢复 Agent 元数据，不恢复并继续运行。
- 不得通过削弱测试断言获得绿色结果。
- 不得新增第三套多智能体主链。
- 不得删除 DISCUSSION 模式，但它不能继续冒充 TASK_TEAM。

---

# 12. 每阶段交付要求

每阶段完成后输出：

1. 修改文件列表。
2. 根因说明。
3. 设计选择。
4. 新增测试。
5. 测试命令与真实结果。
6. 未完成项。
7. 风险。
8. 是否改变兼容接口。

建议拆分提交：

```text
fix(runtime): make task-team production path executable
fix(executor): register real artifacts safely
feat(team): spawn and persist teammate instances
fix(scheduler): reserve agents atomically and enforce concurrency
feat(tasks): add produced and verification states
feat(runtime): add persistent teammate sessions and loops
feat(mailbox): add async delivery and idle wakeup
fix(api): route all team operations through TeamRuntime
feat(recovery): restore and continue team runs
feat(governance): add hooks, plan approval and permission broker
test(e2e): cover real task-team lifecycle
docs: align README with task-team runtime
```

不要推送远程仓库，除非用户明确要求。

---

# 13. 最终验收清单

只有全部满足才可宣布完成：

- [ ] 正常 Run 会创建真实 Teammate。
- [ ] AgentProfile 与 AgentInstance 分离。
- [ ] AgentInstance 复用稳定 Session/Thread。
- [ ] Scheduler 向 Executor 传真实 Task 数据。
- [ ] 不会把同一 Agent 同时错误分配给多个任务。
- [ ] 全局/Profile/Agent 并发限制生效。
- [ ] Task 在 Verifier PASS 前不会 SUCCEEDED。
- [ ] Artifact 注册真实、递归、安全、可恢复。
- [ ] Verifier 读取真实 Artifact 和测试输出。
- [ ] REPAIR 会创建并执行真实修复任务。
- [ ] Mailbox 消息能唤醒并影响目标 Agent。
- [ ] API 所有读写操作使用同一 Runtime。
- [ ] Cancel 真正停止执行。
- [ ] Resume 在空内存进程中能继续执行。
- [ ] Task/Artifact/Agent 全部按 run_id 隔离。
- [ ] 路径越权和危险 Shell 被代码层阻止。
- [ ] 能力缺失不会升级成高权限 Coder。
- [ ] 新 TASK_TEAM E2E 通过。
- [ ] 旧 DISCUSSION 回归测试通过。
- [ ] GitHub Actions 通过。
- [ ] README 与实际架构一致。

---

# 14. 最终执行指令

现在开始工作。

第一步不是立即大规模重构，而是：

1. 读取本文提到的所有文件。
2. 运行当前全量测试。
3. 新增 P0 失败复现测试。
4. 输出一个简短内部实施计划。
5. 从 Phase 1 开始直接修改。
6. 每阶段运行针对性测试和全量测试。
7. 持续完成，直到新 TASK_TEAM 真实端到端通过。

当文档总结与真实代码冲突时，以真实代码和运行结果为准。

始终遵循：

```text
真实运行优于模块存在
持续 AgentInstance 优于一次性 Task Agent
共享任务板优于轮流发言
真实 Artifact 优于 Agent 自述
Verifier 判定优于 Worker 自我宣布
原子状态转换优于 Prompt 约定
可恢复执行优于只恢复元数据
生产 E2E 优于 Mock 单元测试
```
