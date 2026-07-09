"""Memory routes: 读取和搜索记忆。"""

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.logging import logger
from app.memory.cold_memory import get_cold_memory
from app.memory.hot_memory import get_hot_memory

router = APIRouter()


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 5


@router.get("/memory")
def read_memory():
    from app.core.config import settings
    memory_path = Path(settings.memory_file)
    user_path = Path(settings.user_file)
    return {
        "memory": memory_path.read_text(encoding="utf-8") if memory_path.exists() else "",
        "user": user_path.read_text(encoding="utf-8") if user_path.exists() else "",
    }


@router.post("/memory/search")
def search_memory(req: MemorySearchRequest):
    try:
        from app.memory.fts import search_fts
        results = search_fts(req.query, limit=req.limit)
    except Exception as exc:
        logger.warning(f"FTS search failed: {exc}")
        results = get_cold_memory().search(req.query, limit=req.limit)

    return {
        "query": req.query,
        "results": results,
        "count": len(results),
    }
