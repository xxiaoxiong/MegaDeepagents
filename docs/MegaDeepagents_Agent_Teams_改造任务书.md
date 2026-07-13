# MegaDeepagents 向 Claude Code Agent Teams 架构对齐改造任务书

> 适用仓库：`https://github.com/xxiaoxiong/MegaDeepagents.git`  
> 目标读者：Claude Code / 其他 AI 编程智能体  
> 文档用途：直接作为项目改造任务输入  
> 核心目标：将 MegaDeepagents 从“多角色轮流对话 + 一次性 Worker 调度”升级为“持续存在的 Agent Team Runtime”

---

## 0. 执行角色

你现在是该项目的首席架构师、资深 Python 工程师、多智能体运行时工程师和测试负责人。

你需要直接审查并改造当前仓库，不能只输出设计文档，不能只创建空接口，不能把关键功能留成 TODO，也不能通过 Mock、固定返回值或“默认成功”伪造完成状态。

所有改造必须：

1. 基于当前仓库真实代码实施。
2. 保留已有可用能力，避免无必要重写。
3. 实际运行测试。
4. 实际运行端到端任务。
5. 修复过程中发现的相关架构问题。
6. 保证 CLI、API 和 Web 使用同一套多智能体运行时。
7. 最终提交完整改造报告、测试结果和剩余风险。

---

# 1. 改造背景

当前 MegaDeepagents 已经具备较多多智能体基础模块，包括：

- `TeamRunner`
- `TeamRoundExecutor`
- `SpeakerSelector`
- `MessageBus`
- `AgentInbox`
- `SharedTeamState`
- `ActionGuard`
- `ConflictResolver`
- `ReviewRepairLoop`
- `TaskGraph`
- `TaskScheduler`
- `AgentProfile`
- `CapabilityRegistry`
- `DeepAgentExecutor`
- `RunWorkspace`
- `ArtifactStore`
- `Verifier`
- SQLite 持久化
- LangGraph Checkpoint
- LangSmith 可观测性
- FastAPI / CLI / SSE

但是当前项目存在两套并行架构。

## 1.1 旧多智能体主链

旧主链大致为：

```text
User Goal
  ↓
TeamRunner
  ↓
SpeakerSelector
  ↓
一次只选择一个 Agent
  ↓
调用一次 LLM
  ↓
解析 JSON Actions
  ↓
MessageBus / Inbox / SharedTeamState
  ↓
下一轮
```

这是一种受控群聊式多智能体架构，具有消息、角色和状态，但它不是完整的 Agent Team Runtime。

主要问题：

- 每轮只有一个 Speaker。
- Agent 不是持续存在的运行实例。
- Agent 没有长期独立上下文和生命周期。
- Agent 主要生成 Action，而不是真正持续执行工具。
- 缺少共享子任务板和原子认领。
- 缺少 Idle Agent 唤醒。
- 不支持真正的并行 Teammate Loop。

## 1.2 Phase Two 新主链

Phase Two 已经引入：

```text
Complexity Router
  ↓
Planner
  ↓
TaskGraph
  ↓
Scheduler
  ↓
DeepAgent Worker
  ↓
Artifact
  ↓
Verifier
  ↓
Repair / Replan
```

这个方向正确，但当前仍存在问题：

- `SimpleOrchestrator` 强制调用同步 fallback。
- Ready Task 实际仍可能串行执行。
- `DeepAgentExecutor` 每个 Task 临时创建一个 Agent，缺乏持续 Teammate。
- CLI 与 API 仍可能走不同运行时。
- Workspace 传递链不完整。
- ArtifactStore 没有完全接入 Worker 和 Verifier。
- Worker `success=True` 可能被直接视为 Task 成功。
- Verifier 可能没有读取真实产物。
- AgentProfile 是模板，但缺少 AgentInstance。
- 缺少 TaskRun、AgentLease、原子任务认领和 Agent 心跳。

---

# 2. 总体目标

将项目升级为接近 Claude Code Agent Teams 思路的多智能体运行时：

```text
User
  ↓
Team Lead
  ↓
Team Control Plane
  ├─ TeamManager
  ├─ AgentRuntimeManager
  ├─ Shared Task Board
  ├─ Capability Registry
  ├─ Mailbox
  ├─ Permission Broker
  ├─ Hook Gate
  └─ Checkpoint / Recovery
       ↓
  ┌───────────────┬───────────────┬───────────────┐
  ↓               ↓               ↓
Teammate A      Teammate B      Teammate C
独立 Session     独立 Session     独立 Session
独立 Context     独立 Context     独立 Context
独立 Inbox       独立 Inbox       独立 Inbox
持续 Agent Loop  持续 Agent Loop  持续 Agent Loop
  └───────────────┴───────────────┴───────────────┘
                       ↓
              Workspace / Artifact Store
                       ↓
                  Verifier
              PASS / REPAIR / REPLAN
                       ↓
                   Team Lead
```

---

# 3. 必须对齐的 Claude Code Agent Teams 核心思想

本项目不需要复制 Claude Code 的内部代码，但必须对齐以下架构原则。

## 3.1 Lead + Teammates

