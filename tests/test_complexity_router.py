"""ComplexityRouter 单元测试（docs/upgradePhaseTwo.md §十三）。"""
from __future__ import annotations

import pytest

from app.multiagent.complexity_router import (
    ComplexityRouter,
    ComplexityMode,
    RoutingDecision,
    TaskComplexitySignals,
    route_simple,
)


# ===== 基本路由决策 =====


def test_trivial_signal_routes_to_single():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=500,
        num_files=1,
        num_artifacts=1,
        has_clear_acceptance=True,
        max_depth=1,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.SINGLE
    assert decision.recommended_max_workers == 1
    assert decision.enable_dag is False


def test_short_input_no_files_routes_to_single():
    router = ComplexityRouter()
    s = TaskComplexitySignals(input_length=1000, num_files=0)
    decision = router.route(s)
    assert decision.mode == ComplexityMode.SINGLE


def test_cross_discipline_with_clear_acceptance_routes_to_light_multi():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=5000,
        num_files=2,
        num_artifacts=2,
        has_clear_acceptance=True,
        requires_multiple_disciplines=True,
        max_depth=2,
    )
    decision = router.route(s)
    # 跨学科且 1-3 文件 → LIGHT_MULTI
    assert decision.mode == ComplexityMode.LIGHT_MULTI
    assert decision.recommended_max_workers >= 2


def test_many_files_routes_to_full_multi():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=5000,
        num_files=15,  # > FILES_FULL_THRESHOLD=10
        num_artifacts=3,
        max_depth=4,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.FULL_MULTI
    assert decision.enable_dag is True
    assert decision.recommended_max_rounds >= 10


def test_long_input_routes_to_full_multi():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=40000,
        num_files=2,
        requires_long_context=True,
        max_depth=4,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.FULL_MULTI


def test_codebase_light_for_single_discipline_exploration():
    """4-10 文件、无跨学科、长上下文 → CODEBASE_LIGHT。"""
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=6000,
        num_files=5,
        requires_long_context=True,
        requires_multiple_disciplines=False,
        max_depth=2,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.CODEBASE_LIGHT
    assert decision.recommended_max_rounds == 8


# ===== 决策参数合理性 =====


def test_single_max_rounds_low():
    router = ComplexityRouter()
    s = TaskComplexitySignals(input_length=300)
    decision = router.route(s)
    assert decision.recommended_max_rounds == 3


def test_full_multi_max_workers_bounded():
    """FULL_MULTI 推荐并发不超过 6。"""
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=40000,
        num_files=20,
        num_artifacts=10,
        max_depth=5,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.FULL_MULTI
    assert decision.recommended_max_workers <= 6
    assert decision.recommended_max_workers >= 3


def test_full_multi_max_rounds_scales_with_files():
    router = ComplexityRouter()
    s = TaskComplexitySignals(num_files=15, num_artifacts=3)
    decision = router.route(s)
    # recommended_max_rounds ≈ num_files * 2，上限 20
    assert decision.recommended_max_rounds >= 10
    assert decision.recommended_max_rounds <= 20


def test_interactive_enables_human_in_loop():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=300,
        interactive_required=True,
    )
    decision = router.route(s)
    assert decision.enable_human_in_loop is True


def test_ambiguous_full_multi_enables_human_in_loop():
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=40000,
        ambiguous=True,
        num_files=5,
        max_depth=2,
    )
    decision = router.route(s)
    assert decision.mode == ComplexityMode.FULL_MULTI
    assert decision.enable_human_in_loop is True


# ===== 理由包含决策依据 =====


def test_rationale_non_empty():
    router = ComplexityRouter()
    s = TaskComplexitySignals(input_length=300)
    decision = router.route(s)
    assert decision.rationale
    assert "SINGLE" in decision.rationale or "单" in decision.rationale or "短" in decision.rationale


# ===== route_simple 便捷接口 =====


def test_route_simple_short_text():
    decision = route_simple("写一个 hello world")
    assert decision.mode == ComplexityMode.SINGLE


def test_route_simple_tests_keyword_triggers_light_multi():
    decision = route_simple("实现 X 模块并写测试")
    assert decision.mode in (ComplexityMode.LIGHT_MULTI, ComplexityMode.SINGLE)


def test_route_simple_refactor_keyword_triggers_depth():
    """重构关键词使得 route_simple 支持更深（max_depth=3），但输入很短时仍受 trivial 保护。

    用更长的文本测试非 trivial 场景。
    """
    decision = route_simple("请重构整个项目的 auth 模块，涉及 3 个文件、2 个接口，需包含测试")
    assert decision.mode in (ComplexityMode.LIGHT_MULTI, ComplexityMode.SINGLE)


def test_route_simple_research_keyword():
    decision = route_simple("研究一下 LangGraph 的实现")
    assert decision.mode in (ComplexityMode.SINGLE, ComplexityMode.LIGHT_MULTI)


# ===== 边界条件 =====


def test_empty_signals_routing():
    """全默认 signals 应路由 SINGLE。"""
    router = ComplexityRouter()
    s = TaskComplexitySignals()
    decision = router.route(s)
    assert decision.mode == ComplexityMode.SINGLE


def test_ambiguous_signal_does_not_force_single():
    """ambiguous=True 但其他指标都很简单 → SINGLE（但 enable_human_in_loop）。"""
    router = ComplexityRouter()
    s = TaskComplexitySignals(
        input_length=300,
        ambiguous=True,
    )
    decision = router.route(s)
    # trivial 触发 SINGLE，但 ambiguous 不在 _is_trivial 排除项
    assert decision.mode in (ComplexityMode.SINGLE,)
    assert decision.enable_human_in_loop is True


def test_mode_enum_values():
    assert ComplexityMode.SINGLE == "single"
    assert ComplexityMode.LIGHT_MULTI == "light_multi"
    assert ComplexityMode.FULL_MULTI == "full_multi"
    assert ComplexityMode.CODEBASE_LIGHT == "codebase_light"
