# AGENTS.md — 通用智能体（General Agent）维护指南

本文档面向 AI 编程助手，用于持续维护本项目。覆盖项目定位、目录结构、运行方式、数据流、维护陷阱和标准修复流程。

## 1. 项目定位

本项目是 **DeepAgents + LangGraph** 风格的自主任务型智能体底座，包含**两条 Agent 执行路径**：

- **单 Agent 路径**：`CLI / API → TaskService → TaskRunner → DeepAgents create_deep_agent`
  - 状态：`TaskAgentState(DeepAgentState)` + SqliteSaver 检查点 + HITL 审批
- **多 Agent 路径**：`CLI team / API /team-tasks → TeamRunner → MessageBus + SpeakerSelector + ActionGuard + ReviewRepairLoop + ConflictResolver`
  - 状态：`SharedTeamState`（10 种 phase），7 张 SQLite 表全量持久化

**已实现**：SQLite 持久化、SSE 事件流（团队）、HITL 审批、MCP 工具集成（无配置未启用）、Harness Profiles、沙箱隔离、结构化输出、LangSmith 全链路 trace 埋点（T1–T8 已落地）。

**已移除**：自进化（Self-Evolution）、自动 Skill 生成/修改、自动 Curator（Skill/Memory）、Nudge/Review Queue 的源码文件已不可用（仅存 `__pycache__/*.pyc` 字节码残骸）。`.env` 中的 `ENABLE_CURATOR` / `ENABLE_NUDGE` / `ENABLE_EVOLUTION` 等配置位属于**陈旧 leftover**，`config.py` 不读取这些字段，维护时不要据此推断功能存在。如需恢复，参考 `docs/deepagents_hermes_curator_evolution_trae_task_plan.md` 从零实现。

## 2. 目录结构（关键）

```
app/
  agents/__init__.py        # 本地 graph 工厂（researcher/coder/reviewer）
  api/
    routes_chat.py          # POST /chat
    routes_tasks.py         # 单任务 CRUD + approve(reject（approve 为 async + run_in_executor）
    routes_team.py          # 多 Agent：/team-tasks/* （含 hitl-conflicts / hitl-resolve）
    routes_health.py        # /health
    routes_memory.py        # /memory + /memory/search
    routes_skills.py        # /skills CRUD
    routes_streaming.py     # SSE 流式（代码保留，未注册到 main.py）
  backends/__init__.py      # CompositeBackend + _SafeFilesystemBackend + LocalShellBackend
  cli.py                    # Typer CLI：run / task-list / task-show
                            #   子命令组：team（run/list）、skills（list/show/scan）、memory（show/search）
                            #   顶层：tools-list / config-show
  core/
    config.py               # Settings（langsmith_* 8 字段、enable_* 系列、aux/reflection LLM）
    logging.py              # 日志初始化（Windows 必须 UTF-8）
    agent_factory.py        # build_agent() 组装单 Agent
    observability.py        # ★ traceable / trace_span / emit_trace_event / get_current_run_url
    profiles.py             # Harness/Provider Profile 加载
    state_schema.py         # TaskAgentState(DeepAgentState)
    context.py              # AgentContext dataclass
    response_formats.py     # 结构化输出 Pydantic
    runtime.py / schemas.py
  main.py                   # FastAPI 入口；路由无 prefix，全在 routes 文件里写
  memory/                   # 热记忆、冷记忆、FTS 检索
  multiagent/               # ★ 22 模块的多 Agent 团队框架（见 §4）
  permissions.py            # 权限规则
  skills/                   # Skill 加载器、元数据、管理器
  task/
    models.py               # Task / TaskEvent / TaskMessage
    store.py                # SQLite（6 张表）
    service.py              # TaskService（务必保留 get_task_service() 单例）
    runner.py               # TaskRunner（@traceable single_agent_run）+ HITL + SSE 队列
  tools/
    mcp_loader.py           # .mcp.json 发现 + 工具转换
    registry.py             # ToolRegistry（file/memory/skills/task/web/shell_safe/mcp）
  web/                      # index.html / app.js / style.css
langgraph.json             # 本地 graph 注册（researcher/coder/reviewer）
runtime/
  workspace/               # Agent 可写的虚拟文件系统根目录
  memory/                  # MEMORY.md / USER.md
  skills/                  # 静态 Skills
  db/app.sqlite3           # SQLite（单 agent 6 张表 + 多 agent 7 张表）
  cache/                   # LLM 缓存
  logs/
```

