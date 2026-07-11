你现在继续完成 MegaDeepagents 的第三阶段改造。

前两个阶段已经完成：

1. 多智能体语义修复和统一执行逻辑。
2. Orchestrator–Worker、Task Graph、真实工具型 Worker、Artifact 和 Verifier。

本阶段目标是：

**将项目从可运行的多智能体框架，升级为可嵌入企业软件、可恢复、可扩展、可观测、可评测的生产级 Agent Runtime。**

必须实际完成代码、测试、文档和部署配置，不要只输出架构建议。

# 一、生产化目标

系统必须能够支持：

- API 进程重启后任务继续。
- 多个 Worker 实例并行领取任务。
- Worker 崩溃后任务可重新领取。
- 任务取消跨进程生效。
- Tool Call 不重复执行危险副作用。
- 每个 Run 有预算、超时和资源限制。
- 每个租户、用户、Run、Workspace 相互隔离。
- Artifact、Checkpoint 和 Event 可追溯。
- API 调用幂等。
- 支持离线内网部署。
- LangSmith 可选，不作为唯一可观测性依赖。
- 有稳定的 Python SDK 和版本化 API。
- 有可重复的 Evals 和 CI。

# 二、禁止事项

- 禁止继续使用每请求创建 daemon thread 作为正式后台执行方式。
- 禁止依赖内存字典保存运行中任务。
- 禁止把 SQLite 声明为多实例生产数据库。
- 禁止 catch Exception 后静默标记成功。
- 禁止在失败恢复时重复执行已经成功的外部副作用。
- 禁止通过删减测试或降低验收标准完成改造。
- 禁止强绑定某一家云平台。

# 三、建立 Durable Run Service

定义稳定领域模型：

```python
class RunRecord:
    run_id: str
    tenant_id: str
    user_id: str
    mode: str
    status: RunStatus
    graph_version: str
    prompt_version: str
    toolset_version: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    budget: RunBudget
    usage: RunUsage
```

状态至少包括：

```text
QUEUED
RUNNING
WAITING_HUMAN
RETRYING
COMPLETED
INCOMPLETE
FAILED
TIMED_OUT
CANCELLED
```

所有状态转换必须：

- 有合法转换表。
- 使用乐观锁、版本号或 CAS。
- 写入不可变 Event。
- 能在 API 中查询。

# 四、实现持久化 Job Queue 和 Worker Lease

设计抽象：

```python
class JobQueue(Protocol):
    enqueue(...)
    claim(...)
    heartbeat(...)
    complete(...)
    fail(...)
    release(...)
```

默认实现可以使用 SQLite 作为本地开发模式，但架构必须支持 PostgreSQL。

生产模式至少实现 PostgreSQL Adapter，或提供完整可运行的 PostgreSQL 实现。

Worker Lease 必须包含：

- worker_id
- lease_owner
- lease_expires_at
- heartbeat_at
- attempt
- visibility_timeout

必须验证：

```text
Worker A 领取任务
→ A 崩溃且停止心跳
→ Lease 过期
→ Worker B 重新领取
→ 已提交副作用不重复
```

API 创建任务后只负责 enqueue，不直接启动 daemon thread。

# 五、实现副作用幂等与 Tool Call Journal

建立 `tool_calls` 持久化模型：

```python
class ToolCallRecord:
    tool_call_id: str
    run_id: str
    task_id: str
    idempotency_key: str
    tool_name: str
    arguments_hash: str
    status: ToolCallStatus
    result_ref: str | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
```

状态：

```text
PLANNED
APPROVAL_PENDING
RUNNING
SUCCEEDED
FAILED
CANCELLED
COMPENSATED
```

规则：

- 同一个 idempotency key 不得重复执行。
- 恢复后先查询 ToolCallJournal。
- 已成功调用直接复用结果。
- 写文件、Shell、MCP 写操作必须记录。
- 对可补偿操作预留 Compensation 接口。
- 无法保证幂等的工具必须明确标记风险。

# 六、实现预算与资源控制

支持：

```python
class RunBudget:
    max_tokens: int | None
    max_model_calls: int | None
    max_tool_calls: int | None
    max_wall_time_seconds: int | None
    max_parallel_workers: int
    max_cost: Decimal | None
```

运行过程中实时累计：

- 输入 Token
- 输出 Token
- 模型调用次数
- 工具调用次数
- Worker 执行时间
- 失败重试次数
- 成本估算

达到预算时：

