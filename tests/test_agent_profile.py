"""AgentProfile + CapabilityRegistry 单元测试（docs/upgradePhaseTwo.md 测试要求 5-8）。

覆盖：
1. AgentProfile 构造与能力询问
2. CapabilityRegistry 注册与查找
3. find_workers 按能力集筛选
4. find_best_worker 评分排序
5. 指标记录影响评分
6. 默认 Profiles 注册
7. 工具权限策略隔离
"""
from __future__ import annotations

import pytest as _pytest

from app.multiagent.agent_profile import (
    AgentProfile,
    CapabilityRegistry,
    ModelPolicy,
    ToolPolicy,
    get_capability_registry,
    reset_capability_registry,
)


def _make_profile(
    pid: str,
    caps: set[str] | None = None,
    name: str | None = None,
) -> AgentProfile:
    return AgentProfile(
        id=pid,
        name=name or pid,
        role="worker",
        description=f"Profile {pid}",
        capabilities=caps or {"default"},
    )


# ===== 1. AgentProfile 基本 =====


def test_profile_has_capability():
    p = _make_profile("p1", caps={"coding", "file_write"})
    assert p.has_capability("coding")
    assert not p.has_capability("testing")


def test_profile_has_all():
    p = _make_profile("p1", caps={"coding", "file_write", "testing"})
    assert p.has_all_capabilities({"coding", "file_write"})
    assert not p.has_all_capabilities({"coding", "ai_planning"})


def test_profile_tool_policy_default_deny():
    p = AgentProfile(id="restricted", capabilities={"view_only"})
    assert p.tool_policy.deny_all_by_default is True
    assert p.tool_policy.allowed_tools == []


def test_profile_tool_policy_allowed():
    p = AgentProfile(
        id="coder",
        capabilities={"coding", "file_write"},
        tool_policy=ToolPolicy(
            allowed_tools=["create_file", "edit_file"],
            deny_all_by_default=True,
            allow_file_write=True,
        ),
    )
    assert "create_file" in p.tool_policy.allowed_tools
    assert p.tool_policy.allow_file_write is True
    assert p.tool_policy.allow_shell is False


# ===== 2. CapabilityRegistry 基础 =====


def test_register_and_find():
    reg = CapabilityRegistry()
    p1 = _make_profile("p1", caps={"coding"})
    reg.register(p1)
    assert reg.get_profile("p1") is p1
    assert reg.list_profiles() == [p1]


def test_find_workers_by_capability():
    reg = CapabilityRegistry()
    reg.register(_make_profile("coder1", caps={"coding", "file_write"}))
    reg.register(_make_profile("coder2", caps={"coding", "file_write"}))
    reg.register(_make_profile("tester1", caps={"testing", "file_read"}))

    workers = reg.find_workers({"coding"})
    assert len(workers) == 2
    assert {w.id for w in workers} == {"coder1", "coder2"}

    workers2 = reg.find_workers({"testing"})
    assert len(workers2) == 1
    assert workers2[0].id == "tester1"


def test_find_workers_intersection():
    reg = CapabilityRegistry()
    reg.register(_make_profile("all_rounder", caps={"coding", "testing", "file_write"}))
    reg.register(_make_profile("pure_coder", caps={"coding", "file_write"}))

    workers = reg.find_workers({"coding", "testing"})
    assert len(workers) == 1
    assert workers[0].id == "all_rounder"


def test_find_workers_empty_requirements():
    reg = CapabilityRegistry()
    reg.register(_make_profile("a"))
    reg.register(_make_profile("b"))
    # 空能力集 -> 返回全部
    workers = reg.find_workers(set())
    assert len(workers) == 2


def test_find_workers_no_match():
    reg = CapabilityRegistry()
    reg.register(_make_profile("a", caps={"coding"}))
    workers = reg.find_workers({"ai_planning"})
    assert workers == []


def test_unregister():
    reg = CapabilityRegistry()
    reg.register(_make_profile("p1", caps={"coding"}))
    reg.register(_make_profile("p2", caps={"testing"}))
    reg.unregister("p1")
    assert reg.get_profile("p1") is None
    assert len(reg.find_workers({"coding"})) == 0
    assert len(reg.find_workers({"testing"})) == 1


# ===== 3. 评分与负载 =====


def test_find_best_worker_prefers_low_load():
    reg = CapabilityRegistry()
    reg.register(_make_profile("busy", caps={"coding"}))
    reg.register(_make_profile("free", caps={"coding"}))

    # 模拟 busy 有负载
    reg.increment_load("busy")
    reg.increment_load("busy")

    best = reg.find_best_worker({"coding"})
    assert best is not None
    assert best.id == "free"


def test_success_rate_affects_score():
    reg = CapabilityRegistry()
    reg.register(_make_profile("reliable", caps={"coding"}))
    reg.register(_make_profile("unreliable", caps={"coding"}))

    # 记录多次成功/失败
    for _ in range(10):
        reg.record_success("reliable")
    for _ in range(10):
        reg.record_failure("unreliable")

    best = reg.find_best_worker({"coding"})
    assert best is not None
    assert best.id == "reliable"


# ===== 4. 默认 Profiles 注册 =====


def test_default_profiles_registered():
    reset_capability_registry()
    reg = get_capability_registry()
    profiles = reg.list_profiles()
    ids = {p.id for p in profiles}
    assert "planner" in ids
    assert "coder" in ids
    assert "tester" in ids
    assert "reviewer" in ids
    assert "researcher" in ids
    assert "finalizer" in ids


def test_default_coder_capabilities():
    reset_capability_registry()
    reg = get_capability_registry()
    coder = reg.get_profile("coder")
    assert coder is not None
    assert "coding" in coder.capabilities
    assert "file_write" in coder.capabilities
    assert "shell_execute" in coder.capabilities


def test_default_reviewer_readonly():
    reset_capability_registry()
    reg = get_capability_registry()
    reviewer = reg.get_profile("reviewer")
    assert reviewer is not None
    assert reviewer.tool_policy.allow_file_write is False
    assert reviewer.tool_policy.allow_shell is False
    assert "file_write" not in reviewer.capabilities
    assert "file_read" in reviewer.capabilities


def test_default_finalizer_capabilities():
    reset_capability_registry()
    reg = get_capability_registry()
    fin = reg.get_profile("finalizer")
    assert fin is not None
    assert "summarization" in fin.capabilities
    assert "file_write" in fin.capabilities
    # finalizer 不能 testing
    assert "testing" not in fin.capabilities


# ===== 5. Profile 更新覆盖旧索引 =====


def test_re_registration_updates_cap_index():
    reg = CapabilityRegistry()
    p = _make_profile("p1", caps={"coding"})
    reg.register(p)
    assert len(reg.find_workers({"coding"})) == 1

    # 更新能力
    p2 = _make_profile("p1", caps={"testing"})
    reg.register(p2)
    assert len(reg.find_workers({"coding"})) == 0
    assert len(reg.find_workers({"testing"})) == 1
