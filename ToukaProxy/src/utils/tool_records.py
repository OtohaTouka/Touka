"""工具紀錄：做過的事帶進下一輪（ADR-0319）。

送給模型的舊對話裡，助理那則依存下來的工具步驟（``steps``，和前端 ``ToolStep`` 同形狀）組成一段
系統整理的紀錄，加在內容前面（插話標註之後、原本文字之前）；存檔與畫面不變，角色不變、不算回合。

- 詳細（自己這段對話：網頁、心跳）：開頭說明行、每步一行（工具名、最多 3 個參數開頭、
  結果、結果開頭）、結束標記；詳細最多 6 步，其餘併成一行只列名稱與結果；每則最多 900 字。
- 簡短（較舊的輪，或離開這段對話的地方）：一行，只有工具名與固定的結果說法；每則最多 200 字。
- 視窗：輪照禁令 27 算（``count_rounds_from_linear``），每 10 輪移一次：最近 10～19 輪詳細、
  再往前 50 輪簡短、更早不帶。
- 參數與結果開頭：去掉時間尾註、控制與看不見的字元換成空白、遮掉像金鑰的內容
  （``utils/secrets.py``）、角括號與〔〕換成全形、照 code point 截斷。

前端 ``ToukaChat/src/lib/chat/toolRecords.ts`` 是同一套規則，黃金案例檔
``ToukaChat/src/lib/chat/__tests__/toolRecords.golden.json`` 鎖住兩邊輸出一字不差。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from src.services.tool_errors import (
    INTERNAL_ERROR,
    PERMISSION_DENIED,
    RESOURCE_NOT_FOUND,
    TIMEOUT_EXCEEDED,
    UPSTREAM_ERROR,
    VALIDATION_ERROR,
    extract_error_class,
    extract_field,
)

from .history_manager import count_rounds_from_linear
from .interjection_notes import annotate_node
from .secrets import MASK, is_secret_key, redact_secrets_from_content
from .time_utils import TIME_FOOTER_PREFIX
from .tool_record_marks import (
    SPACE_CLASS,
    SPACE_RUN_RE,
    TOOL_RECORD_BRIEF_CLOSE,
    TOOL_RECORD_BRIEF_OPEN,
    TOOL_RECORD_FOOTER,
    TOOL_RECORD_HEADER,
    neutralize_record_markers,
)

RecordLevel = Literal["detailed", "brief"]
RECORD_DETAILED: RecordLevel = "detailed"
RECORD_BRIEF: RecordLevel = "brief"

MAX_ARG_KEYS = 3
MAX_ARG_CHARS = 40
MAX_NAME_CHARS = 40
MAX_RESULT_CHARS = 80
MAX_DETAILED_STEPS = 6
MAX_DETAILED_CHARS = 900
MAX_BRIEF_CHARS = 200
WINDOW_ROUNDS = 10
BRIEF_ROUNDS = 50

# 只認這些錯誤種類（tool_errors 的固定值）；其他字串可能是工具自己寫的，一律只寫「失敗」
KNOWN_ERROR_CLASSES = frozenset({
    VALIDATION_ERROR, PERMISSION_DENIED, RESOURCE_NOT_FOUND,
    TIMEOUT_EXCEEDED, UPSTREAM_ERROR, INTERNAL_ERROR,
})

# 結果的固定說法（給模型看，不是畫面文字）；前端同名常數要一起改
_OK = "成功"
_FAILED = "失敗"
_NOT_RUN = {
    "rejected": "沒有執行（被拒絕）",
    "cancelled": "沒有執行（已取消）",
    "timeout": "沒有執行（核准逾時）",
    "isolation_unacknowledged": "沒有執行（隔離條件不符）",
    "isolation_incomplete": "沒有執行（隔離條件不符）",
}
_UNFINISHED = "沒有結果（這一輪先結束了）"
_UNFINISHED_STATUSES = ("running", "awaiting_approval")
_UNKNOWN_STATUS = "狀態不明"
_UNKNOWN_TOOL = "（不明）"
_ELLIPSIS = "…"
_REST_PREFIX = "另 {count} 步："
_VERIFY_ID = ":verify:"  # 互審驗證指令（<父>:verify:<n>）不是自己跑的

_FOOTER_RE = re.compile(f"[{SPACE_CLASS}]*" + re.escape(TIME_FOOTER_PREFIX) + "[^\n]*")
_FULLWIDTH = str.maketrans({"<": "＜", ">": "＞", "〔": "［", "〕": "］"})


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS


def _field(text: str, limit: int) -> str:
    """壓成一行、遮金鑰、角括號與〔〕換全形，再截斷."""
    flat = SPACE_RUN_RE.sub(" ", text).strip(" ")
    redacted, _ = redact_secrets_from_content(flat)
    return _truncate(redacted.translate(_FULLWIDTH), limit)


def _masked(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: MASK if is_secret_key(str(k)) else _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_masked(value), ensure_ascii=False, separators=(",", ":"), default=str)


def _name(step: dict[str, Any]) -> str:
    tool = step.get("tool")
    name = _field(tool, MAX_NAME_CHARS) if isinstance(tool, str) else ""
    return name or _UNKNOWN_TOOL


def _args_part(args: Any) -> str:
    if args is None:
        return ""
    if not isinstance(args, dict):
        return f"（「{_field(_as_text(args), MAX_ARG_CHARS)}」）"
    items = list(args.items())
    if not items:
        return ""
    shown = [
        f"{_field(str(key), MAX_ARG_CHARS)}=「"
        f"{MASK if is_secret_key(str(key)) else _field(_as_text(value), MAX_ARG_CHARS)}」"
        for key, value in items[:MAX_ARG_KEYS]
    ]
    if len(items) > MAX_ARG_KEYS:
        shown.append(_ELLIPSIS)
    return "（" + "、".join(shown) + "）"


def _error_class(step: dict[str, Any]) -> str | None:
    """新資料直接存在步驟上；舊資料退回解析結果開頭的 JSON。只認固定的錯誤種類."""
    stored = step.get("errorClass")
    result = step.get("result")
    if isinstance(stored, str):
        found: str | None = stored
    else:
        found = extract_error_class(result) if isinstance(result, str) else None
    return found if found in KNOWN_ERROR_CLASSES else None


def _outcome(step: dict[str, Any]) -> tuple[str, bool]:
    """（結果說法, 要不要附結果開頭）."""
    status = step.get("status")
    if status == "success":
        return _OK, True
    approval = step.get("approval")
    decision = approval.get("decision") if isinstance(approval, dict) else None
    if isinstance(decision, str) and decision in _NOT_RUN:
        return _NOT_RUN[decision], False
    if status == "error":
        error_class = _error_class(step)
        return (f"{_FAILED}（{error_class}）" if error_class else _FAILED), True
    if status in _UNFINISHED_STATUSES:
        return _UNFINISHED, False
    return _UNKNOWN_STATUS, False


def _head(step: dict[str, Any], *, failed: bool) -> str:
    result = step.get("result")
    if result is None:
        return ""
    if not isinstance(result, str):
        return _field(_as_text(result), MAX_RESULT_CHARS)
    message = extract_field(result, "message") if failed else None
    text = message if message is not None else _FOOTER_RE.sub("", result)
    return _field(text, MAX_RESULT_CHARS)


def _detailed_line(step: dict[str, Any]) -> str:
    args = _args_part(step.get("args"))
    outcome, with_head = _outcome(step)
    head = _head(step, failed=step.get("status") != "success") if with_head else ""
    arrow = "→ " if args else " → "
    return f"{_name(step)}{args}{arrow}{outcome}" + (f"：{head}" if head else "")


def _brief_item(step: dict[str, Any]) -> str:
    return f"{_name(step)} {_outcome(step)[0]}"


def _listable(step: Any) -> bool:
    """MCP 表單互動不是工具呼叫；互審驗證指令不是自己跑的；不是物件的舊資料略過."""
    if not isinstance(step, dict) or isinstance(step.get("interaction"), dict):
        return False
    step_id = step.get("id")
    return not (isinstance(step_id, str) and _VERIFY_ID in step_id)


def render_tool_record(steps: Any, level: RecordLevel) -> str:
    """把一則回覆的工具步驟寫成紀錄；沒有可列的步驟回空字串."""
    listed = [s for s in steps if _listable(s)] if isinstance(steps, list) else []
    if not listed:
        return ""
    if level == RECORD_BRIEF:
        budget = MAX_BRIEF_CHARS - len(TOOL_RECORD_BRIEF_OPEN) - len(TOOL_RECORD_BRIEF_CLOSE)
        items = "、".join(_brief_item(s) for s in listed)
        return TOOL_RECORD_BRIEF_OPEN + _truncate(items, budget) + TOOL_RECORD_BRIEF_CLOSE
    lines = [f"{i}. {_detailed_line(s)}" for i, s in enumerate(listed[:MAX_DETAILED_STEPS], 1)]
    rest = listed[MAX_DETAILED_STEPS:]
    if rest:
        lines.append(_REST_PREFIX.format(count=len(rest)) + "、".join(_brief_item(s) for s in rest))
    budget = MAX_DETAILED_CHARS - len(TOOL_RECORD_HEADER) - len(TOOL_RECORD_FOOTER) - 2
    return "\n".join((TOOL_RECORD_HEADER, _truncate("\n".join(lines), budget), TOOL_RECORD_FOOTER))


def _first_index_reaching(messages: list[dict[str, Any]], rounds: int) -> int:
    """最小的 i，使 messages[: i + 1] 已有 ``rounds`` 輪（輪數照禁令 27，隨前綴變長只增不減）."""
    lo, hi = 0, len(messages)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_rounds_from_linear(messages[: mid + 1]) >= rounds:
            hi = mid
        else:
            lo = mid + 1
    return lo


def record_levels(messages: list[dict[str, Any]]) -> list[RecordLevel | None]:
    """每則訊息帶哪種紀錄.

    助理那則屬於第 p 輪（它之前、含它的輪數減一，從最舊往新數）；N＝已完成的輪數，
    詳細起點 Bd＝10×floor(N/10)−10、簡短起點 Bb＝Bd−50：p ≥ Bd 詳細、Bb ≤ p < Bd 簡短、
    更早不帶。N 跨過 10 的倍數才改變，快取不會每一輪都失效.
    """
    total = count_rounds_from_linear(messages)
    detailed_from = WINDOW_ROUNDS * (total // WINDOW_ROUNDS) - WINDOW_ROUNDS
    first_detailed = _first_index_reaching(messages, detailed_from + 1)
    first_brief = _first_index_reaching(messages, detailed_from - BRIEF_ROUNDS + 1)
    levels: list[RecordLevel | None] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant" or i < first_brief:
            levels.append(None)
        else:
            levels.append(RECORD_DETAILED if i >= first_detailed else RECORD_BRIEF)
    return levels


def _neutralized(node: dict[str, Any]) -> dict[str, Any]:
    """內容與插話文字裡的假紀錄標記換掉（送出的副本）."""
    copy = dict(node)
    if isinstance(node.get("content"), str):
        copy["content"] = neutralize_record_markers(node["content"])
    interjections = node.get("interjections")
    if isinstance(interjections, list):
        copy["interjections"] = [
            {**item, "text": neutralize_record_markers(item["text"])}
            if isinstance(item, dict) and isinstance(item.get("text"), str)
            else item
            for item in interjections
        ]
    return copy


def annotate_history(
    messages: list[dict[str, Any]], *, level: RecordLevel = RECORD_DETAILED
) -> list[dict[str, Any]]:
    """組送出的歷史，不改傳入的節點.

    每則換掉假標記；助理那則依視窗加紀錄（``level`` 是最多到哪一種），再加插話標註與
    停止／出錯記號.
    """
    out = []
    for node, window in zip(messages, record_levels(messages), strict=True):
        effective = RECORD_BRIEF if RECORD_BRIEF in (window, level) else RECORD_DETAILED
        record = render_tool_record(node.get("steps"), effective) if window else ""
        out.append(annotate_node(_neutralized(node), tool_record=record))
    return out
