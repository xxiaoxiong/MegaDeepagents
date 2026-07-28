# 前端界面

## 项目介绍站

公开官网位于 `website/`，是完全独立的亮色 Vite 静态站点。它默认显示英文，可切换中文，
用于向 GitHub 访客介绍项目定位、核心能力、Root Graph 架构和快速安装方式。

Vercel 只构建这个目录。官网不连接 Runtime API，也不包含运行、审批或仓库操作入口。

```bash
npm --prefix website ci
npm --prefix website run build
```

## Vue 3 运行控制台

前端位于 `frontend/`，使用 Vue 3、TypeScript、Vite、Pinia、Vue Router、原生 fetch、
EventSource 和 Lucide。

### 页面

- 运行列表：搜索、刷新、状态、Agent 数、Task 进度、暂停/恢复/取消。
- 创建运行：目标、模式、团队、仓库、分支、Review、并发和修复预算。
- 运行详情：Agent Mission Control、主线/并行 TaskGraph、健康诊断、团队消息、Artifact
  内容与版本链、审批、完整审计、Git 和错误恢复。
- 系统设置：后端连接地址、脱敏模型配置和运行时限制。

所有运行数据来自 `/api/v1`。Pinia 只保存页面投影，不是权威状态。详情页先并行获取
REST 快照，再通过 SSE 增量合并；事件按 `event_id` 去重并按 `sequence` 排序，页面刷新后
重新从 SQLite 恢复。

审计台常驻在运行详情主视图，不再隐藏在标签页中。它分页加载完整事件历史，提供最新活动
弹幕、执行/工具/恢复/异常分类、全文搜索、结构化摘要和原始 JSON。SSE 断开后按指数退避
重连，并在界面明确显示连接状态与错误。

Mission Control 使用 `/execution` 投影展示团队并行度、关键路径、待处理事项和 Agent
泳道。选择 Agent 或 Task 会联动审计台，只显示对应的工具、协作、失败和交付链路；对话页
也会持续显示团队 live pulse，并可直接跳到指定 Agent。

Artifact Explorer 默认打开最新产物，支持搜索、Markdown/文本/常见图片预览、版本 lineage、
复制链接/内容和下载。`#artifact-{artifact_id}` 深链接会在刷新后恢复选择并滚动到产物区域。

SSE 到达后只刷新事件类型影响的 REST 切片：Token 与心跳不触发快照请求，Task、Agent、
Artifact、诊断和 Git 各自按需刷新。事件集合使用 ID 索引和 sequence 二分插入，避免长任务
中反复全量排序。页面路由与 Markdown 语法高亮语言按需加载，以缩小初始 bundle。

健康面板使用 `/diagnostics` 显示当前阶段、最后活动、延迟重试和阻塞项。错误恢复页可以
重试单个 Task 或全部失败 Task，所有操作都通过 `/api/v1` 留下持久审计记录。

### 构建

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

`VITE_API_BASE_URL` 为空时使用同源 API。用户在设置页填写的地址优先级更高，只保存在
localStorage，绝不保存模型密钥。

界面在 1100px 折叠侧栏，在 760px 切换为底部导航；390px 浏览器验收无横向溢出。

运行控制台由 Docker/FastAPI 同源提供，不发布到公共 Vercel 官网。
