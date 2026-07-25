# 开发指南

## 环境

- Python 3.11 或更高
- Node.js 20 或更高
- Git（代码任务需要）

复制 `.env.example` 为 `.env`。没有 `LLM_API_KEY` 时，API、数据库、前端和非 live
测试可运行；真实 Worker 会 fail-closed。

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
npm --prefix frontend ci
```

后端开发服务：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

前端开发服务：

```bash
npm --prefix frontend run dev
```

Vite 将 `/api` 和 `/health` 代理至 8081。若后端不在本机，可复制
`frontend/.env.example` 并设置 `VITE_API_BASE_URL`。

## 修改原则

1. 所有新 Run 用例进入 `app/application/runs`。
2. 编排行为修改放在 `app/runtime/root_graph`，不要新增 Python while-loop Orchestrator。
3. Task 状态修改通过 TaskBoard/TransactionalTaskService。
4. Worker 只返回 `TaskExecutionResult` 和 Artifact，不直接完成任务。
5. 数据库访问通过 `app/infrastructure/database` 的统一连接。
6. 新实时状态必须先持久化 Event Envelope，再由 SSE 投影。
7. 修改后运行相关测试，再运行完整非 live 套件。

## 目录

```text
app/
  api/v1/                 公共 HTTP/SSE 契约
  application/runs/       Run 用例
  domain/                 V3 领域契约
  runtime/root_graph/     生产 LangGraph
  runtime/supervisor/     结构化 Supervisor
  infrastructure/database/
  multiagent/             保留的控制面与兼容边界
frontend/                 Vue 3 控制台
tests/                    后端测试
docs/refactor-v3/         审计证据
```
