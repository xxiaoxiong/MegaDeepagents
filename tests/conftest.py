"""pytest 全局 fixture：保证 multiagent 测试间的 store 隔离。

根因（Req 9 测试隔离痛点）：
- `app.multiagent.store` 用 `threading.local()` 缓存 sqlite 连接，连接按 `settings.sqlite_path`
  复用。如果上一个测试改了 `sqlite_path` 但没 close_connection，下一个测试拿到的还是旧连接，
  导致 cancel 路由状态等单测在 full suite 下随机失败。
- `MultiAgentStore._store` 模块级单例也会跨测试泄漏。

解决：autouse fixture 在每个测试前重置 sqlite_path 到 tmp_path 下的隔离文件，
并 close 旧连接 + 清单例。这给所有 multiagent / api 测试一个固定的清洁起点。

注意：autouse=True + scope=function 意味着每个测试函数都执行一次重置，开销可接受
（sqlite 连接重建是毫秒级），换来的收益是 determinism。
"""

from __future__ import annotations

import os
import pytest


def pytest_collection_modifyitems(config, items):
    """Live suites are opt-in and never consume credentials in default CI."""
    if os.environ.get("RUN_LIVE_MODEL_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="set RUN_LIVE_MODEL_TESTS=1 with real model credentials",
    )
    for item in items:
        if "live_model" in item.keywords or "real_langsmith" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_multiagent_store(tmp_path):
    """每个测试用独立 sqlite 文件，避免全局 store 单例跨测试污染。"""
    import app.core.config as cfg
    import app.multiagent.store as ma_store

    # 1. 关闭已有的线程本地连接 + 清模块级单例
    ma_store.close_connection()
    if hasattr(ma_store, "_store"):
        ma_store._store = None

    # 2. 指向本次测试专属的 sqlite 文件
    cfg.settings.sqlite_path = str(tmp_path / "test.sqlite3")
    from app.multiagent.agent_registry import reset_agent_registry
    from app.multiagent.agent_runtime_manager import reset_agent_runtime_manager
    from app.multiagent.task_board import reset_task_board
    from app.multiagent.mailbox import reset_mailbox
    from app.multiagent.phase_g_store import reset_agent_run_history
    from app.multiagent.teammate_session import reset_teammate_supervisor
    from app.multiagent.permission import reset_permission_broker
    from app.multiagent.lifecycle_hooks import reset_lifecycle_hook_engine
    from app.multiagent.agent_profile import reset_capability_registry
    from app.multiagent.artifact import reset_default_artifact_store
    from app.multiagent.resume_coordinator import reset_resume_coordinator
    from app.multiagent.run_workspace import reset_workspaces
    from app.multiagent.team_runtime import reset_team_runtime
    reset_agent_registry()
    reset_agent_runtime_manager()
    reset_task_board()
    reset_mailbox()
    reset_agent_run_history()
    reset_teammate_supervisor()
    reset_permission_broker()
    reset_lifecycle_hook_engine()
    reset_capability_registry()
    reset_default_artifact_store()
    reset_resume_coordinator()
    reset_workspaces()
    reset_team_runtime()

    yield

    # 3. teardown：再次清理，避免连接残留到下一个测试
    ma_store.close_connection()
    if hasattr(ma_store, "_store"):
        ma_store._store = None
