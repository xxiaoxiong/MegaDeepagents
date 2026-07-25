# V3 SQLite schema 与所有权

统一配置：`DATABASE_URL`/`SQLITE_PATH`。所有连接经 `app.infrastructure.database.connection`，启用 WAL、busy timeout、foreign keys 和 `synchronous=NORMAL`。

| 表 | 所有者 | 事实 |
|---|---|---|
| `schema_version` | database | 当前迁移版本 |
| `team_runs` | run application | durable run 元数据/状态 |
| `checkpoints*` | LangGraph SqliteSaver | 图执行位置 |
| `task_graph_snapshots` | TransactionalTaskService | 当前版本化计划 |
| `task_graph_mutations` | TransactionalTaskService | 计划变更审计 |
| `control_plane_outbox` | TransactionalTaskService | 事务 outbox |
| `task_board_tasks` | TaskBoard | 任务运行权威状态 |
| `agent_instances` | AgentRegistry | Agent 身份和租约状态 |
| `teammate_sessions` | TeammateSupervisor | 稳定 session |
| `teammate_queue_items` | TeammateSession | session 工作队列 |
| `mailbox_messages` | Mailbox | 团队消息 |
| `artifacts` | ArtifactStore | V3 产物元数据 |
| `structured_permission_requests` | PermissionBroker | 权限请求/决定 |
| `teammate_plans` | PlanApprovalService | 计划审批 |
| `worktree_leases`、`merge_queue` | Git integration | worktree/集成 |
| `tool_invocations` | Tool runtime | tool/副作用 journal |
| `event_envelopes` | event store | 单调 sequence 的 SSE 审计 |
| `tasks`、`task_events`、`task_messages` | legacy TaskService | 单任务兼容 |
| `task_artifacts` | legacy TaskService | 旧单任务附件 |
| `team_rooms` 等群聊表 | legacy DISCUSSION | 只读兼容 |

JSON 统一用 UTF-8 `json.dumps`，时间新代码使用带 UTC offset 的 ISO-8601。旧 naive 时间保留读取兼容。

## artifact 表迁移

连接首次打开时检查旧 `artifacts(id, task_id, path, name, ...)`。若存在，原子重命名/合并为 `task_artifacts`，再由 V3 创建 `artifacts(artifact_id, run_id, relative_path, content_hash, ...)`。迁移有回归测试，旧行不丢失。
