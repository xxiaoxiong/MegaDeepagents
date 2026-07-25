# V3 迁移计划

1. 记录 `9458add` 真实调用图和基线测试。
2. 统一 SQLite 连接策略、WAL、busy timeout、foreign keys。
3. 将旧单任务附件迁移到 `task_artifacts`，释放 `artifacts` 给 V3。
4. 将 `phase_g_store.py` 重命名为 `infrastructure/database/run_store.py`。
5. 新建 run domain、application service、Supervisor 和 LangGraph Root Graph。
6. 让 TeamRuntimeFacade 的 TASK_TEAM 路径调用 Root Graph。
7. 新客户端统一到 `/api/v1/runs`；冻结 DISCUSSION 新建入口。
8. 删除重复 Orchestrator/Scheduler，迁移其有效测试不变量。
9. 建 Vue 3 前端并让 FastAPI 在构建后托管 `frontend/dist`。
10. 更新文档、Docker、CI、部署和迁移说明。

## 兼容策略

- 旧任务/团队历史表不删除。
- `/api/v1` 是稳定写入口。
- 旧 API 在一个迁移周期内保留查询；新建返回迁移提示。
- 旧 DISCUSSION run 可查看或取消，但不可新建/扩展。
- 迁移前建议备份 `runtime/db` 和 `runtime/workspaces`。

## 回滚

代码回滚不应回滚用户数据库。`task_artifacts` 重命名是向前兼容迁移；旧版本如需读取，应使用导出脚本或恢复备份，不能把 V3 `artifacts` 强行改回旧 schema。
