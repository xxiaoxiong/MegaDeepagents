# V3 测试策略

## 默认门禁

```bash
python -m compileall -q app
pytest -m "not live_model and not real_langsmith"
cd frontend && npm run test && npm run build
```

## 后端

- 单元：TaskGraph、TaskBoard、Mutation、Artifact hash、Verifier、Permission、Supervisor。
- 集成：单/团队共用 Root Graph、真实 TaskBoard、真实 Artifact 文件、Verifier gate、Repair、API、SSE replay、SQLite 迁移。
- 恢复/Git：冷启动、稳定 session、worktree、commit、conflict、取消迟到结果。
- Live：仅 `RUN_LIVE_MODEL_TESTS=1`，无凭证明确 skip，不伪造。

测试 Fake 只能作为注入 seam；生产路径不含 Fake fallback。关键完成判定必须经过真实 TaskBoard/Artifact/Verifier 状态变更。

## 前端

- API client 错误和类型。
- Pinia 归并/重放/去重。
- TaskGraph 状态映射。
- Run 控制按钮。
- 审批提交。
- 生产构建。

## 已执行结果

- 基线：511 passed，4 failed，5 skipped（正确 PATH）。
- Windows 修复及 V3 根图/API 后：517 passed，6 skipped。
- 新测试包含旧附件迁移、统一单/团队根图和真实 repair task。
