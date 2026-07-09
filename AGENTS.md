# 通用智能体（General Agent）指南

本文档面向 AI 编程助手，用于持续维护本项目。覆盖项目结构、运行方式、数据流、常见陷阱和标准修复流程。

## 1. 项目定位

本项目是 智能体 + LangGraph + Hermes 风格的自主任务型智能体底座，最终交付形态为：

- `CLI + FastAPI + Web 任务台`
- 具备：记忆系统、Skill 系统、HITL 审批、SQLite 持久化、文件后端
- 升级后新增：异步子智能体、SSE 事件流、MCP 工具集成、Harness Profiles、沙箱隔离、自定义 State Schema、运行时上下文、结构化输出、背景记忆整合、Prompt Caching、Policy Hooks

## 2. 目录结构（关键只读）

```
app/
  agents/
    __init__.py          # 子智能体 graph 工厂（researcher/coder/reviewer）
  api/
    routes_streaming.py  # SSE 流式端点 /tasks/{id}/stream
    routes_chat.py       # /chat 提交任务
    routes_tasks.py      # 审批接口，使用线程池避免阻塞
    ...
  backends/
    __init__.py          # CompositeBackend + _SafeFilesystemBackend + LocalShellBackend
  core/
    config.py            # 配置管理，自动创建目录
    logging.py           # 日志初始化，Windows 下需要 UTF-8
    agent_factory.py     # build_agent() 组装智能体
    profiles.py          # Harness/Provider Profile 加载
    state_schema.py      # TaskAgentState(DeepAgentState)
    context.py           # AgentContext dataclass
    response_formats.py  # 结构化输出 Pydantic 模型
    runtime.py / schemas.py
  task/
    models.py            # Task / TaskEvent / TaskMessage
    store.py             # SQLite 存储（任务/事件/消息/产物）
    service.py           # 业务门面，务必保留 get_task_service()
    runner.py            # Agent 执行封装，写中间消息、处理 HITL + SSE 队列
  tools/
    mcp_loader.py        # MCP .mcp.json 发现 + 工具转换
    registry.py          # ToolRegistry（file/memory/skills/task/web/shell_safe/mcp）
    ...
  web/
    index.html           # 前端结构
    app.js               # 前端轮询 + SSE 降级 + 消息流渲染
    style.css            # 深色主题样式
langgraph.json           # 本地 graph 注册（researcher/coder/reviewer）
runtime/
  workspace/             # Agent 可写的虚拟文件系统根目录
```

## 3. 快速启动

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

浏览器访问：http://127.0.0.1:8081

注意：
- Windows 下必须设置 `PYTHONUTF8=1` 或 `PYTHONUTF8=1`，否则 `gbk` 编码会报错。
- 静态文件由 `StaticFiles(directory=app/web)` 托管。
- `app.mount("/", ...)` 会拦截所有未匹配路由，API 路由必须在 `mount` 之前注册。
- 新增强：`langgraph.json` 用于本地 graph 注册，配合 `langgraph dev` 可启动远程 subagent 服务。

## 4. 核心数据流

```
用户输入
  │
  ▼
routes_chat.py  /chat
  │
  ▼
TaskRunner.run()
  │
  ├─ TaskService.create_task()        -> tasks 表 + task_created 事件
  ├─ TaskService.add_message(user)     -> task_messages 表
  ├─ build_agent()                     -> 智能体（含 SqliteSaver 检查点 + TaskAgentState + AgentContext）
  │
  ├─ agent.invoke() / agent.stream()
  │     │
  │     ├─ 中间消息（assistant / tool） -> task_messages 表
  │     ├─ 触发 HITL -> __interrupt__   -> interrupt_detected 事件
  │     │                                 状态变为 waiting_approval
  │     │
  │     ▼
  │   [人工审批] /tasks/{id}/approve
  │     │
  │     ▼
  │   Command(resume=decisions)
  │     │
  │     ▼
  │   task_completed 事件
  │   final_answer 写入 tasks 表
  │
  ▼
轮询 /tasks/{id} 看状态
轮询 /tasks/{id}/messages 看过程消息
SSE  /tasks/{id}/stream 实时推送（开启 enable_streaming 时）
```

