# General Agent Frame

基于 DeepAgents 原生能力的通用 Agent Runtime。

优先使用 DeepAgents 官方 API，精简自研执行层，不包含自进化、自动 Curator、自动 Skill 生成等能力。定位是稳定、简洁、可运行的通用智能体底座。

## 当前定位

- **DeepAgents 原生能力优先**：tools / backend / memory / skills / permissions / checkpointer / store / interrupt_on
- **精简架构**：API / CLI → TaskService / TaskRunner → DeepAgents create_deep_agent
- **稳定优先**：关闭不稳定 streaming，移除自进化链路，最小任务执行闭环

## 保留能力

- FastAPI 基础接口
- CLI 基础调用
- 单任务执行 + 多会话 thread
- workspace 文件系统（通过 DeepAgents backend 虚拟路径）
- memory 文件读写（/memory/MEMORY.md, /memory/USER.md）
- 静态 Skills 加载与管理
- DeepAgents subagents（async 模式，需外部 graph 服务）
- 基础权限控制（敏感文件 deny）
- 可选 HITL 审批（approve / reject via Command(resume=...)）

## 不再支持的能力

- 自进化（Self-Evolution）
- 自动 Skill 生成 / 修改
- 自动 Curator（Memory Curator / Skill Curator）
- Nudge / Review Queue
- 复杂 streaming SSE
- 未实现 Web / MCP 工具（默认关闭）
- 自定义 shell 执行（默认关闭）

## 快速启动

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

浏览器访问：http://127.0.0.1:8081

## 配置

复制 `.env.example` 为 `.env`，配置模型：

```env
LLM_PROVIDER=openai-compatible
LLM_MODEL=step37-flash
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=http://127.0.0.1:4000/v1
```

关键默认开关：

| 开关 | 默认值 | 说明 |
|---|---|---|
| enable_safe_shell | false | 自定义 shell 工具 |
| enable_web_tools | false | 网页搜索/抓取（占位） |
| enable_mcp_tools | false | MCP 工具集成 |
| enable_streaming | false | SSE 流式输出 |
| enable_subagents | true | 子智能体委派 |
| enable_async_subagents | false | 异步子智能体（需 langgraph.json 服务） |

## CLI 使用

```bash
# 运行任务
python -m app.cli run "帮我生成一个纯前端的待办清单"

# 任务管理
python -m app.cli task-list
python -m app.cli task-show <task_id>

# Skill 管理
python -m app.cli skills list
python -m app.cli skills show <name>
python -m app.cli skills scan

# 记忆管理
python -m app.cli memory show
python -m app.cli memory search "关键词"

# 查看工具与配置
python -m app.cli tools-list
python -m app.cli config-show
```

## API 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 健康检查 |
| POST | /chat | 提交对话任务 |
| GET | /tasks | 任务列表 |
| GET | /tasks/{id} | 任务详情 |
| GET | /tasks/{id}/events | 事件流 |
| GET | /tasks/{id}/messages | 过程消息 |
| POST | /tasks/{id}/approve | 审批通过 |
| POST | /tasks/{id}/reject | 审批拒绝 |
| GET | /tasks/{id}/artifacts/{path} | 产物下载 |
| GET | /memory | 读取记忆 |
| POST | /memory/search | 搜索记忆 |
| GET | /skills | 列出 Skills |
| GET | /skills/{name} | 读取 Skill |
| POST | /skills | 创建 Skill |
| POST | /skills/{name}/pin | 固定 Skill |

## 目录结构

```text
app/
  agents/__init__.py     # 本地 graph 工厂（researcher/coder/reviewer）
  api/                    # REST 路由
  backends/__init__.py   # CompositeBackend + _SafeFilesystemBackend + LocalShellBackend
  core/                   # 配置、日志、Agent 工厂、State Schema、Context
  memory/                 # 热记忆、冷记忆、FTS 检索
  skills/                 # Skill 加载器、元数据、管理器
  task/                   # TaskService / TaskRunner / Store / Models
  tools/                  # ToolRegistry + memory/skills/task/web_shell/mcp 工具
  web/                    # 前端页面
  main.py                 # FastAPI 入口
  cli.py                  # Typer CLI
  permissions.py          # 权限规则
```

```text
runtime/
  workspace/              # Agent 产物根目录
  memory/                 # MEMORY.md / USER.md
  skills/                 # 静态 Skills 目录
  db/                     # SQLite
  cache/                  # LLM 缓存
```

## 如何添加一个人工 Skill

在 `runtime/skills/<name>/SKILL.md` 中写入 frontmatter + 内容，或通过 API：

```bash
curl -X POST http://127.0.0.1:8081/skills \
  -H "Content-Type: application/json" \
  -d '{"name":"my-skill","description":"示例","content":"## 规则\n..."}'
```

## 如何运行测试

```powershell
$env:PYTHONUTF8='1'
python -m pytest tests/
```

## 后续恢复自进化的位置

如需恢复，需关注以下模块：

- `app/evolution/`：评测集、Metric、Prompt registry、Optimizer
- `app/nudge/`：后台回顾队列、Reviewer
- `app/review/`：审批队列、apply / reject 逻辑
- `app/skills/curator.py` + `curator_prompts.py`：Skill 双阶段治理
- `app/skills/archive.py`, `snapshot.py`, `diff.py`, `provenance.py`, `usage.py`：Skill 生命周期
- `app/memory/curator.py`：Memory 治理
- `app/api/routes_curator.py`, `routes_evolution.py`, `routes_review.py`：对应 API 路由
- `app/cli.py`：对应 curator / review / evalset 子命令

恢复方式：将上述模块重新加入 `main.py` 路由、`cli.py` 子命令、`agent_factory.py` 系统提示，并重新开启 config 开关。

## 技术栈

- Python >= 3.11
- DeepAgents 0.6.8
- LangChain 1.3.4
- LangGraph 1.2.4
- FastAPI + Uvicorn
- SQLite + aiosqlite
- Typer + Rich
- Pydantic Settings

## License

MIT
