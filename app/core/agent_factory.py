"""Agent 工厂：组装所有组件并创建智能体。"""

import sqlite3
import threading
from pathlib import Path
from typing import Any

from deepagents import AsyncSubAgent, create_deep_agent
from langchain.tools import tool
from langgraph.cache.base import BaseCache

# 兼容不同 langgraph 版本：
# - 旧版（<1.0）使用 langgraph.cache.sqlite.SqliteCache
# - 新版（>=1.0）移除了该模块，改用 langgraph.checkpoint.sqlite.SqliteSaver 作为底座
try:  # pragma: no cover - 兼容路径，由环境决定
    from langgraph.cache.sqlite import SqliteCache  # type: ignore
except ImportError:  # pragma: no cover
    # 新版 langgraph 不再提供 SqliteCache：用 SqliteSaver 包装一个最小化的 BaseCache 占位
    class SqliteCache(BaseCache):  # type: ignore[no-redef]
        """新 langgraph 缺失 SqliteCache 时的最小占位实现。

        旧版 SqliteCache 用作 LangGraph 的 checkpointer；新版 langgraph 已把
        持久化职责切分到 langgraph.checkpoint.sqlite.SqliteSaver。这里仅保留
        同名类，避免下游 import 报错；调用方应优先使用 SqliteSaver。
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._saver = SqliteSaver(*args, **kwargs) if args or kwargs else None

        def __getattr__(self, name: str) -> Any:
            if self._saver is not None:
                return getattr(self._saver, name)
            raise AttributeError(name)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from app.core.config import settings
from app.core.logging import logger
from app.core.schemas import TaskResult
from app.task.store import get_task_store
from app.task.runner import TaskRunner
from app.tools.registry import ToolRegistry
from app.backends import build_backend
from app.llm_factory import build_model
from app.permissions import build_permissions
from app.core.profiles import register_default_profiles
from app.core.state_schema import TaskAgentState
from app.core.context import AgentContext


# ========== 子智能体系统 ==========

def build_subagents() -> list[dict[str, Any]]:
    """构建子智能体配置列表，返回可供 create_deep_agent 使用的 subagents 参数。"""
    s = settings
    if not s.enable_subagents:
        return []

    if s.enable_async_subagents:
        return [
            AsyncSubAgent(
                name="researcher",
                description="专门做网络调研、资料收集和事实核查。",
                graph_id="researcher",
                url=s.async_subagent_url,
            ),
            AsyncSubAgent(
                name="coder",
                description="专门写代码、调试和运行程序。",
                graph_id="coder",
                url=s.async_subagent_url,
            ),
            AsyncSubAgent(
                name="reviewer",
                description="专门做代码审查、质量检查和文档完善。",
                graph_id="reviewer",
                url=s.async_subagent_url,
            ),
        ]

    return []


# ========== 中间件构建 ==========

def build_middleware(model: Any, backend: Any, subagents: list[dict[str, Any]]) -> list[Any]:
    """组装中间件列表。"""
    return []


# ========== 缓存构建 ==========

def build_cache() -> BaseCache | None:
    """构建 LLM 缓存实例。"""
    s = settings
    if not s.enable_llm_cache:
        return None
    try:
        cache_path = Path(s.llm_cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteCache(path=str(cache_path))
    except Exception as exc:
        logger.warning(f"Failed to initialize LLM cache: {exc}, falling back to no cache")
        return None


# ========== 跨线程存储构建 ==========

def build_cross_thread_store() -> Any | None:
    """构建跨线程持久化存储。"""
    s = settings
    if not s.enable_cross_thread_memory:
        return None
    try:
        store_path = Path(s.cross_thread_memory_path)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteStore(str(store_path))
    except Exception as exc:
        logger.warning(f"Failed to initialize cross-thread store: {exc}, falling back to no store")
        return None


# ========== 用户工具构建 ==========

def _build_user_tools(registry: ToolRegistry, task_runner: TaskRunner):
    """根据注册中心构建用户工具列表。"""
    tools = []

    @tool
    def get_current_time() -> str:
        """返回当前时间字符串，格式为 YYYY-MM-DD HH:MM:SS。"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    tools.append(get_current_time)

    # 工具集工具
    tools.extend(registry.enabled_tools())

    return tools


