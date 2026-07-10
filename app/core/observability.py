"""可观测性中枢：封装 LangSmith，业务代码零侵入降级。

设计原则：
1. 业务代码只 import 本模块，不直接 import langsmith
2. langsmith_enabled=False（默认）时，所有装饰器/上下文都是 no-op，框架本地可跑
3. langsmith_enabled=True + 无 api_key 时降级 offline_log（仅本地写日志摘要）
4. langsmith_enabled=True + 有 api_key 时设置环境变量，让 LangChain ChatDeepSeek/ChatOpenAI
   自动上报 LLM 调用（含 deepagents 内部触发）——方案 A 的核心收益
5. 9 处业务埋点用 traceable / trace_span，自动挂到 LangSmith trace 树
6. SSE 事件通过 emit_trace_event 旁路分发一份为 trace 信号（一举两得）
7. 持久化：TeamRunner 通过 get_current_run_url() 拿到 run URL 落库到 team_rounds

不让 LangSmith 强依赖外网 / 不破坏现有 API / 不影响未配置时的本地体验，
这是 docs/updatePlan.md 与 docs/reviewUpdate.md 的硬性约束。
"""

from __future__ import annotations

import functools
import os
from contextlib import contextmanager
from typing import Any, Callable, Generator
from uuid import UUID

from app.core.config import settings
from app.core.logging import logger


# ====================== 可选依赖：langsmith ======================

_LANGSMITH_AVAILABLE = False
_LangSmithClient = None  # type: ignore[assignment]
_ls_traceable = None  # type: ignore[assignment]
RunTree = None  # type: ignore[assignment]

try:
    from langsmith import Client as _LangSmithClient
    from langsmith import traceable as _ls_traceable
    from langsmith.run_trees import RunTree

    _LANGSMITH_AVAILABLE = True
except ImportError:  # pragma: no cover
    pass


# ====================== 全局状态 ======================

_initialized: bool = False
_enabled: bool = False
_offline_log: bool = True
_service_name: str = "multiagent-frame"
_sample_rate: float = 1.0
_client: Any | None = None


class ObservabilityContext:
    """初始化后保留的运行时上下文句柄。"""

    __slots__ = ("enabled", "offline_log", "service_name", "client", "sample_rate")

    def __init__(self, enabled: bool, offline_log: bool, service_name: str, client: Any | None, sample_rate: float) -> None:
        self.enabled = enabled
        self.offline_log = offline_log
        self.service_name = service_name
        self.client = client
        self.sample_rate = sample_rate

    @property
    def tracing_is_enabled(self) -> bool:
        return self.enabled


# ====================== 初始化 ======================


def init_observability(component: str | None = None) -> ObservabilityContext:
    """进程启动时调用一次，幂等。"""
    global _initialized, _enabled, _offline_log, _service_name, _sample_rate, _client

    if _initialized:
        return ObservabilityContext(_enabled, _offline_log, _service_name, _client, _sample_rate)

    s = settings
    _enabled = bool(s.langsmith_enabled and _LANGSMITH_AVAILABLE)
    _offline_log = bool(s.langsmith_offline_log)
    _service_name = component or s.langsmith_service_name or "multiagent-frame"
    _sample_rate = max(0.0, min(1.0, float(s.langsmith_sample_rate)))

    has_api_key = bool(s.langsmith_api_key)
    actually_export = _enabled and has_api_key and bool(s.langsmith_tracing)

    if not _LANGSMITH_AVAILABLE and s.langsmith_enabled:
        logger.warning("[observability] langsmith_enabled=True 但 langsmith 包未安装，降级 offline_log")
        _enabled = False
        _offline_log = True

    if actually_export:
        os.environ["LANGSMITH_API_KEY"] = s.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = s.langsmith_project or "multiagent-frame"
        os.environ["LANGSMITH_ENDPOINT"] = s.langsmith_endpoint
        os.environ["LANGSMITH_TRACING"] = "true"
        try:
            _client = _LangSmithClient(api_url=s.langsmith_endpoint, api_key=s.langsmith_api_key)
            logger.info(
                f"[observability] LangSmith tracing 已开启 project={s.langsmith_project} "
                f"service={_service_name} sample_rate={_sample_rate}"
            )
        except Exception as exc:
            logger.warning(f"[observability] LangSmith client 创建失败，降级 offline_log: {exc}")
            _enabled = False
            _offline_log = True
    elif _enabled and not has_api_key:
        logger.info("[observability] langsmith_enabled=True 但未配置 API_KEY，仅本地 trace 摘要")
        _enabled = False
        _offline_log = True
    else:
        if _offline_log:
            logger.debug(f"[observability] LangSmith 未开启 service={_service_name}")

    _initialized = True
    return ObservabilityContext(_enabled, _offline_log, _service_name, _client, _sample_rate)


