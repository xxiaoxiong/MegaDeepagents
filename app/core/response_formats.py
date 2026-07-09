"""结构化输出 Schema：子智能体返回可解析的 JSON。"""

from typing import List, Optional

from pydantic import BaseModel, Field


class ResearchFindings(BaseModel):
    """研究子智能体结构化输出。"""

    summary: str = Field(description="研究结论摘要")
    key_findings: List[str] = Field(default_factory=list, description="关键发现列表")
    sources: List[str] = Field(default_factory=list, description="引用来源列表")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="置信度 0-1")
    next_steps: Optional[List[str]] = Field(default=None, description="建议的后续步骤")


class CodeReviewResult(BaseModel):
    """代码审查子智能体结构化输出。"""

    verdict: str = Field(description="审查结论：approve / request_changes / reject")
    issues: List[str] = Field(default_factory=list, description="发现的问题列表")
    suggestions: List[str] = Field(default_factory=list, description="改进建议列表")
    risk_level: str = Field(default="low", description="风险等级：low / medium / high")
    files_reviewed: int = Field(default=0, description="审查的文件数")


class TestReport(BaseModel):
    """测试运行子智能体结构化输出。"""

    passed: bool = Field(description="测试是否全部通过")
    total_tests: int = Field(default=0, description="总测试数")
    passed_tests: int = Field(default=0, description="通过测试数")
    failed_tests: int = Field(default=0, description="失败测试数")
    failures: List[str] = Field(default_factory=list, description="失败用例详情")
    coverage: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="代码覆盖率")
    summary: str = Field(default="", description="测试摘要")