- 一个 Team 只有一个 Lead。
- Teammate 是独立运行实例，而不是同一个 LLM 的角色标签。
- 每个 Teammate 必须拥有独立：
  - `agent_id`
  - `session_id`
  - `thread_id`
  - Checkpoint Namespace
  - Inbox
  - 状态机
  - 当前任务
  - Transcript
  - Workspace 权限
- 第一版不允许 Teammate 再创建嵌套 Team。

## 3.2 Shared Task Board

团队协作的核心不再是“谁下一轮说话”，而是：

```text
哪些任务已满足依赖
→ 哪些任务可以被认领
→ 哪个 Agent 有匹配能力
→ 哪些任务可以并行
→ 哪些任务正在执行
→ 哪些任务等待验证或修复
```

## 3.3 Mailbox

- Agent 可以直接向另一个 Agent 发消息。
- 消息应进入目标 Agent 的私有 Inbox。
- Idle Agent 收到消息后应被唤醒。
- MessageBus 既要持久化，也要支持实时事件通知。
- Agent 不能自动读取其他 Agent 的完整上下文。

## 3.4 独立上下文

- Teammate 不复制 Lead 的完整对话历史。
- Teammate 获得：
  - 项目级说明
  - AgentProfile
  - 当前 TaskAssignment
  - 相关 ArtifactRef
  - 自己的 Inbox
  - 自己的历史摘要
- 无关历史不得进入当前 Prompt。

## 3.5 确定性控制面

- LLM 负责高层规划和动态判断。
- 数据库、状态机、权限、任务认领、质量门必须由确定性代码控制。
- 不允许通过自然语言消息直接改变关键状态。
- 不允许 Agent 自己宣布整个 Run 成功。
- 只有 Verifier 返回 PASS，Run 才能完成。

---

# 4. 非目标

本阶段不要优先实现：

- 多机分布式调度
- Kafka / RabbitMQ
- Kubernetes Worker
- 去中心化 Swarm
- 嵌套 Agent Team
- 无限递归派生
- 多团队跨组织协商
- 超复杂 Workflow DSL
- 大规模微服务拆分
- Redis 分布式锁
- 云端队列

第一版保持：

```text
单进程
+ asyncio
+ SQLite
+ LangGraph Checkpoint
+ 本地 Workspace
+ DeepAgents
```

先把单机 Agent Team Runtime 做正确、做稳定。

---

# 5. 强制执行原则

## 5.1 不得保留两套主运行时

旧的 `TeamRunner` 和新的 Phase Two 不得继续作为两个平行主链。

最终必须形成：

```text
TeamRuntimeFacade
  ├─ TASK_TEAM
  └─ DISCUSSION
```

其中：

- `TASK_TEAM` 是默认模式。
- `DISCUSSION` 仅用于辩论、方案讨论、冲突评审等需要轮流发言的任务。
- CLI、API、Web 默认全部进入 `TASK_TEAM`。
- 原 `SpeakerSelector` 只能服务于 `DISCUSSION` 模式。

## 5.2 不允许伪成功

禁止：

- LLM 调用失败后返回 `completed`。
- Worker 没有产物却返回成功。
- Verifier 异常后自动 PASS。
- 测试没有执行却声称通过。
- Artifact 只是字符串 ID，没有真实文件。
- Scheduler 无论内部结果如何都返回成功。
- 用 Stub Executor 作为生产默认实现。

## 5.3 不允许以 Prompt 代替权限

以下内容不能只写在 Prompt 里：

- 文件路径限制
- Shell 命令限制
- 角色工具白名单
- 共享目录写权限
- Task Ownership
- Artifact 访问控制
- 高风险操作审批

必须由代码和运行时实际强制。

---

# 6. 第一阶段：完成现状审查和基线测试

在修改代码前，先完整阅读：

```text
app/core/agent_factory.py
app/task/
app/tools/
app/backends/
app/permissions.py

app/multiagent/team_runner.py
app/multiagent/round_executor.py
app/multiagent/runtime_adapter.py
app/multiagent/speaker_selector.py
app/multiagent/room.py
app/multiagent/bus.py
app/multiagent/inbox.py
app/multiagent/state.py
app/multiagent/store.py

app/multiagent/orchestrator.py
app/multiagent/task_graph.py
app/multiagent/scheduler.py
app/multiagent/executor.py
app/multiagent/agent_profile.py
app/multiagent/planner.py
app/multiagent/verifier.py
app/multiagent/run_workspace.py
app/multiagent/artifact.py

app/api/routes_team.py
app/cli.py
tests/
```

必须先执行：

```bash
python -m pytest -q
```

记录：

- 当前通过数
- 当前失败数
- 当前跳过数
- 当前测试耗时
- 已知失败原因

再运行至少一个旧多智能体任务和一个 Phase Two 任务，记录：

- 实际调用链
- 实际 Workspace
- 实际产物目录
- 是否发生真实工具调用
- 是否发生真实并行
- Verifier 是否读取真实文件
- CLI 与 API 是否走同一主链

审查结果写入：

```text
docs/agent-team-baseline-audit.md
```

---

# 7. 第二阶段：建立统一 Team Runtime

新增：

