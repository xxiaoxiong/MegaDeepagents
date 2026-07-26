"""Shared retry, timeout, and failure-classification policy.

This module does not execute work and is intentionally not another scheduler.
It gives the production scheduler one deterministic policy for deciding when a
failed assignment should be retried and how long it should wait.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class FailureCategory(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CONTRACT = "contract"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    category: FailureCategory
    retryable: bool
    delay_seconds: float
    reason: str

    def to_dict(self) -> dict[str, str | bool | float]:
        payload = asdict(self)
        payload["category"] = self.category.value
        return payload


class RetryPolicy:
    """Classify failures and apply bounded exponential backoff."""

    _RATE_LIMIT_MARKERS = (
        "429",
        "rate limit",
        "rate_limit",
        "too many requests",
        "quota temporarily",
    )
    _TIMEOUT_MARKERS = (
        "timeout",
        "timed out",
        "deadline exceeded",
        "read timeout",
        "connect timeout",
    )
    _TRANSIENT_MARKERS = (
        "connection reset",
        "connection aborted",
        "connection refused",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "remote protocol",
        "network",
        "502",
        "503",
        "504",
    )
    _AUTH_MARKERS = (
        "401",
        "invalid api key",
        "authentication",
        "unauthorized",
    )
    _PERMISSION_MARKERS = (
        "permission required",
        "permission denied",
        "approval required",
        "forbidden",
        "403",
    )
    _CONTRACT_MARKERS = (
        "validation error",
        "invalid request",
        "schema",
        "output contract",
        "dependency_not_verified",
        "artifact_integrity_failed",
    )
    _CANCEL_MARKERS = ("cancelled", "canceled", "agent_stopped")

    def __init__(
        self,
        *,
        base_delay_seconds: float = 2.0,
        max_delay_seconds: float = 60.0,
        rate_limit_base_delay_seconds: float | None = None,
        rate_limit_max_delay_seconds: float | None = None,
    ) -> None:
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(
            self.base_delay_seconds, float(max_delay_seconds)
        )
        # 429 / quota errors need a much longer backoff than a transient
        # network blip.  The default LLM gateway recovers in tens of
        # seconds, not 2 s; retrying after 2 s simply burns the retry
        # budget while the upstream is still throttling.  Defaults: 15 s
        # base, 300 s cap, so a 4-attempt budget yields 15/30/60/120 s.
        self.rate_limit_base_delay_seconds = max(
            0.0,
            float(rate_limit_base_delay_seconds)
            if rate_limit_base_delay_seconds is not None
            else 15.0,
        )
        self.rate_limit_max_delay_seconds = max(
            self.rate_limit_base_delay_seconds,
            float(rate_limit_max_delay_seconds)
            if rate_limit_max_delay_seconds is not None
            else 300.0,
        )

    def decide(
        self,
        error: str,
        *,
        attempt: int,
        max_attempts: int,
    ) -> RetryDecision:
        category = self.classify(error)
        has_budget = attempt < max_attempts
        retryable_category = category not in {
            FailureCategory.AUTHENTICATION,
            FailureCategory.PERMISSION,
            FailureCategory.CONTRACT,
            FailureCategory.CANCELLED,
        }
        retryable = has_budget and retryable_category
        delay = 0.0
        if retryable:
            if category is FailureCategory.RATE_LIMITED:
                base = self.rate_limit_base_delay_seconds
                cap = self.rate_limit_max_delay_seconds
            else:
                base = self.base_delay_seconds
                cap = self.max_delay_seconds
            exponent = max(0, attempt - 1)
            delay = min(cap, base * (2**exponent))
        reason = (
            "retry_budget_available"
            if retryable
            else "retry_budget_exhausted"
            if not has_budget
            else f"{category.value}_requires_intervention"
        )
        return RetryDecision(
            category=category,
            retryable=retryable,
            delay_seconds=round(delay, 3),
            reason=reason,
        )

    @classmethod
    def classify(cls, error: str) -> FailureCategory:
        value = (error or "").lower()
        if any(marker in value for marker in cls._CANCEL_MARKERS):
            return FailureCategory.CANCELLED
        if any(marker in value for marker in cls._AUTH_MARKERS):
            return FailureCategory.AUTHENTICATION
        if any(marker in value for marker in cls._PERMISSION_MARKERS):
            return FailureCategory.PERMISSION
        if any(marker in value for marker in cls._RATE_LIMIT_MARKERS):
            return FailureCategory.RATE_LIMITED
        if any(marker in value for marker in cls._TIMEOUT_MARKERS):
            return FailureCategory.TIMEOUT
        if any(marker in value for marker in cls._TRANSIENT_MARKERS):
            return FailureCategory.TRANSIENT
        if any(marker in value for marker in cls._CONTRACT_MARKERS):
            return FailureCategory.CONTRACT
        return FailureCategory.UNKNOWN
