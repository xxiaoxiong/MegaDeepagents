# 代码库地图

这份地图定义新增代码应该放在哪里，避免继续把运行时逻辑堆进单个扁平目录。

## 顶层目录

```text
MegaDeepagents/
├─ app/
│  ├─ api/v1/                    # HTTP 契约、校验、SSE
│  ├─ application/runs/          # Run 用例：创建、控制、恢复、诊断、只读执行投影
│  ├─ backends/                  # 虚拟文件系统后端（沙箱读写、路径归一化）
│  ├─ core/                      # 配置、日志、LLM 工厂、Agent 组装
│  ├─ domain/runs/               # 与框架无关的 Run 领域模型
│  ├─ infrastructure/database/   # SQLite 连接与领域存储
│  ├─ memory/                    # 冷记忆（会话/消息）、FTS5 全文检索、PII 脱敏
│  ├─ multiagent/                # Agent、Task、Artifact 领域执行组件
│  ├─ runtime/root_graph/        # 唯一生产编排图
│  ├─ runtime/supervisor/        # 结构化 Supervisor 决策
│  ├─ skills/                    # Skill 元数据与加载器
│  ├─ task/                      # 旧版单 Agent 任务运行器（兼容层）
│  ├─ tools/                     # Worker 可调用工具（MCP、文件、搜索）
│  ├─ permissions.py             # 文件系统权限中间件（.env 拦截等）
│  └─ main.py / cli.py           # 入口
├─ frontend/                     # 自托管 Vue 运行控制台
├─ website/                      # 独立静态项目官网
├─ tests/                        # 默认离线回归
├─ docs/                         # 架构、API、运维和迁移
└─ runtime/                      # 本地数据；日志、DB、缓存不进 Git
```

依赖方向：

```text
api → application → runtime/root_graph
                    ↓
domain ← multiagent ← infrastructure
```

`domain` 不依赖 FastAPI、Vue 或具体数据库。`application` 只编排用例，不新增调度器。
`runtime/root_graph` 是唯一生产控制流。

`application/runs/execution_intelligence.py` 是可观测性读取模型：它只组合现有存储并重放
事件，不认领 Task、不派发 Agent，也不持久化派生指标。

## `app/multiagent` 模块归属

现有 Python 导入路径仍由兼容契约使用，因此本轮不做破坏性的批量移动；新增实现必须按下表
选择现有边界，禁止再创建含义重叠的模块。

| 责任域 | 权威模块 |
|---|---|
| 计划与子问题 | `task_graph.py`、`planner.py`、`transactional_task_service.py` |
| 执行状态与并发 | `task_board.py`、`parallel_scheduler.py`、`agent_runtime_manager.py` |
| Agent 身份与生命周期 | `agent_instance.py`、`agent_registry.py`、`teammate_session.py` |
| Worker 执行 | `executor.py`、`tool_runtime.py`、`shell_policy.py` |
| 产物与验证 | `artifact.py`、`verifier.py` |
| 控制与治理 | `control_plane.py`、`permission.py`、`plan_approval.py` |
| 恢复与持久化 | `resume_coordinator.py`、`store.py`、`run_workspace.py` |
| 通信 | `mailbox.py`、`bus.py`、`inbox.py` |
| 团队构建 | `team_builder.py`、`default_teams.py`、`agent_profile.py` |
| 生命周期审计 | `lifecycle_hooks.py`；事件事实最终写入 Event Envelope |
| Git 隔离 | `git_workspace.py` |

## 冻结兼容层

以下模块只用于读取或控制历史 DISCUSSION/轮次聊天数据：

- `team_runner.py`
- `round_executor.py`
- `speaker_selector.py`
- `termination.py`
- `room.py`
- `review_repair.py`
- `runtime_adapter.py`
- `team_graph.py`

不得从 Root Graph、V1 API 或新 UI 引用它们。迁移窗口结束时应以一次带数据库迁移说明的
独立变更删除，而不是在功能改造中混合搬迁。

## 文件准入规则

新增文件前先回答：

1. 是否能扩展上表中的权威模块？
2. 是否会引入第二个状态枚举、调度器、事件协议或数据库？
3. 是否属于读取投影？读取投影放 `application/`，不写领域状态。
4. 是否属于外部适配器？适配器放 `infrastructure/` 或 `tools/`。
5. 是否是运行数据？运行数据放 `runtime/` 并加入 `.gitignore`。

无法明确归属的代码不应合入。
