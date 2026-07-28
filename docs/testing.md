# 测试策略

## 快速门禁

```bash
python -m compileall -q app
pytest -m "not live_model and not real_langsmith"
npm --prefix frontend test
npm --prefix frontend run build
```

## 覆盖重点

- Root Graph：single/team 共用生产图，真实 TaskBoard、Artifact 和事件投影。
- Verifier：Worker 产出后才验证，失败创建真实 repair task，不放宽断言。
- SQLite：旧 Artifact 表迁移、恢复、并发认领和 event sequence。
- API：创建、边界校验、事件、Artifact 路径安全、脱敏设置。
- 取消：迟到结果、停止 Agent、冷取消和冷恢复。
- Git：worktree 隔离、commit/integration 门禁、仓库清单自动发现、验证命令白名单与环境缺失恢复。
- 前端：API 序列化与错误、SSE reducer 去重排序、流式消息、集成验证状态渲染、生产构建。

`live_model` 和 `real_langsmith` 需要外部凭证，默认跳过。测试专用 Executor 只能通过依赖
注入进入 Root Graph；生产代码没有 FakeExecutor fallback。

## 手工验收

1. 桌面与 390px 移动视口打开运行列表。
2. 创建表单的目标、模式、Review 和限制字段可用。
3. 详情页刷新后从 REST 恢复，SSE 重连不重复事件。
4. 暂停、恢复、取消与审批按钮反映后端状态码。
5. Artifact 只能读取 Run workspace 内文件。
