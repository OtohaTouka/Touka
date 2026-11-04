"""把文字裡看起來像憑證的片段換掉（N7/N9）.

連線錯誤與工具錯誤都可能把 API 回應或網址原文帶出來，存起來或交給 hook 之前先過這裡。
"""

from __future__ import annotations

import re

_CREDENTIAL_PATTERNS = re.compile(
    r"(?i)"
    r"(?:Bearer|Basic|Token|Authorization)\s+\S+"  # Auth header values
    r"|sk-[A-Za-z0-9\-_]+"  # OpenAI-style API keys
    r"|key=[A-Za-z0-9\-_]{8,}"  # URL key params
    r"|api[-_]?key\s*[=:]\s*\S+"  # api_key=... patterns
    r"|[Pp]assword\s*[=:]\s*\S+"  # password=...
    r"|//[^/\s:@]+:[^/\s@]+@"  # URL userinfo (user:pass@host)
)


def redact_credentials(text: str) -> str:
    """把看起來像憑證的片段換成 ``[REDACTED]``。"""
    return _CREDENTIAL_PATTERNS.sub("[REDACTED]", text)