```text
app/multiagent/team_runtime.py
app/multiagent/team_runtime_service.py
app/multiagent/team_run_context.py
```

## 7.1 TeamRunContext

实现：

```python
class TeamRunContext(BaseModel):
    run_id: str
    team_id: str
    mode: TeamRunMode

    workspace_root: str
    artifact_store_id: str
    checkpoint_namespace: str

    trace_id: str | None = None
    user_id: str | None = None

    created_at: datetime
    metadata: dict[str, Any]
```

要求：

- 从 CLI/API 入口创建一次。
- 全链路显式传递。
- 禁止中间组件使用 `cli_run`、`default_run` 等固定生产回退值。
- 所有 Task、Agent、Artifact、Checkpoint 必须关联同一个 Run ID。

## 7.2 TeamRuntimeFacade

提供统一入口：

```python
class TeamRuntimeFacade:
    async def create_run(...)
    async def start_run(...)
    async def resume_run(...)
    async def cancel_run(...)
    async def get_run(...)
    async def send_message(...)
```

CLI、API、Web 只能调用该 Facade，不得直接实例化旧 `TeamRunner` 或 `SimpleOrchestrator`。

## 7.3 模式

```python
class TeamRunMode(str, Enum):
    TASK_TEAM = "task_team"
    DISCUSSION = "discussion"
```

默认：

```text
TASK_TEAM
```

---

# 8. 第三阶段：建立 AgentProfile 与 AgentInstance 分离

保留 `AgentProfile` 作为静态能力模板。

新增：

```text
app/multiagent/agent_instance.py
app/multiagent/agent_runtime_manager.py
app/multiagent/agent_loop.py
```

## 8.1 AgentInstance

实现：

```python
class AgentStatus(str, Enum):
    CREATED = "created"
    SPAWNING = "spawning"
    IDLE = "idle"
    CLAIMING = "claiming"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_PERMISSION = "waiting_permission"
    BLOCKED = "blocked"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"
```

```python
class AgentInstance(BaseModel):
    agent_id: str
    team_id: str
    run_id: str

    profile_id: str
    name: str
    role: str

    session_id: str
    thread_id: str
    checkpoint_namespace: str

    status: AgentStatus
    current_task_id: str | None = None

    workspace_root: str
    last_heartbeat_at: datetime | None = None

    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None = None

    metadata: dict[str, Any]
```

## 8.2 AgentRuntimeManager

实现：

```python
class AgentRuntimeManager:
    async def spawn(...)
    async def resume(...)
    async def start_loop(...)
    async def pause(...)
    async def wake(...)
    async def stop(...)
    async def restart_failed(...)
    async def get(...)
    async def list_by_team(...)
```

要求：

- 每个 AgentInstance 有独立 DeepAgent Thread ID。
- 同一个 AgentInstance 在不同 Task 间复用同一个会话。
- Agent 进入 Idle 后仍可被消息或任务唤醒。
- AgentInstance 状态必须持久化。
- Agent 崩溃不得导致 Team 状态丢失。

---

# 9. 第四阶段：实现持续 Teammate Loop

每个 Teammate 应拥有独立异步循环。

示意：

```python
async def teammate_loop(agent: AgentInstance):
    while not stop_requested(agent):
        await heartbeat(agent)

        message = await mailbox.receive(
            agent_id=agent.agent_id,
            timeout=SHORT_TIMEOUT,
        )

        task = await task_board.claim_next(
            agent_id=agent.agent_id,
            required_capabilities=agent.capabilities,
        )

        if task is not None:
            await execute_task_in_existing_session(agent, task)
            continue

        if message is not None:
            await handle_agent_message(agent, message)
            continue

        await mark_idle(agent)
        await wait_for_wakeup(agent)
```

要求：

- Loop 不得忙轮询。
- 使用 `asyncio.Event`、`asyncio.Condition` 或 Queue 唤醒。
- Agent 收到 Message、Task Assignment、Permission Decision 后可恢复。
- Agent 执行 Task 时继续复用自己的 DeepAgent Session。
- Agent 不得因为一次 Task 完成就被销毁。

---

# 10. 第五阶段：把 TaskGraph 升级为持久化共享任务板

保留现有 `TaskGraph` 算法，但将其升级为控制面的正式任务模型。

新增：

```text
app/multiagent/task_board.py
app/multiagent/task_run.py
app/multiagent/task_claim.py
```

## 10.1 TeamTask

建议扩展状态：

```python
class TaskStatus(str, Enum):
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
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
```

Worker 成功只能进入：

```text
PRODUCED
```

不能直接进入：

```text
SUCCEEDED
```

## 10.2 TaskRun

实现：

```python
class TaskRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
```

```python
class TaskRun(BaseModel):
    task_run_id: str
    task_id: str
    agent_id: str
    run_id: str

    attempt: int
    status: TaskRunStatus

    checkpoint_id: str | None
    artifact_ids: list[str]
    tool_calls: list[dict[str, Any]]

    started_at: datetime
    finished_at: datetime | None

    error: ExecutionError | None
    metadata: dict[str, Any]
```

## 10.3 原子任务认领

必须实现数据库事务级原子认领。

