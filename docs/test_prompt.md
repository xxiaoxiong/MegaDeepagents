"""DeepAgents 升级项目全面测试提示词

请作为 QA 工程师，对 GeneralAgentFrame 进行系统测试。按优先级逐项验证，
遇错记录并继续，最终输出测试报告（通过/失败/阻塞项）。

## 环境准备
- Windows + Python 3.11
- 启动服务：$env:PYTHONUTF8='1'; python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
- 确保端口未被占用，SQLite 文件可读写

## P0 项（必须通过）

### 1. 异步子智能体（P0-1）
- 默认行为验证：直接调用 build_agent() 和 build_subagents()，确认返回 3 个同步 dict 子智能体
- 开启开关验证：设置 enable_async_subagents=true + async_subagent_url=http://127.0.0.1:2024，确认 build_subagents() 返回 AsyncSubAgent（含 graph_id）
- 注意：当前无真实远程 agent 服务，需捕获 "连接失败" 降级逻辑，不崩

### 2. 事件流式输出（P0-2）
- 关闭 streaming 时，POST /chat 创建任务后轮询 /tasks/{id}/messages 仍能收到中间消息
- 开启 streaming（enable_streaming=true），重新发任务，前端/app.js 应优先连接 EventSource /tasks/{id}/stream
- SSE 断连或 2s 内无事件时，前端自动降级到轮询
- 验证 SSE 事件里 tool/assistant 消息带 agent 字段（coordinator 或子智能体名）

### 3. MCP 工具集成（P0-3）
- enable_mcp_tools=false 时，工具集数量不应含 "mcp"
- 创建 .mcp.json（stdio transport，可用 echo/cat 作为 server），开启 enable_mcp_tools=true 后重启服务
- POST /chat 提交任务，观察 agent 是否成功调用 MCP 工具
- 关闭开关后恢复原有工具集

## P1 项（核心能力）

### 4. Harness Profiles（P1-1）
- 创建 runtime/profiles/openai-compatible.yaml，写入 system_prompt_suffix / excluded_tools
- 确认 build_agent() 在启动时自动加载 profile 并注入上下文

### 5. 沙箱隔离（P1-2）
- sandbox_provider=none：默认，shell_execute 正常
- sandbox_provider=local：backend routes 含 /sandbox，execute 工具可调用
- sandbox_provider=daytona/modal：输出警告并回退 none

### 6. 自定义状态 Schema（P1-3）
- build_agent() 成功创建带 TaskAgentState 的 agent
- state 含 task_id / planner_status / subagent_tasks 字段
- 与 SqliteSaver checkpointer 兼容，任务可跨进程恢复

## P2 项（增强体验）

### 7. 运行时上下文（P2-1）
- TaskRunner.run() 向 config 传入 AgentContext(user_id=thread_id, request_id=task_id)
- 自定义工具内通过 ToolRuntime[AgentContext] 可读取 context

### 8. 结构化输出（P2-2）
- researcher 子智能体返回 ResearchFindings JSON（summary/key_findings/sources/confidence）
- reviewer 子智能体返回 CodeReviewResult JSON（verdict/issues/suggestions/risk_level）
- enable_response_format 开启时，主智能体结果解析为 TaskResult

## P3 项（生产级）

### 9. Prompt Caching（P3-1）
- enable_prompt_caching=true 时，create_deep_agent 中间件含 caching
- false 时移除对应 middleware

## 验收标准
1. 不破坏现有 CLI（python -m app.cli task-show <id>）和 API 基础功能
2. 关闭所有新开关时，项目行为与升级前一致
3. 前端 /chat 提交任务，基础流程正常（任务创建→执行→完成/审批）
4. 所有新增模块可被正常 import，无语法/依赖错误
5. SSE 流式在开启时正常推送事件，关闭时自动降级到轮询

## 输出要求
- 按 "测试项 / 通过 / 失败（原因）/ 阻塞" 表格形式输出
- 附关键错误日志片段
- 给出总体通过率与阻塞项建议修复顺序
