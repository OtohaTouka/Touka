"""金鑰遮罩工具 (Secret Masking) —— 全系統單一事實來源 (SSOT)。

GET 類端點回傳設定/連線時，secret 類值一律以遮罩取代真值（已設定但不洩漏內容）；
POST 類端點收到遮罩佔位值時保留既有真值（write-only 金鑰，縱深防禦）。

config 路由（.env）與 connections 路由（connections.json）共用此模組，避免遮罩邏輯
各寫一份而漂移（masking SSOT）。
"""

from __future__ import annotations

import re
from typing import Any

# 遮罩標記：GET 回傳 secret 類值時以此取代真值（非空＝已設定，但不洩漏內容）。
MASK = "••••••••"

# 樣式一律 re.ASCII（ADR-0324）：`\b` 與大小寫只認英數，和前端（JS 不帶 u 旗標）一致，金鑰前後
# 直接接中文時也遮得到。空白不用 `\s`：Python 的 ASCII `\s` 只認英文空白、JS 的 `\s` 認全形空白，
# 寫成兩邊一字不差的字元集（兩種 `\s` 的聯集）。前端 `lib/chat/toolRecords.ts` 複製同一組文字，
# pytest 逐字核對。
_WS = r"[\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
_NOT_WS_OR_QUOTE = (
    r"[^\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff\"']"
)
_IGNORE_CASE = re.IGNORECASE | re.ASCII

# secret 類鍵名特徵（不分大小寫）：金鑰/密鑰/權杖/密碼/憑證。涵蓋 `*_KEY`（如 ANTHROPIC_KEY、
# ACCESS_KEY，非僅 API_KEY）、`apiKey`、`x-api-key`、CREDENTIAL、BEARER、PASSWD 等常見命名，
# 避免 MCP env 機密漏遮罩。
_SECRET_KEY_RE = re.compile(
    r"(API[-_]?KEY|_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|BEARER)", _IGNORE_CASE
)
_CONTENT_SECRET_PATTERNS = (
    re.compile(r"\bBearer" + _WS + r"+[A-Za-z0-9._~+/=-]{12,}", _IGNORE_CASE),
    # 上一條沒遮到的 Basic 與短的 Bearer（整段標頭換掉）
    re.compile(
        r"\bAuthorization" + _WS + r"*:" + _WS + r"*(?:Basic|Bearer)" + _WS + r"+"
        + _NOT_WS_OR_QUOTE + r"+",
        _IGNORE_CASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.ASCII),
    re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}\b", re.ASCII),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b", re.ASCII),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", re.ASCII),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b", re.ASCII),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b", re.ASCII),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", re.ASCII),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", re.ASCII),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL | re.ASCII,
    ),
)
# 只在「做過的事」紀錄裡另外遮的（ADR-0324）：不放進上面共用的鍵名，設定頁會把
# `OAUTH_AUTHORIZATION_URL` 這類不是金鑰的設定也遮掉。字串裡 `"鍵名": "值"` 的值也遮。
_RECORD_EXTRA_KEY_RE = re.compile(r"AUTHORIZATION", _IGNORE_CASE)
_RECORD_KEY_VALUE_RE = re.compile(r'("([A-Za-z0-9_.-]{1,64})" *: *)"(?:[^"\\]|\\.)*"', re.ASCII)


def is_secret_key(key: str) -> bool:
    """鍵名是否命中 secret 特徵（API_KEY/SECRET/TOKEN/PASSWORD）。"""
    return bool(_SECRET_KEY_RE.search(key))


def mask_secret_keys(value: Any) -> Any:
    """遞迴把秘密鍵名（``is_secret_key``）的值換成 ``MASK``；dict 與 list 都走進去，其他值原樣."""
    if isinstance(value, dict):
        return {k: MASK if is_secret_key(str(k)) else mask_secret_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secret_keys(v) for v in value]
    return value


def is_record_secret_key(key: str) -> bool:
    """紀錄裡要遮的鍵名：共用的那些，再加 `Authorization`（ADR-0324）。"""
    return is_secret_key(key) or bool(_RECORD_EXTRA_KEY_RE.search(key))


def mask_record_secret_keys(value: Any) -> Any:
    """``mask_secret_keys`` 的紀錄版：巢狀物件裡 ``is_record_secret_key`` 的值換成 ``MASK``."""
    if isinstance(value, dict):
        return {
            k: MASK if is_record_secret_key(str(k)) else mask_record_secret_keys(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_record_secret_keys(v) for v in value]
    return value


def mask_record_key_values(text: str) -> str:
    """字串裡 ``"鍵名": "值"`` 的寫法，鍵名要遮時值換成 ``MASK``（紀錄專用，ADR-0324）。"""

    def _replace(match: re.Match[str]) -> str:
        if is_record_secret_key(match.group(2)):
            return f'{match.group(1)}"{MASK}"'
        return match.group(0)

    return _RECORD_KEY_VALUE_RE.sub(_replace, text)


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
