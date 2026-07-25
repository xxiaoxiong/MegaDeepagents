# SQLite 数据库

## 连接策略

`app/infrastructure/database/connection.py` 是应用数据库的唯一配置入口：

- 一个线程一个应用连接。
- checkpoint 使用独立连接。
- WAL、foreign keys、`busy_timeout`、`synchronous=NORMAL`。
- 显式事务上下文负责提交与回滚。
- `DATABASE_URL` 和 `SQLITE_PATH` 最终解析为同一个文件。

## 表所有权

| 范围 | 主要表 | 所有者 |
|---|---|---|
| Run | `team_runs` | RunHistory（历史表名，V3 语义为 Agent Run） |
| LangGraph | checkpointer 自有表 | SQLiteSaver |
| Plan | `task_graph_snapshots`、mutations/outbox | TransactionalTaskService |
| Execution | `task_board_tasks`、`task_runs` | TaskBoard / RunHistory |
| Agent | `agent_instances`、teammate session/queue | Registry / Supervisor |
| Message | `mailbox_messages` | Mailbox |
| Artifact | `artifacts` | ArtifactStore |
| Governance | permission、plan approval | 对应 Broker/Service |
| Git | worktree lease、merge queue | Git workspace |
| Audit | `event_envelopes`、`team_events` | RunHistory |
| Migration | `schema_migrations` | connection layer |

`team_events` 暂时保留给旧读取接口；`event_envelopes` 是 V3 SSE 的权威源。

## 迁移

旧版曾把任务附件表命名为 `artifacts`，与 V3 Artifact schema 冲突。启动时会检测旧列
结构，将其无损迁移/合并到 `task_artifacts`，再创建 V3 `artifacts`。迁移在事务内执行，
并记录 schema version。

备份 SQLite 时应同时停止写入，或使用 SQLite 在线备份 API；不要只复制活动 WAL 文件中的
主数据库。单机版本不支持多个容器共享同一个 SQLite volume。