## 3. 快速启动

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

- Windows 下必须设置 `PYTHONUTF8=1`，否则 `gbk` 编码会报错
- 静态文件由 `StaticFiles(directory=app/web)` 托管
- `app.mount("/", ...)` 会拦截所有未匹配路由，**API 路由必须在 `mount` 之前注册**

## 4. 多 Agent 团队模块地图（`app/multiagent/`）

22 个文件，构成核心 diffs。

| 模块 | 关键事实 |
|---|---|
| **team_runner.py** | 轮次循环主驱动；`TeamRunner.run()` 被装饰为 `@traceable(name="team_run")` (L174)；五阶段：select_speaker → agent_llm_call → process_actions → termination_check → review_repair（后两者局部触发） |
| **runtime_adapter.py** | 每个 Agent 的 LLM 调用入口；`_traced_llm_call` 装饰为 `@traceable(name="agent_llm_call", run_type="llm")` (L331)；构造 system prompt、调 `build_model()`、解析 JSON actions |
| **review_repair.py** | 评审-返工闭环；`ReviewRepairLoop.process_review_result()` 装饰为 `@traceable(name="review_repair")` (L56)；追踪 cycle 次数 |
| **event_emitter.py** | 进程内 SSE 总线；按 room_id 分发；同时旁路 `emit_trace_event` 到 LangSmith |
| **store.py** | SQLite 持久化（7 张表）；`team_rounds.langsmith_run_url` 列 (L163) 用于落 LangSmith 直跳链接；含旧库兼容 `ALTER TABLE ADD COLUMN` (L169-177) |
| **action_guard.py** | 角色权限白名单 `DEFAULT_ROLE_ALLOWED_ACTIONS` (L25-74)；越权 action 替换为带拒绝信息的 `no_op` |
| **speaker_selector.py** | 8 级优先规则（requires_response → must-act message_type → phase 对应角色 → 未读 → reply_to → phase fallback → anti-stall → first agent） |
| **conflict_resolver.py** | 冲突裁决规则引擎（Reviewer 否决 / Planner 路线 / 安全优先 / 谁产谁修）+ 兜不住升级 HITL |
| **layered_memory.py** | 四层记忆原型（Working/Episodic/Semantic/Procedural），进程内 dict 实现 |
| **bus.py** | 消息路由总线；direct 只投 to_agent（含别名归一化 DeveloperAgent→Coder 等）、broadcast 按 subscription、system 投所有 Agent |
| **team_graph.py** | LangGraph 可恢复状态图（4 节点 + HITL 中断节点 + SqliteSaver） |
| **room.py** | 多 Agent 任务环境（MessageBus + Inbox + SharedTeamState） |
| **inbox.py** | 私有收件箱 + 相关上下文优先排序 + summarize_old_messages |
| **termination.py** | 6 种终止策略（max_rounds / review_passed / stale_no_progress / stale_no_op / final_message / error_message ...） |
| **state.py** | `SharedTeamState`（10 种 phase + 合法转换校验） |
| **messages.py** | 26 种 MessageType + 别名归一化 normalize_message_type |
| **agent_spec.py** | AgentSpec / TeamSpec / TeamRunConfig / TeamRunResult |
| **default_teams.py** | 预置 `software_dev_team`（5 角色）/ `research_team`（3 角色）；`list_teams()` / `get_team()` |
| **prompts.py** | 6 套角色 prompt（Planner/Coder/Tester/ReviewerAgent/Finalizer/Researcher） |
| **policies.py** | `TeamRunMode`：CONTROLLED_GROUP_CHAT / ROUND_ROBIN / FREE_FORM |
| **models.py** | 聚合导出（无业务逻辑） |

