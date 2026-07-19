# MegaDeepagents

MegaDeepagents 是基于 DeepAgents/LangGraph 的 Agent 运行时。多 Agent 的生产主链是
`TASK_TEAM`；旧的轮次群聊 `DISCUSSION/TeamRunner` 仅保留为兼容模式，不再承接新能力。

## 运行时主链

```text
API / CLI
  → TeamRuntimeFacade
  → SimpleOrchestrator（确定性状态机）
  → TransactionalTaskService（TaskGraph 版本与 Mutation）
  → ParallelTeamScheduler（唯一生产调度器）
  → TeammateSupervisor / stable TeammateSession
  → DeepAgentExecutor
  → ArtifactStore + Verifier
  → GitIntegrationManager
```

- `TaskGraph` 只保存计划结构、依赖、OutputContract 与版本。
- SQLite `TaskBoard` 是认领、尝试、所有权和验证状态的运行态权威事实源。
- Worker 只能提交 `PRODUCED` 证据；Verifier 通过后任务才能进入 `SUCCEEDED`。
- 编码运行可绑定源 Git 仓库。每个 Teammate 使用独立 worktree/分支，提交后由
  Integration Manager 串行集成；普通 Worker 不能修改或推送 `main/master`。
- 生产 Verifier fail-closed：模型不可用、文件仅非空或异常都不会自动 PASS。

完整审计和迁移说明见
[docs/Agent_Team_Parity_Audit.md](docs/Agent_Team_Parity_Audit.md)。

## 快速启动

```bash
python -m pip install -e .
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

Windows PowerShell：

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

复制 `.env.example` 为 `.env` 后配置模型。LangSmith 默认关闭；未配置凭证时不会作为
完成证据。

## TASK_TEAM API

新客户端使用 `/team-runs`：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/team-runs` | 创建 TASK_TEAM run；可传源仓库、基准分支和 SHA |
| GET | `/team-runs/{run_id}` | 运行状态 |
| POST | `/team-runs/{run_id}/pause` | 协作式暂停调度 |
| POST | `/team-runs/{run_id}/resume` | 从 SQLite/checkpoint 恢复同一运行 |
| POST | `/team-runs/{run_id}/cancel` | 取消运行并终结未完成任务 |
| GET | `/team-runs/{run_id}/agents` | Teammate/Session 状态 |
| GET | `/team-runs/{run_id}/tasks` | 权威 TaskBoard 投影 |
| GET | `/team-runs/{run_id}/task-graph` | 版本化 TaskGraph |
| GET | `/team-runs/{run_id}/artifacts` | Artifact 清单 |
| GET | `/team-runs/{run_id}/artifacts/{id}/lineage` | Artifact 修复/版本链 |
| GET | `/team-runs/{run_id}/worktrees` | worktree/租约/分支 |
| GET | `/team-runs/{run_id}/git` | commit、merge queue、PR 元数据 |
| GET | `/team-runs/{run_id}/verification` | 验证与工具证据 |
| GET | `/team-runs/{run_id}/errors` | 错误、冲突和 Repair 过程 |
| GET | `/team-runs/{run_id}/permissions` | 待处理权限请求 |
| POST | `/team-runs/{run_id}/permissions/{id}/decision` | 权限批准/拒绝 |
| GET | `/team-runs/{run_id}/plans` | 待审批 Teammate 计划 |
| POST | `/team-runs/{run_id}/plans/{id}/decision` | 计划批准/驳回 |
| POST | `/team-runs/{run_id}/agents/{agent_id}/messages` | 向运行中 Teammate 发消息 |
| GET | `/team-runs/{run_id}/stream` | 可按单调 sequence 重放的 SSE |

创建 Git 编码运行示例：

```json
{
  "goal": "实现并验证新的 API",
  "team": "software_dev_team",
  "source_repository_path": "/absolute/path/to/repository",
  "base_branch": "main",
  "review_required": true
}
```

旧 `/team-tasks` 路由仍映射到同一个 `TeamRuntimeFacade`；只有显式
`DISCUSSION` 模式才进入 Legacy `TeamRunner`。

## Teammate 与协作工具

每个 `AgentInstance` 对应持久化的 `TeammateSession`，保留 `agent_id`、`session_id`、
`thread_id`、checkpoint namespace、worktree、mailbox cursor、对话状态和命令队列。
任务完成后 Teammate 回到 `IDLE`，不会销毁身份。

DeepAgent 可使用 19 个受治理团队工具：成员/任务查询、原子认领、任务 Mutation、消息、
动态 Teammate、权限请求、计划提交和进度上报。所有写操作经过 Control Plane，Agent 不能
直接把任务标记成功，也不能用消息伪造用户授权。

## 安全与权限

- AgentProfile 是静态工具白名单；`PermissionPolicy/Broker` 执行参数级动态策略。
- 文件写、未知 Shell、网络、包安装和 Git 写操作默认申请权限；secret/destructive 默认拒绝。
- Shell 只执行结构化 argv 且 `shell=False`，区分 Unix、Windows `cmd` 和 PowerShell 策略；
  支持超时、输出上限、进程组终止和运行中取消。
- 文件和 Artifact 使用同目录临时文件 + `os.replace` 原子发布，并拒绝路径/符号链接逃逸。
- Worktree 默认不复制任何本地环境文件；`environment_file_allowlist` 只允许显式列出的
  gitignored 普通文件，并始终拒绝私钥/凭证文件与路径逃逸。

## 测试

默认测试不使用真实模型或 LangSmith 凭证：

```bash
pytest -m "not live_model and not real_langsmith"
```

可选真实模型测试：

```bash
RUN_LIVE_MODEL_TESTS=1 pytest -m live_model
```

`tests/test_agent_team_runtime_v2.py` 使用临时真实 Git 仓库验证 worktree 隔离、提交、集成、
冲突与租约恢复，并覆盖 Session、权限、取消、Artifact、动态团队和 Mutation 不变量。

## Legacy 边界

`app/multiagent/team_runner.py`、`runtime_adapter.py`、`team_graph.py`、Action JSON 和轮次群聊
仅服务 `DISCUSSION` 兼容模式。不要把新工具、权限、Artifact、worktree 或恢复能力加入该链；
新功能必须进入 `TeamRuntimeFacade → ParallelTeamScheduler → DeepAgentExecutor`。
