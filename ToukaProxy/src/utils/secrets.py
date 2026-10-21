"""金鑰遮罩工具 (Secret Masking) —— 全系統單一事實來源 (SSOT)。

GET 類端點回傳設定/連線時，secret 類值一律以遮罩取代真值（已設定但不洩漏內容）；
POST 類端點收到遮罩佔位值時保留既有真值（write-only 金鑰，縱深防禦）。

config 路由（.env）與 connections 路由（connections.json）共用此模組，避免遮罩邏輯
各寫一份而漂移（masking SSOT）。
"""

from __future__ import annotations

import re

# 遮罩標記：GET 回傳 secret 類值時以此取代真值（非空＝已設定，但不洩漏內容）。
MASK = "••••••••"

# secret 類鍵名特徵（不分大小寫）：金鑰/密鑰/權杖/密碼/憑證。涵蓋 `*_KEY`（如 ANTHROPIC_KEY、
# ACCESS_KEY，非僅 API_KEY）、CREDENTIAL、BEARER、PASSWD 等常見命名，避免 MCP env 機密漏遮罩。
_SECRET_KEY_RE = re.compile(r"(?i)(API_KEY|_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|BEARER)")
_CONTENT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def is_secret_key(key: str) -> bool:
    """鍵名是否命中 secret 特徵（API_KEY/SECRET/TOKEN/PASSWORD）。"""
    return bool(_SECRET_KEY_RE.search(key))


def mask_value(key: str, value: str) -> str:
    """依鍵名遮罩：secret 類且非空 → MASK；其餘原樣回傳。

    空值維持空（前端據此判斷未設定）；非 secret 維持明文。GET .env 設定時使用。
    """
    if value and is_secret_key(key):
        return MASK
    return value


def mask_secret(value: str | None) -> str | None:
    """遮罩已知 secret 欄位：非空 → MASK；空/None → 原樣（未設定）。

    用於欄位本身即 secret（如連線 apiKey），無需依鍵名判斷。保留 None/空以利前端
    區分「已設定」與「未設定」。
    """
    return MASK if value else value


def is_masked(value: str | None) -> bool:
    """判斷傳入值是否為遮罩佔位。

    POST write-only：收到遮罩佔位代表「未變更」，應保留既有真值、不覆寫、不落盤。
    """
    return value == MASK


def redact_secrets_from_content(content: str) -> tuple[str, bool]:
    """遮罩外部 resource/prompt 內容中的高信心 credential 形狀。

    這是 DLP backstop，不把任意長字串猜成 secret；installation env 的精確值仍由
    MCP manager 既有的 per-server sanitizer 負責。
    """
    redacted = content
    for pattern in _CONTENT_SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted, redacted != content