### 4.1 软件团队角色 Action 白名单（维护时务必保持一致）

| 角色 | 允许的 action type |
|---|---|
| Planner | send_message / update_state / handoff / no_op |
| Coder | send_message / create_artifact / request_review / handoff / no_op |
| Tester | send_message / create_artifact / handoff / no_op |
| ReviewerAgent | send_message / request_review / respond_critique / no_op |
| Finalizer | send_message / update_state / respond_critique / mark_done / no_op（唯一允许 mark_done）|

修改 `default_teams.py` 中 Agent 的 `allowed_actions` 时，必须同步审视 `action_guard.py::DEFAULT_ROLE_ALLOWED_ACTIONS` 是否覆盖该角色——ActionGuard 实际拦截逻辑**始终**参考这套白名单兜底。

## 5. 核心数据流

### 5.1 单 Agent 路径

```
用户输入 → POST /chat → TaskRunner.run()  [@traceable single_agent_run]
  ├─ TaskService.create_task()              → tasks 表 + task_created
  ├─ TaskService.add_message(user)          → task_messages
  ├─ build_agent()                          → DeepAgents + SqliteSaver + TaskAgentState
  ├─ agent.invoke()
  │   ├─ 中间消息 (assistant/tool)           → task_messages（runner 主动落库）
  │   └─ HITL → __interrupt__ → interrupt_detected → waiting_approval
  └─ [POST /tasks/{id}/approve] async + run_in_executor
      └─ Command(resume=decisions) → task_completed
```

### 5.2 多 Agent 路径

```
用户提交目标 → POST /team-tasks（后台线程）或 CLI team run
  ↓
TeamRunner.run()  [@traceable team_run]
  ├─ 发 user_request → MessageBus → 各 Agent Inbox
  └─ for 每轮:
      ├─ select_speaker     [T3]   SpeakerSelector 8 级规则
      ├─ agent_llm_call     [T4]   runtime_adapter：system prompt + LLM + JSON actions
      │                          ActionGuard 越权拦截 → no_op（带拒绝信息）
      ├─ process_actions    [T5]   actions → messages → bus.publish → state.update
      │                          └─ review_result → ReviewRepairLoop [T7]
      ├─ termination_check  [T6]   6 种策略
      └─ round 落库：team_rounds + langsmith_run_url（来自 get_current_run_url()）
  
冲突升级 → state.issues(status=blocking) → /team-tasks/{id}/hitl-conflicts
人工裁决 → /team-tasks/{id}/hitl-resolve/{issue_id} → Decision + 关闭 Issue
```

## 6. 可观测性集成（`app/core/observability.py`）

### 6.1 9 个 trace 埋点

| 编号 | 埋点                | 类型              | 文件:行                                                        |
|---|---------------------|-------------------|----------------------------------------------------------------|
| T1 | `team_run`          | `@traceable chain`| `app/multiagent/team_runner.py:174`                           |
| T2 | `team_round`        | `trace_span chain`| `app/multiagent/team_runner.py:210`                           |
| T3 | `select_speaker`    | `trace_span chain`| `app/multiagent/team_runner.py:216`                           |
| T4 | `agent_llm_call`    | `@traceable llm`  | `app/multiagent/runtime_adapter.py:331`                       |
| T5 | `process_actions`   | `trace_span chain`| `app/multiagent/team_runner.py:306`                           |
| T6 | `termination_check` | `trace_span chain`| `app/multiagent/team_runner.py:344`                           |
| T7 | `review_repair`     | `@traceable chain`| `app/multiagent/review_repair.py:56`                          |
| T8 | `single_agent_run`  | `@traceable chain`| `app/task/runner.py:186`                                       |
| T9 | `memorize_summary`  | —                 | **计划中，尚未实现**                                            |