要求：

- 多个 Agent 同时认领同一个 Task 时只能一个成功。
- 检查依赖、状态、Owner 和写冲突必须在同一事务中完成。
- SQLite 使用 `BEGIN IMMEDIATE` 或等价可靠方式。
- 不允许先查询后在事务外更新。

提供：

```python
async def claim_task(
    task_id: str,
    agent_id: str,
) -> ClaimResult:
    ...
```

支持：

- Lead 指派
- Teammate 自主认领
- 能力匹配
- Profile 并发限制
- 全局并发限制
- 文件写冲突检查

---

# 11. 第六阶段：真实并行调度

当前同步 fallback 必须移除出生产主链。

## 11.1 Scheduler

将调度器改为真正异步：

```python
class TaskScheduler:
    async def run(...)
    async def schedule_ready_tasks(...)
    async def join_results(...)
```

必须支持：

- 多个无依赖 Ready Task 并行。
- 全局并发上限。
- 每个 Profile 并发上限。
- 一个 Task 失败不影响其他独立 Task 完成。
- 并发结果安全回写。
- Cancel 能传播到所有运行 Task。
- Timeout 能独立终止单个 Task。
- Checkpoint 记录 Inflight Task。

建议：

```python
global_semaphore = asyncio.Semaphore(settings.team_max_concurrency)
profile_semaphores: dict[str, asyncio.Semaphore]
```

## 11.2 禁止伪并行

必须加入端到端测试：

```text
Task A：sleep 2 秒
Task B：sleep 2 秒
```

总耗时应明显小于 4 秒，并接近 2 秒。

LangSmith 或本地事件日志必须显示两个 Task 的执行时间区间重叠。

---

# 12. 第七阶段：将旧 MessageBus 接入新 Team Runtime

保留并复用：

- `MessageBus`
- `AgentInbox`
- direct / broadcast / system
- alias resolution
- dead-letter
- read / ack
- SQLite 持久化

新增实时 Mailbox 层：

```text
app/multiagent/mailbox.py
```

## 12.1 Mailbox

```python
class AgentMailbox:
    async def send(...)
    async def receive(...)
    async def ack(...)
    async def wait(...)
```

实现：

```text
SQLite = 持久化事实源
asyncio.Queue = 进程内实时唤醒
```

## 12.2 新增消息类型

至少支持：

```text
TASK_ASSIGNED
TASK_CLAIMED
TASK_STARTED
TASK_PROGRESS
TASK_BLOCKED
TASK_PRODUCED
TASK_COMPLETED
TASK_FAILED

ARTIFACT_PUBLISHED
ARTIFACT_REJECTED
INTERFACE_CHANGED

HELP_REQUEST
QUESTION
DECISION
REVIEW_REQUEST
REVIEW_RESULT

PLAN_SUBMITTED
PLAN_APPROVED
PLAN_REJECTED

PERMISSION_REQUEST
PERMISSION_DECISION

AGENT_IDLE
AGENT_FAILED
SHUTDOWN_REQUEST
SHUTDOWN_ACCEPTED
```

## 12.3 直接通信

Agent A 可以向 Agent B 发送直接消息。

要求：

- B 不需要等待 Lead 转发。
- B 只读取自己的 Inbox。
- 消息不自动附带 A 的完整上下文。
- 消息包含 Task ID、ArtifactRef、相关证据。
- Idle B 收到消息后必须被唤醒。

---

# 13. 第八阶段：Team Lead 控制面

Lead 可以是 DeepAgent，但不得直接操作底层数据库。

为 Lead 提供确定性 Team Control Tools。

新增：

```text
app/multiagent/team_control_tools.py
```

工具至少包括：

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

每个工具必须：

- 进行 Schema 校验。
- 检查权限。
- 写入审计事件。
- 支持幂等键。
- 返回结构化结果。
- 不允许 Lead 绕过 Verifier 宣布成功。
- 不允许 Lead 替用户审批高风险权限。

第一版约束：

- 每个 Team 最多 5 个 Teammate。
- 默认 3 个以内。
- 不允许嵌套 Team。
- 只有 Lead 可以 Spawn/Stop Teammate。

---

# 14. 第九阶段：Workspace 安全改造

保留 RunWorkspace 思路，但必须把路径检查真正接入所有工具。

目录：

```text
runtime/workspaces/{run_id}/
  shared/
  tasks/{task_id}/
  artifacts/
  checkpoints/
  transcripts/
```

## 14.1 安全路径解析

实现统一：

```python
def safe_resolve(
    base: Path,
    requested: str,
    *,
    allow_absolute: bool = False,
) -> Path:
    ...
```

必须拒绝：

```text
../
../../outside.txt
C:\Windows\...
/etc/passwd
绝对路径越界
符号链接越界
```

## 14.2 权限

- Worker 默认只写自己的 `tasks/{task_id}`。
- Reviewer 默认只读。
- Tester 可以写测试目录，但不能任意修改业务代码。
- Planner 默认不能直接写 Artifact。
- Shared 目录必须经过显式 Publish。
- Artifact 目录只能由 ArtifactStore 管理。
- Checkpoint 目录只能由运行时管理。
- 禁止 Worker 直接访问其他 Run。

