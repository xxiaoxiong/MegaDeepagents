"""Memory 分层实验：多 Agent 系统中的分层记忆。

设计（P2-2 实验性原型）：
1. **Working Memory（工作记忆）**：当前轮次上下文，本轮结束后清空
2. **Episodic Memory（情景记忆）**：单次 task run 内的轮次/消息序列
3. **Semantic Memory（语义记忆）**：跨 task 持久化的事实/学到的知识（KB）
4. **Procedural Memory（程序记忆）**：跨 task 持久化的"如何做某事的方法"（SOP）

每个 Agent 可有独立的私有记忆域（参考 AgentSpec.private_memory_scope），
同时团队共享一份 Semantic / Procedural 集体记忆（cross-agent learning）。

本模块只实现"分层 + 检索 + 写入"基础能力，不依赖外部向量库；
向量检索为后续阶段增强点，当前先用关键词/SQL FTS 兜底。

存储位置：
- Working：进程内 dict，无持久化
- Episodic：MultiAgent store 的 agent_messages / team_rounds
- Semantic / Procedural：扩展 store 的 memory_entries 表
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.logging import logger


# ===== Memory 层级 =====

class MemoryTier:
    """记忆层级常量类（用 str 子类避开 Enum 复杂序列化）。"""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @classmethod
    def all_tiers(cls) -> list[str]:
        return [cls.WORKING, cls.EPISODIC, cls.SEMANTIC, cls.PROCEDURAL]


@dataclass
class MemoryEntry:
    """单条记忆项。"""

    id: str
    tier: str
    agent_scope: str | None = None  # None = team-shared；非空 = 该 Agent 私有
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_accessed_at: datetime | None = None
    access_count: int = 0
    importance: float = 0.5  # 0-1
    decay_rate: float = 0.01

    def touch(self) -> None:
        self.last_accessed_at = datetime.utcnow()
        self.access_count += 1


class WorkingMemory:
    """工作记忆：本轮上下文，进程内 dict 暂存。最快但易失。"""

    def __init__(self) -> None:
        self._store: dict[tuple[str | None, str], MemoryEntry] = {}
        self._lock = threading.Lock()

    def set(
        self,
        key: str,
        content: str,
        agent_scope: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=f"wm_{key}",
            tier=MemoryTier.WORKING,
            agent_scope=agent_scope,
            content=content,
            metadata=metadata or {},
        )
        with self._lock:
            self._store[(agent_scope, key)] = entry
        return entry

    def get(self, key: str, agent_scope: str | None = None) -> MemoryEntry | None:
        with self._lock:
            entry = self._store.get((agent_scope, key))
            if entry:
                entry.touch()
            return entry

    def clear_for_round(self) -> int:
        """清空整层（每轮结束）。"""
        with self._lock:
            n = len(self._store)
            self._store.clear()
        return n

    def list_scope(self, agent_scope: str | None = None) -> list[MemoryEntry]:
        with self._lock:
            return [e for (sc, _), e in self._store.items() if sc == agent_scope]


class EpisodicMemory:
    """情景记忆：单次 task run 内的轮次/消息序列。

    用 deque + 重要度+衰减打分实现"近因 + 重要度"双因子召回。
    """

    def __init__(self, capacity: int = 200) -> None:
        self._entries: deque[MemoryEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(
        self,
        content: str,
        agent_scope: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=f"ep_{int(time.time()*1000)}_{len(self._entries)}",
            tier=MemoryTier.EPISODIC,
            agent_scope=agent_scope,
            content=content,
            importance=importance,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries.append(entry)
        return entry

    def retrieve(
        self,
        query: str,
        agent_scope: str | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """简易关键词召回 + 重要度衰减打分。"""
        with self._lock:
            pool = [e for e in self._entries if e.agent_scope in (None, agent_scope)]

        def score(e: MemoryEntry) -> float:
            # 关键词命中加分
            kw_score = 0.0
            for kw in [w for w in query.split() if w]:
                if kw.lower() in e.content.lower():
                    kw_score += 0.3
            # 重要度 + 衰减
            age_days = max((datetime.utcnow() - e.created_at).total_seconds() / 86400, 0)
            importance = e.importance * (1.0 - e.decay_rate * age_days)
            return kw_score + importance

        ranked = sorted(pool, key=score, reverse=True)
        for e in ranked[:limit]:
            e.touch()
        return ranked[:limit]

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


class PersistentMemory:
    """Semantic / Procedural 记忆的进程内持久层（实验性）。

    生产环境应改用 sqlstore + 向量索引；当前用 in-memory dict 兜底，
    保证接口稳定，方便后续替换实现。
    """

    def __init__(self, tier: str) -> None:
        if tier not in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL):
            raise ValueError(f"PersistentMemory 只支持 semantic/procedural，传入：{tier}")
        self.tier = tier
        self._entries: dict[str, MemoryEntry] = {}
        self._lock = threading.Lock()

    def add(
        self,
        id: str,
        content: str,
        agent_scope: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=id,
            tier=self.tier,
            agent_scope=agent_scope,
            content=content,
            importance=importance,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[id] = entry
        return entry

    def get(self, id: str) -> MemoryEntry | None:
        with self._lock:
            e = self._entries.get(id)
            if e:
                e.touch()
            return e

    def retrieve(
        self,
        query: str,
        agent_scope: str | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """关键词模糊检索，按重要度+命中率排序。"""
        with self._lock:
            pool = [e for e in self._entries.values() if e.agent_scope in (None, agent_scope)]

        def score(e: MemoryEntry) -> float:
            kw_score = 0.0
            for kw in [w for w in query.split() if w]:
                if kw.lower() in e.content.lower():
                    kw_score += 0.5
            return kw_score + e.importance

        ranked = sorted(pool, key=score, reverse=True)[:limit]
        for e in ranked:
            e.touch()
        return ranked

    def all_entries(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._entries.values())


class LayeredMemorySystem:
    """四层记忆系统：Working + Episodic + Semantic + Procedural。

    特性：
    1. 每个 Agent 有私有 Working + 私有 Episodic，team-shared 的 Semantic/Procedural
    2. 可向指定层 add / retrieve
    3. 记忆衰减：每次 retrieve 按重要度降权（实验性）
    4. 跨 Agent 共享层用 None agent_scope
    """

    def __init__(self) -> None:
        self.working = WorkingMemory()
        self.episodic: dict[str, EpisodicMemory] = {}  # by task_id
        self.semantic = PersistentMemory(MemoryTier.SEMANTIC)
        self.procedural = PersistentMemory(MemoryTier.PROCEDURAL)
        self._ep_lock = threading.Lock()

    # ---- Episodic 分 task 管理 ----
    def get_episodic(self, task_id: str) -> EpisodicMemory:
        with self._ep_lock:
            if task_id not in self.episodic:
                self.episodic[task_id] = EpisodicMemory()
            return self.episodic[task_id]

    # ---- 跨层统一接口 ----
    def add(
        self,
        tier: str,
        content: str,
        agent_scope: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
        key: str | None = None,
    ) -> MemoryEntry:
        if tier == MemoryTier.WORKING:
            return self.working.set(
                key or "default", content, agent_scope=agent_scope, metadata=metadata
            )
        if tier == MemoryTier.EPISODIC:
            if not task_id:
                raise ValueError("episodic 层需要 task_id")
            return self.get_episodic(task_id).add(
                content, agent_scope=agent_scope, importance=importance, metadata=metadata
            )
        if tier == MemoryTier.SEMANTIC:
            entry_id = (
                metadata.get("id") if metadata else None
            ) or f"sem_{int(time.time()*1000)}"
            return self.semantic.add(
                entry_id, content, agent_scope=agent_scope, importance=importance, metadata=metadata
            )
        if tier == MemoryTier.PROCEDURAL:
            entry_id = (
                metadata.get("id") if metadata else None
            ) or f"proc_{int(time.time()*1000)}"
            return self.procedural.add(
                entry_id, content, agent_scope=agent_scope, importance=importance, metadata=metadata
            )
        raise ValueError(f"unknown tier: {tier}")

    def retrieve(
        self,
        tier: str,
        query: str,
        agent_scope: str | None = None,
        limit: int = 5,
        task_id: str | None = None,
        key: str | None = None,
    ) -> list[MemoryEntry]:
        if tier == MemoryTier.WORKING:
            ret: list[MemoryEntry] = []
            if key:
                e = self.working.get(key, agent_scope=agent_scope)
                if e:
                    ret.append(e)
            else:
                ret = self.working.list_scope(agent_scope=agent_scope)
            return ret
        if tier == MemoryTier.EPISODIC:
            if not task_id:
                raise ValueError("episodic retrieve 需要 task_id")
            return self.get_episodic(task_id).retrieve(query, agent_scope=agent_scope, limit=limit)
        if tier == MemoryTier.SEMANTIC:
            return self.semantic.retrieve(query, agent_scope=agent_scope, limit=limit)
        if tier == MemoryTier.PROCEDURAL:
            return self.procedural.retrieve(query, agent_scope=agent_scope, limit=limit)
        raise ValueError(f"unknown tier: {tier}")

    def snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """调试用：返回各层计数。"""
        return {
            "working": len(self.working.list_scope(None)) + sum(
                len(self.working.list_scope(a)) for a in []
            ),
            "episodic": self.get_episodic(task_id).size() if task_id else sum(
                e.size() for e in self.episodic.values()
            ),
            "semantic": len(self.semantic.all_entries()),
            "procedural": len(self.procedural.all_entries()),
        }


# 进程单例
_global_layered_memory: LayeredMemorySystem | None = None
_singleton_lock = threading.Lock()


def get_layered_memory() -> LayeredMemorySystem:
    global _global_layered_memory
    with _singleton_lock:
        if _global_layered_memory is None:
            _global_layered_memory = LayeredMemorySystem()
        return _global_layered_memory
