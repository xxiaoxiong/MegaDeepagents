# MegaDeepagents V3

[简体中文](README.md) | [English](README.en.md)

[Project website](https://megadeepagents.vercel.app) ·
[Architecture](docs/architecture.md) ·
[API](docs/api.md) ·
[Deployment](docs/deployment.md)

MegaDeepagents 是一个单机优先、可恢复、可观测、受治理的多智能体任务运行平台。浏览器、
API 与 CLI 的新请求进入同一套 LangGraph Root Graph；DeepAgents 只负责 Worker
Agent Loop，任务的最终成功由控制面和 Verifier 判定。

> 如果这个项目对你的多智能体工程有帮助，欢迎点一个 Star；问题、运行反馈和改进建议请通过 Issue 提交。

![MegaDeepagents — One runtime. Accountable agent teams.](website/public/og.png)

## V3 主链

```text
Vue 3 / API / CLI
        ↓
RunApplicationService
        ↓
LangGraph Root Graph
   ├── single path
   └── team supervisor path
        ↓
TaskGraph → TransactionalTaskService → TaskBoard
        ↓
ParallelTeamScheduler → DeepAgentExecutor
        ↓
ArtifactStore → Verifier → Repair / Replan / HITL / Finalize
        ↓
SQLite + replayable Event Envelope
```

- `TaskGraph` 是版本化计划结构；`TaskBoard` 是执行状态、认领和尝试次数的权威事实源。
- Worker 只能提交 `PRODUCED` 产物和证据，不能直接写入 `SUCCEEDED`。
- Verifier fail-closed；模型、测试、格式或 Artifact 完整性验证失败时不会伪造成功。
- SQLite 使用统一连接、WAL、busy timeout 和事务，支持进程重启后的 Run 恢复。
- 生命周期、工具、心跳、验证、重试和失败都会进入可重放审计流；控制台可查看完整 payload。
- Mission Control 将持久事件重放为每个 Agent 的执行泳道、关键路径、并行效率和注意项。
- Artifact 可直接预览、下载、追溯版本，并通过深链接交付给用户。
- 失败按限流、超时、网络、权限、契约等分类，有界指数退避；操作者可恢复单个或全部失败 Task。
- Run 健康诊断会明确标记正常、待处理、卡住、失败和完成，不再依赖“盯着页面猜”。
- 代码任务可以绑定 Git 仓库，每个 Agent 使用独立 worktree，集成通过受治理队列完成。
- LangSmith 完全可选；没有凭证时平台仍可离线运行。

## 本地启动

需要 Python 3.11+ 和 Node.js 20+。

```bash
cp .env.example .env
python -m venv .venv
python -m pip install -e ".[dev]"
npm --prefix frontend ci
```

启动后端：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

另一个终端启动前端：

```bash
npm --prefix frontend run dev
```

打开 `http://127.0.0.1:5173`。生产构建由 FastAPI 同源提供：

```bash
npm --prefix frontend run build
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```

持久数据保存在 `megadeepagents-data` volume。若要让 Agent 操作宿主机仓库，通过
`HOST_REPOSITORY_ROOT` 将允许的仓库父目录挂载到容器 `/repositories`。

## API

新客户端只使用 `/api/v1`：

| 能力 | 端点 |
|---|---|
| Run | `POST/GET /api/v1/runs` |
| 控制 | `POST /api/v1/runs/{id}/pause|resume|retry|cancel` |
| 健康诊断 | `GET /api/v1/runs/{id}/diagnostics` |
| 执行智能 | `GET /api/v1/runs/{id}/execution` |
| 实时事件 | `GET /api/v1/runs/{id}/stream?after_sequence=N` |
| Task / Graph | `GET /api/v1/runs/{id}/tasks`、`task-graph` |
| Agent / 消息 | `GET /api/v1/runs/{id}/agents`、`POST .../messages` |
| Artifact | `GET /api/v1/runs/{id}/artifacts`、`.../content`、`.../download` |
| 审批 | `GET/POST .../permissions`、`.../plans` |
| 验证 / 错误 / Git | `GET .../verification`、`errors`、`git`、`worktrees` |

完整契约见 [docs/api.md](docs/api.md)，交互式 OpenAPI 为 `/docs`。

## 测试

```bash
python -m compileall -q app
pytest -m "not live_model and not real_langsmith"
npm --prefix frontend test
npm --prefix frontend run build
```

真实模型和 LangSmith 测试默认跳过，必须显式配置凭证后运行。测试不会将模型不可用当作成功。

## 部署边界

Vercel 只发布 `website/` 中的中英文项目介绍站，不承载运行控制台、Python Worker、
LangGraph、SQLite 或本地 worktree。自托管 Docker 镜像同时提供持久后端与
`frontend/` 运行控制台；两者属于不同部署面。

详见：

- [架构](docs/architecture.md)
- [代码库地图](docs/codebase-map.md)
- [开发](docs/development.md)
- [部署](docs/deployment.md)
- [数据库](docs/database.md)
- [可观测性与恢复](docs/observability.md)
- [V3 迁移](docs/migration-v3.md)
- [重构审计](docs/refactor-v3/00-current-runtime-audit.md)
