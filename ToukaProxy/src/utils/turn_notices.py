"""畫面上的小提示（ADR-0322 PR 2b）.

工具計時、攔截、工具輪額度、中斷、拒答與各種警告這類系統說明，以前是一行文字混在 AI 的回覆裡：
存成 AI 說的話，下一輪 AI 以為是自己寫的，SNS 還把它們送到外面。現在改送一個 ``turn_notice``
事件，不進回覆文字、不送給模型：

- 網頁照種類（固定列舉）用 i18n 顯示，存在助理節點的 ``notices``（每則只存種類與參數）；
  心跳、網頁文字版存檔與 SNS 由 ``utils/tool_step_collector`` 存同一個欄位。
- VS Code 擴充不再寫第三份種類對照，直接顯示事件帶的 ``text``（後端組好、已截短）。

舊對話 content 裡的這些行，組歷史送給模型時拿掉（``strip_legacy_log_lines``）。樣式清單記的是
舊對話裡實際有的格式，不跟著目前的提示文字改；和前端 ``ToukaChat/src/lib/chat/turnNotices.ts``
同名常數一字不差（測試比對），黃金案例前後端共用。
"""

from __future__ import annotations

from typing import Any

from src.utils.secret_text import sanitize_error_text

TURN_NOTICE_EVENT = "turn_notice"
NOTICES_FIELD = "notices"
MAX_NOTICES_PER_NODE = 50
MAX_NOTICE_CHARS = 300

NOTICE_TOOL_FINISHED = "tool_finished"
NOTICE_TOOL_FAILED = "tool_failed"
NOTICE_TOOL_EXCEPTION = "tool_exception"
NOTICE_TOOL_BLOCKED = "tool_blocked"
NOTICE_HOOK_DENIED = "hook_denied"
NOTICE_APPROVAL_BLOCKED = "approval_blocked"
NOTICE_ROUNDS_EXHAUSTED = "rounds_exhausted"
NOTICE_COST_EXHAUSTED = "cost_exhausted"
NOTICE_INTERRUPTED = "interrupted"
NOTICE_REFUSAL = "refusal"
NOTICE_SAFETY = "safety_filtered"
NOTICE_TOOL_HALLUCINATION = "tool_hallucination"
NOTICE_RECORD_IMITATION = "record_imitation"
NOTICE_MEMORY_HALLUCINATION = "memory_hallucination"
NOTICE_HONESTY = "honesty_warning"
NOTICE_KINDS = (
    NOTICE_TOOL_FINISHED,
    NOTICE_TOOL_FAILED,
    NOTICE_TOOL_EXCEPTION,
    NOTICE_TOOL_BLOCKED,
    NOTICE_HOOK_DENIED,
    NOTICE_APPROVAL_BLOCKED,
    NOTICE_ROUNDS_EXHAUSTED,
    NOTICE_COST_EXHAUSTED,
    NOTICE_INTERRUPTED,
    NOTICE_REFUSAL,
    NOTICE_SAFETY,
    NOTICE_TOOL_HALLUCINATION,
    NOTICE_RECORD_IMITATION,
    NOTICE_MEMORY_HALLUCINATION,
    NOTICE_HONESTY,
)

# 舊對話裡的日誌行：(開頭, 結尾)。從開頭比對到它產生時自帶的結尾為止（不比到行尾，模型在同一行
# 引用時後面自己寫的字才不會被吃掉），不要求在行首（模型先寫字再呼叫工具時，日誌行直接黏在後面）。
# 中間不能換行、不能有結尾的第一個字、不能超過 LEGACY_LOG_MAX_MIDDLE；結尾空字串表示開頭就是整段。
LEGACY_LOG_LINES: tuple[tuple[str, str], ...] = (
    ("🚫 攔截未授權調用: ", "\n"),
    ("🚫 hook 拒絕: ", "\n"),
    ("🖐️ 待審批攔截（fail-closed）: ", "\n"),
    ("✅ `時間記錄：", "`\n"),
    ("❌ `工具執行失敗`", "\n"),
    ("❌ 執行發生例外：", "\n"),
    ("⏹️ 已由使用者中斷。", ""),
    ("⚠️ 已達總量上限（累計成本），進行最後總結。", "\n"),
    ("⚠️ 工具輪額度已盡（", "），進行最後總結。\n"),
    ("⚠️ 模型拒絕回答。", ""),
    ("⚠️ 模型拒絕回答：", ""),
    ("⚠️ 回應被廠商的安全過濾擋下。", ""),
    ("⚠️ **[工具幻覺警告]** ", "。"),
    ("⚠️ **[記憶幻覺警告]** ", "。"),
    ("⚠️ **[誠實度警報]** ", "不要憑空編造。"),
)
LEGACY_LOG_MAX_MIDDLE = 300


def _capped(text: str) -> str:
    return text[:MAX_NOTICE_CHARS]


def _clean_params(params: dict[Any, Any]) -> dict[str, str | int]:
    """只收字串與整數（布林不算），字串截短."""
    return {
        _capped(str(key)): _capped(value) if isinstance(value, str) else value
        for key, value in params.items()
        if isinstance(value, (str, int)) and not isinstance(value, bool)
    }


def turn_notice_event(kind: str, text: str, **params: Any) -> dict[str, Any]:
    """一則小提示事件；``text`` 是給擴充顯示的那一行（前後換行不留），參數與文字都截短."""
    return {
        "type": TURN_NOTICE_EVENT,
        "kind": kind,
        "params": _clean_params(params),
        "text": _capped(text.strip("\n")),
    }


def tool_exception_notice(tool: str, detail: str) -> dict[str, Any]:
    """工具出例外：例外原文先消毒才放進小提示（會被存下、畫面看得到）."""
    safe = sanitize_error_text(detail)
    return turn_notice_event(
        NOTICE_TOOL_EXCEPTION, f"❌ 執行發生例外：{safe}", tool=tool, detail=safe
    )


def stored_notice(event: dict[str, Any]) -> dict[str, Any] | None:
    """事件存進節點的樣子（種類與參數）；種類不認得或參數不是物件就不存."""
    kind = event.get("kind")
    params = event.get("params")
    if kind not in NOTICE_KINDS or not isinstance(params, dict):
        return None
    return {"kind": kind, "params": _clean_params(params)}


def _strip_one(text: str, start: str, end: str) -> str:
    out: list[str] = []
    pos = 0
    while True:
        found = text.find(start, pos)
        if found < 0:
            out.append(text[pos:])
            return "".join(out)
        after = found + len(start)
        stop = after
        if end:
            closing = text.find(end, after)
            middle = text[after:closing] if closing >= 0 else ""
            if (
                closing < 0
                or len(middle) > LEGACY_LOG_MAX_MIDDLE
                or "\n" in middle
                or end[0] in middle
            ):
                out.append(text[pos:after])
                pos = after
                continue
            stop = closing + len(end)
        out.append(text[pos:found])
        pos = stop


def strip_legacy_log_lines(text: str) -> str:
    """拿掉舊日誌行；有拿掉才去掉前後多出來的換行，沒有就原樣返回."""
    stripped = text
    for start, end in LEGACY_LOG_LINES:
        stripped = _strip_one(stripped, start, end)
    return text if stripped == text else stripped.strip("\n")
