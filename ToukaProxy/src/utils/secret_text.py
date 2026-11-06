"""把文字裡看起來像憑證的片段換掉（N7/N9）.

連線錯誤與工具錯誤都可能把 API 回應或網址原文帶出來，存起來或交給 hook 之前先過這裡。
"""

from __future__ import annotations

import re

from src.utils.mcp_secrets import sanitize_diagnostic

_CREDENTIAL_PATTERNS = re.compile(
    r"(?i)"
    r"(?:Bearer|Basic|Token|Authorization)\s+\S+"  # Auth header values
    r"|sk-[A-Za-z0-9\-_]+"  # OpenAI-style API keys
    r"|key=[A-Za-z0-9\-_/+=]{8,}"  # URL key params
    # 密碼、金鑰、權杖欄位：= 或 :，名稱與值可有引號（JSON 的引號可能再被跳脫一次）
    r"|(?:passw(?:or)?d|pwd|secret|private[-_]?key|api[-_]?key|token)"
    r"(?:[-_]?(?:access[-_]?)?key)?\\?[\"']?\s*[=:]\s*"
    r"(?:\\?\"[^\"]*\"|\\?'[^']*'|\S+)"
    r"|//[^/\s:@]+:[^/\s@]+@"  # URL userinfo (user:pass@host)
)


def redact_credentials(text: str) -> str:
    """把看起來像憑證的片段換成 ``[REDACTED]``。"""
    return _CREDENTIAL_PATTERNS.sub("[REDACTED]", text)


def sanitize_error_text(text: str) -> str:
    """錯誤內容要交給別人之前：遮憑證與本機路徑，限長（遮兩次結果不變）。"""
    # 限長切完尾端可能剛好是空白，不去掉的話遮第二次會少一個字
    return sanitize_diagnostic(redact_credentials(text)).rstrip()
