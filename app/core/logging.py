"""日志配置：统一的日志初始化。"""

import logging
import sys
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler

from app.core.config import settings


def setup_logging(level: Optional[str] = None) -> logging.Logger:
    """Initialize logging system, output to both console and file."""
    log_level = level or "INFO"
    logger = logging.getLogger("app")
    logger.setLevel(log_level)

    # Rich console handler
    rich_handler = RichHandler(
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
    )
    rich_handler.setLevel(log_level)
    rich_fmt = logging.Formatter("%(message)s", datefmt="[%X]")
    rich_handler.setFormatter(rich_fmt)
    logger.addHandler(rich_handler)

    # File handler
    log_path = Path(settings.log_dir) / "agent.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


logger = setup_logging()
