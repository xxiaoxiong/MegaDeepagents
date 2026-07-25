# 死代码、重复实现与兼容代码分类

删除前检查了 Python import、FastAPI 注册、CLI、动态 import、测试、SQLite 恢复和文档引用。

| 模块/范围 | 分类 | 处理 |
|---|---|---|
| `runtime/root_graph/*` | `ACTIVE_PRODUCTION` | V3 唯一顶层编排 |
| `parallel_scheduler.py` | `ACTIVE_PRODUCTION` | 保留，负责原子认领与并发 Worker |
| `task_graph.py`、`task_board.py` | `ACTIVE_PRODUCTION` | 保留并明确计划/执行事实边界 |
| `transactional_task_service.py` | `ACTIVE_PRODUCTION` | 保留，所有图 Mutation 事务化 |
| `executor.py` | `ACTIVE_PRODUCTION` | 保留 DeepAgents Worker harness |
| `artifact.py`、`verifier.py` | `ACTIVE_PRODUCTION` | 保留，完成判定 fail-closed |
| `permission.py`、`plan_approval.py` | `ACTIVE_PRODUCTION` | 保留 |
| `git_workspace.py` | `ACTIVE_PRODUCTION` | 保留 worktree 与集成门禁 |
| `team_runtime.py` | `ACTIVE_COMPATIBILITY` | V3 application service 的临时 facade |
| `routes_team.py` | `ACTIVE_COMPATIBILITY` | 旧路由只读/迁移兼容，新客户端禁用 |
| `team_runner.py` 及群聊簇 | `DEPRECATED` | 冻结；不再允许创建新运行，后续主版本删除 |
| `orchestrator.py` | `DUPLICATED` | 由 V3 Root Graph 替代并删除 |
| `orchestrator_graph.py` | `BROKEN` / `DUPLICATED` | 非生产图，含内存执行器降级，删除 |
| `scheduler.py` | `DUPLICATED` | 由 ParallelTeamScheduler 替代，数据契约迁出后删除 |
| `parallel_runner.py` | `UNREACHABLE` | 未接生产主链，删除 |
| `conflict_resolver.py` | `DOCUMENTATION_ONLY` | 未接 GitIntegrationManager，删除或归档设计文档 |
| `app/web/*` | `DEPRECATED` | Vue 构建产物稳定后移除 |
| `tests/test_orchestrator*.py` | `TEST_ONLY` | 随被替代模块删除，V3 根图集成测试接管不变量 |
| `docs/upgradePhase*.md` 等阶段报告 | `DOCUMENTATION_ONLY` | 历史归档，不再作为运行指南 |
| Fake/Scripted Worker | `TEST_ONLY` | 只允许测试注入，生产代码不得 fallback |

## 明确保留的不变量

- Worker 成功只表示产出证据，不能直接完成任务。
- TaskBoard 原子认领和 run/task 复合边界。
- Artifact 同 run、直接依赖、verified、文件存在、hash 一致。
- 权限不能由 Agent 自批。
- Git 集成先于 TaskBoard 最终成功。
- 取消后的迟到结果不能回写成功。

## 已确认的重复事实

- 旧 `team_events` 与 V3 `event_envelopes`。
- 旧 task attachment 与 V3 Artifact 曾同名。
- `TaskScheduler` 与 `ParallelTeamScheduler`。
- `SimpleOrchestrator` 与 `UnifiedOrchestratorGraph`。
- 旧 TeamRunner/TeamGraph 与 V3 Root Graph。
- 单 Agent Task 状态与团队 Board 状态。

兼容数据保留不等于允许继续创建旧类型数据；V3 新写入只进入统一运行时。
