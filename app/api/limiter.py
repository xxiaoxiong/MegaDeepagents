"""共享速率限制器，避免循环导入。

注意：slowapi 的 Limiter 在初始化时会用 starlette Config 读取 .env；
starlette 在 Windows 上用平台默认编码（GBK）打开 .env，会导致 UTF-8 .env
（含中文注释/中文值）解析失败、抛 UnicodeDecodeError 串到 import 层。

为了避免这一环境问题阻塞 import（影响 test_smoke 与所有 import app.main 的路径），
本模块在构造 Limiter 时通过显式加载 .env（UTF-8）后的环境变量传给 slowapi，
并捕获构造异常后退化为 no-op limiter（仍保留 .limit 装饰器的兼容接口）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from slowapi.util import get_remote_address

from app.core.config import settings


class _NoopLimiter:
    """Limiter 构造失败时的无操作替代品，避免阻塞 import。"""

    def __init__(self) -> None:
        self.enabled = False

    def limit(self, *_args: Any, **_kwargs: Any) -> Callable[[Any], Any]:
        def decorator(fn: Any) -> Any:
            return fn
        return decorator


def _build_limiter() -> Any:
    """构造 slowapi Limiter；失败时回退到 _NoopLimiter。"""
    try:
        from slowapi import Limiter

        # 解决 Windows 上 starlette 用 GBK 读 UTF-8 .env 的问题：
        # 先以 UTF-8 把 .env 装进 os.environ（仅在缺失时填，避免覆盖现有值），
        # 然后 slowapi 即使再读 .env 抛 UnicodeDecodeError 也不再阻塞 critical 配置。
        env_path = Path(".env")
        if env_path.is_file():
            try:
                with env_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            key, _, value = line.partition("=")
                            key = key.strip()
                            value = value.strip().strip("\"'")
                            os.environ.setdefault(key, value)
            except Exception:
                pass

        return Limiter(
            key_func=get_remote_address,
            default_limits=[f"{settings.rate_limit_per_minute}/minute"],
        )
    except Exception:  # pragma: no cover - 环境兼容兜底
        # slowapi 构造失败也不应阻塞整个 app 的 import
        return _NoopLimiter()


limiter = _build_limiter()