def is_enabled() -> bool:
    """热路径廉价判断当前是否在 tracing。"""
    if not _initialized:
        init_observability()
    return _enabled


def reset_for_test() -> None:
    """测试专用：重置全局状态，清理环境变量。"""
    global _initialized, _enabled, _offline_log, _service_name, _sample_rate, _client
    _initialized = False
    _enabled = False
    _offline_log = True
    _service_name = "multiagent-frame"
    _sample_rate = 1.0
    _client = None
    for k in ["LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "LANGSMITH_TRACING"]:
        os.environ.pop(k, None)


# ====================== 装饰器 ======================


def traceable(
    name: str | None = None,
    run_type: str = "chain",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    process_inputs: Callable[[tuple], dict[str, Any]] | None = None,
    process_outputs: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable[[Callable], Callable]:
    """函数级 trace 装饰器。

    enabled 时透传到 langsmith.traceable；
    disabled 时若 offline_log 仍打 [trace] 摘要；
    disabled 且 offline_log=False 时完全 no-op。

    process_inputs / process_outputs 可选：将函数入参/出参映射为可序列化的简洁 dict，
    使得 LangSmith UI 上看到的 span 内容具有业务语义（而非 Python 对象字串）。
    """
    if not _initialized:
        init_observability()

    def _wrap_inputs(fn: Callable, args: tuple, kwargs: dict) -> dict:
        if process_inputs:
            try:
                return process_inputs(args)
            except Exception:
                pass
        # fallback：仅记录参数字符串前 200 字
        return {"args": str(args)[:200], "kwargs": str(kwargs)[:200]}

    def _wrap_outputs(result: Any) -> dict:
        if process_outputs:
            try:
                return process_outputs(result)
            except Exception:
                pass
        try:
            return {"result": str(result)[:200]}
        except Exception:
            return {"result": "<unrepr>"}

    def _decorate(fn: Callable) -> Callable:
        if not _enabled or _ls_traceable is None:
            if not _offline_log:
                return fn
            @functools.wraps(fn)
            def _unnop(*args, **kwargs):
                _log_span("enter", name or fn.__name__, run_type, _wrap_inputs(fn, args, kwargs), None)
                try:
                    result = fn(*args, **kwargs)
                    _log_span("exit", name or fn.__name__, run_type, _wrap_outputs(result), None)
                    return result
                except Exception as exc:
                    _log_span("error", name or fn.__name__, run_type, None, error=exc)
                    raise
            return _unnop

        ls_kwargs: dict[str, Any] = {"run_type": run_type}
        if name:
            ls_kwargs["name"] = name
        if tags:
            ls_kwargs["tags"] = tags
        base_meta = {"service": _service_name}
        if metadata:
            base_meta.update(metadata)
        if process_inputs:
            ls_kwargs["process_inputs"] = process_inputs
        if process_outputs:
            ls_kwargs["process_outputs"] = process_outputs
        ls_kwargs["metadata"] = base_meta

        decorated = _ls_traceable(**ls_kwargs)(fn)

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            if _offline_log:
                _log_span("enter", name or fn.__name__, run_type, _wrap_inputs(fn, args, kwargs), None)
            try:
                result = decorated(*args, **kwargs)
            except Exception as exc:
                if _offline_log:
                    _log_span("error", name or fn.__name__, run_type, None, error=exc)
                raise
            if _offline_log:
                _log_span("exit", name or fn.__name__, run_type, _wrap_outputs(result), None)
            return result

        _wrapper._ls_decorated = decorated  # type: ignore[attr-defined]
        return _wrapper

    return _decorate


# ====================== 上下文管理器 trace_span ======================


@contextmanager
def trace_span(
    name: str,
    run_type: str = "chain",
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """上下文管理器：手动开/关 span，自动挂到当前 LangSmith trace 树。

    策略：用 RunTree 手动创建 span，通过 tracing_context(parent=span) 设进 LangSmith contextvar，
    使得 with 块内的 @traceable 函数（如 _traced_llm_call）的 LLM 调用自动成为此 span 的子 run。

    disabled / offline 模式：仅打 [trace] 摘要日志。
    """
    if not _initialized:
        init_observability()

    span_info: dict[str, Any] = {"name": name, "run_type": run_type, "metadata": metadata}

    if not _enabled or RunTree is None:
        if _offline_log:
            _log_span("enter", name, run_type, metadata, None)
        try:
            yield span_info
        finally:
            if _offline_log:
                _log_span("exit", name, run_type, metadata, None)
        return

    base_meta = {"service": _service_name}
    if metadata:
        base_meta.update(metadata)

    if _offline_log:
        _log_span("enter", name, run_type, metadata, None)

    # 手动构造 RunTree。langsmith 0.10 把 metadata 存在 extra["metadata"] 中
    child_run = RunTree(
        name=name,
        run_type=run_type,
        inputs={},
        tags=tags or [],
        extra=({"metadata": base_meta, **(extra or {})}),
    )

    span_info["run"] = child_run

    # 用 tracing_context 将 child_run 设为当前 contextvar 的 parent
    # 这样 with 块内的 @traceable 函数（T4 agent_llm_call）会自动把 LLM run 挂到 child_run 下
    from langsmith.run_helpers import tracing_context as _tc

    try:
        with _tc(parent=child_run, metadata=base_meta, tags=tags or []):
            try:
                yield span_info
            except Exception as exc:
                try:
                    child_run.end(error=str(exc))
                    child_run.post()
                except Exception:
                    pass
                if _offline_log:
                    _log_span("error", name, run_type, metadata, error=exc)
                raise
            else:
                try:
                    child_run.end()
                    child_run.post()
                except Exception:
                    pass
                if _offline_log:
                    _log_span("exit", name, run_type, metadata, None)
    except Exception as exc:
        logger.warning(f"[observability] tracing_context failed for {name}: {exc}")
        try:
            yield span_info
        finally:
            if _offline_log:
                _log_span("exit", name, run_type, metadata, None)


# ====================== LLM 调用辅助 ======================


def traced_llm_invoke(
    llm: Any,
    prompt: str,
    *,
    run_name: str = "llm_call",
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Any:
    """对裸 LLM invoke 包一层 trace（给 deepagents 外的临时 LLM 调用用）。"""
    if not _enabled or llm is None:
        return llm.invoke(prompt)
    try:
        with trace_span(run_name, run_type="llm", metadata=metadata, tags=tags):
            return llm.invoke(prompt)
    except Exception as exc:
        logger.warning(f"[observability] traced_llm_invoke failed, fallback to raw: {exc}")
        return llm.invoke(prompt)


# ====================== SSE 事件桥接 ======================


def emit_trace_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """把 SSE 事件旁路分发到 LangSmith 当前 span。"""
    if not _initialized:
        init_observability()
    if _offline_log:
        _log_span("event", event_type, "event", payload)
    if not _enabled:
        return
    try:
        from langsmith.run_helpers import get_current_run_tree
        parent_run = get_current_run_tree()
    except Exception:
        parent_run = None
    if parent_run is not None and hasattr(parent_run, "add_event"):
        try:
            parent_run.add_event({"name": event_type, "data": payload or {}})
        except Exception:
            pass


# ====================== run_url 取值 ======================


def get_current_run_url() -> str | None:
    """业务代码（如 TeamRunner.save_round）拿当前轮的 LangSmith run URL。

    由于 langsmith 0.10 的 RunTree 没有 `.url()` 方法，用 `.id` 拼接。
    """
    if not _enabled or not _initialized:
        return None
    try:
        from langsmith.run_helpers import get_current_run_tree
        parent_run = get_current_run_tree()
    except Exception:
        return None
    if parent_run is None:
        return None
    run_id = parent_run.id
    if not run_id:
        return None
    if isinstance(run_id, UUID):
        run_id_str = str(run_id)
    else:
        run_id_str = str(run_id)
    # 从 client 的 web_url 或默认拼接
    try:
        base = _client.web_url if _client and hasattr(_client, "web_url") else "https://smith.langchain.com"
    except Exception:
        base = "https://smith.langchain.com"
    return f"{base}/o/default/projects/p/default/r/{run_id_str}?poll=true"


# ====================== 本地日志摘要 ======================


def _log_span(phase: str, name: str, run_type: str, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
    """offline_log 模式：把 span 摘要打到 logger。

    Args:
        phase: enter / exit / error / event
        name: span 名
        run_type: chain / llm / event
        payload: enter 时的 metadata / event 时的 payload
        error: error 时附异常
    """
    payload_str = ""
    if payload:
        try:
            import json
            payload_str = json.dumps(payload, ensure_ascii=False, default=str)[:300]
        except Exception:
            payload_str = str(payload)[:300]

    if phase == "enter":
        logger.info(f"[trace] enter name={name} type={run_type} meta={payload_str}")
    elif phase == "exit":
        logger.info(f"[trace] exit  name={name} type={run_type}")
    elif phase == "error":
        logger.warning(f"[trace] error name={name} type={run_type} err={error} meta={payload_str}")
    elif phase == "event":
        logger.info(f"[trace] event name={name} payload={payload_str}")
