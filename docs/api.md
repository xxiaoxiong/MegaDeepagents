# API v1

公共前缀为 `/api/v1`，OpenAPI 位于 `/docs`。请求和响应由 Pydantic 校验；列表端点设有
limit 上限，所有子资源先校验 `run_id` 边界。

## Run

| 方法 | 路径 | 状态码 | 说明 |
|---|---|---:|---|
| POST | `/runs` | 202 | 创建并后台启动 |
| GET | `/runs?limit=50` | 200 | 最近运行 |
| GET | `/runs/{run_id}` | 200 | 运行详情 |
| POST | `/runs/{run_id}/pause` | 200 | 协作式暂停 |
| POST | `/runs/{run_id}/resume` | 202 | 从持久状态恢复 |
| POST | `/runs/{run_id}/retry` | 202 | 重试失败 Task 并创建恢复代次 |
| POST | `/runs/{run_id}/cancel` | 200 | 取消且阻止迟到成功 |
| POST | `/runs/{run_id}/messages` | 202 | 向活跃团队广播 |
| GET | `/runs/{run_id}/events` | 200 | 持久事件分页 |
| GET | `/runs/{run_id}/stream` | 200 | SSE |
| GET | `/runs/{run_id}/diagnostics` | 200 | 健康、静默、阻塞和重试诊断 |
| GET | `/runs/{run_id}/execution` | 200 | Agent 执行泳道、关键路径和并行效率只读投影 |

创建请求中的 `mode` 为 `auto|single|team`。`repository_path` 必须是后端可访问的绝对
Git 路径；浏览器路径本身不会上传仓库。

## 子资源

- Agents：`/runs/{run_id}/agents`、`/agents/{agent_id}`、消息、停止。
- Tasks：`/runs/{run_id}/tasks`、`/tasks/{task_id}?run_id=...`、`task-graph`。
- Artifacts：清单、详情、lineage、文本 content、download。
- Governance：permissions 和 plans 的待审批清单与 decision。
- Evidence：verification、errors、worktrees、git。
- Settings：`GET /settings`，密钥只返回是否已配置。

Artifact content 最大预览 512 KiB；二进制返回 415；download 与 content 都会校验解析后
路径仍位于 Run workspace 内。

### Execution intelligence

`GET /runs/{run_id}/execution` 从权威 Run、TaskBoard、Agent、Artifact 和持久事件重放生成
一个只读投影，不创建新的状态源。响应包含：

- `summary`：完成进度、墙钟/活跃时间、并行倍率、利用率、峰值并发、工具/重试/交接/
  Artifact 数量，以及当前关键路径；
- `agents`：每个 Agent 的当前任务、能力、任务/工具/事件计数、交付物和去噪后的最近活动；
- `tasks`：依赖、阻塞项、尝试次数、责任 Agent、Artifact 和关键路径标记；
- `attention`：失败、阻塞、重试压力、交付缺失和静默 Agent。

该端点面向控制台和自动化观察者；状态变更仍必须使用现有 Run、Agent、Task 和审批命令端点。

## SSE

```text
id: 123
data: {"event_id":"evt_...","run_id":"run_...","event_type":"TaskStarted",
       "sequence":123,"timestamp":"...","payload":{}}
```

重连时把最后序号传给 `after_sequence`。`sequence` 在单个 Run 内单调递增，事件先写
SQLite 再发送；空闲期间发送 keepalive 注释。

### 故障恢复

`POST /runs/{run_id}/retry` 的请求体：

```json
{
  "task_id": null,
  "reason": "upstream service recovered",
  "reset_attempts": false
}
```

`task_id` 为空时重试全部失败、待修复或待重排 Task。操作会保留已成功 Task 和 Artifact，
记录 `ManualRetryRequested`，并在新的 checkpoint namespace 中重新进入同一 Root Graph。
没有可恢复 Task 时返回 409。

常见错误：404 资源或边界不存在，409 当前状态不允许操作，415 Artifact 无文本预览，
422 请求校验失败，429 超过速率限制。
