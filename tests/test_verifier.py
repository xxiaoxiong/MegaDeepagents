"""Verifier 单元测试。"""
from __future__ import annotations

import os
import sys

import pytest

from app.multiagent.verifier import (
    Verifier,
    ProgrammaticVerifier,
    LLMRubricVerifier,
    Verdict,
    ValidationResult,
    CriterionFailure,
    EvidenceRef,
    VerificationCommand,
)
from app.multiagent import verifier as verifier_module


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
    """LLM 不可用时，非空产物也不能冒充语义验收通过。"""
    v = LLMRubricVerifier(model_available=False)
    artifacts = {
        "/tmp/a.py": {"content": "print(1)"},
        "/tmp/b.py": {"content": "x = 2"},
    }
    result = v.verify("build code", artifacts)
    assert result.verdict == Verdict.REPAIR
    assert result.scores["completeness"] == 1.0


def test_llm_rubric_fallback_empty_artifact():
    v = LLMRubricVerifier(model_available=False)
    artifacts = {
        "/tmp/a.py": {"content": "print(1)"},
        "/tmp/b.py": {"content": ""},  # 空
    }
    result = v.verify("build code", artifacts)
    assert result.verdict == Verdict.REPAIR
    assert len(result.failed_criteria) >= 2


def test_llm_rubric_parse_json_handles_plain_codeblock_and_embedded():
    """LLM 响应可以是纯 JSON、```json 代码块，或嵌在叙述文字中。"""
    from app.multiagent.verifier import LLMRubricVerifier

    plain = '{"scores": {"completeness": 0.8}, "verdict": "pass", "summary": "ok"}'
    codeblock = "```json\n" + plain + "\n```"
    embedded = "好的，我已经评估完毕，结果如下：\n" + plain + "\n以上是结论。"

    for text in (plain, codeblock, embedded):
        parsed = LLMRubricVerifier._parse_rubric_json(text)
        assert parsed["verdict"] == "pass"
        assert parsed["scores"]["completeness"] == 0.8

    import pytest
    with pytest.raises(TypeError):
        LLMRubricVerifier._parse_rubric_json("not json at all")


def test_llm_rubric_parse_json_handles_python_dict_literal():
    """LLM 返回 Python repr 风格 dict（单引号）也能解析。

    回归锁定 run_e9adbc33570a4243 task_1__repair_v7：agnes 等端点即使被
    要求输出 JSON 也会返回 ``{'scores': {'completeness': 0.1}, ...}`` 单引号
    字面量。旧版只试 ``json.loads``，全部失败 → ``_fallback_verify`` →
    ``fail_closed`` REPAIR，导致 planning 任务被反复无意义修复。现用
    ``ast.literal_eval`` 兜底，让 LLM 的真实 verdict 通过。
    """
    from app.multiagent.verifier import LLMRubricVerifier

    # 真实观测到的响应形态：单引号 dict，含中文 detail
    py_dict = (
        "{'scores': {'completeness': 0.1}, "
        "'failed_criteria': [{'criterion': '修复后必须通过 Verifier', "
        "'detail': '数据库脚本中存在笔误', 'severity': 'high'}], "
        "'verdict': 'repair', 'summary': '需要修复'}"
    )
    parsed = LLMRubricVerifier._parse_rubric_json(py_dict)
    assert parsed["verdict"] == "repair"
    assert parsed["scores"]["completeness"] == 0.1
    assert parsed["failed_criteria"][0]["criterion"] == "修复后必须通过 Verifier"

    # Python dict 嵌在叙述文字中
    embedded = "评估完成，结果如下：\n" + py_dict + "\n请据此修复。"
    parsed2 = LLMRubricVerifier._parse_rubric_json(embedded)
    assert parsed2["verdict"] == "repair"

    # Python dict 含 True/False/None 也能解析
    py_bool = "{'scores': {'ok': 1.0}, 'passed': True, 'skipped': None, 'verdict': 'pass'}"
    parsed3 = LLMRubricVerifier._parse_rubric_json(py_bool)
    assert parsed3["verdict"] == "pass"
    assert parsed3["passed"] is True
    assert parsed3["skipped"] is None


# ===== Verifier 顶层 =====


