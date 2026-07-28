# 可观测性、审计与故障恢复

MegaDeepagents 把“现在执行到哪一步”视为运行时契约，而不是调试时才打开的日志。每个
Run 都有一条持久、可重放的审计流；浏览器断开不会影响执行，也不会丢失已经发生的事件。

## 三层信号

| 层 | 数据 | 用途 |
|---|---|---|
| 领域状态 | Run、TaskGraph、TaskBoard、Artifact、审批 | 恢复和完成判定的权威事实 |
| 审计事件 | `event_envelopes` 中的单调 sequence | UI 时间线、SSE 重放、责任追踪 |
| 外部 Trace | 可选 LangSmith span | LLM 延迟、Token、成本与质量分析 |

日志不是权威状态。`runtime/logs/` 和 `runtime/cache/` 是本地运行数据，已从 Git 源码中
排除；不能用日志文件替代 TaskBoard 或事件信封。

## 持久事件

生命周期事件在没有注册 Hook 时也会写入数据库。典型顺序：

```text
RunCreated → RunStarted
  → planning_started → planning_attempt_failed? → team_build_completed
  → SchedulerStarted → SchedulerRoundStarted
  → TaskClaimed → TaskStarted → TaskHeartbeat*
  → BeforeToolUse → AfterToolUse
  → TaskProduced → VerificationStarted → VerificationCompleted
  → TaskCompleted | TaskRetryScheduled | TaskFailedPermanently
  → collection_completed → RunCompleted | repair_planned | replan_requested
```

每个事件信封包含：

- `event_id`：全局事件标识；
- `run_id`、可选 `agent_id`、`task_id`：责任边界；
- `event_type`：稳定事件类型；
- `sequence`：Run 内单调递增序号；
- `timestamp`：发生时间；
- `payload`：错误、工具、原因、决策和上下文。

工具输入或结果若包含敏感信息，仍应在工具适配层脱敏。审计完整不等于公开密钥。

## SSE 与断线恢复

`GET /api/v1/runs/{run_id}/stream?after_sequence=N` 会先从 SQLite 重放 `N` 之后的事件，
再持续推送新事件。前端记录最后序号，使用指数退避重连；恢复连接后按 `event_id` 去重并
按 `sequence` 排序。

详情页的“运行审计台”默认展示：

- 最新活动弹幕；
- 执行、工具、恢复和异常分类；
- Agent、Task、trace 与完整 JSON；
- 搜索、复制、暂停弹幕和向前加载历史；
- 流连接状态及最近连接错误。

前端会分页读取完整历史，不再把审计视图限制为最近 150 条。

## Agent 执行智能投影

`GET /api/v1/runs/{run_id}/execution` 将持久事件与领域状态重放为面向操作者的 Mission
Control 投影。它不是第二套调度状态，也不会反向修改 TaskBoard。

投影同时提供：

- 每个 Agent 独立的执行泳道、当前任务、能力、工具调用、交付物和高信号事件；
- Task 依赖、阻塞原因、责任归属与最长未完成关键路径；
- 可观测活跃工时、墙钟时间、并行倍率、利用率和峰值并发；
- 失败、阻塞、重试压力、缺少最终交付和静默 Agent 等注意项。

控制台默认折叠 `assistant_token`、心跳、调度轮次以及重复的工具开始/结束事件，避免高频
噪声淹没决策过程；“原始事件”开关可随时恢复完整审计。点击 Agent 或 Task 会把详细轨迹
与全局时间线联动过滤，因此既能看团队并发，也能追责到单个 Worker。

## 心跳与卡住诊断

执行中的 Assignment 定期写入 `TaskHeartbeat`。诊断端点：

```text
GET /api/v1/runs/{run_id}/diagnostics
```

它是只读投影，组合 Run 状态、TaskBoard、活跃 Assignment 和最后事件，返回：

- `health`：`healthy | attention | stalled | failed | completed`；
- 当前 `phase`、最后活动时间和静默秒数；
- 各 Task 状态数量、延迟重试队列；
- 阻塞原因和可重试 Task；
- 面向操作者的建议动作。

`STALLED_RUN_THRESHOLD_SECONDS` 控制无活动告警阈值，默认 180 秒。

## 自动重试

调度器使用唯一的 `RetryPolicy` 对错误分类：

| 分类 | 默认策略 |
|---|---|
| `rate_limited`、`timeout`、`transient` | 在预算内指数退避 |
| `unknown` | 在预算内保守重试 |
| `authentication`、`permission`、`contract` | 立即等待人工处理 |
| `cancelled` | 不重试 |

退避为 `base × 2^(attempt-1)`，并受最大值限制。TaskBoard 持久化
`next_attempt_at`、`error_history`、尝试次数和失败分类，调度器等待时写
`RetryBackoffWaiting`，不会消耗空转轮次。

相关配置：

```env
TASK_EXECUTION_TIMEOUT_SECONDS=900
RETRY_BASE_DELAY_SECONDS=2
RETRY_MAX_DELAY_SECONDS=60
AUDIT_HEARTBEAT_INTERVAL_SECONDS=15
STALLED_RUN_THRESHOLD_SECONDS=180
```

## 人工恢复

失败、待修复或待重排 Task 可以显式重入同一个 Run：

```http
POST /api/v1/runs/{run_id}/retry
Content-Type: application/json

{
  "task_id": "task_optional",
  "reason": "upstream service recovered",
  "reset_attempts": false
}
```

省略 `task_id` 会恢复所有可重试 Task。恢复操作：

1. 记录 `ManualRetryRequested`；
2. 增加 `recovery_generation`；
3. 创建新的 checkpoint namespace；
4. 复用同一 Root Graph、TaskGraph 和 TaskBoard；
5. 保留已成功任务与既有 Artifact。

它不会创建第二套调度器，也不会把失败直接改写成成功。

## 可选 LangSmith

LangSmith 默认关闭，主要用于 LLM 调用和 Root Graph 的外部 trace。未配置时，本地持久
审计仍完整可用。

```env
LANGSMITH_ENABLED=true
LANGSMITH_API_KEY=lsv2_pt_xxx
LANGSMITH_PROJECT=megadeepagents
LANGSMITH_TRACING=true
LANGSMITH_SAMPLE_RATE=1.0
```

模型调用由 LangChain/LangGraph 自动挂接 span；业务完成语义仍由 TaskBoard、Artifact 和
Verifier 决定，不能由外部 trace 决定。

## 验证

```bash
pytest tests/test_observability.py tests/test_reliability_and_diagnostics.py -q
```

真实 LangSmith 测试只有设置 `RUN_LIVE_MODEL_TESTS=1` 时才执行，默认测试不会使用凭据或
访问真实模型。
