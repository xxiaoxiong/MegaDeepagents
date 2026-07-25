# V3 架构

## 权威边界

V3 只有一条新建运行主链：`RunApplicationService → TeamRuntimeFacade →
runtime/root_graph`。Facade 暂时保留是为了读取和控制历史数据，不再决定编排策略。

| 层 | 唯一职责 | 不负责 |
|---|---|---|
| FastAPI | 校验、鉴权边界、SSE、用例调用 | 调度 Worker |
| Root Graph | 编排位置、分支、interrupt/resume | 保存完整 TaskBoard |
| Supervisor | 输出结构化决策 | 直接写数据库或宣布成功 |
| TaskGraph | 计划、依赖、契约、版本 | 原子认领 |
| TaskBoard | 状态、所有权、尝试、取消门禁 | 计划语义 |
| DeepAgentExecutor | 动态任务上下文与 Worker Loop | 整体完成判定 |
| ArtifactStore | 文件、hash、版本、lineage | 任务状态 |
| Verifier | 证据门禁和修复/重排结论 | 伪造产物 |
| SQLite | 领域事实、checkpoint、事件重放 | 分布式消息队列 |

## Root Graph

```text
intake → complexity_router
             ├─ single_plan ─────────────┐
             └─ team_supervisor → build_team
                                         ↓
                     dispatch → collect → verify
                                           ├─ pass → finalize
                                           ├─ repair → dispatch
                                           ├─ replan → team_supervisor
                                           ├─ hitl → human_interrupt
                                           └─ fail
```

`single`、`team` 和 `auto` 使用同一个编译图。显式模式是确定性的；`auto` 可以调用
结构化 Supervisor，调用失败时只回退路由，不会回退成成功。

## 可靠性

- LangGraph 使用独立 SQLite checkpointer 连接，避免应用事务破坏 checkpoint。
- TaskBoard 以 `(run_id, task_id)` 为复合标识，多个 Run 不会互相覆盖。
- API 后台任务异常会持久化 `failed` 和 `RunFailed` 事件。
- SSE 通过单调 `sequence` 重放，断开连接不影响正在运行的任务。
- 取消状态优先于迟到 Worker 结果。
- Git worktree、权限、计划审批和 Artifact 都经过控制面。

## Legacy

`DISCUSSION/TeamRunner` 已冻结，创建新 DISCUSSION 会被拒绝。相关表和只读恢复边界保留一个
迁移周期，便于查看和取消旧记录；V3 新请求不再进入轮次群聊。旧 `/team-tasks` 只作为
TASK_TEAM 兼容适配器，Vue 和新集成只使用 `/api/v1`。