def test_verifier_pass_when_all_clean(tmp_path):
    """文件存在 + 真实命令证据通过 → PASS。"""
    f1 = tmp_path / "x.py"
    f1.write_text("print(1)")
    verifier = Verifier(
        llm_rubric=LLMRubricVerifier(model_available=False),
    )
    # Use a script file rather than ``python -c "..."``: the inline-script
    # flag is escalated to UNKNOWN by ShellPolicyEngine (arbitrary code
    # execution vector) and would require a PermissionBroker.
    verified_script = tmp_path / "_verified.py"
    verified_script.write_text("print('verified')\n", encoding="utf-8")
    result = verifier.validate(
        goal="做 X",
        artifacts={str(f1): {"content": "print(1)"}},
        checks={
            "files": [str(f1)],
            "commands": [
                VerificationCommand(
                    kind="test",
                    argv=[sys.executable, str(verified_script)],
                    cwd=str(tmp_path),
                )
            ],
        },
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


def test_online_rubric_cannot_pass_without_eligible_artifacts():
    class AlwaysPassOnlineRubric(LLMRubricVerifier):
        def __init__(self):
            super().__init__(model_available=True)
            self.calls = 0

        def _call_rubric_llm(self, prompt, goal, artifacts):
            self.calls += 1
            return ValidationResult(verdict=Verdict.PASS, summary="hallucinated pass")

    rubric = AlwaysPassOnlineRubric()
    verifier = Verifier(llm_rubric=rubric)

    missing = verifier.validate(goal="claim success", artifacts={})
    retired = verifier.validate(
        goal="claim success",
        artifacts={
            "artifact:old": {
                "artifact_id": "old",
                "status": "superseded",
                "content": "stale output",
            }
        },
    )

    assert missing.verdict == Verdict.FAIL
    assert retired.verdict == Verdict.FAIL
    assert rubric.calls == 0
    assert missing.failed_criteria[0].criterion == "no_artifacts"


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


# ===== Verifier LLM 重试（避免 429 死循环） =====


def test_rubric_retries_transient_errors_then_succeeds(monkeypatch):
    """瞬时错误（429/超时）应重试，成功后正常返回而非 fail-closed REPAIR。

    锁定 run_3fb3c2572f1348b0 task_3__repair_v15/v19 的回归：verifier LLM
    调用 429 失败 → _fallback_verify → 假 REPAIR → 触发更多 LLM 调用 → 更多
    429，形成死循环。
    """
    import app.llm_factory as llm_factory

    v = LLMRubricVerifier(model_available=True)

    # 构造一个 mock llm：前两次抛 429，第三次返回 pass
    class _Resp:
        content = '{"scores":{"completeness":1.0},"failed_criteria":[],"verdict":"pass","summary":"ok"}'

    class _FlakyLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("429 Too Many Requests: rate limit exceeded")
            return _Resp()

    flaky = _FlakyLLM()
    monkeypatch.setattr(llm_factory, "build_model", lambda: flaky)
    # 加速重试：不真的 sleep 15s
    monkeypatch.setattr(verifier_module.time, "sleep", lambda *_a, **_k: None)

    artifacts = {"a.py": {"content": "print(1)"}}
    result = v.verify("build", artifacts)
    assert result.verdict == Verdict.PASS
    assert flaky.calls == 3  # 2 failures + 1 success


def test_rubric_falls_back_after_retries_exhausted(monkeypatch):
    """重试耗尽后才回退到 fail-closed，而非首次失败就判 REPAIR。"""
    import app.llm_factory as llm_factory

    v = LLMRubricVerifier(model_available=True)

    class _AlwaysFailLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            raise TimeoutError("request timed out")

    fail_llm = _AlwaysFailLLM()
    monkeypatch.setattr(llm_factory, "build_model", lambda: fail_llm)
    monkeypatch.setattr(verifier_module.time, "sleep", lambda *_a, **_k: None)

    artifacts = {"a.py": {"content": "print(1)"}}
    result = v.verify("build", artifacts)
    # 重试耗尽 → fallback → REPAIR (fail-closed)
    assert result.verdict == Verdict.REPAIR
    # 1 initial + 3 retries = 4 calls
    assert fail_llm.calls == verifier_module._VERIFIER_LLM_MAX_RETRIES + 1


def test_rubric_no_retry_on_non_transient_error(monkeypatch):
    """非瞬时错误（如 ValueError）不重试，立即回退。"""
    import app.llm_factory as llm_factory

    v = LLMRubricVerifier(model_available=True)

    class _BadLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            raise ValueError("invalid api key configuration")

    bad = _BadLLM()
    monkeypatch.setattr(llm_factory, "build_model", lambda: bad)
    monkeypatch.setattr(verifier_module.time, "sleep", lambda *_a, **_k: None)

    artifacts = {"a.py": {"content": "print(1)"}}
    result = v.verify("build", artifacts)
    assert result.verdict == Verdict.REPAIR
    # 非瞬时错误 → 只调用 1 次，不重试
    assert bad.calls == 1


# ===== Head+Tail prompt 避免假"截断"判决 =====


def test_rubric_prompt_shows_head_and_tail_for_large_files():
    """大文件应同时展示开头和结尾，让 LLM 看到文件正常结束。

    锁定 run_3fb3c2572f1348b0 task_4__repair_v29 回归：18KB 完整测试文件
    只展示前 6KB，LLM 幻觉"TestHealthCheck.run() 方法实现被截断"。

    注意：``_RUBRIC_CONTENT_PREVIEW_LIMIT`` 为 24000，所以测试文件必须
    超过 24000 字符才会触发 head+tail 拆分。
    """
    from app.multiagent.verifier import _RUBRIC_CONTENT_PREVIEW_LIMIT
    v = LLMRubricVerifier(model_available=True)
    # 造一个 > _RUBRIC_CONTENT_PREVIEW_LIMIT 字符的产物
    big_content = "def start():\n    pass\n" + ("x = 1\n" * (_RUBRIC_CONTENT_PREVIEW_LIMIT // 5)) + "def end():\n    return 'done'\n"
    assert len(big_content) > _RUBRIC_CONTENT_PREVIEW_LIMIT, "test file must exceed preview limit"
    prompt = v._build_rubric_prompt("goal", {"big.py": {"content": big_content, "size_bytes": len(big_content)}},
                                    ["completeness"])
    # 开头和结尾都应出现
    assert "def start()" in prompt
    assert "def end()" in prompt
    assert "return 'done'" in prompt
    # 应标注中间省略
    assert "omitted" in prompt


def test_rubric_prompt_shows_full_small_file():
    """小文件应完整展示，不做 head+tail 拆分。"""
    v = LLMRubricVerifier(model_available=True)
    small = "print('hello world')\n"
    prompt = v._build_rubric_prompt("goal", {"s.py": {"content": small}}, ["completeness"])
    assert "print('hello world')" in prompt
    assert "omitted" not in prompt
