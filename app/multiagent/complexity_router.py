"""Complexity Router：根据任务特征选择 single-agent 或 multi-agent 模式。

requirements（docs/upgradePhaseTwo.md §十三）：

支持模式：
1. **SINGLE**: 简单任务，单 Agent 串行完成
2. **LIGHT_MULTI**: 轻度多 Agent；如 plan → code → test 三步
3. **FULL_MULTI**: 完整多 Agent；构建任务依赖图 DAG 跑并行
4. **CODEBASE_LIGHT**: 代码形态 exploratory；单 Agent + 局部增强（subagent/spawn）

判别维度：
- 长度（input 长度上限 8000 chars）
- 模糊性（明确 vs 开放）
- 依赖数（输入文件数）
- 验收标准可量化度
- 上下文读取量（>10 文件 → CODE 多步）
- 输出制品数
- interactive_step（HITL 中断）
- agent_profile 的特殊需求（如 multi_repair）

设计原则：
- Router 决策仅基于简单规则（>LLM），不引入新 LLM 调用延迟。
- 输出包含模式 + 理由 + 推荐参数（max_rounds / max_workers 等）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.logging import logger


class ComplexityMode(str, Enum):
    """任务复杂度模式。"""

    SINGLE = "single"
    LIGHT_MULTI = "light_multi"
    FULL_MULTI = "full_multi"
    CODEBASE_LIGHT = "codebase_light"


@dataclass
class TaskComplexitySignals:
    """任务复杂度信号（Router 输入）。

    每个信号都有默认值，方便外部按需构造；未提供时按默认规则推算。
    """

    input_length: int = 0
    num_files: int = 0  # 涉及的文件数 / deps
    num_artifacts: int = 1  # 输出制品数
    has_clear_acceptance: bool = False
    requires_multiple_disciplines: bool = False  # 跨多个学科（代码+测试+文档）
    requires_external_research: bool = False
    requires_long_context: bool = False  # >8k token 单文件？
    interactive_required: bool = False  # 需要 HITL
    max_depth: int = 1  # 任务潜在递归深度
    ambiguous: bool = False  # 描述含糊 / 多解


@dataclass
class RoutingDecision:
    """Router 的输出决策。"""

    mode: ComplexityMode
    rationale: str
    recommended_max_rounds: int = 5
    recommended_max_workers: int = 1
    recommended_max_artifacts: int = 1
    enable_dag: bool = False
    enable_verifier: bool = True
    enable_human_in_loop: bool = False
    notes: list[str] = field(default_factory=list)


class ComplexityRouter:
    """根据 TaskComplexitySignals 选择复杂度模式。"""

    # 阈值
    INPUT_LENGTH_LIGHT_THRESHOLD = 8000  # chars
    INPUT_LENGTH_FULL_THRESHOLD = 30000
    FILES_LIGHT_THRESHOLD = 3
    FILES_FULL_THRESHOLD = 10
    ARTIFACTS_LIGHT_THRESHOLD = 2
    ARTIFACTS_FULL_THRESHOLD = 5

    def route(self, signals: TaskComplexitySignals) -> RoutingDecision:
        """路由决策。

        决策优先级（按触发强度递增）：
        1. 大量文件 / 多学科 / 多 artifacts / 长输入 → FULL_MULTI
        2. 中等复杂度（少量依赖、无需并行）→ LIGHT_MULTI
        3. 代码探索类（<10 文件、需多次 read）→ CODEBASE_LIGHT
        4. 默认 → SINGLE
        """
        scores = self._compute_mode_scores(signals)
        # 选最高分模式
        best_mode = max(scores, key=scores.get)
        best_score = scores[best_mode]

        # 安全回退：极简单任务（输入短、无依赖、明确验收）→ SINGLE
        if signals._is_trivial():
            best_mode = ComplexityMode.SINGLE

        decision = self._build_decision(best_mode, signals, scores)
        decision.rationale = self._rationale(best_mode, signals, scores)
        return decision

    def _compute_mode_scores(self, s: TaskComplexitySignals) -> dict[ComplexityMode, float]:
        """对每种模式打分（0~N）。"""
        scores: dict[ComplexityMode, float] = {
            ComplexityMode.SINGLE: 0.0,
            ComplexityMode.LIGHT_MULTI: 0.0,
            ComplexityMode.FULL_MULTI: 0.0,
            ComplexityMode.CODEBASE_LIGHT: 0.0,
        }

        # SINGLE：简单任务
        if s.input_length < self.INPUT_LENGTH_LIGHT_THRESHOLD and s.num_files <= 1:
            scores[ComplexityMode.SINGLE] += 2.0
        if s.has_clear_acceptance and not s.requires_multiple_disciplines:
            scores[ComplexityMode.SINGLE] += 1.0
        if s.num_artifacts <= 1 and s.max_depth == 1:
            scores[ComplexityMode.SINGLE] += 1.0

        # LIGHT_MULTI：轻度多步
        if (
            self.FILES_LIGHT_THRESHOLD >= s.num_files > 1
            or (s.num_artifacts >= 1 and s.requires_multiple_disciplines)
        ):
            scores[ComplexityMode.LIGHT_MULTI] += 2.0
        if 1 < s.max_depth <= 3:
            scores[ComplexityMode.LIGHT_MULTI] += 2.0
        if s.has_clear_acceptance and s.requires_multiple_disciplines:
            scores[ComplexityMode.LIGHT_MULTI] += 1.0

        # FULL_MULTI：重度多 Agent
        if s.num_files > self.FILES_FULL_THRESHOLD:
            scores[ComplexityMode.FULL_MULTI] += 3.0
        if s.input_length > self.INPUT_LENGTH_FULL_THRESHOLD:
            scores[ComplexityMode.FULL_MULTI] += 3.0
        if s.num_artifacts > self.ARTIFACTS_FULL_THRESHOLD:
            scores[ComplexityMode.FULL_MULTI] += 2.0
        if s.max_depth > 3:
            scores[ComplexityMode.FULL_MULTI] += 2.0
        if s.requires_external_research and s.requires_multiple_disciplines:
            scores[ComplexityMode.FULL_MULTI] += 1.0

        # CODEBASE_LIGHT：中等代码探索
        if 3 < s.num_files <= self.FILES_FULL_THRESHOLD and not s.requires_multiple_disciplines:
            scores[ComplexityMode.CODEBASE_LIGHT] += 2.0
        if s.requires_long_context and not s.requires_multiple_disciplines:
            scores[ComplexityMode.CODEBASE_LIGHT] += 1.0

        return scores

    def _build_decision(
        self,
        mode: ComplexityMode,
        s: TaskComplexitySignals,
        scores: dict[ComplexityMode, float],
    ) -> RoutingDecision:
        # interactive 即使在 SINGLE 模式也要启用 HITL
        interactive = s.interactive_required or (s.ambiguous and not s._is_trivial())
        if mode == ComplexityMode.SINGLE:
            return RoutingDecision(
                mode=mode,
                rationale="",
                recommended_max_rounds=3,
                recommended_max_workers=1,
                enable_dag=False,
                enable_verifier=True,
                enable_human_in_loop=interactive,
            )
        if mode == ComplexityMode.LIGHT_MULTI:
            return RoutingDecision(
                mode=mode,
                rationale="",
                recommended_max_rounds=6,
                recommended_max_workers=min(3, max(2, s.num_artifacts)),
                enable_dag=False,  # 串行 DAG 拓扑
                enable_verifier=True,
                enable_human_in_loop=s.interactive_required,
            )
        if mode == ComplexityMode.CODEBASE_LIGHT:
            return RoutingDecision(
                mode=mode,
                rationale="",
                recommended_max_rounds=8,
                recommended_max_workers=2,
                enable_dag=False,
                enable_verifier=True,
                enable_human_in_loop=False,
            )
        # FULL_MULTI
        return RoutingDecision(
            mode=mode,
            rationale="",
            recommended_max_rounds=min(20, max(10, s.num_files * 2)),
            recommended_max_workers=min(6, max(3, s.num_artifacts)),
            recommended_max_artifacts=s.num_artifacts,
            enable_dag=True,
            enable_verifier=True,
            enable_human_in_loop=s.interactive_required or s.ambiguous,
        )

    def _rationale(
        self,
        mode: ComplexityMode,
        s: TaskComplexitySignals,
        scores: dict[ComplexityMode, float],
    ) -> str:
        if mode == ComplexityMode.SINGLE:
            return (
                f"输入短({s.input_length}c) / {s.num_files} 文件 / "
                f"明确验收 → 单 Agent 串行即可"
            )
        if mode == ComplexityMode.LIGHT_MULTI:
            return (
                f"{s.num_files} 文件 / {s.num_artifacts} 制品 / "
                f"跨学科({s.requires_multiple_disciplines}) → 轻度多 Agent"
            )
        if mode == ComplexityMode.CODEBASE_LIGHT:
            return (
                f"{s.num_files} 文件需要探索 / 单学科 / 长上下文 "
                f"({s.requires_long_context}) → 单 Agent + 强化探索"
            )
        return (
            f"重负载: {s.num_files} 文件 / {s.num_artifacts} 制品 / "
            f"输入 {s.input_length}c / 深度 {s.max_depth} → 完整多 Agent + DAG"
        )


# 为 TaskComplexitySignals 添加辅助方法（在原 dataclass 外修补，避免影响 dataclass 装饰器）
def _signals_is_trivial(self: TaskComplexitySignals) -> bool:
    """是否为「极简单」任务——硬性 SINGLE 触发条件。"""
    return (
        self.input_length < 2000
        and self.num_files <= 1
        and self.num_artifacts <= 1
        and not self.requires_multiple_disciplines
        and not self.requires_external_research
        and self.max_depth <= 1
        and not self.ambiguous
        and not self.interactive_required
    )


TaskComplexitySignals._is_trivial = _signals_is_trivial  # type: ignore[attr-defined]


def route_simple(input_text: str) -> RoutingDecision:
    """简单便捷接口：用文本长度推算 signals 后路由。

    Args:
        input_text: 任务描述或上下文

    Returns:
        RoutingDecision
    """
    signals = TaskComplexitySignals(
        input_length=len(input_text),
        num_files=0,
        num_artifacts=1,
        has_clear_acceptance=False,
        max_depth=1,
    )
    # 基于 input_text 启发：含 "test" / "build" / "refactor" 等字眼调高复杂度
    lower_text = input_text.lower()
    if any(k in lower_text for k in ["test", "测试", "verify", "验证"]):
        signals.requires_multiple_disciplines = True
    if any(k in lower_text for k in ["refactor", "重写", "重构", "migrate", "迁移"]):
        signals.max_depth = 3
        signals.num_artifacts = 2
    if any(k in lower_text for k in ["研究", "research", "调研"]):
        signals.requires_external_research = True
    if any(k in lower_text for k in ["讨论", "discuss", "确认", "review"]):
        signals.interactive_required = True
    if len(input_text) > ComplexityRouter.INPUT_LENGTH_FULL_THRESHOLD:
        signals.requires_long_context = True

    router = ComplexityRouter()
    return router.route(signals)
