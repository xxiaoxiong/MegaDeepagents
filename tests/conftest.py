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

import pytest


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

    yield

    # 3. teardown：再次清理，避免连接残留到下一个测试
    ma_store.close_connection()
    if hasattr(ma_store, "_store"):
        ma_store._store = None
