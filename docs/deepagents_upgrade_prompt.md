# DeepAgents 进阶升级提示词

> 用途：交给 AI 编辑器（如 Cursor、Copilot、Windsurf 等）进行下一步实现  
> 背景文档：`docs/deepagents_framework_evaluation.md`

---

## 【角色定义】

你是一位精通 **DeepAgents (>=0.6.0) + LangGraph + LangChain** 的 Python 后端工程师。  
当前项目是 **GeneralAgentFrame** —— 一个基于 DeepAgents 构建的自主任务型智能体框架，已具备子智能体调度、文件后端、权限控制、HITL 审批和 SQLite 持久化能力。

你的任务是在**不破坏现有功能**的前提下，逐项补齐 DeepAgents 中高级能力，提升项目的异步并发能力、流式用户体验和生态扩展性。

---

## 【核心约束】

1. **不动现有架构**：`TaskRunner`、`TaskService`、`ToolRegistry`、`_SafeFilesystemBackend` 等核心模块保持稳定，只做增量扩展。
2. **遵循官方 API**：所有 DeepAgents 新能力必须参考官方文档 `https://docs.langchain.com/oss/python/deepagents`，使用官方推荐的参数名和模式。
3. **保持 Windows 兼容**：现有 `_SafeFilesystemBackend` 对 Windows `\\?\` 路径的处理必须保留。
4. **保留 Skill 治理体系**：项目自定义的 Skill curator/snapshot/archive 机制是特色，不要用 deepagents 原生 SkillsMiddleware 替代。
5. **保留记忆治理体系**：项目自定义的 HotMemory/ColdMemory + review queue 提议机制保留，不要改为 agent 直接 edit_file。
6. **向后兼容**：新增功能必须通过配置开关（`settings.enable_xxx`）控制，默认关闭或保留原有行为。

---

## 【升级任务清单（按优先级）】

### P0-1：异步子智能体（Async SubAgents）

**目标**：将当前同步阻塞的子智能体升级为异步非阻塞模式。

**要求**：
1. 在 `app/core/agent_factory.py` 中，将 `build_subagents()` 返回的 list[dict] 升级为支持 `AsyncSubAgent` 规格。
2. 定义 3 个异步子智能体：`researcher`、`coder`、`reviewer`，使用 `AsyncSubAgent(name=..., description=..., graph_id=...)`。
3. 在 `langgraph.json`（如不存在则新建）中注册对应 graph：`researcher`、`coder`、`reviewer`。
4. 每个子智能体 graph 使用 `create_agent`（LangChain 轻量 agent）或自定义 LangGraph graph，必须包含 `messages` state key。
5. `create_deep_agent` 调用处增加 `async_subagents=async_subagents` 参数。
6. 保持原有同步子智能体作为 fallback：当 `enable_async_subagents=False` 时回退到 dict-based 同步模式。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/async-subagents
- https://docs.langchain.com/oss/python/deepagents/code/subagents

---

### P0-2：事件流式输出（Event Streaming）

**目标**：将前端轮询改为 SSE/WebSocket 流式推送，支持实时消息和子智能体追踪。

**要求**：
1. 在 `app/api/` 下新增 `routes_streaming.py`，提供 `/tasks/{task_id}/stream` SSE 端点。
2. 使用 `agent.astream_events(input, version="v3")` 替代当前的 `agent.stream()` + 轮询。
3. 通过 `stream.subagents` 追踪子智能体生命周期，利用 `lc_agent_name` metadata 区分 coordinator/subagent 消息。
4. 前端 `app.js` 新增 `EventSource` 连接，替换 `/tasks/{id}/messages` 的 1s 轮询。
5. 保留原有轮询作为 fallback：当 `enable_streaming=False` 时继续用轮询。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/event-streaming

---

### P0-3：MCP 工具集成（Model Context Protocol）

**目标**：集成 MCP 生态，支持通过配置文件动态加载外部工具。

**要求**：
1. 新增 `app/tools/mcp_loader.py`，负责加载 `.mcp.json` 配置并初始化 MCP 工具。
2. 支持 discovery locations：项目根目录 `.mcp.json`、用户目录 `~/.deepagents/.mcp.json`。
3. 支持 stdio/SSE/HTTP 三种 transport。
4. 在 `ToolRegistry.register_all()` 中增加 `mcp` toolset，与现有 `file/memory/skills/task/web/shell_safe` 并列。
5. 提供工具过滤（`allowedTools` / `disabledTools`）支持。
6. 保留现有自研工具（web_search、shell_execute）作为默认 fallback。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/code/mcp-tools

---

### P1-1：Harness Profiles（模型适配配置）

**目标**：将模型特定的 prompt/tool/middleware 配置从代码中剥离，支持外部 YAML/JSON 配置。

**要求**：
1. 新增 `app/core/profiles.py`，负责注册 HarnessProfile 和 ProviderProfile。
2. 支持从 `profiles/` 目录加载 YAML 配置文件（如 `openai.yaml`、`deepseek.yaml`、`anthropic.yaml`）。
3. 典型配置项：
   - `system_prompt_suffix`：模型特定的 prompt 后缀
   - `excluded_tools`：该模型不需要的工具
   - `tool_description_overrides`：工具描述覆盖
   - `general_purpose_subagent.enabled`：是否启用通用子智能体
4. 在 `build_agent()` 调用 `create_deep_agent` 前完成 profile 注册。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/profiles

---

### P1-2：沙箱隔离（Sandbox Backends）

**目标**：提供可选的沙箱执行环境，替代或补充当前白名单 shell。

**要求**：
1. 新增 `app/backends/sandbox.py`，封装 `LocalShellBackend`（开发用）和可选远程 sandbox（如 Daytona/Modal）。
2. 在 `build_backend()` 中增加条件分支：当 `enable_sandbox=True` 且配置了 sandbox provider 时，使用 sandbox backend。
3. sandbox 模式下自动获得 `execute` 工具，可替换自定义 `shell_execute`。
4. 保留 `shell_execute` 作为 non-sandbox 模式 fallback。
5. 配置项：`sandbox_provider`（none/local/daytona/modal）、`sandbox_root_dir`、`sandbox_timeout`。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/sandboxes
- https://docs.langchain.com/oss/python/deepagents/backends#localshellbackend-local-shell

---

### P1-3：自定义状态 Schema（DeepAgentState）

**目标**：将任务元数据纳入 LangGraph state，享受自动 checkpoint 持久化。

**要求**：
1. 定义 `TaskAgentState(DeepAgentState)`，增加字段：
   - `task_id: str`
   - `planner_status: str`（planned/in_progress/completed）
   - `subagent_tasks: list[dict]`（子智能体任务追踪）
2. 在 `build_agent()` 中通过 `state_schema=TaskAgentState` 传入。
3. 在 `TaskRunner` 中，将 `task_id`、`thread_id` 等元数据从实例变量迁移到 agent state（通过 `runtime.state` 访问）。
4. 注意：保持与现有 `SqliteSaver` checkpointer 的兼容性。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/context-engineering#custom-state-schema

---

### P2-1：运行时上下文（Runtime Context）

**目标**：支持 per-run 的 user_id、feature_flags 等上下文传递。

**要求**：
1. 定义 `AgentContext` dataclass，包含 `user_id: str`、`feature_flags: dict`、`request_id: str`。
2. 在 `create_deep_agent` 中增加 `context_schema=AgentContext`。
3. 在 `TaskRunner.run()` 中，通过 `context=AgentContext(user_id=..., ...)` 传入。
4. 在自定义工具中，通过 `ToolRuntime[AgentContext]` 读取上下文。
5. 为后续多租户隔离做准备。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/context-engineering#runtime-context

---

### P2-2：子智能体结构化输出（Structured Output）

**目标**：让子智能体返回可解析的 JSON，而非自由文本。

**要求**：
1. 定义 Pydantic 模型 `ResearchFindings`、`CodeReviewResult`、`TestReport`。
2. 在 `build_subagents()` 中，为每个子智能体增加 `response_format` 字段。
3. 父智能体收到结构化 JSON 后，可自动解析并调用后续工具（如写报告、执行修改）。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/subagents#structured-output

---

### P2-3：背景记忆整合（Background Consolidation）

**目标**：在对话间隙自动整理和提炼记忆。

**要求**：
1. 新增 `app/memory/consolidation.py`，实现 `ConsolidationAgent`。
2. 该 agent 读取近期对话历史，提取关键事实，更新 MEMORY.md。
3. 通过 APScheduler 或 LangGraph cron 触发（可配置间隔，如 6 小时）。
4. 与现有 review queue 机制协同：consolidation 生成提议，人工审批后生效。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/memory#background-consolidation

---

### P2-4：情景记忆 Thread Search

**目标**：利用 LangGraph checkpoint 实现真正的对话历史检索。

**要求**：
1. 在 `app/memory/thread_search.py` 中实现 `search_past_conversations()` 工具。
2. 使用 `langgraph_sdk.get_client().threads.search()` 搜索历史线程。
3. 按 `user_id` 或 `thread_id` 过滤，支持时间范围。
4. 将结果接入现有 `session_search` 工具，作为冷记忆的增强。

**参考文档**：
- https://docs.langchain.com/oss/python/deepagents/memory#episodic-memory

---

### P3-1：Prompt Caching 显式配置

**目标**：为所有主流 provider 配置显式缓存。

**要求**：
1. 针对 DeepSeek/OpenAI 配置 provider-specific caching middleware。
2. 在 `settings` 中增加 `enable_prompt_caching: bool` 开关。
3. 在 `build_middleware()` 中根据 provider 类型添加对应 caching middleware。

---

### P3-2：Policy Hooks（审计/限速）

**目标**：在文件后端层增加审计日志和访问控制。

**要求**：
1. 在 `_SafeFilesystemBackend` 或新建的 `AuditedFilesystemBackend` 中增加 policy hooks。
2. 记录所有读写操作的审计日志（user_id、path、operation、timestamp）。
3. 支持速率限制（如每分钟最大文件操作数）。
4. 审计日志写入 SQLite 或独立日志文件。

---

## 【代码规范】

1. **类型注解**：所有新增函数必须包含类型注解（Python 3.11+）。
2. **日志规范**：使用 `app.core.logging.logger`，关键操作必须有 `logger.info/debug/warning/error`。
3. **异常处理**：捕获具体异常类型，不要裸 `except Exception`；失败时必须有降级逻辑。
4. **配置驱动**：所有新功能开关通过 `settings.enable_xxx` 控制，并在 `config.py` 中补充默认值。
5. **测试要求**：每个新模块至少包含 3 个单元测试（正常路径、异常路径、边界条件）。
6. **文档字符串**：公 有函数/类必须有 docstring，说明用途、参数、返回值。

---

## 【验收标准】

完成所有 P0 任务后，必须满足：

1. **异步子智能体**：启动 research/code review 任务时，主对话不被阻塞；可以在子智能体运行中途收到用户输入。
2. **事件流式**：前端能实时看到子智能体的 "started/progress/completed" 状态，消息以打字机效果呈现。
3. **MCP 工具**：在 `.mcp.json` 中配置一个 MCP 服务器（如 filesystem 或 github）后，agent 能自动发现并使用其工具。
4. **不破坏现有功能**：原有同步子智能体、轮询、自研工具在配置开关关闭新功能时完全可用。

---

## 【参考资源】

- DeepAgents 官方文档：https://docs.langchain.com/oss/python/deepagents
- 项目 AGENTS.md：`AGENTS.md`
- 现有核心文件：
  - `app/core/agent_factory.py`（agent 构建入口）
  - `app/backends.py`（后端路由）
  - `app/tools/registry.py`（工具注册中心）
  - `app/task/runner.py`（任务执行封装）
  - `app/core/config.py`（配置管理）

---

## 【实现顺序建议】

```
第 1 步：P0-1 异步子智能体（重构 build_subagents + langgraph.json）
    ↓
第 2 步：P0-2 事件流式输出（新增 SSE 端点 + 前端改造）
    ↓
第 3 步：P0-3 MCP 工具集成（新增 mcp_loader + ToolRegistry 扩展）
    ↓
第 4 步：P1-1 Harness Profiles（提取模型适配配置）
    ↓
第 5 步：P1-2 沙箱隔离（可选 backend）
    ↓
第 6 步：P1-3 自定义状态 Schema（迁移元数据到 state）
    ↓
第 7 步：P2 系列（运行时上下文、结构化输出、记忆增强）
    ↓
第 8 步：P3 系列（Caching、Policy Hooks）
```

每完成一个任务，更新 `docs/deepagents_framework_evaluation.md` 中的实现状态（🔴→🟡→🟢）。

---

## 【最后提醒】

- 不要删除任何现有代码或配置文件。
- 新增功能必须**向后兼容**，默认行为不变。
- 遇到 API 不确定时，优先查看官方文档或源码（`site-packages/deepagents/`）。
- 每次提交前运行 `python -m app.cli task-show <id>` 验证基础任务流程正常。
