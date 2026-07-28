# 部署指南

## 推荐拓扑

```text
GitHub 访客 ──→ Vercel 项目介绍站 (website/)

项目操作者 ──→ 持久 Docker 主机
                 ├── FastAPI + LangGraph + DeepAgents
                 ├── Vue 运行控制台 (frontend/)
                 └── SQLite volume + workspaces + Git repositories
```

Vercel 的函数实例是无状态、短生命周期的。把 SQLite、长时间 Agent Loop 或 Git
worktree 放进 Vercel 会导致运行丢失、文件不可见和超时。仓库中的 `vercel.json`
因此只构建纯静态 `website/`；它是项目介绍页，不连接生产 Runtime API。

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

## Vercel 项目官网

当前正式官网：<https://megadeepagents.vercel.app>

项目根目录执行：

```bash
npm --prefix website ci
npm --prefix website run build
npx vercel --prod
```

官网默认英文，通过页面右上角切换中文；语言偏好只保存在浏览器 localStorage。站点不需要
API 地址、模型密钥或运行时环境变量。

## 自托管运行时

`Dockerfile` 会先构建 Vue，再把 `frontend/dist` 复制到 Python 镜像。运行镜像同时包含
Git、Node 和 npm，供 Agent 和仓库级门禁验证常见 Python/Node 项目；pnpm、yarn、Rust、
Go 等运行时需要在派生镜像中显式安装。访问后端根路径即可打开运行控制台，API 使用同源
地址。公共官网和运行控制台刻意分离。

## 健康检查

- `GET /health`
- `GET /api/v1/settings`
- `GET /docs`

部署后创建一个不写仓库的最小 Run，确认 `events`、SSE 重连、SQLite 重启恢复和取消均工作。
