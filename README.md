# General Agent Frame

基于 DeepAgents 的通用 Agent Runtime，支持**单 Agent 任务执行**与**多 Agent 团队协作**两条路径，并集成 LangSmith 可观测性。

- 单 Agent 路径：DeepAgents `create_deep_agent` + 文件后端 + HITL 审批 + SQLite 持久化
- 多 Agent 路径：5 角色软件团队（Planner / Coder / Tester / ReviewerAgent / Finalizer）+ ActionGuard 权限护栏 + SpeakerSelector 路由 + ReviewRepairLoop 评审闭环 + ConflictResolver 冲突裁决
- 可观测性：LangSmith trace 嵌入 9 个埋点（T1–T7 已落地 + 待补 T8/T9），每轮 run URL 自动落库到 `team_rounds.langsmith_run_url`

## 快速启动

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

浏览器访问：http://127.0.0.1:8081

> Windows 下必须设置 `PYTHONUTF8=1`，否则 `gbk` 编码会报错。

## 配置

复制 `.env.example` 为 `.env`，配置模型与可观测性：

```env
# 主模型
LLM_PROVIDER=openai-compatible
LLM_MODEL=step37-flash
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=http://127.0.0.1:4000/v1

# LangSmith 可观测性（可选，默认关闭）
LANGSMITH_ENABLED=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=multiagent-frame
```

### 关键开关

| 开关                          | 默认值                              | 说明                                                     |
| ----------------------------- | ----------------------------------- | -------------------------------------------------------- |
| enable_safe_shell             | false                               | 自定义 shell 工具                                        |
| enable_web_tools              | false                               | 网页搜索/抓取（占位）                                    |
| enable_mcp_tools              | false                               | MCP 工具集成                                             |
| enable_streaming              | false                               | SSE 流式输出（路由暂未注册）                             |
| enable_subagents              | true                                | 单 Agent 路径的同步子智能体委派                          |
| enable_async_subagents        | false                               | 异步子智能体（需 langgraph.json 服务）                   |
| enable_response_format        | false                               | 结构化输出（TaskResult JSON 解析）                       |
| enable_llm_cache              | true                                | LLM 调用缓存                                             |
| hitl_required_for_write       | true                                | 文件写入需 HITL 审批                                     |
| hitl_required_for_skill_change| true                                | Skill 变更需 HITL                                        |
| hitl_required_for_memory_change| true                               | 记忆变更需 HITL                                          |
| sandbox_provider              | none                                | none / local / daytona / modal                           |
| langsmith_enabled             | false                               | LangSmith 可观测性总开关                                 |

完整字段见 `app/core/config.py` 的 `Settings` 类。

## CLI 使用

```bash
# ===== 单 Agent =====
python -m app.cli run "帮我生成一个纯前端的待办清单"
python -m app.cli run "..." --thread-id my-session --auto-approve

# 任务管理
python -m app.cli task-list
python -m app.cli task-show <task_id>

# ===== 多 Agent 团队 =====
python -m app.cli team run "请用 Python 实现一个温度转换工具"
python -m app.cli team run "..." --team software_dev_team --max-rounds 10 --review
python -m app.cli team list                 # 列出可用团队模板

# ===== Skill 管理 =====
python -m app.cli skills list
python -m app.cli skills show <name>
python -m app.cli skills scan

# ===== 记忆管理 =====
python -m app.cli memory show
python -m app.cli memory search "关键词"

# ===== 工具与配置 =====
python -m app.cli tools-list
python -m app.cli config-show
```

## 可观测性

集成 LangSmith，在单 Agent 与多 Agent 路径均有埋点。配置 `.env` 后自动开启，详见 [docs/observability.md](docs/observability.md)。