### 6.2 公开 API

| API | 作用 |
|---|---|
| `init_observability(component)` | 进程启动调用一次（幂等）；读 settings 决定 LangSmith 启停 |
| `is_enabled()` | 热路径廉价判断当前是否 tracing |
| `traceable(name, run_type, tags, metadata)` | 函数级 trace 装饰器；disabled 时若 offline_log 仍打摘要 |
| `trace_span(name, run_type, metadata, tags)` | 上下文管理器 span；用 tracing_context + RunTree.create_child 挂到当前 trace 树 |
| `emit_trace_event(event_type, payload)` | 把 SSE 事件旁路分发到当前 LangSmith run 的 add_event |
| `get_current_run_url()` | 取当前 LangSmith run 的直跳 URL（TeamRunner.save_round 落库用） |
| `traced_llm_invoke(llm, prompt, ...)` | 给 deepagents 外的临时 LLM 调用包一层 trace |
| `reset_for_test()` | 测试专用：重置全局状态 + 清环境变量 |

### 6.3 离线模式

- `langsmith_enabled=false`：不上报，但 `offline_log=true`（默认）时本地 logger 仍打 `[trace] enter/exit` 摘要
- `enabled=true` 但未配置 `API_KEY`：自动降级为 offline_log 不上报（`init_observability` L150-157 处理）
- `enabled=true` + client 构造失败：同样降级为 offline_log（L142-149）

### 6.4 已知限制

`trace_span` 在 **langsmith 0.10** 下通过 `RunTree.create_child` + `tracing_context(parent=child_run)` 实现 child run 挂载，实测在 LangSmith UI 上 child span **可能显示为同级独立 run**而非挂在父 run 下。原因是 `get_current_run_tree()` 在 `@traceable` 装饰函数内因 weakref 机制返回 None，tracing_context 的 push 与之不完整兼容。

**T4 `agent_llm_call`** 因装饰的是真函数（非上下文管理器），其内的 LangChain `ChatOpenAI` 子调用可被正确挂为 child；UI 上至少能看到 LLM 调用→ChatOpenAI 的两层数据，token 用量和 prompt/response 完整。这是当前观测的核心价值路径。**修改 trace_span 时不要破坏这条主链**。

## 7. 数据库 Schema（SQLite，13 张表）

### 单 Agent（`app/task/store.py`，6 张）

```
tasks              # task_id PK, user_input, status, thread_id, final_answer, ...
task_events        # id PK, task_id, event_type, data JSON
task_messages      # id PK, task_id, role(user|assistant|system|tool), content, extra JSON
artifacts          # id PK, task_id, path, name, size_bytes
skills             # id PK, name UNIQUE, path, description, created_by, source, state, ...
skill_usage_events # id PK, skill_id, task_id, event_type, metadata_json
```

### 多 Agent（`app/multiagent/store.py`，7 张）

```
team_rooms        # room_id PK, task_id, goal, team_name, team_spec_json, config_json,
                  # state_json, status, terminated
team_agents       # room_id + agent_name UNIQUE, agent_json
agent_messages    # id PK, task_id, room_id, from_agent, to_agent, visibility,
                  # message_type, content, cause_by, reply_to, requires_response,
                  # evidence JSON, artifact_refs JSON, metadata JSON
agent_inbox       # room_id + agent_name + message_id UNIQUE, is_read, read_at
team_decisions    # id PK, room_id, title, rationale, decided_by, alternatives JSON
team_issues       # id PK, room_id, title, severity, status, owner, evidence JSON
team_rounds       # room_id, round_number, selected_speaker, action_summary,
                  # message_ids JSON, termination_reason,
                  # langsmith_run_url TEXT  ★ L163
```