## 5. 常见开发任务与正确操作

### 5.1 修改任务服务接口

`TaskService` 是全局单例，必须保留 `get_task_service()`。

```python
# 正确：通过 get_task_service() 获取实例
task_service = get_task_service()

# 新增方法记得同时修改 TaskService 类和 get_task_service 单例
```

### 5.2 写中间消息到前端可见

修改 `app/task/runner.py` 的 `run()` 方法：

```python
# 正确方式：直接调用 task_service
self.task_service.add_message(self.task_id, "tool", content, {"name": name})
self.task_service.add_message(self.task_id, "assistant", content, {})

# 前端通过 /tasks/{id}/messages 轮询看到这些消息
# 开启 streaming 时，runner 内部还会把消息 push 到 asyncio.Queue，SSE 实时推送
```

### 5.3 修改审批接口

审批接口已经改成**异步非阻塞**模式：

```python
@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str):
    # 1. 立即切回 running，前端不再卡顿
    task_service.update_status(task_id, TaskStatus.RUNNING)
    
    # 2. 把耗时的恢复逻辑扔到线程池
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, _approve_sync, task_id)
    
    # 3. 前端会通过轮询 /tasks/{id} 看到状态变化
    res = await asyncio.shield(future)
    return res
```

前端需要同时：
- 点击按钮后设置 `isApproving = true` 防止重复点击
- 按钮显示 "⏳ 处理中..."
- 不停止轮询，继续等待状态变化

### 5.4 前端消息渲染

前端 `app.js` 有三个独立轮询 + 一个 SSE 通道：

| 通道 | 频率/行为 | 作用 |
|---|---|---|
| `/tasks/{id}` | 1.5s | 看状态 + 审批条 |
| `/tasks/{id}/events` | 1.5s | 看系统事件流 |
| `/tasks/{id}/messages` | 1s | 看 user/assistant/tool 过程消息 |
| `/tasks/{id}/stream` | SSE | 开启 streaming 时的实时消息 + 事件 |

新建消息轮询时必须清空 `knownMessageIds`，否则旧消息不会重新渲染。

### 5.5 子智能体与 Graph 注册

- 同步子智能体定义在 `app/core/agent_factory.py` 的 `build_subagents()`，返回 list[dict]。
- 异步子智能体使用 `AsyncSubAgent(name, description, graph_id, url)`，`graph_id` 对应 `langgraph.json` 中注册的 graph 名称。
- 本地 graph 工厂定义在 `app/agents/__init__.py`，通过 `create_agent()` 构建轻量 LangChain agent。
- `langgraph.json` 放在项目根目录，格式参考官方文档。

### 5.6 沙箱与后端