# ========== SqliteSaver 全局缓存 ==========

_sqlite_saver: SqliteSaver | None = None
_sqlite_lock = threading.Lock()


def _get_sqlite_saver() -> SqliteSaver:
    """获取全局 SqliteSaver 实例，保持连接打开。"""
    global _sqlite_saver
    if _sqlite_saver is None:
        with _sqlite_lock:
            if _sqlite_saver is None:
                conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False)
                _sqlite_saver = SqliteSaver(conn=conn)
    return _sqlite_saver


# ========== 主 Agent 构建 ==========

def build_agent(
    task_id: str | None = None,
    thread_id: str = "default",
    auto_approve: bool | None = None,
):
    """构建一个配置完整的智能体实例。"""
    register_default_profiles()
    s = settings
    model = build_model()
    backend = build_backend()
    permissions = build_permissions()

    # 任务 runner 和存储
    task_store = get_task_store()
    task_runner = TaskRunner(task_store, task_id=task_id, thread_id=thread_id)

    # 工具注册中心
    registry = ToolRegistry(task_runner=task_runner)
    registry.register_all()
    tools = _build_user_tools(registry, task_runner)

    # HITL 配置
    effective_auto_approve = auto_approve if auto_approve is not None else True
    interrupt_on = None
    if s.hitl_required_for_write and not effective_auto_approve:
        interrupt_on = {
            "write_file": True,
            "edit_file": True,
            "execute": {"allowed_decisions": ["approve", "reject"]},
        }

    # 子智能体
    subagents = build_subagents()

    # 中间件
    middleware_list = build_middleware(model, backend, subagents)

    # 响应格式
    response_format = TaskResult if s.enable_response_format else None

    # 缓存
    cache = build_cache()

    # 跨线程存储
    cross_thread_store = build_cross_thread_store()

    # 系统提示词：基于 DeepAgents 原生能力，无自进化
    system_prompt = (
        "你是一个通用任务型智能体，目标是用可用工具和文件系统帮助用户完成任务。\n"
        "规则：\n"
        "- 先用工具执行任务，再将结果写入 /workspace/ 或 /memory/。\n"
        "- 只有在工具成功返回后，再向用户汇报结果；不要在调用工具前给出承诺或结论。\n"
        "- 如果工具执行失败，说明失败原因，不要编造结果。\n"
        "- 如果 glob 返回空、ls 返回空或 read_file 文件不存在，不要直接判定任务失败；"
        "应报告该条件为空，并继续完成其他可执行部分。\n"
        "- 对于生成类任务，即使输入目录为空也要正常产出文件；"
        "对于分析类任务，空结果本身就是有效结果，应如实记录。\n"
        "- 重要信息可以写入 /memory/MEMORY.md 或 /memory/USER.md，供后续会话使用。\n"
        "- 不要访问敏感路径或敏感文件。\n"
        "- 最终回答说明完成项、产物路径和未完成项。\n"
    )

    # 使用持久化的 SqliteSaver，支持跨进程恢复
    checkpointer = _get_sqlite_saver()

    context = AgentContext(user_id=thread_id or "default")
    agent = create_deep_agent(
        name=s.app_name,
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        skills=[s.skills_dir],
        memory=[s.memory_file, s.user_file],
        backend=backend,
        permissions=permissions,
        interrupt_on=interrupt_on,
        debug=s.app_env == "dev",
        checkpointer=checkpointer,
        state_schema=TaskAgentState,
        context_schema=AgentContext,
        subagents=subagents if subagents else None,
        middleware=middleware_list if middleware_list else None,
        response_format=response_format,
        store=cross_thread_store,
        cache=cache,
    )

    logger.info(
        f"Agent built: name={s.app_name}, model={s.llm_model}, tools={len(tools)}, "
        f"subagents={len(subagents)}, middleware={len(middleware_list)}, "
        f"auto_approve={effective_auto_approve}, cache={'on' if cache else 'off'}"
    )
    return agent
