"""Verifier：V3 统一验证引擎。

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
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal

from app.core.logging import logger


# ===== Verifier LLM 调用重试 =====
#
# ``LLMRubricVerifier._call_rubric_llm`` 在 run 期间会与多个 worker agent 的
# LLM 调用并发抢额度。OpenAI 兼容端点（agnes / 其它代理）在并发压力下频繁返回
# 429 / 网关超时，导致 verifier 的单次 invoke 失败 → ``_fallback_verify`` →
# fail-closed REPAIR → 触发新一轮 repair（更多 LLM 调用）→ 更多 429，形成死循环
# （run_3fb3c2572f1348b0 task_3__repair_v15 / v19：agent 实际已修复产物，但
# verifier LLM 调用 429 失败 → 假 REPAIR → 无意义修复两轮）。
#
# 修复：对 verifier 的 LLM 调用做有限次指数退避重试（base 15s，上限 300s，符合
# 项目约定），仅在重试耗尽后才回退到 fail-closed。这把"瞬时 429"和"真的没模型"
# 区分开，避免一次抖动就判 REPAIR。
_VERIFIER_LLM_MAX_RETRIES = 3
_VERIFIER_LLM_BASE_DELAY = 15.0
_VERIFIER_LLM_MAX_DELAY = 300.0

# ===== 产物内容预览上限 =====
#
# ``enrich_with_artifact_store`` 读取 artifact 文件并截取前 N 字符存入
# ``entry["content"]``；``_build_rubric_prompt`` 再把这个字符串展示给 LLM。
# 两个阈值必须一致，否则 enrich 保留了 24000 字符但 prompt builder 只展示
# 6000 字符，中间的代码对 LLM 不可见 → LLM 幻觉出 correctness 失败
# （run_55507ebfce5744e8 task_2__repair_v31：22441 字符的 index.html，
# head 3000 + tail 3000 展示，中间 16441 字符被省略，LLM 看不到
# Notifications 组件的 default case 和 LoginPage，幻觉出"未闭合条件判断"
# 和"缺少 strict 属性"——后者甚至不是 React Router v6 的真实 API）。
_RUBRIC_CONTENT_PREVIEW_LIMIT = 24000


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Return True for transient LLM errors worth retrying (429/timeout/connection)."""
    msg = str(exc).lower()
    if any(k in msg for k in ("429", "rate limit", "rate_limit", "too many requests")):
        return True
    if any(k in msg for k in ("timeout", "timed out", "connection", "socket",
                              "temporarily", "unavailable", "503", "502", "500",
                              "retry", "overloaded")):
        return True
    # langchain/openai exception types (if importable)
    try:
        from openai import RateLimitError, APITimeoutError, APIConnectionError, InternalServerError
        if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError,
                            InternalServerError)):
            return True
    except Exception:
        pass
    return False


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


def _no_artifacts_result() -> ValidationResult:
    """Return the non-overridable completion failure for missing evidence."""
    return ValidationResult(
        verdict=Verdict.FAIL,
        scores={
            "completeness": 0.0,
            "correctness": 0.0,
            "consistency": 0.0,
            "evidence": 0.0,
        },
        failed_criteria=[
            CriterionFailure(
                "no_artifacts",
                "No eligible durable artifacts are available for verification",
                severity="high",
            )
        ],
        summary="No eligible artifacts; verification failed closed",
    )


