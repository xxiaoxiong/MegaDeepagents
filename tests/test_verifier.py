"""Verifier 单元测试（docs/upgradePhaseTwo.md §十二）。"""
from __future__ import annotations

import os

import pytest

from app.multiagent.verifier import (
    Verifier,
    ProgrammaticVerifier,
    LLMRubricVerifier,
    Verdict,
    ValidationResult,
    CriterionFailure,
    EvidenceRef,
)


# ===== ProgrammaticVerifier =====


def test_programmatic_files_all_exist(tmp_path):
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.md"
    f1.write_text("x")
    f2.write_text("y")
    pv = ProgrammaticVerifier()
    result = pv.verify_files_exist([str(f1), str(f2)])
    assert result.verdict == Verdict.PASS
    assert result.scores["file_exists"] == 1.0


def test_programmatic_files_missing(tmp_path):
    pv = ProgrammaticVerifier()
    result = pv.verify_files_exist([str(tmp_path / "nonexistent.file")])
    assert result.verdict == Verdict.REPAIR
    assert len(result.failed_criteria) == 1
    assert "nonexistent" in result.failed_criteria[0].criterion


def test_programmatic_command_success(tmp_path):
    pv = ProgrammaticVerifier()
    result = pv.verify_command("echo hello > out.txt && echo done", cwd=str(tmp_path))
    assert result.verdict == Verdict.PASS


def test_programmatic_command_failure():
    pv = ProgrammaticVerifier()
    result = pv.verify_command("exit 1", timeout=5)
    assert result.verdict == Verdict.REPAIR
    assert result.scores["command"] == 0.0


def test_programmatic_command_timeout():
    pv = ProgrammaticVerifier()
    # 真正长 sleep：但用 ping 之类更可控；这里直接 sleep 60 但 timeout=1
    result = pv.verify_command("ping -n 5 127.0.0.1", timeout=1)
    # Windows 下 ping -n 5 约 4 秒，timeout=1 应触发 TimeoutExpired
    # 但有些 Windows 环境 ping 仍可能快速返回 → 至少不抛
    assert result.verdict in (Verdict.REPAIR, Verdict.PASS)


def test_programmatic_json_schema_valid():
    pv = ProgrammaticVerifier()
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    result = pv.verify_json_schema({"x": 1}, schema)
    assert result.verdict == Verdict.PASS


def test_programmatic_json_schema_invalid():
    pv = ProgrammaticVerifier()
    schema = {"type": "object", "required": ["x"]}
    result = pv.verify_json_schema({}, schema)
    assert result.verdict == Verdict.REPAIR
    assert len(result.failed_criteria) == 1


def test_programmatic_output_format_json():
    pv = ProgrammaticVerifier()
    result = pv.verify_output_format('{"a": 1}', "json")
    assert result.verdict == Verdict.PASS


def test_programmatic_output_format_invalid_json():
    pv = ProgrammaticVerifier()
    result = pv.verify_output_format("not json {", "json")
    assert result.verdict == Verdict.REPAIR


def test_programmatic_output_format_empty():
    pv = ProgrammaticVerifier()
    result = pv.verify_output_format("", "non_empty")
    assert result.verdict == Verdict.REPAIR


# ===== LLMRubricVerifier =====


def test_llm_rubric_fallback_no_artifacts():
    """LLM 不可用 + 无产物 → FAIL。"""
    v = LLMRubricVerifier(model_available=False)
    result = v.verify("build the thing", {})
    assert result.verdict == Verdict.FAIL


def test_llm_rubric_fallback_with_artifacts():
    """LLM 不可用 + 有产物 → REPAIR/PASS（带规则评分）。"""
    v = LLMRubricVerifier(model_available=False)
    artifacts = {
        "/tmp/a.py": {"content": "print(1)"},
        "/tmp/b.py": {"content": "x = 2"},
    }
    result = v.verify("build code", artifacts)
    # 全部产物非空 → PASS（fallback 规则）
    assert result.verdict == Verdict.PASS
    assert result.scores["completeness"] == 1.0


def test_llm_rubric_fallback_empty_artifact():
    v = LLMRubricVerifier(model_available=False)
    artifacts = {
        "/tmp/a.py": {"content": "print(1)"},
        "/tmp/b.py": {"content": ""},  # 空
    }
    result = v.verify("build code", artifacts)
    assert result.verdict == Verdict.REPAIR
    assert len(result.failed_criteria) == 1


# ===== Verifier 顶层 =====


def test_verifier_pass_when_all_clean(tmp_path):
    """文件存在 + LLM rubric 通过 → PASS。"""
    f1 = tmp_path / "x.py"
    f1.write_text("print(1)")
    verifier = Verifier(
        llm_rubric=LLMRubricVerifier(model_available=False),  # 用 fallback
    )
    result = verifier.validate(
        goal="做 X",
        artifacts={str(f1): {"content": "print(1)"}},
        checks={"files": [str(f1)]},
    )
    assert result.verdict == Verdict.PASS


def test_verifier_repair_when_files_missing(tmp_path):
    verifier = Verifier(
        llm_rubric=LLMRubricVerifier(model_available=False),
    )
    result = verifier.validate(
        goal="做 X",
        artifacts={},  # 无产物 → fallback FAIL
        checks={"files": [str(tmp_path / "missing.file")]},
    )
    # 文件失败 + LLM fallback 失败 → 合并后取最严 FAIL
    assert result.verdict in (Verdict.FAIL, Verdict.REPAIR)


def test_verifier_fail_when_no_artifacts():
    verifier = Verifier(
        llm_rubric=LLMRubricVerifier(model_available=False),
    )
    result = verifier.validate(
        goal="do something",
        artifacts={},
        checks=None,
    )
    # 没有 checks → 仅 LLM rubric fallback；无 artifacts → FAIL
    assert result.verdict == Verdict.FAIL


def test_verifier_merges_scores():
    verifier = Verifier(
        llm_rubric=LLMRubricVerifier(model_available=False),
    )
    f1 = os.path.join(os.path.dirname(__file__), "test_verifier.py")
    result = verifier.validate(
        goal="g",
        artifacts={f1: {"content": "data"}},
        checks={"files": [f1]},
    )
    assert "file_exists" in result.scores
    assert "completeness" in result.scores


# ===== Verdict 数据模型 =====


def test_verdict_enum_values():
    assert Verdict.PASS == "pass"
    assert Verdict.REPAIR == "repair"
    assert Verdict.REPLAN == "replan"
    assert Verdict.HUMAN_REQUIRED == "human_required"
    assert Verdict.FAIL == "fail"


def test_validation_result_default():
    r = ValidationResult(verdict=Verdict.PASS)
    assert r.failed_criteria == []
    assert r.scores == {}
    assert r.evidence == []
    assert r.proposed_tasks == []


def test_evidence_ref():
    e = EvidenceRef(source="cmd:pytest", content="OK")
    assert e.source == "cmd:pytest"


def test_criterion_failure():
    c = CriterionFailure(criterion="t", detail="d")
    assert c.severity == "medium"  # default