| 埋点 | 类型 | 位置 | 说明 |
|---|---|---|---|
| T1 `team_run` | `@traceable chain` | `app/multiagent/team_runner.py:174` | 团队任务顶层 run |
| T2 `team_round` | `trace_span chain` | `app/multiagent/team_runner.py:210` | 每轮 chain span |
| T3 `select_speaker` | `trace_span chain` | `app/multiagent/team_runner.py:216` | 发言选举 span |
| T4 `agent_llm_call` | `@traceable llm` | `app/multiagent/runtime_adapter.py:331` | 每个 Agent 的 LLM 调用 |
| T5 `process_actions` | `trace_span chain` | `app/multiagent/team_runner.py:306` | Action 处理 span |
| T6 `termination_check` | `trace_span chain` | `app/multiagent/team_runner.py:344` | 终止判定 span |
| T7 `review_repair` | `@traceable chain` | `app/multiagent/review_repair.py:56` | 评审-返工闭环 |
| T8 `single_agent_run` | `@traceable chain` | `app/task/runner.py:186` | 单 Agent 任务顶层 run |
| T9 `memorize_summary` | — | — | 计划中，尚未落地 |

启用后，每轮结束时 `TeamRunner.save_round()` 通过 `get_current_run_url()` 拿到当前 LangSmith run URL，落库到 `team_rounds.langsmith_run_url`，可通过 `GET /team-tasks/{task_id}/rounds` 返回的字段直跳 LangSmith UI 查看完整 trace。

### 离线模式

- `langsmith_enabled=false`：不打 LangSmith，但 `offline_log=true` 时本地 logger 仍打 `[trace] enter/exit` 摘要
- `enabled=true` 但未配置 `API_KEY`：自动降级为 `offline_log`，日志输出降级提示，不上报

## API 路由

### 基础

| 方法 | 路径                         | 说明             |
| ---- | ---------------------------- | ---------------- |
| GET  | /health                      | 健康检查         |
| POST | /chat                        | 提交单 Agent 任务 |
| GET  | /tasks                       | 任务列表         |
| GET  | /tasks/{id}                  | 任务详情         |
| GET  | /tasks/{id}/events           | 事件流           |
| GET  | /tasks/{id}/messages         | 过程消息         |
| POST | /tasks/{id}/approve          | 审批通过（async+线程池） |
| POST | /tasks/{id}/reject           | 审批拒绝         |
| GET  | /tasks/{id}/artifacts/{path} | 产物下载         |
| GET  | /tasks/{id}/preview/{path}   | 产物预览         |
| DELETE | /tasks/{id}                 | 删除任务         |
| GET  | /memory                      | 读取记忆         |
| POST | /memory/search               | 搜索记忆         |
| GET  | /skills                      | 列出 Skills      |
| GET  | /skills/{name}               | 读取 Skill       |
| POST | /skills                      | 创建 Skill       |
| POST | /skills/{name}/pin           | 固定 Skill       |

### 团队多 Agent

| 方法 | 路径                                     | 说明                            |
| ---- | ---------------------------------------- | ------------------------------- |
| POST | /team-tasks                              | 创建并启动团队任务（后台线程）  |
| GET  | /team-tasks/{id}                         | 查询团队任务状态                |
| GET  | /team-tasks/{id}/messages                | 团队消息流                      |
| GET  | /team-tasks/{id}/state                   | 共享团队状态                    |
| GET  | /team-tasks/{id}/agents                  | 团队 Agent 列表                 |
| POST | /team-tasks/{id}/messages                | 人工注入消息                    |
| POST | /team-tasks/{id}/cancel                  | 取消团队任务                    |
| GET  | /team-tasks/{id}/rounds                   | 每轮记录（含 `langsmith_run_url`）|
| GET  | /team-tasks/{id}/events                  | SSE 实时事件流                  |
| GET  | /team-tasks/{id}/hitl-conflicts          | 待裁决冲突清单                  |
| POST | /team-tasks/{id}/hitl-resolve/{issue_id} | 人工裁决冲突                    |

## 多 Agent 团队架构

### 软件开发团队（`software_dev_team`）

5 个角色，max_rounds=20，按 CONTROLLED_GROUP_CHAT 模式协作：

