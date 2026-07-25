# Vue 3 前端设计

技术栈：Vue 3、TypeScript、Vite、Pinia、Vue Router、原生 fetch/EventSource、Lucide icons、Vitest。

## 页面

- 运行列表：状态、模式、团队、更新时间、控制操作。
- 新建运行：目标、模式、团队、仓库、分支、review、低风险审批、上下文。
- 运行详情：Agent、TaskGraph、事件、审批、错误、Artifact、Verification。
- 设置：只读显示服务端 provider/model/LangSmith/并发策略，不回显密钥。

## 状态策略

- 页面进入先 REST 全量加载，再连接 SSE。
- 保存最后 `sequence`；断线指数退避并用 `after_sequence` 补齐。
- event envelope 以 `event_id` 去重，客户端最多保留 1,000 条。
- 控制按钮按 run status 禁用，失败显示可重试反馈。
- DAG 对大图按层布局并限制动画，节点展示状态、Agent、尝试和 Artifact 数。
- 响应式三栏在窄屏折叠为 tabs。

前端只访问 `/api/v1`；不拼服务器绝对路径。
