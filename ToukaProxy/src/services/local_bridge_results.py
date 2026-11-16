"""小幫手沒做成時給模型看的錯誤（ADR-0312、ADR-0317）.

工具和核准前的預覽都用這一份，錯誤碼只對應一種說法。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from src.services import tool_errors
from src.services.local_bridge import (
    CODE_BUSY,
    CODE_NO_DECISION,
    CODE_OFFLINE,
    CODE_PAIRING_CHANGED,
    CODE_RESULT_UNKNOWN,
    CODE_TIMED_OUT,
)

if TYPE_CHECKING:
    from src.models.local_bridge import LocalJobResult

DO_NOT_WORK_AROUND: Final = "Tell the user. Do not try to reach the user's files another way."

# code → (錯誤類別, 訊息, 建議)
_BRIDGE_FAILURES: Final[dict[str, tuple[str, str, str]]] = {
    CODE_OFFLINE: (
        tool_errors.UPSTREAM_ERROR,
        "The local helper on the user's computer is not running.",
        f"Ask the user to start it. {DO_NOT_WORK_AROUND}",
    ),
    CODE_BUSY: (
        tool_errors.UPSTREAM_ERROR,
        "The local helper has too many requests waiting.",
        "Wait for the earlier requests to finish.",
    ),
    CODE_PAIRING_CHANGED: (
        tool_errors.UPSTREAM_ERROR,
        "The local helper pairing changed while this request was waiting.",
        DO_NOT_WORK_AROUND,
    ),
    CODE_NO_DECISION: (
        tool_errors.TIMEOUT_EXCEEDED,
        "No answer came in time, so nothing was changed.",
        "Do not retry unless the user asks. The user must be at the computer to approve.",
    ),
    CODE_TIMED_OUT: (
        tool_errors.TIMEOUT_EXCEEDED,
        "The local helper did not answer in time, so nothing was changed.",
        DO_NOT_WORK_AROUND,
    ),
    CODE_RESULT_UNKNOWN: (
        tool_errors.TIMEOUT_EXCEEDED,
        "The change may have been made, but the result never came back.",
        "Do not retry. Ask the user to check the local helper window.",
    ),
}


def failure_text(result: LocalJobResult) -> str:
    """小幫手拒絕、出錯、離線或逾時：通道的錯誤碼有固定說法，其他照小幫手的訊息。"""
    known = _BRIDGE_FAILURES.get(result.code or "")
    if known is not None:
        error_class, message, suggestion = known
        return tool_errors.build_tool_error(error_class, message, suggestion=suggestion)
    if result.status == "refused":
        return tool_errors.build_tool_error(
            tool_errors.PERMISSION_DENIED,
            result.message or "The local helper refused this request.",
            suggestion="Do not retry the same request. Tell the user what was refused.",
            reason=result.code or "refused",
        )
    return tool_errors.build_tool_error(
        tool_errors.UPSTREAM_ERROR,
        result.message or "The local helper could not finish this request.",
        suggestion=DO_NOT_WORK_AROUND,
        reason=result.code or "error",
    )