- 文件后端由 `app/backends/__init__.py` 中的 `_SafeFilesystemBackend` 提供，保留 Windows `\\?\` 路径处理。
- 沙箱通过 DeepAgents 官方 `LocalShellBackend` 实现，`build_backend()` 在 `sandbox_provider=local` 时注入 `/sandbox` 路由。

## 6. 典型陷阱与已经踩过的坑

### 陷阱 1：修改 service.py 时丢掉了单例函数

- 现象：`ImportError: cannot import name 'get_task_service'`
- 原因：重写时只留了类，忘了保留模块级的 `_task_service` 和 `get_task_service()`
- 正确做法：任何对 `service.py` 的重构都要检查 `get_task_service()` 是否还在

### 陷阱 2：中间消息只写了变量，没有进数据库

- 现象：Web 端只看到最终答案，看不到 Agent 执行过程
- 原因：`result.value.messages` 里的中间消息没有落库
- 正确做法：在 `runner.py` 的 `run()` 里遍历 messages 并调用 `self.task_service.add_message()`

### 陷阱 3：审批接口同步阻塞

- 现象：点击批准后浏览器转圈圈，多点几次就重复提交
- 原因：`approve_task` 是同步的，HTTP 连接被长时间占用
- 正确做法：改为 `async` + `run_in_executor`，先返回 200 再后台执行

### 陷阱 4：CSS 变量在浏览器缓存失效

- 现象：改了 style.css 但界面没变化
- 正确做法：`Ctrl+Shift+R` 强制刷新，或在 CSS 文件头加版本注释

### 陷阱 5：Windows 编码错误

- 现象：`gbk` codec can't decode / encode
- 正确做法：启动命令前加 `$env:PYTHONUTF8='1'`，或在 `main.py` 中设置环境变量

### 陷阱 6：流式事件队列泄漏

- 现象：任务完成后 SSE 队列未清理，导致后续任务事件串号
- 原因：`task_stream_queues` 未在任务结束/失败/中断时移除
- 正确做法：在 `run()` 的所有出口（完成/失败/超时/中断）调用 `remove_stream_queue(task_id)`

## 7. 数据库 Schema（SQLite）

```sql
tasks           # 任务主表
task_events     # 事件流（task_created, interrupt_detected, ...）
task_messages   # 过程消息（user, assistant, tool）
artifacts       # 产物记录
```

`store.py` 中 `TaskStore._row_to_task()` 会自动加载关联的 events/messages/artifacts。

## 8. 关键依赖版本

```
python >= 3.11
deepagents >= 0.6.0
langchain >= 1.0.0
langgraph >= 1.0.0
fastapi >= 0.111.0
mcp >= 1.27.0        # MCP 工具集成依赖
```

## 9. 标准修复清单

当遇到以下情况时，按清单处理：

### 服务启动失败
1. 检查 `PYTHONUTF8=1` 是否设置
2. 检查数据库文件是否被占用
3. 查看日志输出

### 前端样式不生效
1. 浏览器 `Ctrl+Shift+R`
2. 确认 `app/web/style.css` 内容正确
3. 确认 `index.html` 中 `<link rel="stylesheet" href="style.css">`

### 审批卡顿
1. 确认 routes_tasks.py 的 `approve_task` 是 async
2. 确认前端 `isApproving` 标志已设置
3. 确认按钮有 loading 态

### 消息不显示
1. 确认 runner.py 调用 `self.task_service.add_message()`
2. 确认 service.py 有 `add_message` 方法
3. 确认 routes_tasks.py 有 `/tasks/{id}/messages` 接口
4. 确认前端 `messageTimer` / `streamSource` 运行中

### 任务状态长期 running
1. 检查 events 最后一条是否是 `interrupt_detected`（说明卡在审批）
2. 调用 `POST /tasks/{id}/approve` 恢复
3. 检查 runner.py 的 `_approve_sync` 是否抛出异常

## 10. 给 AI 助手的特别提醒

1. **不要把 service 类重写成纯函数**，要保持 `TaskService` 类和 `get_task_service()` 单例
2. **不要删除 task_messages 相关代码**，这是前端展示过程消息的核心
3. **不要改回同步审批**，异步 + 线程池是经过验证的正确方案
4. **不要清除静态文件目录**，用户可能在 runtime/workspace 下有重要产物
5. **修改前端前先读现有代码**，当前的轮询和消息渲染已经比较完整，碎片化修改容易引入 bug
6. **保持跨平台兼容**：新增的沙箱、MCP、流式逻辑需同时考虑 Windows 路径和编码问题
7. **配置开关优先**：所有新功能默认关闭或保留原有行为，通过 `settings.enable_xxx` 控制
8. **不要删除 `langgraph.json`**：它是异步子智能体 graph 注册的入口，缺省会破坏 AsyncSubAgent 的 graph 发现

## 11. 测试命令速查

```powershell
# CLI 单任务
python -m app.cli run "帮我生成一个不用框架的纯前端的项目"

# CLI 查看任务
python -m app.cli task-show <task_id>

# API 测试
python -c "import requests; print(requests.get('http://127.0.0.1:8081/health').text)"
python -c "import requests; print(requests.get('http://127.0.0.1:8081/tasks').status_code)"
python -c "import requests; print(requests.post('http://127.0.0.1:8081/chat', json={'message': 'test'}).status_code)"

# SSE 流式测试
python -c "import requests; r=requests.get('http://127.0.0.1:8081/tasks/<id>/stream'); print(r.status_code)"
```
