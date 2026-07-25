# V3 迁移说明

## 行为变化

1. 新入口为 `POST /api/v1/runs`，模式为 `auto|single|team`。
2. 单 Agent 与团队不再使用两套编排器，都进入 Root Graph。
3. 新建 `DISCUSSION` 被拒绝；历史记录仍可读取和取消。
4. Worker 成功不等于 Task 成功，必须通过 Verifier。
5. SSE 使用持久 `event_envelopes.sequence`，客户端应保存最后序号。
6. Vue 控制台取代 `app/web` 静态页面。
7. 旧 chat/task/memory/skills HTTP 路由默认不挂载；临时迁移可设置
   `ENABLE_LEGACY_API=true`，但不得用于新集成。

## 代码迁移

- 删除 `SimpleOrchestrator`、旧 UnifiedOrchestratorGraph、TaskScheduler、
  ParallelRunner 和未接生产的 ConflictResolver。
- Run Store 从临时阶段命名迁至 `app/infrastructure/database/run_store.py`。
- Worker 返回契约迁至 `app/domain/tasks/models.py`。
- Supervisor 决策迁至 `app/domain/runs/models.py` 和 `app/runtime/supervisor`。
- API 用例迁至 `app/application/runs`，公共路由迁至 `app/api/v1`。

## 数据迁移

数据库启动时原地、事务化迁移，不删除历史 Run：

- 旧任务附件 `artifacts` → `task_artifacts`。
- V3 Artifact 使用包含 run/task/hash/version/lineage 的新 `artifacts`。
- 新增 TaskGraph snapshot 与 Event Envelope。
- 历史 `team_runs` 表名保留，避免破坏已有数据；语义已统一为 Agent Run。

执行迁移前备份 `runtime/db/app.sqlite3`。V3 不支持降级后继续写同一个数据库。

## 客户端迁移

旧 `/team-tasks` 客户端应切换到 `/api/v1/runs`。状态映射：

| 旧状态 | V3 状态 |
|---|---|
| completed | succeeded |
| interrupted | waiting_human / paused / cancelled |
| incomplete | failed |

Artifact 不再通过任意服务器路径读取；使用 V3 content 或 download 端点。
