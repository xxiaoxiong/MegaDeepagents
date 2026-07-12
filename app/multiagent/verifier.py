"""Verifier：统一验证引擎（docs/upgradePhaseTwo.md §十二）。

设计分层：
1. **程序化验证**（ProgrammaticVerifier）：文件存在、JSON Schema、测试命令、构建命令、Lint、输出格式
2. **LLM Rubric 验证**（LLMRubricVerifier）：完整性、正确性、与目标一致性、证据充分度
3. **人工审批**（HumanApprovalTier）：高风险写操作、无法自动判断、预算超限、冲突无法处理

Verifier 是整个 TaskGraph 的最终判决节点——只有 Verifier 返回 pass，
Run/Graph 才能进入 COMPLETED。

与 Scheduler 的关系：
- Scheduler 认为 DAG 全 SUCCEEDED 后，转交 Verifier 做整体验证
- 若 Verifier 返回 repair，Scheduler 通过 add_repair_task 新增修复节点
- 若 Verifier 返回 replan，Planner 重新生成部分或全部 DAG
- 若 Verifier 返回 human_required，系统进入 HITL 等待
- 若 Verifier 返回 fail，Run 进入 FAILED
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Literal

from app.core.logging import logger


class Verdict(str, Enum):
    PASS = "pass"
    REPAIR = "repair"
    REPLAN = "replan"
    HUMAN_REQUIRED = "human_required"
    FAIL = "fail"


# ===== 数据模型 =====


@dataclass
class CriterionFailure:
    """一项验收标准失败。"""

    criterion: str
    detail: str
    severity: str = "medium"  # low / medium / high
    proposed_fix: str = ""


@dataclass
class EvidenceRef:
    """证据引用。"""

    source: str  # file_path / tool_output / message_id
    content: str = ""
    artifact_id: str | None = None


@dataclass
class TaskProposal:
    """Verifier 建议的新任务（用于 repair / replan 路径）。"""

    title: str
    objective: str
    required_capabilities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    priority: int = 5


@dataclass
class ValidationResult:
    """统一验证结果。"""

    verdict: Verdict
    scores: dict[str, float] = field(default_factory=dict)
    failed_criteria: list[CriterionFailure] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    proposed_tasks: list[TaskProposal] = field(default_factory=list)
    error: str | None = None
    summary: str = ""


# ===== 具体验证器 =====


class ProgrammaticVerifier:
    """程序化验证器：检查文件存在、命令执行、输出格式。"""

    def verify_file_exists(self, file_path: str) -> bool:
        return os.path.isfile(file_path)

    def verify_files_exist(self, file_paths: list[str]) -> ValidationResult:
        """批量检查文件是否存在。"""
        failed: list[CriterionFailure] = []
        passed = 0
        for fp in file_paths:
            if os.path.isfile(fp):
                passed += 1
            else:
                failed.append(CriterionFailure(
                    criterion=f"file_exists:{fp}",
                    detail=f"文件不存在: {fp}",
                    severity="high",
                ))
        total = len(file_paths)
        score = passed / total if total else 1.0
        return ValidationResult(
            verdict=Verdict.PASS if not failed else Verdict.REPAIR,
            scores={"file_exists": score},
            failed_criteria=failed,
            summary=f"{passed}/{total} 文件存在",
        )

    def verify_command(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int = 30,
        expected_returncode: int = 0,
    ) -> ValidationResult:
        """执行命令，检查返回码。"""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=cwd, timeout=timeout,
            )
            success = result.returncode == expected_returncode
            evidence = EvidenceRef(
                source=f"cmd:{command[:80]}",
                content=f"rc={result.returncode}, stdout={result.stdout[:200]}",
            )
            failed = []
            if not success:
                failed.append(CriterionFailure(
                    criterion=f"command:{command[:60]}",
                    detail=f"期望 rc={expected_returncode}，实际 rc={result.returncode}",
                    severity="high",
                ))
            return ValidationResult(
                verdict=Verdict.PASS if success else Verdict.REPAIR,
                scores={"command": 1.0 if success else 0.0},
                failed_criteria=failed,
                evidence=[evidence],
                summary=f"命令 {'成功' if success else '失败'}: {command[:60]}...",
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(
                verdict=Verdict.REPAIR,
                scores={"command": 0.0},
                failed_criteria=[CriterionFailure(
                    criterion=f"command_timeout:{command[:60]}",
                    detail=f"超时 {timeout}s",
                    severity="medium",
                )],
                summary=f"命令超时: {command[:60]}...",
            )
        except Exception as exc:
            return ValidationResult(
                verdict=Verdict.FAIL,
                error=str(exc),
                summary=f"命令异常: {exc}",
            )

    def verify_json_schema(self, data: dict, schema: dict) -> ValidationResult:
        """检查 JSON 数据是否符合 schema。"""
        try:
            import jsonschema
            jsonschema.validate(data, schema)
            return ValidationResult(
                verdict=Verdict.PASS,
                scores={"json_schema": 1.0},
                summary="JSON Schema 验证通过",
            )
        except Exception as exc:
            return ValidationResult(
                verdict=Verdict.REPAIR,
                scores={"json_schema": 0.0},
                failed_criteria=[CriterionFailure(
                    criterion="json_schema",
                    detail=str(exc),
                    severity="medium",
                )],
                summary=f"JSON Schema 验证失败: {exc}",
            )

    def verify_output_format(self, content: str, required_format: str) -> ValidationResult:
        """检查输出格式是否符合要求（简单格式校验）。"""
        if required_format == "json":
            import json
            try:
                json.loads(content)
                return ValidationResult(verdict=Verdict.PASS, scores={"format": 1.0})
            except json.JSONDecodeError as exc:
                return ValidationResult(
                    verdict=Verdict.REPAIR,
                    scores={"format": 0.0},
                    failed_criteria=[CriterionFailure("json_format", str(exc))],
                )
        elif required_format == "non_empty":
            ok = bool(content and content.strip())
            return ValidationResult(
                verdict=Verdict.PASS if ok else Verdict.REPAIR,
                scores={"format": 1.0 if ok else 0.0},
            )
        return ValidationResult(verdict=Verdict.PASS, scores={"format": 1.0})


class LLMRubricVerifier:
    """LLM Rubric 验证器：使用 LLM 评估质量指标。

    仅当 model_available=True 时启用；否则回退到基于规则的基本评分。

    无状态设计：所有评估所需的 (goal, artifacts) 都通过 verify() 入参传入，
    _call_rubric_llm 失败时把同一份入参传给 _fallback_verify，保证
    fallback 评分时有真实产物可看——避免历史上"prompt 当 goal、artifacts={}"
    造成的假 REPAIR。
    """

    def __init__(self, model_available: bool = True) -> None:
        self._model_available = model_available

    def verify(
        self,
        goal: str,
        artifacts: dict[str, dict[str, Any]],
        rubrics: list[str] | None = None,
    ) -> ValidationResult:
        """使用 LLM 按 rubric 进行评估。

        Args:
            goal: 原始任务目标
            artifacts: {artifact_path: {content, ...}} 产物字典
            rubrics: 评估维度列表，默认 ["completeness", "correctness", "consistency", "evidence"]

        Returns:
            ValidationResult
        """
        if not self._model_available:
            return self._fallback_verify(goal, artifacts)

        rubrics = rubrics or ["completeness", "correctness", "consistency", "evidence"]
        try:
            prompt = self._build_rubric_prompt(goal, artifacts, rubrics)
            return self._call_rubric_llm(prompt, goal, artifacts)
        except Exception as exc:
            logger.warning(f"[LLMRubricVerifier] LLM 调用失败，回退规则评分: {exc}")
            return self._fallback_verify(goal, artifacts)

    def _call_rubric_llm(
        self, prompt: str, goal: str, artifacts: dict[str, dict[str, Any]],
    ) -> ValidationResult:
        """调用 LLM 实现 rubric 验证。

        LLM 调用本身失败时（网络/解析异常），用真实 (goal, artifacts) 走
        _fallback_verify，而非历史上的 (prompt, {})，避免假 REPAIR。
        """
        try:
            from app.llm_factory import build_model
            # 使用低成本模型做评估
            llm = build_model()
            response = llm.invoke([
                ("system", "你是一个严格的验证评估员。评估后输出 JSON，格式为："
                          "{'scores': {'completeness': 0~1, ...}, "
                          "'failed_criteria': [{'criterion':..., 'detail':..., 'severity':...}], "
                          "'verdict': 'pass'|'repair'|'replan'|'human_required'|'fail', "
                          "'summary': '...'}。"),
                ("user", prompt),
            ])
            text = getattr(response, "content", str(response))
            import json
            parsed = json.loads(text) if isinstance(text, str) else text
        except Exception as exc:
            logger.warning(f"[LLMRubricVerifier] LLM rubric 调用失败: {exc}")
            # 关键修复：传真实 (goal, artifacts) 而非 (prompt, {})
            return self._fallback_verify(goal, artifacts)

        failed = [
            CriterionFailure(c=c["criterion"], d=c["detail"], s=c.get("severity", "medium"))
            for c in parsed.get("failed_criteria", [])
        ]
        verdict_str = parsed.get("verdict", "repair")
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.REPAIR
        return ValidationResult(
            verdict=verdict,
            scores=parsed.get("scores", {}),
            failed_criteria=failed,
            summary=parsed.get("summary", "LLM rubric 验证完成"),
        )

    def _build_rubric_prompt(
        self,
        goal: str,
        artifacts: dict[str, dict[str, Any]],
        rubrics: list[str],
    ) -> str:
        lines = [f"目标: {goal}"]
        for path, info in artifacts.items():
            content = info.get("content", "") or info.get("content_preview", "")
            lines.append(f"\n--- 产物: {path} ---\n{content[:500]}")
        lines.append(f"\n评估维度: {', '.join(rubrics)}")
        return "\n".join(lines)

    def _fallback_verify(
        self,
        goal: str,
        artifacts: dict[str, dict[str, Any]],
    ) -> ValidationResult:
        """LLM/模型不可用时的基于规则回退验证。"""
        if not artifacts:
            return ValidationResult(
                verdict=Verdict.FAIL,
                scores={"completeness": 0.0, "correctness": 0.0, "consistency": 0.0, "evidence": 0.0},
                failed_criteria=[CriterionFailure("no_artifacts", "无产物供评估", severity="high")],
                summary="无产物，验证失败",
            )
        # 基础规则
        failed: list[CriterionFailure] = []
        scores: dict[str, float] = {}
        # 完整性：每个产物非空
        empty = sum(1 for v in artifacts.values() if not v.get("content") and not v.get("content_preview"))
        non_empty = len(artifacts) - empty
        completeness = non_empty / len(artifacts) if artifacts else 0.0
        scores["completeness"] = completeness
        if empty:
            failed.append(CriterionFailure(
                "artifacts_empty", f"{empty}/{len(artifacts)} 个产物为空", severity="medium"
            ))
        # 默认指标
        scores["correctness"] = 0.5
        scores["consistency"] = 0.7
        scores["evidence"] = 0.3 if not artifacts else 0.6
        verdict = Verdict.REPAIR if failed else Verdict.PASS
        return ValidationResult(
            verdict=verdict,
            scores=scores,
            failed_criteria=failed,
            summary=f"规则验证: {non_empty}/{len(artifacts)} 产物非空",
        )


# ===== 顶层 Verifier =====


class Verifier:
    """统一验证器：组合三个层级。"""

    def __init__(
        self,
        programmatic: ProgrammaticVerifier | None = None,
        llm_rubric: LLMRubricVerifier | None = None,
        human_approval: Any | None = None,
    ):
        self.programmatic = programmatic or ProgrammaticVerifier()
        self.llm_rubric = llm_rubric or LLMRubricVerifier(model_available=True)
        self.human_approval = human_approval  # 预留：未来接入 HITL

    def validate(
        self,
        goal: str,
        artifacts: dict[str, dict[str, Any]],
        checks: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """执行全部三层验证。

        Args:
            goal: 原始目标
            artifacts: {artifact_path: {content/content_preview, ...}}
            checks: 额外程序化检查项，如 {"files": [...], "command": "...", "json_schema": {...}}

        Returns:
            最终验证结果（取各层最差 verdict）
        """
        all_results: list[ValidationResult] = []

        # 1. 程序化验证
        checks = checks or {}
        if checks:
            if "files" in checks:
                all_results.append(self.programmatic.verify_files_exist(checks["files"]))
            if "command" in checks:
                all_results.append(self.programmatic.verify_command(
                    checks["command"],
                    cwd=checks.get("cwd"),
                    timeout=checks.get("timeout", 30),
                ))
            if "json_schema" in checks and "data" in checks:
                all_results.append(self.programmatic.verify_json_schema(
                    checks["data"], checks["json_schema"]
                ))
            if "format" in checks:
                content = checks.get("content", "")
                all_results.append(self.programmatic.verify_output_format(content, checks["format"]))

        # 2. LLM Rubric
        rubric_result = self.llm_rubric.verify(goal, artifacts, checks.get("rubrics"))
        all_results.append(rubric_result)

        # 3. 合成
        return self._merge_results(all_results)

    def _merge_results(self, results: list[ValidationResult]) -> ValidationResult:
        """合并多个验证结果：取最严格 verdict + 累计失败准则。"""
        if not results:
            return ValidationResult(verdict=Verdict.PASS, summary="无验证项")

        # verdict 等级（从严格到宽松）：FAIL > HUMAN_REQUIRED > REPLAN > REPAIR > PASS
        verdict_rank = {
            Verdict.PASS: 0,
            Verdict.REPAIR: 1,
            Verdict.REPLAN: 2,
            Verdict.HUMAN_REQUIRED: 3,
            Verdict.FAIL: 4,
        }

        merged_scores: dict[str, list[float]] = defaultdict(list)
        all_failed: list[CriterionFailure] = []
        all_evidence: list[EvidenceRef] = []
        all_proposals: list[TaskProposal] = []
        worst_verdict = Verdict.PASS
        errors: list[str] = []

        for r in results:
            for k, v in r.scores.items():
                merged_scores[k].append(v)
            all_failed.extend(r.failed_criteria)
            all_evidence.extend(r.evidence)
            all_proposals.extend(r.proposed_tasks)
            if verdict_rank.get(r.verdict, 0) > verdict_rank.get(worst_verdict, 0):
                worst_verdict = r.verdict
            if r.error:
                errors.append(r.error)

        avg_scores = {
            k: sum(v) / len(v) for k, v in merged_scores.items()
        }

        return ValidationResult(
            verdict=worst_verdict,
            scores=avg_scores,
            failed_criteria=all_failed,
            evidence=all_evidence,
            proposed_tasks=all_proposals,
            summary=f"验证完成: {worst_verdict.value} ({len(all_failed)} 项失败)",
            error="; ".join(errors) if errors else None,
        )
