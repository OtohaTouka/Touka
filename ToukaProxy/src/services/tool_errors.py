"""工具錯誤結構化 SSOT（工具介面重設計 Tier 1，提案 §5）.

模型面的工具失敗訊息統一為可解析的 JSON：``{"error_class": ..., "message": ...,
"suggestion": ...}``，讓模型能依錯誤類別自我修正（validation → 補參數重試、
permission → 換工具或說明限制、not_found → 勿再呼叫…）。UI 面（SSE type=log /
type=error 事件）維持中文人話，不經此模組。

慣例：
  - ``error_class`` 必須是 JSON 第一個 key —— ``is_tool_error`` 以前綴偵測，
    容忍下游（如時間注入）在字串尾端附註。
  - message / suggestion 一律英文、不洩漏設定內部細節（延續 E#21 精神）。
"""

from __future__ import annotations

import json
from typing import Any

# --- 錯誤類別常量（提案 §5 的五類 ＋ internal_error 承接未預期例外）---
VALIDATION_ERROR = "validation_error"
PERMISSION_DENIED = "permission_denied"
RESOURCE_NOT_FOUND = "resource_not_found"
TIMEOUT_EXCEEDED = "timeout_exceeded"
UPSTREAM_ERROR = "upstream_error"
INTERNAL_ERROR = "internal_error"

_PREFIX = '{"error_class"'
_STATUS_PREFIX = '{"status"'  # 非錯誤的結構化狀態（如 approval_pending / running）
_SUCCESS_PREFIX = '{"ok"'  # P2a-2：結構化成功（供執行層穩健判定，免子字串誤判）


def build_tool_error(
    error_class: str,
    message: str,
    *,
    suggestion: str | None = None,
    **fields: Any,
) -> str:
    """組出模型面的結構化錯誤 JSON 字串（error_class 保證為第一個 key）.

    Args:
        error_class: 錯誤類別（本模組常量之一）.
        message: 英文、可讀、不洩漏內部細節的一句話.
        suggestion: 模型可據以自我修正的建議（可選）.
        **fields: 額外結構化欄位（如 denied_tool、reason、timeout_ms）.

    Returns:
        str: JSON 字串.
    """
    payload: dict[str, Any] = {"error_class": error_class, "message": message}
    if suggestion:
        payload["suggestion"] = suggestion
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def build_tool_success(
    message: str,
    *,
    preview: str | None = None,
    **fields: Any,
) -> str:
    """組出模型面的結構化成功 JSON（``ok`` 保證為第一個 key）.

    供「回傳最終狀態」的動詞（edit/insert）使用：讓執行層以結構化前綴穩健判定成功，
    不再靠 ``"Error:"``/``"❌"`` 子字串——回傳內容（如帶 ❌ 的檔案片段）不會被誤判成錯誤。

    Args:
        message: 英文、可讀的一句話（如 "Edited foo.py"）.
        preview: 選填的變更後片段（帶行號），讓模型免重讀即可驗證.
        **fields: 額外結構化欄位.

    Returns:
        str: JSON 字串.
    """
    payload: dict[str, Any] = {"ok": True, "message": message}
    if preview is not None:
        payload["preview"] = preview
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def is_tool_success(text: str) -> bool:
    """判斷字串是否為本模組產生的結構化成功 payload（前綴偵測，容忍尾端附註）."""
    return bool(text) and text.lstrip().startswith(_SUCCESS_PREFIX)


def is_tool_error(text: str) -> bool:
    """判斷字串是否為（或起始於）本模組產生的結構化錯誤.

    以前綴偵測而非完整解析：容忍下游在尾端附註（如時間footer），
    也避免對每個成功結果做 JSON 解析的成本.
    """
    return bool(text) and text.lstrip().startswith(_PREFIX)


def is_structured_payload(text: str) -> bool:
    """判斷字串是否為結構化模型面 payload（錯誤或狀態 JSON）.

    供執行層決定「不要附加時間尾綴等自由文字」——結構化 payload（``error_class``
    錯誤或 ``status`` 狀態，如 execute_command 的 approval_pending）必須保持
    JSON 可解析。
    """
    if not text:
        return False
    stripped = text.lstrip()
    return stripped.startswith((_PREFIX, _STATUS_PREFIX, _SUCCESS_PREFIX))


def extract_error_class(text: str) -> str | None:
    """從模型面結構化錯誤字串解析 ``error_class``（供事件封套抬成一級屬性）.

    以 ``raw_decode`` 只解析開頭第一個 JSON 值，容忍下游在尾端附註（如時間 footer）；
    非本模組錯誤（``is_tool_error`` 為否）、狀態 payload 或解析失敗一律回 ``None``.
    """
    if not is_tool_error(text):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except (ValueError, TypeError):
        return None
    error_class = obj.get("error_class") if isinstance(obj, dict) else None
    return error_class if isinstance(error_class, str) else None


def classify_exception(exc: BaseException) -> str:
    """把 Python 例外映射到錯誤類別（供執行層 except 路徑使用）."""
    if isinstance(exc, TimeoutError):
        return TIMEOUT_EXCEEDED
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED
    if isinstance(exc, FileNotFoundError):
        return RESOURCE_NOT_FOUND
    if isinstance(exc, (TypeError, ValueError)):
        return VALIDATION_ERROR
    return INTERNAL_ERROR


_SUGGESTIONS: dict[str, str] = {
    VALIDATION_ERROR: "Check the tool's parameter schema and retry with corrected arguments.",
    PERMISSION_DENIED: "Do not retry this tool; explain the restriction or use an allowed one.",
    RESOURCE_NOT_FOUND: "Verify the target exists (or use a tool from the provided list).",
    TIMEOUT_EXCEEDED: "Retry with a smaller scope, or inform the user about the timeout.",
    UPSTREAM_ERROR: "The failure is on a backing service; retry later or inform the user.",
    INTERNAL_ERROR: "Do not blindly retry; report the failure to the user.",
}


def from_exception(exc: BaseException, detail: str) -> str:
    """由例外直接組出結構化錯誤（detail 應已截斷、不含堆疊）."""
    error_class = classify_exception(exc)
    return build_tool_error(
        error_class,
        f"Tool execution failed: {detail}",
        suggestion=_SUGGESTIONS[error_class],
    )
