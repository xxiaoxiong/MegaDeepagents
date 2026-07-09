"""子智能体构建模块：为异步子智能体创建本地 graph。"""

from typing import Any

from langchain.agents import AgentState, create_agent


def _build_agent(
    name: str,
    system_prompt: str,
    model: str | None = None,
    tools: list[Any] | None = None,
    debug: bool = False,
) -> Any:
    """构建一个轻量 LangChain agent（用于子智能体 graph 注册）。

    使用 `create_agent` 自动包含 `messages` state key（AgentState 默认字段）。
    """
    from app.llm_factory import build_model

    effective_model = model or build_model()
    return create_agent(
        model=effective_model,
        tools=tools or [],
        system_prompt=system_prompt,
        debug=debug,
        name=name,
    )


def get_researcher_graph(debug: bool = False) -> Any:
    """构建 researcher 子智能体 graph。"""
    system_prompt = (
        "你是一个专业的研究专家。"
        "你的职责是根据用户的问题进行深入的资料收集、分析和整理。"
        "\n"
        "规则：\n"
        "- 优先使用搜索结果和工具收集可靠信息。\n"
        "- 对收集到的信息进行交叉验证，标注来源可信度。\n"
        "- 输出结构化报告，包含关键发现、数据来源和结论。\n"
        "- 如果遇到信息冲突，客观呈现不同观点。\n"
        "- 最终交付物保存到 /workspace 目录。"
    )
    return _build_agent(name="researcher", system_prompt=system_prompt, debug=debug)


def get_coder_graph(debug: bool = False) -> Any:
    """构建 coder 子智能体 graph。"""
    system_prompt = (
        "你是一个编程专家。你的职责是编写高质量、可运行的代码。"
        "\n"
        "规则：\n"
        "- 使用合适的数据结构和算法。\n"
        "- 代码中包含必要的注释和错误处理。\n"
        "- 优先编写可读性强、可维护的代码。\n"
        "- 编写后尽量运行验证，确保代码可执行。\n"
        "- 所有项目文件保存在 /workspace 目录。"
    )
    return _build_agent(name="coder", system_prompt=system_prompt, debug=debug)


def get_reviewer_graph(debug: bool = False) -> Any:
    """构建 reviewer 子智能体 graph。"""
    system_prompt = (
        "你是一个代码审查专家。你的职责是检查代码质量、提出改进建议和完善文档。"
        "\n"
        "规则：\n"
        "- 检查代码风格、命名规范和架构设计。\n"
        "- 识别潜在的性能问题和安全隐患。\n"
        "- 提出具体、可执行的改进建议。\n"
        "- 帮助完善 README、注释和 API 文档。"
    )
    return _build_agent(name="reviewer", system_prompt=system_prompt, debug=debug)


# 同步子智能体配置（fallback 用）
SYNC_SUBAGENT_SPECS: list[dict[str, Any]] = [
    {
        "name": "researcher",
        "description": "专门做网络调研、资料收集和事实核查。当用户需要查找资料、整理信息、验证事实时使用此子智能体。",
        "system_prompt": (
            "你是一个专业的研究专家。"
            "你的职责是根据用户的问题进行深入的资料收集、分析和整理。"
            "\n"
            "规则：\n"
            "- 优先使用搜索结果和工具收集可靠信息。\n"
            "- 对收集到的信息进行交叉验证，标注来源可信度。\n"
            "- 输出结构化报告，包含关键发现、数据来源和结论。\n"
            "- 如果遇到信息冲突，客观呈现不同观点。\n"
            "- 最终交付物保存到 /workspace 目录。"
        ),
        "tools": [],  # 继承主智能体工具
    },
    {
        "name": "coder",
        "description": "专门写代码、调试和运行程序。当用户需要编写代码、修复 bug、运行脚本或搭建项目时使用此子智能体。",
        "system_prompt": (
            "你是一个编程专家。你的职责是编写高质量、可运行的代码。"
            "\n"
            "规则：\n"
            "- 使用合适的数据结构和算法。\n"
            "- 代码中包含必要的注释和错误处理。\n"
            "- 优先编写可读性强、可维护的代码。\n"
            "- 编写后尽量运行验证，确保代码可执行。\n"
            "- 所有项目文件保存在 /workspace 目录。"
        ),
        "tools": [],  # 继承主智能体工具
    },
    {
        "name": "reviewer",
        "description": "专门做代码审查、质量检查和文档完善。当用户需要对已有代码进行审查、优化或编写文档时使用此子智能体。",
        "system_prompt": (
            "你是一个代码审查专家。你的职责是检查代码质量、提出改进建议和完善文档。"
            "\n"
            "规则：\n"
            "- 检查代码风格、命名规范和架构设计。\n"
            "- 识别潜在的性能问题和安全隐患。\n"
            "- 提出具体、可执行的改进建议。\n"
            "- 帮助完善 README、注释和 API 文档。"
        ),
        "tools": [],  # 继承主智能体工具
    },
]