| 角色 | 职责 | Action 白名单 |
|---|---|---|
| **Planner** | 拆解任务、指派负责人 | `send_message / update_state / handoff / no_op` |
| **Coder** | 实现代码、提交评审 | `send_message / create_artifact / request_review / handoff / no_op` |
| **Tester** | 编写测试、产出测试产物 | `send_message / create_artifact / handoff / no_op` |
| **ReviewerAgent** | 审查产物、给出通过/修复决策 | `send_message / request_review / respond_critique / no_op` |
| **Finalizer** | 汇总产出、收尾 | `send_message / update_state / respond_critique / mark_done / no_op` |

> 越权 action 会被 `ActionGuard`（`app/multiagent/action_guard.py`）拦截并替换为带拒绝信息的 `no_op`，保留到运行日志中可在 LangSmith 追溯。

### TeamRunner.run() 轮次循环

```
init  →  发 user_request 到 MessageBus，emit task_started
  ↓
┌─→ select_speaker   (SpeakerSelector 8 级优先规则)         [T3]
│   ↓
│   agent_llm_call   (运行时构造 system prompt + 调 LLM)   [T4]
│   ↓
│   process_actions  (actions→messages→bus.publish→state)   [T5]
│     └─ 出 review_result → ReviewRepairLoop.process(...)   [T7]
│   ↓
│   termination_check (6 种终止策略)                        [T6]
│   ↓
└─ 未终止 → 下一轮
   终止 → emit task_terminated，落库 team_rounds + langsmith_run_url
```

### 核心 22 个模块（`app/multiagent/`）

| 模块 | 作用 |
|---|---|
| `team_runner` | 轮次循环主驱动 |
| `runtime_adapter` | 每个 Agent 的 LLM 调用入口、prompt 构造、JSON actions 解析 |
| `review_repair` | 评审-返工闭环（cycle 次数追踪） |
| `event_emitter` | 进程内 SSE 事件总线 + 旁路 emit_trace_event |
| `store` | SQLite 持久化（7 张表） |
| `action_guard` | 运行时角色权限白名单过滤 |
| `speaker_selector` | 8 级规则选下一发言 |
| `conflict_resolver` | 冲突裁决（规则引擎 + 升级 HITL） |
| `layered_memory` | 四层记忆原型（Working/Episodic/Semantic/Procedural） |
| `bus` | 消息路由总线（direct / broadcast / system） + 别名归一化 |
| `team_graph` | LangGraph 可恢复状态图骨架（实验性/未启用）：4 节点 + SqliteSaver checkpoint；与 TeamRunner.run() 共享 TeamRoundExecutor 单轮组件，**未接到 API**，恢复逻辑尚未经过生产化测试 |
| `room` | 多 Agent 任务环境（MessageBus + Inbox + SharedTeamState） |
| `inbox` | 私有收件箱 + 相关上下文排序 |
| `termination` | 6 种终止策略 |
| `state` | SharedTeamState（10 种 phase + 合法转换校验） |
| `messages` | 26 种 MessageType + 别名归一化 |
| `agent_spec` / `default_teams` / `prompts` / `policies` / `models` | 配置与导出 |

### SQLite 表

**单 Agent**（`app/task/store.py`，6 张）：`tasks` / `task_events` / `task_messages` / `artifacts` / `skills` / `skill_usage_events`

**多 Agent**（`app/multiagent/store.py`，7 张）：`team_rooms` / `team_agents` / `agent_messages` / `agent_inbox` / `team_decisions` / `team_issues` / `team_rounds`（含 `langsmith_run_url` 列）

### SSE 事件类型（实际 emit 的 6 种）

`task_started` / `speaker_selected` / `actions_emitted` / `message_published` / `termination` / `task_terminated`

> `event_emitter.py` docstring 还预留了 `round_started` / `agent_thought` / `state_updated` / `review_request` / `review_result` / `artifact_created` / `error`，当前未实际 emit。

## 目录结构

