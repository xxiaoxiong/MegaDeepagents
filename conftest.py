"""Pytest 配置：确保 app 在路径中，并初始化 runtime 目录。

可观测性默认 disabled，避免测试触外网；
real_langsmith marker 仅当 LANGSMITH_API_KEY 环境变量存在时跑。
"""

import os
import sys
from pathlib import Path

# 确保 app 能被导入
sys.path.insert(0, str(Path(__file__).parent / "app"))

import pytest
from app.core.config import settings

# 强制关闭 LangSmith，防止测试期间向 LangSmith 发送真实请求
# （settings 默认从 .env 加载可能含 LANGSMITH_ENABLED=true，必须在此覆盖）
settings.langsmith_enabled = False
settings.langsmith_api_key = ""

# 确保 runtime 目录存在
settings._ensure_dirs()

# 初始化可观测性（默认 disabled）；只在显式开启时才真装饰
from app.core import observability
observability.init_observability(component="pytest")


def pytest_collection_modifyitems(items, config):
    """给 real_langsmith marker 在缺 KEY 时自动加 skip。"""
    if not os.environ.get("LANGSMITH_API_KEY"):
        skip_marker = pytest.mark.skip(reason="需 LANGSMITH_API_KEY 才运行 real_langsmith 测试")
        for item in items:
            if "real_langsmith" in item.keywords:
                item.add_marker(skip_marker)