## 14.3 Shell

重构 `execute` 工具：

- 默认不用任意 `shell=True`。
- 优先参数数组。
- 命令白名单或策略检查。
- 危险命令触发 HITL。
- 超时。
- 输出上限。
- 工作目录锁定。
- 记录命令、返回码、耗时。
- 后续可接 Sandbox Provider。

---

# 15. 第十阶段：Artifact-first 协作

将 `ArtifactStore` 接入完整执行闭环。

## 15.1 Worker 输出

Worker 写入 Task Workspace 后：

```text
ArtifactPublisher
  ↓
扫描变更
  ↓
计算 Hash
  ↓
ArtifactStore.create()
  ↓
持久化 Artifact
  ↓
更新 Task.output_artifact_ids
  ↓
发送 ARTIFACT_PUBLISHED
```

## 15.2 Artifact 必须持久化

当前内存注册表必须升级为 SQLite 注册表。

新增表：

```text
artifacts
artifact_versions
artifact_relations
artifact_validations
```

Artifact 至少包含：

```text
artifact_id
run_id
task_id
type
relative_path
content_hash
size_bytes
version
produced_by
status
predecessor_id
parent_artifact_id
created_at
metadata
```

## 15.3 下游消费

下游 Agent 只能通过：

```python
artifact_reader.read_for_task(...)
```

读取 Artifact。

禁止通过超长消息直接传完整代码或长报告。

## 15.4 递归扫描

Artifact 扫描必须：

- 支持嵌套目录。
- 忽略临时文件。
- 忽略缓存目录。
- 不误收其他 Task 文件。
- 计算真实 Hash。
- 重复内容保持幂等。
- 修复时生成新版本，不覆盖旧版本审计记录。

---

# 16. 第十一阶段：Verifier 成为唯一完成判定者

## 16.1 状态流

正确状态流：

```text
RUNNING
  ↓
PRODUCED
  ↓
VERIFYING
  ├─ PASS → SUCCEEDED
  ├─ REPAIR → REPAIR_REQUIRED
  ├─ REPLAN → REPLAN_REQUIRED
  ├─ HUMAN_REQUIRED → WAITING_HUMAN
  └─ FAIL → FAILED
```

## 16.2 Verifier 读取真实 Artifact

禁止用：

```text
Task objective
Task title
Agent 自述
```

代替真实产物。

Verifier 必须读取：

- 文件内容
- 文件 Hash
- 测试输出
- 构建输出
- Lint 输出
- ToolCall 记录
- Artifact Validation 结果

## 16.3 验证层

保留三层验证：

1. Programmatic Verifier
2. LLM Rubric Verifier
3. Human Approval

程序化检查至少支持：

- 文件存在
- 文件非空
- JSON Schema
- 测试命令
- 构建命令
- Lint
- 格式检查
- Artifact Hash
- 必需 Artifact 类型
- 验收条件

## 16.4 修复现有问题

修复所有 Verifier 参数、构造、异常处理问题。

原则：

- Verifier 异常不能自动 PASS。
- 无 Artifact 不能 PASS。
- Worker success 不等于 Task success。
- Finalizer 不得跳过 Verifier。
- Run 只有全部关键 Task 通过后才能 COMPLETED。

---

# 17. 第十二阶段：Plan Approval 与 HITL

实现类似 Agent Teams 的阶段性权限升级。

## 17.1 Plan Approval

高风险 Teammate 可以先进入只读 Plan Mode：

```text
Agent 创建计划
  ↓
PLAN_SUBMITTED
  ↓
Lead / User 审批
  ├─ APPROVED → 获得执行权限
  └─ REJECTED → 返回修改意见
```

## 17.2 Permission Request

新增：

```text
permission_requests
human_decisions
```

流程：

```text
Tool 即将执行高风险动作
  ↓
PermissionManager
  ↓
创建 PermissionRequest
  ↓
Agent 状态 WAITING_PERMISSION
  ↓
用户批准 / 拒绝
  ↓
继续或失败
```

安全原则：

- Agent 不能替用户授权。
- Agent A 转述“用户同意”不能让 Agent B 获得权限。
- 被拒绝的操作不能换一个 Agent 绕过。
- 所有权限决定必须可审计。

---

# 18. 第十三阶段：Hook Gate

新增：

```text
app/multiagent/hooks.py
app/multiagent/hook_runner.py
```

至少支持：

```text
TaskCreated
TaskClaimed
TaskStarted
PreToolUse
PostToolUse
TaskProduced
TaskCompleted
AgentIdle
AgentFailed
PlanSubmitted
PermissionRequested
RunCompleted
```

Hook 可以：

- 允许
- 拒绝
- 修改有限字段
- 返回反馈
- 触发人工介入

关键原则：

```text
模型提出状态变更
→ Hook Gate 决定是否允许
```

例如：

- 测试没通过，不允许 TaskCompleted。
- 产物不存在，不允许 AgentIdle。
- 高风险 Shell 未审批，不允许 PreToolUse。
- 验证未通过，不允许 RunCompleted。

---

# 19. 第十四阶段：Checkpoint、恢复和故障处理

