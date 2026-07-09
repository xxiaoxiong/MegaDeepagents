"""记忆工具：session_search。read_hot_memory 已由 DeepAgents MemoryMiddleware 提供。"""

from typing import Any

from langchain.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.memory.cold_memory import get_cold_memory
from app.memory.summarizer import summarize_results_sync


def build_memory_tools():
    """构建 Agent 可调用的记忆工具列表。"""
    tools = []

    @tool
    def session_search(query: str, limit: int = 5) -> str:
        """搜索历史会话记录。支持中英文检索。"""
        try:
            from app.memory.fts import search_fts
            results = search_fts(query, limit=limit)
        except Exception as exc:
            logger.warning(f"FTS search failed, falling back to cold memory: {exc}")
            results = get_cold_memory().search(query, limit=limit)

        if not results:
            return "未找到相关历史记录。"

        # LLM 摘要
        summary = summarize_results_sync(results)
        if summary:
            return f"检索到 {len(results)} 条历史记录。\n\n摘要：\n{summary}"

        # 降级为 raw preview
        previews = []
        for r in results[:3]:
            previews.append(f"[{r.get('role', '?')}] {r.get('content', '')[:200]}")
        return f"检索到 {len(results)} 条历史记录（摘要失败，显示预览）：\n" + "\n---\n".join(previews)

    tools.extend([session_search])
    return tools