- 不得误标完成。
- 根据策略进入 INCOMPLETE、WAITING_HUMAN 或 FAILED。
- 输出已完成结果和缺失项。
- 写入明确 termination reason。

# 七、实现多租户和资源隔离

所有核心资源必须带：

```text
tenant_id
user_id
run_id
```

包括：

- Run
- Task
- Artifact
- Workspace
- Memory
- Approval
- Tool Call
- Trace
- API 查询

Workspace 路径：

```text
runtime/tenants/{tenant_id}/runs/{run_id}/
```

API 必须校验资源 Ownership，禁止通过猜测 ID 读取其他用户任务。

实现最小可用认证：

- 开发模式可关闭。
- 生产模式支持 API Key 或 JWT。
- 权限模型至少包括：
  - run:create
  - run:read
  - run:cancel
  - artifact:read
  - approval:resolve
  - admin

# 八、强化 Sandbox 和工具安全

明确区分：

```text
none
local-dev
container
remote
```

生产配置不得把 LocalShellBackend 宣称为强隔离。

至少实现：

- 命令超时。
- 工作目录限制。
- 环境变量白名单。
- 网络访问策略。
- 文件大小限制。
- 进程输出限制。
- 禁止访问宿主敏感路径。
- Shell 命令审计。
- 高风险命令审批。
- MCP Server 白名单。
- MCP Tool 权限分级。
- 外部 URL 域名白名单或 Egress Policy。

安全默认值必须是 deny-by-default。

# 九、Artifact Store 生产化

实现 ArtifactStore 抽象：

```python
class ArtifactStore(Protocol):
    put(...)
    get(...)
    list_versions(...)
    verify_hash(...)
    delete(...)
```

默认支持本地文件系统，架构支持：

- S3
- MinIO
- 企业内部对象存储

必须具备：

- SHA-256
- MIME Type
- 大小限制
- 版本
- Producer
- Reviewer
- ValidationResult
- Retention Policy
- Tenant 隔离
- 下载鉴权

数据库只保存元数据，不把大型二进制内容直接塞入普通 JSON 字段。

# 十、Checkpointer 与 Store 分层

明确区分：

1. Checkpointer
   - Graph Node 恢复
   - Thread/Run 级短期状态
   - HITL
   - Time Travel
2. Domain Store
   - Run
   - Task Graph
   - Artifact Metadata
   - Tool Calls
   - Usage
   - Approval
   - Audit Event
3. Cross-run Memory Store
   - 用户偏好
   - 组织知识
   - 可复用经验
   - SOP

不要继续将这些职责混在一个 SQLite 文件或一个 Shared State JSON 中。

# 十一、实现供应商中立可观测性

保留 LangSmith 可选集成，同时增加 OpenTelemetry。

Trace 层级至少包括：

```text
run
  ├─ plan
  ├─ task_node
  │    ├─ worker_execution
  │    ├─ model_call
  │    ├─ tool_call
  │    └─ artifact_write
  ├─ verification
  ├─ repair
  └─ finalize
```

Metric 至少包括：

- run count
- success rate
- incomplete rate
- failure rate
- average completion time
- Token usage
- Tool call count
- Retry count
- Worker utilization
- Queue depth
- Lease expiration
- Checkpoint restore count
- Human approval wait time
- Cost per successful Run

日志必须包含：

```text
trace_id
run_id
task_id
agent_id
tenant_id
```

不得记录 API Key、完整 Secret 或不必要的敏感 Prompt。

# 十二、实现正式 Evals

建立：

```text
evals/
  cases/
  rubrics/
  runners/
  reports/
```

至少包含：

1. 单 Agent 简单任务。
2. 多模块编码任务。
3. 并行研究任务。
4. 测试失败后修复。
5. Worker 崩溃恢复。
6. 模型输出非法 Schema。
7. 工具调用超时。
8. MCP 服务不可用。
9. 预算耗尽。
10. 人工审批。
11. 取消任务。
12. Prompt Injection。
13. 越权工具调用。
14. Artifact 篡改。
15. 多租户隔离。

每个 Eval 记录：

- 是否成功。
- 最终质量分。
- Token。
- 工具调用。
- 总时长。
- 重试数。
- 人工介入数。
- Artifact 完整性。
- 是否发生越权。

提供可重复运行的命令并生成 JSON 和 Markdown 报告。

# 十三、API 产品化

使用版本化 API：