## 19.1 持久化内容

必须持久化：

- TeamRun
- AgentInstance
- Agent 状态
- TaskGraph
- TeamTask
- TaskRun
- Task Claim
- Message / Inbox
- Artifact
- ValidationResult
- PermissionRequest
- Checkpoint
- Event
- Budget

## 19.2 恢复策略

进程重启后：

- SUCCEEDED Task 不重跑。
- PRODUCED Task 重新进入 VERIFYING。
- RUNNING Task 标记为 RECOVERING。
- 可恢复 AgentSession 则恢复。
- 不可恢复则基于相同 AgentInstance 身份重建 Session。
- Artifact 保留。
- Inbox 保留。
- Pending Permission 保留。
- Inflight Tool Side Effect 必须依赖幂等键判断是否重放。

## 19.3 Heartbeat 与 Lease

新增：

```text
agent_leases
```

字段：

```text
agent_id
lease_owner
lease_expires_at
last_heartbeat_at
```

避免同一个 AgentInstance 被多个 Runtime 同时执行。

单机第一版仍要实现 Lease 抽象，后续才能平滑升级。

---

# 20. 第十五阶段：数据库升级

现有表保留兼容，新架构至少增加：

```text
team_runs

agent_instances
agent_sessions
agent_status_events
agent_leases

team_tasks
task_dependencies
task_runs
task_claims

artifacts
artifact_versions
artifact_relations
artifact_validations

permission_requests
human_decisions

team_events
runtime_checkpoints
```

必须建立以下审计链：

```text
TeamRun → AgentInstance → TaskRun
Task → Artifact → ValidationResult
Message → Recipient → Read/Ack
PermissionRequest → HumanDecision → ToolCall
```

要求：

- 提供数据库迁移机制。
- 不要只依赖运行时 `CREATE TABLE IF NOT EXISTS`。
- 增加 schema version。
- 为高频查询创建索引。
- SQLite 事务边界明确。
- 并发写入增加重试与 busy timeout。

---

# 21. 第十六阶段：API、CLI 和 Web 统一

## 21.1 API

统一为：

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
POST   /team-runs/{run_id}/tasks
POST   /team-runs/{run_id}/tasks/{task_id}/assign
POST   /team-runs/{run_id}/tasks/{task_id}/retry

GET    /team-runs/{run_id}/artifacts
GET    /team-runs/{run_id}/events
GET    /team-runs/{run_id}/messages

GET    /team-runs/{run_id}/permissions
POST   /team-runs/{run_id}/permissions/{request_id}/approve
POST   /team-runs/{run_id}/permissions/{request_id}/reject
```

旧 `/team-tasks` 可以保留兼容，但必须内部调用新 Facade。

## 21.2 CLI

建议：

```bash
python -m app.cli team run "..."
python -m app.cli team status <run_id>
python -m app.cli team agents <run_id>
python -m app.cli team tasks <run_id>
python -m app.cli team message <run_id> <agent_id> "..."
python -m app.cli team approve <run_id> <request_id>
python -m app.cli team cancel <run_id>
python -m app.cli team resume <run_id>
```

CLI 和 API 必须产生相同运行时数据。

## 21.3 Web

至少展示：

- Team Lead
- Teammate 状态
- Agent 当前任务
- Task Board
- Task 依赖
- Message Timeline
- Artifact
- Verification
- Permission Request
- LangSmith Trace 链接
- 失败与重试

---

# 22. 第十七阶段：可观测性

保留 LangSmith，同时增加本地事件模型。

每个事件至少包含：

```text
event_id
run_id
team_id
agent_id
task_id
task_run_id
event_type
timestamp
trace_id
payload
```

必须可观察：

- Team 创建
- Agent Spawn
- Agent 状态变化
- Task 创建
- Task Claim
- Task 开始
- Tool Call
- Artifact 发布
- Verifier 结果
- Repair / Replan
- Permission 等待
- Idle / Wake
- Agent Failure
- Run 完成

LangSmith Trace 层级建议：

```text
team_run
  ├─ planning
  ├─ agent_spawn
  ├─ scheduler_round
  │   ├─ task_run:A
  │   │   ├─ agent_loop
  │   │   └─ tool_calls
  │   └─ task_run:B
  │       ├─ agent_loop
  │       └─ tool_calls
  ├─ verification
  └─ repair
