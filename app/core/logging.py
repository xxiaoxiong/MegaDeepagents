"""日志配置：统一的日志初始化。

生产环境（``APP_ENV=production``）文件日志输出 JSON 结构化格式，包含
``run_id`` / ``request_id`` 等上下文字段，便于日志聚合系统采集和过滤。
开发环境保持 Rich 人类可读格式。控制台始终用 Rich，文件按环境切换。
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from rich.logging import RichHandler

from app.core.config import settings

# ---- Context variables for structured logging ----

_run_id_var: ContextVar[str] = ContextVar("run_id", default="")
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def set_run_context(run_id: str = "", request_id: str = "") -> None:
    """Bind ``run_id`` / ``request_id`` to the current async/task context."""
    if run_id:
        _run_id_var.set(run_id)
    if request_id:
        _request_id_var.set(request_id)


def clear_run_context() -> None:
    _run_id_var.set("")
    _request_id_var.set("")


class _ContextFilter(logging.Filter):
    """Inject context variables into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        record.request_id = _request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", "")
        if run_id:
            payload["run_id"] = run_id
        request_id = getattr(record, "request_id", "")
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: Optional[str] = None) -> logging.Logger:
    """Initialize logging system, output to both console and file."""
    log_level = level or "INFO"
    logger = logging.getLogger("app")
    logger.setLevel(log_level)

    # Context filter — applied to all handlers so run_id / request_id are
    # always present on the record, regardless of handler format.
    ctx_filter = _ContextFilter()

    # Rich console handler — human-readable for both dev and prod stdout.
    rich_handler = RichHandler(
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
    )
    rich_handler.setLevel(log_level)
    rich_fmt = logging.Formatter("%(message)s", datefmt="[%X]")
    rich_handler.setFormatter(rich_fmt)
    rich_handler.addFilter(ctx_filter)
    logger.addHandler(rich_handler)

    # File handler — JSON in production, plain text in development.
    log_path = Path(settings.log_dir) / "agent.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(log_level)
    if settings.app_env == "production":
        file_handler.setFormatter(JsonFormatter())
    else:
        file_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_fmt)
    file_handler.addFilter(ctx_filter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logging()