```text
POST   /api/v1/runs
GET    /api/v1/runs/{run_id}
POST   /api/v1/runs/{run_id}/cancel
POST   /api/v1/runs/{run_id}/resume
GET    /api/v1/runs/{run_id}/events
GET    /api/v1/runs/{run_id}/tasks
GET    /api/v1/runs/{run_id}/artifacts
POST   /api/v1/approvals/{approval_id}/resolve
```

要求：

- POST 创建 Run 支持 `Idempotency-Key`。
- 使用统一错误结构。
- 支持分页。
- SSE 支持断线重连和 Last-Event-ID。
- API 返回明确 Run 状态和 termination reason。
- 老 API 提供兼容层和弃用说明，不能直接无提示删除。

# 十四、Python SDK

提供最小可用 SDK：

```python
client = MegaDeepagentsClient(...)

run = client.runs.create(
    goal="...",
    mode="auto",
)

for event in client.runs.stream(run.id):
    ...

client.runs.cancel(run.id)
client.approvals.resolve(...)
client.artifacts.download(...)
```

SDK 需要：

- 类型提示。
- 超时。
- 重试。
- 错误模型。
- 同步接口，条件允许时提供异步接口。
- README 示例。

# 十五、数据库迁移

禁止继续只在启动时手写 `ALTER TABLE`。

引入正式迁移机制，例如 Alembic，或选择与项目技术栈一致的迁移方案。

要求：

- 从当前旧数据库可迁移。
- 迁移有版本号。
- CI 能从空库升级到最新版本。
- CI 能验证旧 Schema 升级。
- 提供备份和回滚说明。

# 十六、CI 与开源工程

完善：

- GitHub Actions。
- lint。
- type check。
- unit tests。
- integration tests。
- migration tests。
- security checks。
- packaging test。
- Docker build。
- 离线模式测试。

补齐或完善：

- 项目名称统一为 MegaDeepagents。
- `pyproject.toml` 元数据。
- License。
- Contributing。
- Security Policy。
- Architecture 文档。
- API 文档。
- Deployment 文档。
- 示例项目。
- Changelog。
- Semantic Versioning。
- Release 流程。

# 十七、部署方案

提供：

1. 本地开发模式：
   - SQLite
   - 本地 Artifact
   - 单 Worker
2. 单机生产模式：
   - PostgreSQL
   - 多 Worker
   - 本地或 MinIO Artifact
   - OTel
3. 企业内网模式：
   - 自定义 OpenAI-compatible 模型
   - 内网 MCP
   - 无外网依赖
   - LangSmith 关闭
   - OTel/Prometheus/Grafana
   - 私有对象存储

提供 Docker Compose，包含必要的健康检查和持久化卷。

# 十八、故障恢复验收

必须实际验证：

```text
1. 创建复杂 Run
2. 执行到部分 Task 完成
3. 强制杀死 Worker
4. 等待 Lease 过期
5. 启动新 Worker
6. 从 checkpoint 恢复
7. 已完成 Tool Call 不重复
8. 未完成 Task 被重新领取
9. 最终 Verifier 通过
10. Run 正确 Completed
```

还必须验证：

- API 重启不丢 Run。
- Cancel 跨进程生效。
- 两个租户无法读取对方 Artifact。
- 同一个 Idempotency-Key 不创建重复 Run。
- 达到预算后进入正确非成功状态。

# 十九、最终验收标准

完成后项目必须达到：

- 无 daemon thread 承担正式任务执行。
- 无关键运行状态只存在于内存。
- 支持 PostgreSQL 生产运行。
- 支持 Worker Lease 和故障重领。
- 支持幂等 Tool Call。
- 支持跨进程 Cancel 和 Resume。
- 支持租户与 Workspace 隔离。
- 支持预算、超时和并发限制。
- 支持本地与对象 Artifact Store。
- 支持 OTel。
- 支持正式 Evals。
- 支持版本化 API。
- 支持 Python SDK。
- 支持数据库迁移。
- 默认测试全部通过。
- Docker Compose 能启动完整系统。
- 企业内网环境可关闭所有外网依赖。

# 二十、最终交付

完成后输出：

1. 生产架构图。
2. Run 生命周期。
3. Worker Lease 和故障恢复机制。
4. Tool 幂等机制。
5. 安全与多租户模型。
6. 数据库 Schema 和迁移说明。
7. API 与 SDK 使用示例。
8. Docker Compose 启动方式。
9. 执行过的所有测试。
10. 故障恢复实验结果。
11. Eval 报告摘要。
12. 当前版本仍存在的限制。

所有“已完成”都必须有代码、测试或实际运行结果作为证据。