```text
app/
  agents/__init__.py     # 本地 graph 工厂（researcher/coder/reviewer）
  api/                    # REST 路由（routes_chat/tasks/team/health/memory/skills）
  backends/__init__.py    # CompositeBackend + _SafeFilesystemBackend + LocalShellBackend
  cli.py                  # Typer CLI（run / team / skills / memory / ...）
  core/
    config.py             # Settings（含 langsmith_* 8 字段）
    logging.py
    agent_factory.py      # build_agent()
    profiles.py           # Harness/Provider Profile
    state_schema.py       # TaskAgentState(DeepAgentState)
    context.py            # AgentContext
    response_formats.py   # 结构化输出 Pydantic
    observability.py      # traceable / trace_span / emit_trace_event / get_current_run_url
    runtime.py / schemas.py
  main.py                 # FastAPI 入口（路由无 prefix，全在 routes 文件里写）
  memory/                 # 热记忆、冷记忆、FTS 检索
  multiagent/             # 22 模块的多 Agent 团队框架
  permissions.py          # 权限规则
  skills/                 # Skill 加载器、元数据、管理器
  task/                   # TaskService / TaskRunner / Store / Models（单 Agent 路径）
  tools/                  # ToolRegistry + memory/skills/task/web/shell_safe/mcp
  web/                    # 前端页面（index.html / app.js / style.css）
runtime/
  workspace/              # Agent 产物根目录
  memory/                 # MEMORY.md / USER.md
  skills/                 # 静态 Skills 目录
  db/                     # app.sqlite3
  cache/                  # LLM 缓存
  logs/
langgraph.json            # 本地 graph 注册（researcher/coder/reviewer）
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
python -m pytest tests/                                # 全量 ~140 个
python -m pytest tests/test_observability.py -v        # 可观测性 ~22 个
python -m pytest tests/test_multiagent_*.py             # 多 Agent 模块约 100 个
```

覆盖：smoke / config / permissions / observability / team_runner / complex_task（6 大场景）/ state / message_bus / termination / review_repair / speaker_selector / inbox / hitl_api / layered_memory / conflict_resolver / action_guard / team_graph。

## 与业界框架对比

| 维度 | MultiAgentFrame | MetaGPT | CrewAI | AutoGen |
|---|---|---|---|---|
| 消息路由 | ✅ 结构化 subscription + 别名归一化 | ✅ Role.watch | ⚠️ 串行 | ✅ 结构化 |
| 私有 inbox | ✅ 强制隔离 | ✅ msg_buffer | ❌ 共享 | ⚠️ 部分 |
| action 护栏 | ✅ 双层白名单（ActionGuard） | ❌ | ❌ | ❌ |
| 路由黑洞检测 | ✅ 独创（unknown→broadcast） | ❌ | ❌ | ❌ |
| 评审闭环 | ✅ ReviewRepairLoop | ❌ | ❌ | ❌ |
| 冲突裁决 | ✅ 规则引擎 + HITL 升级 | ❌ | ❌ | ✅ LLM judger |
| SSE 事件 | ✅ 6 类实时 | ❌ | ❌ | ❌ |
| SQLite 持久 | ✅ 13 张表（6+7） | ❌ 仅对话 | ❌ | ⚠️ 部分 |
| LangSmith 可观测 | ✅ 9 埋点 + run URL 落库 | ❌ | ❌ | ❌ |
| 中文支持 | ✅ 全中文注释 | ✅ | ❌ | ❌ |

**核心独特优势**：ActionGuard 双层白名单 + 路由黑洞检测 + ReviewRepairLoop 评审闭环 + LangSmith 全链路 trace。

## 技术栈

- Python >= 3.11
- DeepAgents 0.6.8
- LangChain 1.3.4 / langchain-openai 1.2.2 / langchain-deepseek 1.1.0
- LangGraph 1.2.4
- FastAPI 0.136.3 + Uvicorn 0.48.0
- Pydantic 2.13.4 / pydantic-settings 2.14.1
- aiosqlite 0.22.1
- Typer 0.26.7 + Rich 15.0.0
- langsmith >= 0.10.0（可选依赖 `[observability]`）

## License

MIT