def _eligible_artifacts(
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Exclude evidence that the artifact lifecycle has already retired."""
    eligible: dict[str, dict[str, Any]] = {}
    for key, info in artifacts.items():
        status = info.get("status")
        status_value = getattr(status, "value", status)
        if status_value in {"rejected", "superseded"}:
            continue
        eligible[key] = info
    return eligible


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
        artifacts = _eligible_artifacts(artifacts)
        if not artifacts:
            return _no_artifacts_result()
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

        对瞬时错误（429 / 超时 / 连接）做有限次指数退避重试，避免一次抖动就
        fail-closed REPAIR 触发 repair 死循环。
        """
        from app.llm_factory import build_model
        # 使用低成本模型做评估
        llm = build_model()
        # NB: 示例必须用双引号的标准 JSON。早期版本在 prompt 里写了
        # ``{'scores': ...}`` 单引号 Python dict，LLM 照抄导致 json.loads
        # 失败 → _fallback_verify → fail_closed REPAIR（run_e9adbc33570a4243）。
        # 解析器现已兼容 Python dict 字面量（ast.literal_eval），但 prompt
        # 仍应示范正确 JSON 以减少 fallback。
        messages = [
            ("system", "你是一个严格的验证评估员。评估后只输出一个 JSON 对象"
                      "（不要输出任何解释文字、不要用 Markdown 代码块），"
                      "用双引号，格式为："
                      '{"scores": {"completeness": 0~1, "correctness": 0~1, '
                      '"consistency": 0~1, "evidence": 0~1}, '
                      '"failed_criteria": [{"criterion": "...", "detail": "...", '
                      '"severity": "low|medium|high"}], '
                      '"verdict": "pass|repair|replan|human_required|fail", '
                      '"summary": "..."}。'
                      "\n\n重要：failed_criteria 中的 criterion 字段只能使用语义"
                      "评估维度名称（如 completeness/完整性、correctness/正确性、"
                      "consistency/一致性、evidence/证据性）。禁止使用 json_schema、"
                      "command、file_hash、files、format 等程序化检查名称——这些"
                      "由系统自动执行，不需要 LLM 评估。"),
            ("user", prompt),
        ]
        text: str | None = None
        last_exc: Exception | None = None
        for attempt in range(_VERIFIER_LLM_MAX_RETRIES + 1):
            try:
                response = llm.invoke(messages)
                text = getattr(response, "content", str(response))
                break
            except Exception as exc:
                last_exc = exc
                if attempt < _VERIFIER_LLM_MAX_RETRIES and _is_transient_llm_error(exc):
                    delay = min(
                        _VERIFIER_LLM_BASE_DELAY * (2 ** attempt),
                        _VERIFIER_LLM_MAX_DELAY,
                    )
                    logger.warning(
                        "[LLMRubricVerifier] LLM 调用瞬时失败（第 %d 次），"
                        "%.1f 秒后重试: %s",
                        attempt + 1, delay, exc,
                    )
                    time.sleep(delay)
                    continue
                # 非瞬时错误或重试耗尽：走 fallback
                logger.warning(
                    "[LLMRubricVerifier] LLM rubric 调用失败（不再重试）: %s", exc
                )
                return self._fallback_verify(goal, artifacts)

        if text is None:
            # 所有重试都失败
            logger.warning(
                "[LLMRubricVerifier] LLM rubric 调用 %d 次重试均失败: %s",
                _VERIFIER_LLM_MAX_RETRIES, last_exc,
            )
            return self._fallback_verify(goal, artifacts)

        import json
        try:
            parsed = self._parse_rubric_json(text) if isinstance(text, str) else text
        except Exception as exc:
            logger.warning(
                "[LLMRubricVerifier] rubric JSON 解析失败，回退规则评分: %s", exc
            )
            # 关键修复：传真实 (goal, artifacts) 而非 (prompt, {})
            return self._fallback_verify(goal, artifacts)

        # 过滤 LLM 幻觉产生的程序化检查名称。LLM 偶尔会在 failed_criteria
        # 中使用 "json_schema"/"command"/"file_hash" 等保留名称（observed:
        # run_bc2472cb08354dd4 task_1 验证返回 criterion="json_schema"
        # detail="argument of type 'NoneType' is not iterable"），这些名称
        # 是 ProgrammaticVerifier 的专属检查项，LLM 不应使用。保留它们会导致
        # 假失败（verdict 被拉高到 replan/repair）+ 误导开发者以为是代码 bug。
        _RESERVED_PROGRAMMATIC_CRITERIA = frozenset({
            "json_schema", "command", "file_hash", "files", "format",
            "diff_scope", "forbidden_changes",
        })
        failed: list[CriterionFailure] = []
        for c in parsed.get("failed_criteria", []):
            if not isinstance(c, dict):
                continue
            crit_name = c.get("criterion", "")
            if not isinstance(crit_name, str) or not crit_name.strip():
                continue
            if crit_name.lower().strip() in _RESERVED_PROGRAMMATIC_CRITERIA:
                logger.info(
                    "[LLMRubricVerifier] 过滤 LLM 幻觉的程序化检查名称 %r",
                    crit_name,
                )
                continue
            detail = c.get("detail", "")
            if not isinstance(detail, str):
                detail = str(detail)
            failed.append(CriterionFailure(
                criterion=crit_name,
                detail=detail,
                severity=c.get("severity", "medium"),
            ))
        verdict_str = parsed.get("verdict", "repair")
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.REPAIR

        # 如果所有 failed_criteria 都是被过滤掉的程序化检查名称幻觉，
        # 把 verdict 降级为 PASS——没有真正的语义失败就不应触发 repair/replan。
        if not failed and verdict in (Verdict.REPAIR, Verdict.REPLAN, Verdict.FAIL):
            logger.info(
                "[LLMRubricVerifier] 所有 failed_criteria 均为幻觉程序化检查，"
                "verdict %s → PASS", verdict.value,
            )
            verdict = Verdict.PASS

        return ValidationResult(
            verdict=verdict,
            scores=parsed.get("scores", {}),
            failed_criteria=failed,
            summary=parsed.get("summary", "LLM rubric 验证完成"),
        )

    @staticmethod
    def _parse_rubric_json(text: str) -> dict[str, Any]:
        """多策略解析 rubric JSON。

        兼容：
        1. 标准 JSON（如响应中直接给出 ``{"scores": ...}``）；
        2. Markdown 代码块包裹（如 `` ```json\\n{"scores": ...}\\n``` ``）；
        3. 内嵌于中文说明文字中的 JSON（提取首个 ``{...}`` 块）；
        4. Python dict 字面量（单引号 / ``True`` / ``False`` / ``None``）——
           某些 LLM 端点即使被要求输出 JSON 也会返回 Python repr 风格的 dict
           （如 ``{'scores': {'completeness': 0.1}, ...}``）。此前这种响应
           让 ``json.loads`` 全部失败 → ``_fallback_verify`` → ``fail_closed``
           REPAIR，造成 planning 任务被反复无意义修复（run_e9adbc33570a4243
           task_1__repair_v7：LLM 实际已给出评分但解析失败，3 轮 repair 全
           白跑）。``ast.literal_eval`` 只求值字面量，安全且能处理单引号 dict。
        """
        import ast
        import json

        def _try_loads(s: str) -> dict[str, Any] | None:
            s = s.strip()
            if not s:
                return None
            # 1. 标准 JSON（双引号、true/false/null）
            try:
                result = json.loads(s)
                if isinstance(result, dict):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass
            # 2. Python dict 字面量（单引号、True/False/None）—— ast.literal_eval
            #    只求值字面量（字符串/数字/tuple/list/dict/bool/None），不会执行
            #    函数调用或属性访问，可安全用于解析 LLM 输出。
            try:
                result = ast.literal_eval(s)
                if isinstance(result, dict):
                    return result
            except (ValueError, SyntaxError):
                pass
            return None

        # 直接尝试整段
        parsed = _try_loads(text)
        if parsed is not None:
            return parsed
        # 尝试剥离 markdown 代码块
        strip_prefixes = ("```json", "```", "```JSON")
        for prefix in strip_prefixes:
            if text.startswith(prefix):
                rest = text[len(prefix):]
                # 找到闭合的 ```
                end = rest.find("```")
                if end != -1:
                    candidate = rest[:end].strip()
                    parsed = _try_loads(candidate)
                    if parsed is not None:
                        return parsed
                break
        # 尝试提取首个 { ... } 块
        start = text.find("{")
        if start != -1:
            end = text.rfind("}")
            if end > start:
                candidate = text[start:end + 1]
                parsed = _try_loads(candidate)
                if parsed is not None:
                    return parsed
        # raise TypeError，让上层 catch 后走 _fallback_verify
        raise TypeError(f"LLMRubricVerifier 无法解析 rubric JSON: {text[:300]}")

    def _build_rubric_prompt(
        self,
        goal: str,
        artifacts: dict[str, dict[str, Any]],
        rubrics: list[str],
    ) -> str:
        lines = [f"目标: {goal}"]
        for path, info in artifacts.items():
            content = info.get("content", "") or info.get("content_preview", "")
            size = info.get("size_bytes", 0) or len(content)
            # Show up to _RUBRIC_CONTENT_PREVIEW_LIMIT chars so the LLM can
            # actually evaluate completeness/correctness.  The previous 500-char
            # limit meant a 30 KB OpenAPI spec or 13 KB architecture document
            # had <2% of its content visible, causing the rubric to flag
            # "completeness" / "evidence" failures on every repair round
            # (run_8dfe5fb9dae74962 task_1: 3 consecutive repair rounds with no
            # real progress).
            #
            # This must match the cap in ``enrich_with_artifact_store`` — if
            # enrich keeps 24000 chars but the prompt only shows 6000, the LLM
            # sees head+tail with a large gap in the middle and hallucinates
            # correctness failures on the hidden code (run_55507ebfce5744e8
            # task_2__repair_v31: 22441-char index.html, 16441 chars omitted).
            preview_limit = _RUBRIC_CONTENT_PREVIEW_LIMIT
            header = f"--- 产物: {path} (size: {size} bytes)"
            if len(content) <= preview_limit:
                # Whole file fits — show it in full.
                lines.append(f"\n{header} ---\n{content}")
                continue
            # Large file: show HEAD + TAIL so the LLM can see the file both
            # starts and ends properly.  Showing only the head made the rubric
            # hallucinate "method truncated" / "file incomplete" on every large
            # artifact (run_3fb3c2572f1348b0 task_4__repair_v29: 18KB test file
            # was complete at 493 lines, but only the first 6KB was visible so
            # the LLM falsely claimed TestHealthCheck.run() was truncated).
            head_len = preview_limit // 2
            tail_len = preview_limit - head_len
            head = content[:head_len]
            tail = content[-tail_len:]
            omitted = len(content) - head_len - tail_len
            header += (
                f", showing first {head_len} chars + last {tail_len} chars "
                f"({omitted} chars omitted in the middle)"
            )
            header += " ---"
            lines.append(
                f"\n{header}\n{head}\n"
                f"\n... [{omitted} chars omitted ] ...\n"
                f"\n{tail}"
            )
        lines.append(f"\n评估维度: {', '.join(rubrics)}")
        lines.append(
            "\n注意：产物内容可能因长度限制只展示开头和结尾（中间省略）。"
            "省略不代表产物不完整——文件大小已标注。请基于已展示的开头与结尾"
            "内容评估质量，不要仅因内容省略而判定 repair。如果产物非空、结构"
            "完整（有合理的开头和结尾），应视为满足完整性要求。"
        )
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
        # 接入 ArtifactStore，让 Verifier 能读到注册表里的真实
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
                # Read the persisted bytes through the ArtifactStore's safe
                # resolver.  Directly joining ``root`` + ``rel`` and ``open()``
                # ing the result bypassed ``_safe_path`` and let a symlink
                # swapped by the producing agent (``ln -sf /runtime/.env
                # output.txt``) leak secrets into the rubric prompt.  Going
                # through ``read_bytes`` enforces the workspace boundary and
                # rejects traversal / absolute paths.
                try:
                    raw = self.artifact_store.read_bytes(aid)
                except Exception:
                    raw = None
                if raw is not None:
                    # Reject symlinks defensively: even with ``_safe_path`` a
                    # previously-created symlink whose target was inside the
                    # workspace at creation time could later be repointed.
                    # ``read_bytes`` already returns the resolved bytes, but
                    # we surface the integrity flag so the rubric layer can
                    # down-rank tampered evidence.
                    try:
                        integrity_ok = self.artifact_store.verify_integrity(aid)
                    except Exception:
                        integrity_ok = False
                    entry["content_full_available"] = bool(integrity_ok)
                    entry["integrity_verified"] = bool(integrity_ok)
                    if integrity_ok:
                        text = raw.decode("utf-8", errors="ignore")
                        # Capture both a head window and the full text.  The
                        # rubric prompt builder (``_build_rubric_prompt``)
                        # shows head+tail for large files so the LLM can see
                        # the file ends properly; capping at 8000 chars here
                        # made the "tail" land in the middle of large files
                        # (run_3fb3c2572f1348b0 task_4__repair_v29: 18KB test
                        # file — the real ending was never shown, causing false
                        # "method truncated" verdicts).  Must stay in sync with
                        # ``_RUBRIC_CONTENT_PREVIEW_LIMIT`` used by the prompt
                        # builder — if they diverge the LLM sees a truncated
                        # view and hallucinates correctness failures
                        # (run_55507ebfce5744e8 task_2__repair_v31).
                        entry.setdefault("content", text[:_RUBRIC_CONTENT_PREVIEW_LIMIT])
                    else:
                        entry.setdefault(
                            "content",
                            "[artifact integrity check failed; content withheld]",
                        )
                else:
                    entry["content_full_available"] = False
                    entry["integrity_verified"] = False
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
        # 接入 ArtifactStore 读取真实文件和元数据。
        artifacts = self._enrich_with_artifact_store(artifacts)
        artifacts = _eligible_artifacts(artifacts)
        # Durable evidence is a completion invariant, not a rubric.  A model
        # verdict or a passing command can never promote an evidence-free task.
        if not artifacts:
            return _no_artifacts_result()
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
            if checks.get("json_schema") is not None and checks.get("data") is not None:
                all_results.append(self.programmatic.verify_json_schema(
                    checks["data"], checks["json_schema"]
                ))
            if checks.get("format") is not None:
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