```

并行任务在 Trace 中必须显示真实时间重叠。

---

# 23. 必须修复的现有具体问题

改造过程中必须重点确认并修复：

1. `SimpleOrchestrator._schedule()` 不得继续强制 `_run_sync_fallback()`。
2. CLI 创建的 RunWorkspace 必须真实传入 Executor。
3. API 与 CLI 不得走两套不同主链。
4. `DeepAgentExecutor` 不得每个 Task 都丢失 AgentInstance 身份。
5. Artifact ID 必须来自真实 ArtifactStore。
6. ArtifactStore 注册表必须持久化。
7. Artifact 扫描必须递归。
8. Verifier 必须读取真实文件。
9. Worker 成功不得直接标记 Task SUCCEEDED。
10. `LLMRubricVerifier` 的数据模型构造必须正确。
11. `_run_single()` 失败不得返回 completed。
12. Scheduler 失败不得仍然返回 success。
13. 文件工具不得允许绝对路径越权。
14. `execute` 工具不得无约束使用任意 Shell。
15. AgentProfile 的 `model_policy` 必须真正影响模型选择。
16. CapabilityRegistry 的 load / success / failure 指标必须真实更新。
17. `max_concurrency` 必须真实生效。
18. Task Budget 必须真实执行。
19. WorkspacePolicy 必须真实执行。
20. MemoryPolicy / ContextPolicy 不得只停留在数据模型。

---

# 24. 建议模块结构

最终建议：

```text
app/multiagent/
  runtime/
    team_runtime.py
    team_runtime_service.py
    team_run_context.py
    team_manager.py
    agent_runtime_manager.py
    agent_loop.py
    worker_pool.py

  agents/
    agent_profile.py
    agent_instance.py
    capability_registry.py
    agent_factory.py

  tasks/
    task_graph.py
    task_board.py
    task_models.py
    task_run.py
    task_claim.py
    scheduler.py

  messaging/
    messages.py
    message_bus.py
    mailbox.py
    inbox.py

  artifacts/
    artifact.py
    artifact_store.py
    artifact_publisher.py
    artifact_reader.py

  verification/
    verifier.py
    programmatic.py
    llm_rubric.py
    human_approval.py

  governance/
    permissions.py
    hooks.py
    hook_runner.py
    action_guard.py
    conflict_resolver.py

  persistence/
    store.py
    migrations.py
    checkpoints.py

  modes/
    discussion_runtime.py
    task_team_runtime.py

  observability/
    events.py
    tracing.py
