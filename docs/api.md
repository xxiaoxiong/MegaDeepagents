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
