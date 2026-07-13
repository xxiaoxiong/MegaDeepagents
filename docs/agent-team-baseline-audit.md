# MegaDeepagents Agent Teams Baseline Audit

> 日期：2026-07-13  
> 项目：MegaDeepagents  
> 用途：Agent Teams 架构改造基线记录

---

## 1. 测试基线

```text
pytest: 425 passed, 4 warnings, 0 failed, 0 skipped
耗时: 1290.17s (21min 30s)
警告: FastAPI on_event deprecation (不影响功能)
```

## 2. 关键架构发现

### 2.1 两套并行执行路径

| 维度 | CLI `team run` (Phase Two) | API `POST /team-tasks` | CLI `team run --legacy` |
|------|---------------------------|----------------------|------------------------|
| 编排器 | `SimpleOrchestrator` | `TeamRunner` + `TeamRoundExecutor` | `TeamRunner` |
| 返回类型 | `OrchestrationResult` | `TeamRunResult` | `TeamRunResult` |
| 持久化 | 无直接持久化 | `get_multiagent_store()` (SQLite) | 同 API |
| Workspace | 创建但不传入执行器 ❌ | 不创建 | 不创建 |
| Run ID | `"cli_" + uuid.hex[:12]` | `"task_" + uuid.hex[:8]` | 同 API |
| 默认团队 | 忽略 team 参数 | `software_dev_team` | `software_dev_team` |

### 2.2 断链清单

#### A. RunContext / Workspace 断链
- `RunWorkspace` 在 CLI 中创建（`cli.py:80`）但从未传入 `run_orchestrated()`（`cli.py:91-97`）
- `DeepAgentExecutor.__init__` 接受 `workspace_root` 但主链未传入
- `execute_task()` 回退到 `_default_workspace_root()` = `runtime/workspaces/default_run`
- `TeamRunner` 完全不接受或使用 `RunWorkspace`

#### B. ArtifactStore 断链
- `ArtifactStore` 是纯内存注册表（`artifact.py:176`），无 SQLite 持久化
- `DeepAgentExecutor.execute()` 生成伪 Artifact ID（`f"{assignment.task_id}:{f.name}"`，`executor.py:473`），不经过 `ArtifactStore`
- `SimpleOrchestrator._verify()` 构造的 artifacts dict 来自 `node.objective[:200]`（`orchestrator.py:265`），而非真实产物

#### C. Verifier 断链
- `SimpleOrchestrator._verify()` 的 artifacts 数据是 node.objective 截断（`orchestrator.py:265`）
- `LLMRubricVerifier._build_rubric_prompt()` 内容截断到 500 字符（`verifier.py:305`）
- `ProgrammaticVerifier` 的方法已完善但不被主链调用

#### D. Scheduler 伪成功
- `_InMemoryWorkerExecutor`（`scheduler.py:337-352`）总是返回 `success=True`，是生产默认 stub
- `SimpleOrchestrator._schedule()` 总是返回 `True`（`orchestrator.py:250`），忽略 scheduler 实际结果
- `_run_sync_fallback()` 总是返回 `status="completed"`（`scheduler.py:313`），即使调度失败

#### E. 单 Agent 伪成功
- `SimpleOrchestrator._run_single()` 在 LLM 异常时仍返回 `status="completed"`（`orchestrator.py:202`）

#### F. Agent 身份断链
- `DeepAgentExecutor` 每个 Task 创建新 Agent（`executor.py:443`），不保持 AgentInstance 身份
- 无 `AgentInstance` 模型，无会话复用
- Agent thread_id 结构为 `f"{context.run_id}:{assignment.task_id}"`，Task 间不共享

#### G. 权限与安全断链
- `execute` 工具无条件使用 `shell=True`（`executor.py:246`）
- `create_file`/`edit_file` 允许绝对路径（`executor.py:208`）
- 无命令白名单，无 HITL
- `WorkspacePolicy` 等 policy 模型存在但不被运行时强制执行

#### H. 持久化断链
- `ArtifactStore` 是进程内内存注册表，重启丢失
- 缺少 `team_runs`、`agent_instances`、`team_tasks`、`task_runs`、`permission_requests` 等表
- 单 Agent 和多 Agent 使用不同 SQLite store（`get_task_service()` vs `get_multiagent_store()`）

### 2.3 已有可复用模块

以下模块设计良好，应保留复用：
- `TaskGraph` + `TaskNode` — DAG 数据模型和算法正确
- `Artifact` + `ArtifactStore` — 模型完整，仅缺持久化
- `RunWorkspace` — 目录布局和安全检查正确
- `ProgrammaticVerifier` — 完整实现
- `CapabilityRegistry` — 线程安全，指标齐全
- `AgentProfile` — 完整的 policy 体系
- `planner.py` — `plan_with_llm` 完整
- `store.py` — SQLite 持久化基座

### 2.4 改进建议

1. 创建 `TeamRunContext` 统一 run 上下文传递
2. 创建 `TeamRuntimeFacade` 作为统一入口
3. `ArtifactStore` 增加 SQLite 持久化
4. `DeepAgentExecutor` 接入 `ArtifactStore` 生成真实 Artifact
5. `SimpleOrchestrator._verify()` 读取文件内容进行验证
6. 替换 `_InMemoryWorkerExecutor` 为真实 executor
7. `_run_single()` 失败时不返回 completed
8. 新增数据库表支持新数据模型
9. CLI 将 workspace 传递到 executor 和 verifier
10. 文件工具加路径安全检查