```

不要求一次性完全移动所有旧文件，但最终职责必须清晰，禁止循环依赖。

---

# 25. 测试要求

不得只增加 Mock 单元测试。

## 25.1 单元测试

至少覆盖：

- Task 状态合法转换
- Agent 状态合法转换
- DAG 环检测
- Ready Task
- 原子 Claim
- Capability 匹配
- Profile 并发限制
- Global 并发限制
- Mailbox direct
- Inbox ack
- Idle wake
- Workspace 越界
- Artifact Hash
- Artifact Version
- Verifier 状态转换
- Permission 审批
- Hook 阻止完成
- Resume 幂等

## 25.2 并发测试

20 个协程同时认领同一 Task，只能一个成功。

两个独立 Task 各休眠 2 秒，总耗时应接近 2 秒。

同一 Profile `max_concurrency=1` 时两个任务不得同时执行。

## 25.3 Workspace 安全测试

以下全部拒绝：

```text
../outside.txt
../../outside.txt
/etc/passwd
C:\Windows\System32\test.txt
指向外部的符号链接
```

## 25.4 Artifact 测试

- 无真实文件不得生成 Artifact。
- Artifact Hash 必须与磁盘一致。
- 修复后必须生成 Version 2。
- 旧版本必须保留。
- 下游 Task 只能读取声明的 Artifact。
- ArtifactStore 重启后仍可查询。

## 25.5 Verifier 测试

- Worker 声称成功但无文件：失败。
- 文件存在但测试失败：REPAIR。
- 测试通过但验收条件不足：REPAIR 或 REPLAN。
- Verifier 异常：FAIL，不得 PASS。
- 所有条件通过：PASS。

## 25.6 恢复测试

中途停止进程，再恢复：

- SUCCEEDED Task 不重跑。
- PRODUCED Task 重新验证。
- RUNNING Task 进入恢复流程。
- Artifact 保留。
- Inbox 保留。
- AgentInstance 身份保留。
- 权限请求保留。

## 25.7 端到端测试

至少设计以下 E2E：

### 场景 A：软件开发

```text
Lead
├─ Coder：实现功能
├─ Tester：编写测试
└─ Reviewer：审查结果
```

要求：

- Coder 和另一个独立任务真实并行。
- Tester 读取 Coder Artifact。
- Reviewer 读取真实代码和测试结果。
- 失败后生成 Repair Task。
- 修复后重新验证。
- 最终输出真实文件。

### 场景 B：研究团队

```text
Lead
├─ Researcher A：来源一
├─ Researcher B：来源二
└─ Synthesizer：综合
```

要求：

- 两个 Researcher 并行。
- 结果通过 ArtifactRef 传递。
- Synthesizer 不读取其他 Agent 完整上下文。
- 最终报告包含证据引用。

### 场景 C：直接 Agent 消息

- Tester Idle。
- Coder 发送 `INTERFACE_CHANGED`。
- Tester 被唤醒。
- Tester 更新测试。
- Lead 可以看到事件链。

---

# 26. 完成标准

只有满足以下条件才能宣布本阶段完成。

## 26.1 架构

- [ ] CLI、API、Web 使用统一 TeamRuntimeFacade。
- [ ] 默认主链是 TASK_TEAM。
- [ ] 旧群聊模式降级为 DISCUSSION。
- [ ] AgentProfile 与 AgentInstance 完全分离。
- [ ] Teammate 是持续运行实例。
- [ ] Teammate 有独立 Session、Thread、Inbox 和状态机。
- [ ] Shared Task Board 是任务控制中心。
- [ ] Task 支持原子认领。
- [ ] 多个 Agent 可以真正并行。
- [ ] Mailbox 可以唤醒 Idle Agent。
- [ ] ArtifactStore 接入真实产物。
- [ ] Verifier 是唯一成功判定者。
- [ ] Workspace 安全边界由代码强制。
- [ ] Permission / Hook 可阻止危险或不合格操作。
- [ ] Checkpoint / Resume 不重复已完成 Task。

## 26.2 质量

- [ ] 全量测试通过。
- [ ] 新增并发测试通过。
- [ ] 新增安全测试通过。
- [ ] 新增恢复测试通过。
- [ ] 至少一个真实 E2E 任务通过。
- [ ] LangSmith 或本地 Trace 能看到并行 Agent。
- [ ] 无生产默认 Stub。
- [ ] 无 LLM 失败后自动 completed。
- [ ] 无无产物成功。
- [ ] 无 Verifier 异常后 PASS。

---

# 27. 执行顺序

请严格按以下阶段实施，不要一开始就大范围改名和移动文件。

## Phase A：修复断链

1. 统一 RunContext。
2. 修复 Workspace 传递。
3. 修复 ArtifactStore 接入。
4. 修复 Verifier 读取真实 Artifact。
5. 修复 Scheduler 返回状态。
6. 修复单 Agent 伪成功。
7. 修复路径与 Shell 安全。
8. 补测试。

## Phase B：统一主链

1. 新建 TeamRuntimeFacade。
2. CLI/API 统一入口。
3. 旧 TeamRunner 降级为 Discussion Runtime。
4. Phase Two 成为默认 TASK_TEAM。
5. 补双入口一致性测试。

## Phase C：持续 Teammate

1. AgentInstance。
2. AgentRuntimeManager。
3. 持续 Agent Loop。
4. Idle/Wake。
5. Stable Thread ID。
6. Agent 状态持久化。
7. 补生命周期测试。

## Phase D：共享任务板

1. TeamTask。
2. TaskRun。
3. TaskDependency。
4. 原子 Claim。
5. Lead 指派。
6. Teammate 自主认领。
7. 补竞争测试。

## Phase E：真实并行

1. Async Scheduler。
2. Global Semaphore。
3. Profile Semaphore。
4. 并发结果回写。
5. Cancel / Timeout。
6. LangSmith 并行 Trace。
7. 补耗时测试。

## Phase F：Mailbox 与治理

1. 实时 Mailbox。
2. Idle Agent 唤醒。
3. Team Control Tools。
4. Plan Approval。
5. Permission Broker。
6. Hook Gate。
7. 补 HITL 与 Hook 测试。

## Phase G：恢复与产品化

1. 数据库迁移。
2. Checkpoint。
3. Resume。
4. Heartbeat / Lease。
5. API 完善。
6. Web 展示。
7. E2E。
8. 文档。

---

# 28. 每个阶段的提交要求

每完成一个阶段：

1. 运行相关单元测试。
2. 运行全量测试。
3. 更新文档。
4. 更新架构图。
5. 输出修改文件列表。
6. 输出测试命令和结果。
7. 输出未完成项。
8. 不要把多个完全不同阶段塞进一个巨大提交。

建议提交信息：

```text
refactor(runtime): unify team run context
fix(workspace): enforce run and task isolation
feat(agents): add persistent agent instances
feat(tasks): add atomic shared task board
feat(runtime): add async teammate loops
feat(scheduler): enable real parallel task execution
feat(mailbox): wake idle teammates on messages
feat(governance): add permission broker and hook gates
feat(recovery): persist and resume agent team runs
```

---

# 29. 最终交付物

完成后必须提供：

```text
docs/agent-team-architecture.md
docs/agent-team-migration.md
docs/agent-team-runtime.md
docs/agent-team-testing.md
docs/agent-team-known-limitations.md
```

并在最终回复中说明：

1. 最终目标架构。
2. 删除或降级了哪些旧逻辑。
3. 新增了哪些核心模块。
4. AgentInstance 如何工作。
5. Task Board 如何工作。
6. Teammate 如何持续运行。
7. 并行如何实现。
8. Workspace 如何隔离。
9. Artifact 如何传递。
10. Verifier 如何判定。
11. Permission 和 Hook 如何治理。
12. Checkpoint 如何恢复。
13. 测试通过情况。
14. E2E 结果。
15. 剩余风险。

---

# 30. 最终执行指令

现在开始执行。

不要只生成计划，不要只修改 README，不要只创建接口。

第一步先完成仓库审查和基线测试，然后从 Phase A 开始实施。

遇到旧架构和新架构冲突时，以以下原则决策：

```text
TaskGraph 优于轮流发言
持续 AgentInstance 优于一次性 Worker
真实 Artifact 优于自然语言转述
Verifier 优于 Finalizer 自我宣布
确定性状态机优于 Prompt 约定
原子任务认领优于非事务分配
异步受控并行优于同步轮询
统一 Runtime 优于双主链并存
```

在保证测试和兼容性的前提下，持续完成改造，直到满足本文档的完成标准。
