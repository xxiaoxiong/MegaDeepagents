"""敏感信息过滤：检测和脱敏 API Key、Token、密码等。"""

import re
from typing import List


SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(sk-[A-Za-z0-9]{20,})\b"),
    re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{10,})['\"]?"),
    re.compile(r"(?i)(token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{10,})['\"]?"),
    re.compile(r"(?i)(password|passwd)\s*[:=]\s*['\"]?([^\s'\"']{3,})['\"]?"),
    re.compile(r"(?i)\b(ghp_[A-Za-z0-9]{36,})\b"),  # GitHub PAT
    re.compile(r"(?i)\b(xox[baprs]-[A-Za-z0-9-]+)\b"),  # Slack token
]


def detect_sensitive(text: str) -> List[str]:
    findings: List[str] = []
    for pat in SENSITIVE_PATTERNS:
        for m in pat.finditer(text):
            findings.append(m.group(0))
    return findings


def redact(text: str, placeholder: str = "[REDACTED]") -> str:
    for pat in SENSITIVE_PATTERNS:
        text = pat.sub(placeholder, text)
    return text
