# 部署指南

## 推荐拓扑

```text
Vercel (Vue 静态控制台)
            ↓ HTTPS / SSE
持久 Docker 主机 (FastAPI + LangGraph + DeepAgents)
            ↓
SQLite volume + workspaces + Git repositories
```

Vercel 的函数实例是无状态、短生命周期的。把 SQLite、长时间 Agent Loop 或 Git
worktree 放进 Vercel 会导致运行丢失、文件不可见和超时，因此仓库中的 `vercel.json`
只构建 `frontend/`。

## 后端 Docker

```bash
cp .env.example .env
# 至少填写 LLM_API_KEY，并收紧 CORS_ORIGINS
docker compose up -d --build
```

生产环境必须：

- 使用持久 volume 挂载 `/data`。
- 通过 HTTPS 反向代理暴露 8081。
- 将 `CORS_ORIGINS` 设置为实际 Vercel 域名，不使用 `*`。
- 只挂载 Agent 被允许修改的仓库父目录。
- 备份 `/data/app.sqlite3` 和 `/data/workspaces`。
- 单机只启动一个负责同一 SQLite 文件的应用实例。

## Vercel 前端

当前正式控制台：<https://megadeepagents.vercel.app>

项目根目录执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
npx vercel --prod
```

在 Vercel 项目设置中添加：

```text
VITE_API_BASE_URL=https://your-runtime.example.com
```

修改构建期环境变量后必须重新部署。也可以在控制台“系统设置”中临时保存 API 地址；
它只保存在当前浏览器 localStorage。

## 一体化部署

`Dockerfile` 会先构建 Vue，再把 `frontend/dist` 复制到 Python 镜像。访问后端根路径即可
打开控制台，API 同源时无需 `VITE_API_BASE_URL`。

## 健康检查

- `GET /health`
- `GET /api/v1/settings`
- `GET /docs`

部署后创建一个不写仓库的最小 Run，确认 `events`、SSE 重连、SQLite 重启恢复和取消均工作。
