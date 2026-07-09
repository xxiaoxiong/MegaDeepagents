"""Pytest 配置：确保 app 在路径中，并初始化 runtime 目录。"""

import sys
from pathlib import Path

# 确保 app 能被导入
sys.path.insert(0, str(Path(__file__).parent / "app"))

import pytest
from app.core.config import settings

# 确保 runtime 目录存在
settings._ensure_dirs()
