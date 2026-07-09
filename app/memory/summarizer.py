"""检索后 LLM 摘要：将历史检索结果生成五段式摘要。"""

import asyncio
import random
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.llm_factory import build_model


FIVE_PART_PROMPT = """基于以下历史检索结果，生成一段五段式摘要，不要超过 200 字：

1. 用户当时想完成什么
2. Agent 做了什么
3. 得到什么结论
4. 涉及哪些文件、命令、URL 或技术细节
5. 还有哪些未解决事项

历史检索结果：
{results}
"""


async def summarize_results(results: list[dict[str, Any]], max_concurrency: int = 3) -> str | None:
    """并发摘要检索结果。"""
    if not results:
        return None

    semaphore = asyncio.Semaphore(max_concurrency)
    model = build_model()

    async def _summarize_one(result: dict[str, Any]) -> str | None:
        async with semaphore:
            for attempt in range(3):
                try:
                    prompt = FIVE_PART_PROMPT.format(results=result.get("content", "")[:500])
                    response = await model.ainvoke(prompt)
                    return response.content
                except Exception as exc:
                    logger.warning(f"Summarize attempt {attempt+1} failed: {exc}")
                    await asyncio.sleep(0.5 * (attempt + 1))
            return None

    summaries = await asyncio.gather(*[_summarize_one(r) for r in results[:5]])
    valid = [s for s in summaries if s]
    if not valid:
        return None

    # 简单合并
    return "\n\n".join(valid)


def summarize_results_sync(results: list[dict[str, Any]]) -> str | None:
    """同步版本的摘要。"""
    if not results:
        return None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(summarize_results(results))