> `team_rounds` 老库缺 `langsmith_run_url` 列时，`store._init_multiagent_db` 会自动 `ALTER TABLE` 补列（L169-177）。

## 8. 常见开发任务与正确操作

### 8.1 修改任务服务（单 Agent）

`TaskService` 是全局单例，必须保留 `get_task_service()`（`app/task/service.py:99`）。

```python
# 正确
task_service = get_task_service()
# 新增方法：同时改 TaskService 类和 get_task_service 单例
```

### 8.2 修改团队服务

团队路径用 `app/multiagent/store.py` 的 `MultiAgentStore` 直接落库；事件用 `event_emitter.EventEmitter` + `add_trace_event` 旁路 LangSmith。

### 8.3 写中间消息到前端可见

`app/task/runner.py::run()` 必须把 `result.value.messages` 遍历后调 `task_service.add_message()`；前端只在 `/tasks/{id}/messages` 轮询可见。

### 8.4 修改审批接口（单 Agent）

审批已改成**异步非阻塞**模式（`app/api/routes_tasks.py:139`）：

```python
@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str):
    task_service.update_status(task_id, TaskStatus.RUNNING)  # 立即切回 running
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, _approve_sync, task_id)  # 线程池 max_workers=2
    return await asyncio.shield(future)
```

前端配合：按钮点击后 `isApproving = true` 防重；显示 "⏳ 处理中..."；不停轮询。

### 8.5 修改团队 HITL

`/team-tasks/{id}/hitl-conflicts` 查 `state.issues` 中 blocking 类；`/team-tasks/{id}/hitl-resolve/{issue_id}` 写 Decision + 关闭 Issue，恢复团队循环。两条都在 `app/api/routes_team.py`。

### 8.6 修改前端消息渲染

`app/web/app.js` 三个独立轮询 + 一个 SSE 通道：

| 通道 | 频率 | 作用 |
|---|---|---|
| `/tasks/{id}` | 1.5s | 状态 + 审批条 |
| `/tasks/{id}/events` | 1.5s | 系统事件流 |
| `/tasks/{id}/messages` | 1s | user/assistant/tool 过程消息 |
| `/team-tasks/{id}/events` | SSE | 团队实时事件 |

新建消息轮询时必须清空 `knownMessageIds`，否则旧消息不会重渲染。

### 8.7 子智能体与 Graph 注册

- 同步子智能体：`app/core/agent_factory.py::build_subagents()` 返回 `list[dict]`
- 异步子智能体：`AsyncSubAgent(name, description, graph_id, url)`，`graph_id` 对应 `langgraph.json`
- 本地 graph 工厂：`app/agents/__init__.py::create_agent()`
- **不要删除 `langgraph.json`** —— 缺省会破坏 AsyncSubAgent 的 graph 发现

### 8.8 沙箱与后端

