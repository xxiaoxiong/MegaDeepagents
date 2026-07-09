"""Smoke test：模块导入与入口存在性。"""

import importlib


def test_import_core_modules():
    mods = [
        "app.core.config",
        "app.core.logging",
        "app.core.agent_factory",
        "app.core.schemas",
        "app.core.runtime",
        "app.task.models",
        "app.task.store",
        "app.task.service",
        "app.task.runner",
        "app.memory.hot_memory",
        "app.memory.cold_memory",
        "app.memory.fts",
        "app.memory.summarizer",
        "app.memory.tools",
        "app.skills.loader",
        "app.skills.manager",
        "app.skills.tools",
        "app.tools.registry",
        "app.tools.file_tools",
        "app.tools.web_tools",
        "app.tools.task_tools",
        "app.tools.mcp_loader",
        "app.api.routes_health",
        "app.api.routes_tasks",
        "app.api.routes_chat",
        "app.api.routes_memory",
        "app.api.routes_skills",
        "app.api.routes_team",
        "app.main",
        "app.cli",
    ]
    for m in mods:
        importlib.import_module(m)


def test_import_multiagent_modules():
    """多智能体模块全部可导入。"""
    mods = [
        "app.multiagent",
        "app.multiagent.models",
        "app.multiagent.messages",
        "app.multiagent.state",
        "app.multiagent.agent_spec",
        "app.multiagent.bus",
        "app.multiagent.inbox",
        "app.multiagent.room",
        "app.multiagent.store",
        "app.multiagent.runtime_adapter",
        "app.multiagent.speaker_selector",
        "app.multiagent.termination",
        "app.multiagent.review_repair",
        "app.multiagent.team_runner",
        "app.multiagent.prompts",
        "app.multiagent.policies",
        "app.multiagent.default_teams",
    ]
    for m in mods:
        importlib.import_module(m)


def test_cli_has_app():
    from app.cli import app
    assert app is not None


def test_api_has_app():
    from app.main import app as fastapi_app
    assert fastapi_app is not None
