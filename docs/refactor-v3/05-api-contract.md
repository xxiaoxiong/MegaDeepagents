# `/api/v1` 契约

统一入口 `POST /api/v1/runs`，`mode` 为 `auto | single | team`。请求使用 Pydantic `extra=forbid`；创建返回 202。

## Run

- `POST /runs`
- `GET /runs?limit=`
- `GET /runs/{run_id}`
- `POST /runs/{run_id}/pause|resume|cancel`
- `GET /runs/{run_id}/events?after_sequence=&limit=`
- `GET /runs/{run_id}/stream?after_sequence=`

## Tasks 与 Agents

- `GET /runs/{run_id}/task-graph`
- `GET /runs/{run_id}/tasks`
- `GET /runs/{run_id}/agents`
- `POST /runs/{run_id}/agents/{agent_id}/messages`

## Artifact、审批与验证

- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/artifacts/{artifact_id}`
- `GET /runs/{run_id}/artifacts/{artifact_id}/lineage`
- `GET /runs/{run_id}/artifacts/{artifact_id}/download`
- `GET /runs/{run_id}/permissions`
- `POST /runs/{run_id}/permissions/{id}/decision`
- `GET /runs/{run_id}/plans`
- `POST /runs/{run_id}/plans/{id}/decision`
- `GET /runs/{run_id}/verification`
- `GET /runs/{run_id}/errors`
- `GET /settings`（密钥只返回 configured 布尔值）

所有子资源先验证 `run_id`；Artifact 下载把 resolved path 限制在 run workspace；SSE 从持久 `sequence` 重放。

错误状态：422 schema、404 不存在/不属于 run、409 状态冲突、202 异步操作、429 限流。