- 文件后端 `app/backends/__init__.py` 中 `_SafeFilesystemBackend` 保留 Windows `\\?\` 路径处理
- 沙箱通过 DeepAgents `LocalShellBackend`，`build_backend()` 在 `sandbox_provider=local` 时注入 `/sandbox`

### 8.9 添加新 trace 埋点

1. 优先用 `@traceable(name="xxx", run_type="chain|llm")` 装饰函数（父子链稳定）
2. 不能装饰函数时用 `with trace_span("xxx"):` 上下文管理器
3. 关键路径加 `emit_trace_event(event_type, payload)` 让 SSE 事件也进 LangSmith
4. 在本文件 §6.1 表格中登记新埋点

## 9. 已踩过的坑（典型陷阱）

| # | 现象 | 原因 | 正确做法 |
|---|---|---|---|
| 1 | `ImportError: cannot import name 'get_task_service'` | 重写 `service.py` 只留类，丢了模块级 `_task_service` 和 `get_task_service()` | 任何重构都要检查 `get_task_service()` 是否还在 |
| 2 | Web 端只看到最终答案，看不到过程 | `result.value.messages` 没落库 | runner.run() 遍历 messages 调 `add_message()` |
| 3 | approve 后浏览器转圈、重复提交 | approve 同步阻塞 | `async` + `run_in_executor`，先 200 后台执行 |
| 4 | 改 style.css 但界面没变 | 浏览器缓存 | `Ctrl+Shift+R` 强刷 |
| 5 | `gbk` codec can't decode/encode | Windows 默认编码 | `$env:PYTHONUTF8='1'` 或 `main.py` 设环境变量 |
| 6 | 任务完成后 SSE 队列事件串号 | `task_stream_queues` 未清理 | `run()` 所有出口（完成/失败/超时/中断）调 `remove_stream_queue(task_id)` |
| 7 | team_rounds 缺 langsmith_run_url 列 | 老库未升级 | `store._init_multiagent_db` 已自动 `ALTER TABLE ADD COLUMN`，无需手动 |
| 8 | LangSmith UI 上 child span 显示为同级 | langsmith 0.10 下 `get_current_run_tree()` 返回 None | 不影响数据上报；保证 T4 `agent_llm_call` 链路正常即可（见 §6.4） |
| 9 | Coder 越权发 `respond_critique` 被 ActionGuard 拦截为 no_op | 角色权限白名单设计 | 这是预期行为；在 LangSmith 的 agent_llm_call output 中可追溯拒绝信息 |
| 10 | `DeveloperAgent` 收不到消息 | Planner 产出的 to_agent 用了别名 | MessageBus 别名归一化自动 `DeveloperAgent→Coder`，日志可见 `alias: ...` |
| 11 | `.env` 里有 `ENABLE_CURATOR` / `ENABLE_NUDGE` / `ENABLE_EVOLUTION` 但代码找不到 | 这些是早期已移除模块的**陈旧 leftover 配置**，源码已删（仅 `__pycache__/*.pyc` 字节码残骸），`config.py` 也不读这些字段 | 维护时**不要**据此以为这些功能存在而去实现缺口；如需恢复参考 `docs/deepagents_hermes_curator_evolution_trae_task_plan.md` 设计方案从零写起 |

## 10. 标准修复清单

### 服务启动失败
1. 检查 `PYTHONUTF8=1`
2. 检查 `runtime/db/app.sqlite3` 是否被占用
3. 查看日志输出

### 前端样式不生效
1. 浏览器 `Ctrl+Shift+R`
2. 确认 `app/web/style.css` 内容正确
3. 确认 `index.html` 中 `<link rel="stylesheet" href="style.css">`

### 审批卡顿（单 Agent）
1. 确认 `routes_tasks.py::approve_task` 是 async
2. 确认前端 `isApproving` 标志已设置
3. 确认按钮有 loading 态

### 消息不显示
1. 确认 `runner.py` 调 `task_service.add_message()`
2. 确认 `service.py` 有 `add_message` 方法
3. 确认 `routes_tasks.py` 有 `/tasks/{id}/messages`
4. 确认前端 `messageTimer` / `streamSource` 运行中

### 任务状态长期 running
1. 看 events 最后一条是否 `interrupt_detected`（卡在审批）
2. 调 `POST /tasks/{id}/approve`
3. 检查 `runner._approve_sync` 是否抛异常

### LangSmith trace 未上行
1. 检查 `LANGSMITH_ENABLED=true` + `LANGSMITH_API_KEY` 已配
2. 看 logs 是否有 `[observability] langsmith_enabled=True 但未配置 API_KEY` 降级提示
3. 用 `python -m app.cli team run ... --max-rounds 2` 跑一次后查 `team_rounds.langsmith_run_url` 是否落库

## 11. 关键依赖版本

```
python >= 3.11
deepagents == 0.6.8
langchain == 1.3.4
langchain-openai == 1.2.2
langchain-deepseek == 1.1.0
langgraph == 1.2.4
fastapi == 0.136.3
uvicorn == 0.48.0
pydantic == 2.13.4
pydantic-settings == 2.14.1
aiosqlite == 0.22.1
typer == 0.26.7
rich == 15.0.0
langsmith >= 0.10.0     # 可选依赖：pip install -e ".[observability]"
mcp >= 1.27.0           # MCP 工具集成依赖（按需）
```

## 12. 测试

```powershell
$env:PYTHONUTF8='1'
python -m pytest tests/                          # 全量 ~140 个
python -m pytest tests/test_observability.py -v   # 可观测性 ~22 个
python -m pytest tests/test_multiagent_*.py       # 多 Agent 模块约 100 个
```

测试矩阵（17 个文件）：

| 模块 | 文件 | 用例数 |
|---|---|---|
| smoke / config / permissions | test_smoke / test_config / test_permissions | ~10 |
| 可观测性 | test_observability（3 个 class） | 18 |
| TeamRunner + complex task 6 场景 | test_multiagent_team_runner / test_multiagent_complex_task | ~16 |
| SharedTeamState | test_multiagent_state | 14 |
| MessageBus | test_multiagent_message_bus | 8 |
| Termination | test_multiagent_termination | 10 |
| ReviewRepair | test_multiagent_review_repair | 7 |
| SpeakerSelector | test_multiagent_speaker_selector | 7 |
| Inbox | test_multiagent_inbox | 3 |
| HITL API | test_multiagent_hitl_api | 4 |
| LayeredMemory | test_multiagent_layered_memory | 9 |
| ConflictResolver | test_multiagent_conflict_resolver | 11 |
| ActionGuard | test_multiagent_action_guard | 14 |
| TeamGraph | test_multiagent_team_graph | 5 |

## 13. 给 AI 助手的特别提醒

1. **不要把 service 类重写成纯函数**：保持 `TaskService` 类和 `get_task_service()` 单例
2. **不要删除 task_messages 相关代码**：前端展示过程消息的核心
3. **不要改回同步审批**：async + run_in_executor 是经过验证的正确方案
4. **不要清除静态文件目录**：用户可能在 `runtime/workspace` 下有重要产物
5. **修改前端前先读现有代码**：碎片化修改容易引入 bug
6. **保持跨平台兼容**：新增的沙箱、MCP、流式、可观测性逻辑需同时考虑 Windows 路径和编码
7. **配置开关优先**：所有新功能默认关闭或保留原有行为，通过 `settings.enable_xxx` 控制
8. **不要删除 `langgraph.json`**：异步子智能体 graph 注册入口
9. **保留 `langsmith_run_url` 列 + `ALTER TABLE` 兼容逻辑**：老库升级依赖它
10. **修改 ActionGuard 白名单时同步审视 `default_teams.py`**：兜底白名单是这套
11. **修改 trace_span 时不要破坏 T4 `agent_llm_call` 主链**：这是 LangSmith 上唯一稳定的两层链路（见 §6.4）
12. **不要为已废弃的 curator/evolution/nudge 添加代码**：当前配置里没有这些开关

## 14. CLI / API 速查

```powershell
# CLI 单 Agent
python -m app.cli run "帮我生成一个不用框架的纯前端的项目"
python -m app.cli task-show <task_id>

# CLI 团队
python -m app.cli team run "请用 Python 实现 X" --max-rounds 6 --review
python -m app.cli team list

# API 冒烟
python -c "import requests; print(requests.get('http://127.0.0.1:8081/health').text)"
python -c "import requests; print(requests.get('http://127.0.0.1:8081/tasks').status_code)"
python -c "import requests; print(requests.post('http://127.0.0.1:8081/chat', json={'message':'test'}).status_code)"

# 团队 API + LangSmith URL 验证
python -c "import requests, json; r=requests.get('http://127.0.0.1:8081/team-tasks/<id>/rounds'); [print(x['langsmith_run_url']) for x in r.json()]"
```
