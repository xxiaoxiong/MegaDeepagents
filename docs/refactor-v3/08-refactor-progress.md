# V3 重构进度

## 已完成

- [x] 基线调用图、SQLite 和测试审计。
- [x] 单一 SQLite 连接策略、WAL、busy timeout、foreign keys。
- [x] 旧附件表无损迁移，解决 `artifacts` schema 冲突。
- [x] `phase_g_store` 移至 `infrastructure/database/run_store.py`。
- [x] Run domain model、Supervisor structured decision。
- [x] 统一 LangGraph Root Graph：single/team/verify/repair/replan/HITL/finalize。
- [x] TeamRuntimeFacade TASK_TEAM 接入新根图。
- [x] `/api/v1` Run/Task/Agent/Artifact/审批/验证/SSE。
- [x] FastAPI lifespan 与冷启动恢复。
- [x] Windows shell absolute executable、取消和 symlink capability 处理。
- [x] 删除重复 Orchestrator/Scheduler 与对应失效测试。
- [x] 冻结 DISCUSSION 新建入口，保留历史读取边界。
- [x] Vue 3 控制台、前端测试、生产构建和响应式浏览器验收。
- [x] Docker、Vercel 配置与最终文档。
- [x] 后端最终回归：446 passed、1 skipped、5 deselected。
- [x] 前端最终回归：3 个测试文件、4 项测试全部通过。
- [x] OpenAPI 验证：35 个 V1/基础设施路径，默认不暴露旧 `/chat` 和 Team API。
- [x] Vercel Production 发布并验证首页、静态资源和 Open Graph 图片。
- [x] 将 Vercel 从运行控制台调整为独立中英文项目介绍站；英文为默认语言。

## 最终验证

- [x] `compileall` 与完整非 live 回归。
- [x] Vue TypeScript 检查与生产构建。
- [x] Vercel production deploy：<https://megadeepagents.vercel.app>。
- [ ] Docker 镜像拉取验证：Dockerfile 已被 BuildKit 正常解析，但 Docker Hub
  IPv6 连接连续两次超时，未能下载 `node:22-alpine` 基础镜像。

## 风险

- SQLite 适合当前单机目标，不适合 Vercel 无状态函数承载长任务。
- Vercel 仅部署前端；持久 Python Worker/API 需 Docker 主机或容器平台。
- 历史 DISCUSSION 表在一个迁移周期内保留，尚未做物理删除。
- 旧兼容模块仍使用 `datetime.utcnow()`，Python 3.12 会产生弃用告警；下一迁移周期应统一为
  UTC aware datetime。
