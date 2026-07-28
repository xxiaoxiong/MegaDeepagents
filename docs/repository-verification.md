# 仓库级集成验证

代码型 Run 只有在 Agent worktree 已合并、每个当前任务通过 Verifier、并且集成 worktree
通过仓库级检查后才能完成。单个 Agent 声明“测试通过”不等于整个 Run 完成。

## 验证计划

Root Graph 会把 Run 或 `repository` 元数据中的 `integration_test_commands` 持久化到根任务。
每项命令使用结构化 argv，不经过 shell：

```json
{
  "integration_test_commands": [
    {
      "label": "backend",
      "argv": ["python", "-m", "pytest", "-q"],
      "cwd": ".",
      "timeout_seconds": 600
    },
    {
      "label": "frontend",
      "argv": ["npm", "test"],
      "cwd": "frontend",
      "timeout_seconds": 600
    }
  ]
}
```

允许的命令限于常见测试、构建、静态检查入口。绝对工作目录、`..` 目录穿越、非有限超时、
`python -c` 和其他任意命令会被拒绝。旧字段 `integration_test_argv` 仍兼容。

没有显式配置时，集成管理器会在有限深度内发现：

- Python：`tests/`、`pyproject.toml`、`pytest.ini`、`setup.cfg`、`tox.ini`；
- Node：`package.json` 中非占位的 `test` 和 `build`，并识别 npm、pnpm、yarn；
- Rust：根目录 `Cargo.toml`；
- Go：根目录 `go.mod`。

自动发现不会联网安装依赖。Node 检查可以只读复用源仓库或保留 worktree 中的
`node_modules`，通过临时目录链接投影到集成 worktree；验证结束立即移除链接。

## 状态与恢复

每项检查会持久化并通过 SSE 投影：

- `IntegrationVerificationStarted`；
- `IntegrationVerificationCompleted`，包含退出码、超时/取消、耗时和截断日志；
- `IntegrationVerificationUnavailable`，包含缺失运行时、依赖或目录链接能力。

检查实际执行但返回非零时，Run 失败；运行时或依赖缺失属于环境阻塞，Run 进入
`waiting_human`。补齐环境后恢复同一个 Run，会重新进入唯一的 Root Graph 和集成门禁，
不会伪造成功或另起旁路流程。

官方 Docker 运行镜像包含 Python、Git、Node 和 npm。pnpm、yarn、Rust、Go 等仓库仍需在
自托管镜像中显式安装对应运行时，或提供受允许的验证命令。
