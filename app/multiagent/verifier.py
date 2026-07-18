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
import shlex
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
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
    affected_files: list[str] = field(default_factory=list)


@dataclass
class EvidenceRef:
    """证据引用。"""

    source: str  # file_path / tool_output / message_id
    content: str = ""
    artifact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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


@dataclass
class VerificationCommand:
    kind: str
    argv: list[str]
    cwd: str | None = None
    timeout: int = 120
    expected_returncode: int = 0


@dataclass
class VerificationPlan:
    """Executable verification contract compiled from planner output."""

    required_files: list[str] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)
    json_schema: dict[str, Any] | None = None
    json_data: Any | None = None
    expected_output_format: str | None = None
    commands: list[VerificationCommand] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    semantic_rubric: list[str] = field(default_factory=list)
    diff_scope: list[str] = field(default_factory=list)
    forbidden_changes: list[str] = field(default_factory=list)
    workspace_root: str | None = None

    @classmethod
    def from_output_contract(cls, contract: Any, workspace_root: str | None = None) -> "VerificationPlan":
        plan = cls(workspace_root=workspace_root)
        for required in getattr(contract, "required_artifacts", []) or []:
            # Artifact roles such as "repair_patch" are semantic.  Values
            # that look like real paths become programmatic file checks.
            if "/" in required or "\\" in required or "." in Path(required).name:
                plan.required_files.append(
                    str(Path(workspace_root, required)) if workspace_root and not Path(required).is_absolute()
                    else required
                )
            else:
                plan.semantic_rubric.append(f"required artifact role: {required}")
        for criterion in getattr(contract, "acceptance_criteria", []) or []:
            plan.acceptance_criteria.append(criterion)
            prefix, separator, value = criterion.partition(":")
            kind = prefix.strip().lower()
            if separator and kind in {"test", "lint", "type-check", "typecheck", "build", "security", "command"}:
                argv = shlex.split(value.strip(), posix=os.name != "nt")
                if argv:
                    plan.commands.append(VerificationCommand(kind=kind, argv=argv,
                                                             cwd=workspace_root))
            else:
                plan.semantic_rubric.append(criterion)
        return plan

    def to_checks(self) -> dict[str, Any]:
        return {
            "verification_plan": self,
            "files": self.required_files,
            "file_hashes": self.file_hashes,
            "json_schema": self.json_schema,
            "data": self.json_data,
            "format": self.expected_output_format,
            "commands": self.commands,
            "acceptance_criteria": self.acceptance_criteria,
            "rubrics": self.semantic_rubric,
            "diff_scope": self.diff_scope,
            "forbidden_changes": self.forbidden_changes,
            "cwd": self.workspace_root,
        }


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
        command: str | list[str],
        cwd: str | None = None,
        timeout: int = 30,
        expected_returncode: int = 0,
    ) -> ValidationResult:
        """执行命令，检查返回码。"""
        try:
            from app.multiagent.shell_policy import ShellCommandRunner
            argv = shlex.split(command, posix=os.name != "nt") if isinstance(command, str) else command
            result = ShellCommandRunner().run(argv, cwd=cwd or os.getcwd(), timeout=timeout)
            success = result.returncode == expected_returncode
            evidence = EvidenceRef(
                source=f"cmd:{' '.join(argv)[:80]}",
                content=f"rc={result.returncode}\nstdout={result.stdout[:2000]}\nstderr={result.stderr[:1000]}",
                metadata={"argv": argv, "returncode": result.returncode,
                          "stdout": result.stdout, "stderr": result.stderr,
                          "environment": result.environment,
                          "duration_seconds": result.duration_seconds},
            )
            failed = []
            if not success:
                failed.append(CriterionFailure(
                    criterion=f"command:{' '.join(argv)[:60]}",
                    detail=f"期望 rc={expected_returncode}，实际 rc={result.returncode}",
                    severity="high",
                ))
            return ValidationResult(
                verdict=Verdict.PASS if success else Verdict.REPAIR,
                scores={"command": 1.0 if success else 0.0},
                failed_criteria=failed,
                evidence=[evidence],
                summary=f"命令 {'成功' if success else '失败'}: {' '.join(argv)[:60]}...",
            )
        except Exception as exc:
            return ValidationResult(
                verdict=Verdict.REPAIR,
                scores={"command": 0.0},
                failed_criteria=[CriterionFailure(
                    criterion="command_execution", detail=str(exc), severity="high",
                    proposed_fix="use structured argv or request required permission",
                )],
                error=str(exc),
                summary=f"命令异常: {exc}",
            )

    def verify_hashes(self, expected: dict[str, str]) -> ValidationResult:
        from app.multiagent.artifact import compute_content_hash
        failures: list[CriterionFailure] = []
        for path, expected_hash in expected.items():
            candidate = Path(path)
            if not candidate.is_file():
                failures.append(CriterionFailure("file_hash", f"file missing: {path}",
                                                 "high", affected_files=[path]))
                continue
            actual = compute_content_hash(candidate.read_bytes())
            if actual != expected_hash:
                failures.append(CriterionFailure("file_hash", f"hash mismatch: {path}",
                                                 "high", affected_files=[path]))
        return ValidationResult(verdict=Verdict.PASS if not failures else Verdict.REPAIR,
                                failed_criteria=failures,
                                scores={"file_hash": 1.0 if not failures else 0.0})

    def verify_forbidden_changes(self, changed_files: list[str], forbidden: list[str],
                                 allowed_scope: list[str] | None = None) -> ValidationResult:
        import fnmatch
        violations = [path for path in changed_files
                      if any(fnmatch.fnmatch(path, pattern) for pattern in forbidden)]
        if allowed_scope:
            violations.extend(path for path in changed_files
                              if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed_scope))
        violations = sorted(set(violations))
        failures = [CriterionFailure("forbidden_changes", "change outside allowed diff scope",
                                     "high", affected_files=violations)] if violations else []
        return ValidationResult(verdict=Verdict.REPAIR if failures else Verdict.PASS,
                                failed_criteria=failures,
                                scores={"diff_scope": 0.0 if failures else 1.0})

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

    def __init__(self, model_available: bool = True, fail_closed: bool = True) -> None:
        self._model_available = model_available
        self._fail_closed = fail_closed

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
            CriterionFailure(criterion=c["criterion"], detail=c["detail"], severity=c.get("severity", "medium"))
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
        """Fail closed when semantic correctness cannot be established."""
        if not artifacts:
            return ValidationResult(
                verdict=Verdict.FAIL,
                scores={"completeness": 0.0, "correctness": 0.0, "consistency": 0.0, "evidence": 0.0},
                failed_criteria=[CriterionFailure("no_artifacts", "无产物供评估", severity="high")],
                summary="无产物，验证失败",
            )
        # Basic inspection can prove incompleteness, never semantic
        # correctness.  Non-empty wrong code therefore remains REPAIR.
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
        if self._fail_closed:
            failed.append(CriterionFailure(
                "semantic_verifier_unavailable",
                "LLM rubric verifier is unavailable; non-empty output is not correctness evidence",
                severity="high", proposed_fix="restore verifier model or provide programmatic test/reviewer evidence",
            ))
            verdict = Verdict.REPAIR
        else:
            # Explicit legacy-only mode for old deterministic harnesses.  The
            # TASK_TEAM facade never enables this.
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
        artifact_store: Any | None = None,
    ):
        self.programmatic = programmatic or ProgrammaticVerifier()
        self.llm_rubric = llm_rubric or LLMRubricVerifier(model_available=True)
        self.human_approval = human_approval  # 预留：未来接入 HITL
        # Phase Two #16: 接入 ArtifactStore，让 Verifier 能读到注册表里的真实
        # artifact 元数据与文件内容（content_hash / size / produced_by / version）。
        self.artifact_store = artifact_store

    def _enrich_with_artifact_store(
        self, artifacts: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """用注册表里的 Artifact 元数据 + 真实文件内容补全 artifacts 字典。

        artifacts 入参通常是 orchestrator 拼出来的 {artifact_key: {content_preview, status}}
        它只覆盖了"workspace 里看到的文件预览"。注册表里还可能存了 schema 化的
        content_hash / size / produced_by / version，这些对 LLM Rubric 评分有用。
        本方法以 artifact_id 形式补 entry，不破坏已有 key。
        """
        if self.artifact_store is None:
            return artifacts
        try:
            # 取所有 artifact 元数据
            registry = getattr(self.artifact_store, "_registry", {})
            # Per-task verification passes explicit artifact ids.  In that
            # case never leak unrelated artifacts from the same run into the
            # evidence set.  Run-level verification (path-like keys only)
            # may still enrich from the complete registry.
            requested_ids = {
                str(info.get("artifact_id") or key.removeprefix("artifact:"))
                for key, info in artifacts.items()
                if info.get("artifact_id") or key.startswith("artifact:") or key in registry
            }
            for aid, art in registry.items():
                if requested_ids and aid not in requested_ids:
                    continue
                key = f"artifact:{aid}"
                entry = artifacts.get(key, {"status": "registered"})
                # 注入注册表元数据
                entry["artifact_id"] = aid
                entry["type"] = getattr(art, "type", "")
                if hasattr(art, "type") and hasattr(art.type, "value"):
                    entry["type"] = art.type.value
                entry["content_hash"] = getattr(art, "content_hash", "")
                entry["size_bytes"] = getattr(art, "size_bytes", 0)
                entry["version"] = getattr(art, "version", 1)
                entry["produced_by"] = getattr(art, "produced_by", "")
                entry["path"] = getattr(art, "path", "")
                # 尝试读真实文件内容
                root = getattr(self.artifact_store, "_root_path", None)
                rel = getattr(art, "path", "")
                if root and rel:
                    import os as _os
                    full = _os.path.join(root, rel)
                    if _os.path.isfile(full):
                        try:
                            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                            entry.setdefault("content", content[:5000])
                            entry["content_full_available"] = True
                        except Exception:
                            entry["content_full_available"] = False
                artifacts[key] = entry
        except Exception as exc:
            logger.warning(f"[Verifier] enrich_with_artifact_store 失败: {exc}")
        return artifacts

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
        # Phase Two #16: 接入 ArtifactStore 读取真实文件 + 元数据
        artifacts = self._enrich_with_artifact_store(artifacts)
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
            for command in checks.get("commands", []) or []:
                if isinstance(command, VerificationCommand):
                    all_results.append(self.programmatic.verify_command(
                        command.argv, cwd=command.cwd or checks.get("cwd"),
                        timeout=command.timeout,
                        expected_returncode=command.expected_returncode,
                    ))
                else:
                    all_results.append(self.programmatic.verify_command(
                        command.get("argv", []), cwd=command.get("cwd") or checks.get("cwd"),
                        timeout=command.get("timeout", 120),
                        expected_returncode=command.get("expected_returncode", 0),
                    ))
            if checks.get("file_hashes"):
                all_results.append(self.programmatic.verify_hashes(checks["file_hashes"]))
            if "json_schema" in checks and "data" in checks:
                all_results.append(self.programmatic.verify_json_schema(
                    checks["data"], checks["json_schema"]
                ))
            if "format" in checks:
                content = checks.get("content", "")
                all_results.append(self.programmatic.verify_output_format(content, checks["format"]))
            if checks.get("changed_files") is not None and (
                checks.get("forbidden_changes") or checks.get("diff_scope")
            ):
                all_results.append(self.programmatic.verify_forbidden_changes(
                    checks.get("changed_files", []), checks.get("forbidden_changes", []),
                    checks.get("diff_scope", []),
                ))

        # 2. LLM Rubric
        semantic_rubrics = checks.get("rubrics") or []
        programmatic_pass = any(
            result.verdict == Verdict.PASS and any(
                key in result.scores for key in ("command", "json_schema", "file_hash")
            ) for result in all_results
        )
        rubric_result = self.llm_rubric.verify(goal, artifacts, semantic_rubrics)
        # A real passing test/build/static check is valid evidence even when a
        # semantic model is unavailable.  Natural-language criteria still
        # require semantic review.
        if (rubric_result.verdict == Verdict.REPAIR and programmatic_pass
                and not semantic_rubrics
                and all(f.criterion == "semantic_verifier_unavailable"
                        for f in rubric_result.failed_criteria)):
            rubric_result = ValidationResult(
                verdict=Verdict.PASS, scores={"programmatic_evidence": 1.0},
                evidence=[EvidenceRef(source="verification_plan",
                                      content="programmatic command/hash/schema evidence passed")],
                summary="programmatic evidence satisfies the contract",
            )
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
