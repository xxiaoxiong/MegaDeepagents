# Vue 3 控制台

前端位于 `frontend/`，使用 Vue 3、TypeScript、Vite、Pinia、Vue Router、原生 fetch、
EventSource 和 Lucide。

## 页面

- 运行列表：搜索、刷新、状态、Agent 数、Task 进度、暂停/恢复/取消。
- 创建运行：目标、模式、团队、仓库、分支、Review、并发和修复预算。
- 运行详情：Run 控制、TaskGraph、Agent、团队消息、Artifact、审批、事件、Git 和错误。
- 系统设置：后端连接地址、脱敏模型配置和运行时限制。

所有运行数据来自 `/api/v1`。Pinia 只保存页面投影，不是权威状态。详情页先并行获取
REST 快照，再通过 SSE 增量合并；事件按 `event_id` 去重并按 `sequence` 排序，页面刷新后
重新从 SQLite 恢复。

## 构建

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

`VITE_API_BASE_URL` 为空时使用同源 API。用户在设置页填写的地址优先级更高，只保存在
localStorage，绝不保存模型密钥。

界面在 1100px 折叠侧栏，在 760px 切换为底部导航；390px 浏览器验收无横向溢出。
