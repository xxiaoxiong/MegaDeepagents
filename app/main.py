"""FastAPI 入口：聚合所有路由。"""

import os
import sys
import threading
from contextlib import asynccontextmanager

# Windows 下强制 Python 使用 UTF-8，避免 open() 默认 GBK 导致 'gbk' codec can't decode 错误
if sys.platform == 'win32':
    os.environ['PYTHONUTF8'] = '1'

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.api.limiter import limiter
from app.api.routes_health import router as health_router
from app.api.routes_team import router as team_router
from app.api.v1 import router as v1_router
from app.core.config import settings
from app.core.logging import logger


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize durable services and stop background work cleanly."""
    from app.core.observability import init_observability
    from app.skills.metadata import _init_db, get_connection

    init_observability(component="api")
    try:
        _init_db(get_connection())
    except Exception as exc:
        logger.warning("Skills DB init warmup skipped: %s", exc)

    try:
        from app.application.runs.service import get_run_service

        recovered = get_run_service().recover_incomplete()
        if recovered:
            logger.info("Scheduled %s durable run(s) for recovery", recovered)
    except Exception as exc:
        logger.warning("Run recovery warmup skipped: %s", exc)

    cleanup_stop = threading.Event()
    cleanup_thread: threading.Thread | None = None

    if settings.enable_legacy_api:
        def cleanup_loop() -> None:
            from app.task.runner import cleanup_stale_runners

            while not cleanup_stop.wait(300):
                cleaned = cleanup_stale_runners()
                if cleaned:
                    logger.info("Cleaned up %s stale runner(s)", cleaned)

        cleanup_thread = threading.Thread(
            target=cleanup_loop,
            daemon=True,
            name="runner-cleanup",
        )
        cleanup_thread.start()
    logger.info("Starting %s...", settings.app_name)
    try:
        yield
    finally:
        cleanup_stop.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2)
        from app.infrastructure.database.connection import close_connection

        logger.info("Shutting down...")
        if settings.enable_legacy_api:
            from app.api.routes_tasks import shutdown_executor

            shutdown_executor()
        close_connection()


app = FastAPI(
    title=settings.app_name,
    description="MegaDeepagents V3 unified LangGraph agent runtime",
    version="3.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse
    detail = (
        {"code": "rate_limited", "message": "Rate limit exceeded"}
        if request.url.path.startswith("/api/v1")
        else "Rate limit exceeded. Please try again later."
    )
    return JSONResponse(
        status_code=429,
        content={"detail": detail},
    )


@app.exception_handler(HTTPException)
async def v1_http_error_handler(request: Request, exc: HTTPException):
    """Give V1 clients stable machine-readable errors without changing Legacy."""
    from fastapi.responses import JSONResponse

    if not request.url.path.startswith("/api/v1"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    status_codes = {
        403: "forbidden",
        404: "not_found",
        409: "state_conflict",
        415: "unsupported_artifact",
        429: "rate_limited",
    }
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "detail": {
                "code": status_codes.get(exc.status_code, "request_failed"),
                "message": str(exc.detail),
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def v1_validation_error_handler(
    request: Request, exc: RequestValidationError
):
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    if not request.url.path.startswith("/api/v1"):
        return JSONResponse(
            status_code=422, content={"detail": jsonable_encoder(exc.errors())}
        )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "Request validation failed",
                "fields": jsonable_encoder(exc.errors()),
            }
        },
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
if settings.enable_legacy_api:
    from app.api.routes_chat import router as chat_router
    from app.api.routes_memory import router as memory_router
    from app.api.routes_skills import router as skills_router
    from app.api.routes_tasks import router as tasks_router

    app.include_router(tasks_router, tags=["legacy-tasks"], deprecated=True)
    app.include_router(chat_router, tags=["legacy-chat"], deprecated=True)
    app.include_router(memory_router, tags=["legacy-memory"], deprecated=True)
    app.include_router(skills_router, tags=["legacy-skills"], deprecated=True)
app.include_router(
    team_router,
    tags=["legacy-team-compatibility"],
    deprecated=True,
    include_in_schema=False,
)
app.include_router(v1_router, tags=["v1"])

# 挂载 Web 静态文件（SPA fallback：未匹配的客户端路由回退到 index.html，
# 让 /chat、/chat/:runId、/runs/:runId 等深链与刷新都能正常进入 Vue Router）
from pathlib import Path
from starlette.exceptions import HTTPException as StarletteHTTPException


class SpaStaticFiles(StaticFiles):
    """SPA 静态文件：文件不存在时回退到 index.html，由前端路由接管。"""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", SpaStaticFiles(directory=str(frontend_dist), html=True), name="web")
