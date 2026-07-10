"""FastAPI 入口：聚合所有路由。"""

import os
import sys

# Windows 下强制 Python 使用 UTF-8，避免 open() 默认 GBK 导致 'gbk' codec can't decode 错误
if sys.platform == 'win32':
    os.environ['PYTHONUTF8'] = '1'

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.api.limiter import limiter
from app.api.routes_health import router as health_router
from app.api.routes_tasks import router as tasks_router
from app.api.routes_chat import router as chat_router
from app.api.routes_memory import router as memory_router
from app.api.routes_skills import router as skills_router
from app.api.routes_team import router as team_router
# streaming 已关闭，路由暂不注册
from app.core.config import settings
from app.core.logging import logger

app = FastAPI(
    title=settings.app_name,
    description="DeepAgents 原生能力优先的通用 Agent Runtime",
    version="0.1.0",
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 API 路由
app.include_router(health_router, tags=["health"])
app.include_router(tasks_router, tags=["tasks"])

app.include_router(chat_router, tags=["chat"])
app.include_router(memory_router, tags=["memory"])
app.include_router(skills_router, tags=["skills"])
app.include_router(team_router, tags=["team"])

# 挂载 Web 静态文件
from pathlib import Path
web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")


@app.on_event("startup")
def on_startup():
    import threading

    from app.skills.metadata import _init_db, get_connection
    from app.core.observability import init_observability

    # 初始化 LangSmith 可观测性（默认 False 时为 no-op，无外网依赖）
    init_observability(component="api")

    try:
        _init_db(get_connection())
    except Exception as exc:
        logger.warning(f"Skills DB init warmup skipped: {exc}")

    # 启动后台定时清理任务
    def _cleanup_loop():
        import time
        from app.task.runner import cleanup_stale_runners
        while True:
            time.sleep(300)  # 每 5 分钟检查一次
            cleaned = cleanup_stale_runners()
            if cleaned:
                logger.info(f"Cleaned up {cleaned} stale runner(s)")

    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True, name="runner-cleanup")
    cleanup_thread.start()

    logger.info(f"Starting {settings.app_name}...")


@app.on_event("shutdown")
def on_shutdown():
    from app.api.routes_tasks import shutdown_executor

    logger.info("Shutting down...")
    try:
        shutdown_executor()
    except Exception as exc:
        logger.warning(f"Executor shutdown failed: {exc}")
