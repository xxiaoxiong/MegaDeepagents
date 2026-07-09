"""LayeredMemory 测试。"""

from __future__ import annotations

from app.multiagent.layered_memory import (
    LayeredMemorySystem,
    MemoryTier,
)


def test_working_memory_rw():
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.WORKING, "当前目标是分析架构", agent_scope="Planner", key="goal")
    entries = lm.retrieve(MemoryTier.WORKING, "", agent_scope="Planner", key="goal")
    assert len(entries) == 1
    assert "分析架构" in entries[0].content


def test_episodic_memory_rw():
    lm = LayeredMemorySystem()
    lm.add(
        MemoryTier.EPISODIC,
        "Planner 发出了 plan 消息",
        agent_scope="Planner",
        importance=0.8,
        task_id="task1",
    )
    results = lm.retrieve(MemoryTier.EPISODIC, "plan", agent_scope="Planner", task_id="task1")
    assert len(results) >= 1
    assert "plan" in results[0].content.lower()


def test_episodic_retrieve_returns_top_k():
    """过多条目时，retrieve 返回 limit 限制数量。"""
    lm = LayeredMemorySystem()
    task = "ep_k"
    for i in range(10):
        lm.add(
            MemoryTier.EPISODIC,
            f"event {i}",
            agent_scope="Coder",
            importance=0.3,
            task_id=task,
        )
    # 检索前 3 条
    results = lm.retrieve(MemoryTier.EPISODIC, "event", agent_scope="Coder", limit=3, task_id=task)
    assert len(results) <= 3


def test_semantic_memory_retrieve_fuzzy():
    """语义记忆关键词不精确匹配时也能模糊召回。"""
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.SEMANTIC, "Planner 不能直接宣布完成", importance=0.9)
    results = lm.retrieve(MemoryTier.SEMANTIC, "Planner 完成")
    assert len(results) >= 1


def test_procedural_memory_add():
    """程序记忆的增删查。"""
    lm = LayeredMemorySystem()
    lm.add(
        MemoryTier.PROCEDURAL,
        "Review workflow: review_request → critique → revision → re-review",
        importance=0.95,
        agent_scope=None,
    )
    results = lm.retrieve(MemoryTier.PROCEDURAL, "review workflow")
    assert len(results) >= 1


def test_memory_tier_separation():
    """不同层互不污染。"""
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.WORKING, "working_data", key="x")
    lm.add(MemoryTier.EPISODIC, "episodic_data", task_id="t1")
    lm.add(MemoryTier.SEMANTIC, "semantic_data", agent_scope="Planner")
    # retrieve 时指定不同层返回不同结果
    w = lm.retrieve(MemoryTier.WORKING, "", key="x")
    e = lm.retrieve(MemoryTier.EPISODIC, "episodic_data", task_id="t1")
    s = lm.retrieve(MemoryTier.SEMANTIC, "semantic_data", agent_scope="Planner")
    assert len(w) == 1
    assert len(e) == 1
    assert len(s) == 1
    assert w[0].content != e[0].content
    assert e[0].content != s[0].content


def test_agent_scope_isolation():
    """不同 agent 的 working/episodic 隔离。"""
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.WORKING, "Planner working data", agent_scope="Planner", key="round1")
    lm.add(MemoryTier.WORKING, "Coder working data", agent_scope="Coder", key="round1")
    # Coder retrieve 不应拿到 Planner 的数据
    coder_wm = lm.retrieve(MemoryTier.WORKING, "", agent_scope="Coder", key="round1")
    assert len(coder_wm) == 1
    assert "Coder" in coder_wm[0].content
    # Planner retrieve 自己的
    planner_wm = lm.retrieve(MemoryTier.WORKING, "", agent_scope="Planner", key="round1")
    assert len(planner_wm) == 1
    assert "Planner" in planner_wm[0].content


def test_touch_updates_access_count():
    """每次 retrieve 增加 access_count。"""
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.SEMANTIC, "important fact", importance=0.8)
    e = lm.semantic.all_entries()[0]
    before = e.access_count
    lm.retrieve(MemoryTier.SEMANTIC, "important")
    assert e.access_count > before


def test_layered_memory_snapshot():
    """snapshot 返回所有层的条目数量。"""
    lm = LayeredMemorySystem()
    lm.add(MemoryTier.WORKING, "w", key="w1")
    lm.add(MemoryTier.EPISODIC, "e", task_id="t1")
    lm.add(MemoryTier.SEMANTIC, "s")
    snap = lm.snapshot(task_id="t1")
    assert snap["working"] > 0 or snap["episodic"] > 0 or snap["semantic"] > 0 or snap["procedural"] > 0
