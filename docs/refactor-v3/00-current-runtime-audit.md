# V3 重构前真实运行时审计

审计基线：`main@9458add`。证据来自入口注册、调用图、SQLite DDL、测试和实际执行，而非文件名推断。

## 目录与入口

- `app/main.py` 注册 FastAPI；旧静态页来自 `app/web`。
- `app/cli.py` 提供单任务、团队、Skills 和 Memory 命令。
- `app/task/*` 是早期单 Agent 任务服务。
- `app/multiagent/*` 同时包含 TASK_TEAM 生产链、DISCUSSION 兼容链和实验实现。
- `app/api/routes_*.py` 是历史 API；V3 入口新增于 `app/api/v1`。
- `runtime/` 保存 SQLite、workspace、checkpoint、memory 和离线 trace。

## 重构前单 Agent 主链

```text
/chat 或 /tasks
→ TaskService（进程单例）
→ TaskRunner
→ app.core.agent_factory
→ DeepAgents create_deep_agent
→ task store / task_messages / task_events
```

单 Agent 使用 DeepAgents，但拥有独立 Task、HITL、SSE 和持久化模型，没有进入团队控制面。

## 重构前多 Agent 主链

默认 TASK_TEAM：

```text
API / CLI
→ TeamRuntimeFacade
→ SimpleOrchestrator（Python 状态机/循环）
→ TransactionalTaskService
→ ParallelTeamScheduler（asyncio 循环）
→ TaskBoard 原子认领
→ TeammateSession
→ DeepAgentExecutor
→ ArtifactStore / Verifier / GitIntegrationManager
```

显式 DISCUSSION：

```text
API / CLI --legacy
→ TeamRuntimeFacade
→ TeamRunner / TeamRoundExecutor
→ SpeakerSelector / Action JSON / TeamRoom
```

另有 `orchestrator_graph.py → TaskScheduler` 实验图。它不在生产入口中，并在未注入 Worker 时使用内存执行器。

## 关键模块与真实调用者

| 模块 | 重构前调用者 | 事实角色 |
|---|---|---|
| `TeamRuntimeFacade` | CLI、`routes_team` | 团队入口与生命周期门面 |
| `SimpleOrchestrator` | TASK_TEAM facade、旧测试 | 顶层 Python 编排 |
| `ParallelTeamScheduler` | `SimpleOrchestrator` | 生产任务认领与并发执行 |
| `TaskScheduler` | 旧 orchestrator、实验图、测试 | 重复调度器 |
| `TaskGraph` | Planner、Orchestrator、Scheduler、恢复 | 计划结构、契约、版本 |
| `TaskBoard` | Scheduler、控制面、恢复、API | 运行态权威事实源 |
| `DeepAgentExecutor` | Scheduler | Worker harness |
| `ArtifactStore` | Executor、Verifier、API | 产物文件、hash、lineage |
| `TeamRunner` | DISCUSSION 入口 | 冻结兼容运行时 |
| `TeamGraph` | DISCUSSION 可选路径 | 旧群聊图，不是 V3 控制面 |

## 状态、checkpoint 与事实源

- Run：`team_runs` 加进程内 `_active_runs` 缓存。
- 计划：`task_graph_snapshots` 与 `task_graph_mutations`。
- 任务运行：`task_board_tasks`。
- Agent/Session：`agent_instances`、`teammate_sessions`、`teammate_queue_items`。
- Artifact：`artifacts` 和 workspace 文件。
- 审批：`structured_permission_requests`、`teammate_plans`。
- 事件：旧 `team_events` 与新 `event_envelopes`；重构前协议不统一。
- 单 Agent checkpoint：DeepAgents/LangGraph checkpointer。
- TASK_TEAM checkpoint：主要依靠 TaskGraph/TaskBoard 恢复；顶层编排位置没有统一 LangGraph checkpoint。

`TaskGraph` 是计划和契约；`TaskBoard` 是认领、尝试、所有权和执行状态。两者不能互相替代。

## API、CLI、Web 一致性

重构前不一致。CLI 和旧团队 API可进入 TASK_TEAM 或 DISCUSSION；`/chat`、`/tasks` 进入单 Agent；旧 Web 同时调用多套端点。V3 必须让新客户端只使用 `/api/v1/runs`。

## LangGraph、DeepAgents、LangSmith 使用程度

- LangGraph：单 Agent 内部和部分旧 TeamGraph 使用；不是 TASK_TEAM 顶层生产编排内核。
- DeepAgents：单 Agent 与团队 Worker 都真实使用，但上下文和控制面不同。
- LangSmith：可选 tracing，覆盖模型/工具的部分路径；无凭证时离线运行。不能作为完成证据。

## 最严重的十个问题

1. 单 Agent、TASK_TEAM、DISCUSSION 三套入口和事实模型。
2. 顶层生产编排依赖普通 Python 循环，而非统一 LangGraph。
3. `SimpleOrchestrator`、`orchestrator_graph`、`TaskScheduler` 与 `ParallelTeamScheduler` 职责重复。
4. DISCUSSION 仍能通过 CLI 创建新运行。
5. 多个模块自行管理 SQLite 连接和初始化顺序。
6. 单任务附件和 V3 Artifact 曾共用 `artifacts` 表名但 schema 不兼容。
7. 事件/SSE 有内存 emitter、旧事件表和持久事件 envelope 多套实现。
8. API 直接拼接多个 store，缺少统一 application service。
9. `phase_g_store` 等临时阶段命名进入生产代码。
10. README 把 `SimpleOrchestrator` 描述为生产主链，与 V3 目标不符。

## 不能直接删除的兼容边界

- `TaskService` 单例和 `task_messages`：旧任务 API 测试与已有数据依赖。
- 旧 `/tasks`、`/chat`、`/team-runs`、`/team-tasks` 查询：需要迁移期只读兼容。
- DISCUSSION 表和模型：已有房间/历史记录仍需读取和取消，禁止新建即可。
- `langgraph.json`、Skills、Memory：外部工具与部署可能引用。
- 旧 SQLite 表：必须保留或显式迁移，不能丢用户数据。

## 基线验证

- 纠正本机 Git/PATH 前：507 passed，8 failed，5 skipped。
- 纠正 PATH 后：511 passed，4 failed，5 skipped。
- 4 个失败均为 Windows 命令分类、取消信号和 symlink 权限差异，不是业务断言。
