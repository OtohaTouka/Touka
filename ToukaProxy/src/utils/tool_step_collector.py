"""後端記下工具步驟（ADR-0319 PR 2）.

心跳、網頁文字版存檔（分頁關掉時）與 SNS 寫回時，讀同一條 SSE 的工具事件，組成和前端
``ToolStep`` 同形狀的步驟，存在助理節點的 ``steps``；下一輪由 ``utils/tool_records`` 寫成工具紀錄，
畫面也照常顯示。配對規則同前端 ``ToukaChat/src/lib/stream_tree.ts`` 的 ``applyEventToTree``，
不同的地方：

- 核准只存結果（``approval.decision``）：存下來的是事後紀錄，沒有可以再按的核准資料。
- MCP 表單互動不是工具呼叫，不存。
- 有存檔上限（結果、工具名與參數鍵名、每個參數值、整組參數、每則步驟數），超過的註明截斷；
  丟掉的步驟數記在節點上（只供診斷，紀錄與畫面不讀）。
- 帶 NaN／Infinity 的事件整個略過（存檔不接受這種值；前端 ``JSON.parse`` 失敗也是整個略過）。
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

from src.utils.secrets import mask_secret_keys
from src.utils.tool_events import (
    TOOL_APPROVAL_REQUEST,
    TOOL_APPROVAL_RESOLVED,
    TOOL_ERROR,
    TOOL_RESULT,
    TOOL_START,
    TOOL_STREAM,
)

# 存檔上限（code point）：紀錄只讀結果開頭 80 字與參數開頭 40 字，這裡留給畫面看
MAX_STORED_RESULT_CHARS = 2000
MAX_STORED_ARG_CHARS = 500
MAX_STORED_ARGS_CHARS = 4000  # 整組參數序列化後的上限，超過就改存截斷的 JSON 字串
MAX_STORED_NAME_CHARS = 200  # 工具名與參數鍵名
MAX_STORED_STEPS = 30
TRUNCATED_NOTE = "…（存檔時截斷）"
# 超過 MAX_STORED_STEPS 而沒存的步驟數，記在助理節點上（和 steps 並列）
STEPS_DROPPED_FIELD = "stepsDropped"

_STATUS_RUNNING = "running"
_STATUS_AWAITING = "awaiting_approval"
_STATUS_SUCCESS = "success"
_STATUS_ERROR = "error"
_SSE_DATA = "data: "


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATED_NOTE


def _capped_value(value: Any, limit: int) -> Any:
    """字串照字數截；其他值序列化後超過上限就改存截斷的 JSON 字串.

    變成字串後，下一輪的紀錄就不能再照鍵名遮蔽（``password`` 這類鍵），所以先把秘密鍵的值換掉。
    """
    if isinstance(value, str):
        return _cap(value, limit)
    try:
        dumped = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return _cap(str(mask_secret_keys(value)), limit)
    if len(dumped) <= limit:
        return value
    return _cap(json.dumps(mask_secret_keys(value), ensure_ascii=False), limit)


def _capped_args(args: Any) -> Any:
    if not isinstance(args, dict):
        return None if args is None else _capped_value(args, MAX_STORED_ARG_CHARS)
    capped = {
        _cap(str(key), MAX_STORED_NAME_CHARS): _capped_value(value, MAX_STORED_ARG_CHARS)
        for key, value in args.items()
    }
    return _capped_value(capped, MAX_STORED_ARGS_CHARS)


def _reject_constant(name: str) -> NoReturn:
    """``json.loads`` 遇到 NaN、Infinity 時呼叫：讓這個事件解析失敗、整個略過."""
    raise ValueError(name)


class ToolStepCollector:
    """一個回覆（網頁一輪、心跳一個脈衝、SNS 一則）的工具步驟."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._by_call_id: dict[str, dict[str, Any]] = {}
        self._dropped: set[str] = set()

    def feed_sse(self, chunk: str) -> None:
        """讀一段 SSE 文字（可含多個事件）；不是 JSON 物件的資料行略過."""
        for raw in chunk.split("\n"):
            line = raw.strip()
            if not line.startswith(_SSE_DATA):
                continue
            try:
                event = json.loads(line[len(_SSE_DATA):], parse_constant=_reject_constant)
            except ValueError:
                continue
            if isinstance(event, dict):
                self.feed(event)

    def feed(self, event: dict[str, Any]) -> None:
        """讀一個已解析的事件；不是工具事件的略過（MCP 表單互動也略過）."""
        kind = event.get("type")
        if kind == TOOL_START:
            self._start(event)
        elif kind == TOOL_APPROVAL_REQUEST:
            self._awaiting(event)
        elif kind == TOOL_APPROVAL_RESOLVED:
            step = self._by_call_id.get(str(event.get("call_id") or ""))
            if step is not None and isinstance(event.get("decision"), str):
                step["approval"] = {"decision": event["decision"]}
        elif kind == TOOL_RESULT:
            self._result(event)
        elif kind == TOOL_STREAM:
            self._stream(event)
        elif kind == TOOL_ERROR:
            step = self._by_call_id.get(str(event.get("id") or ""))
            if step is not None:
                step["status"] = _STATUS_ERROR
                message = event.get("message")
                step["result"] = _cap(message, MAX_STORED_RESULT_CHARS) if isinstance(
                    message, str) else message

    @property
    def steps(self) -> list[dict[str, Any]]:
        """目前的步驟（複本）."""
        return [dict(step) for step in self._steps]

    def node_fields(self) -> dict[str, Any]:
        """要存進助理節點的欄位；沒有步驟回空 dict（節點不多欄位）."""
        fields: dict[str, Any] = {}
        if self._steps:
            fields["steps"] = self.steps
        if self._dropped:
            fields[STEPS_DROPPED_FIELD] = len(self._dropped)
        return fields

    def _add(self, step: dict[str, Any]) -> dict[str, Any] | None:
        if len(self._steps) >= MAX_STORED_STEPS:
            self._dropped.add(step["id"])
            return None
        self._steps.append(step)
        self._by_call_id[step["id"]] = step
        return step

    def _start(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("id") or "")
        step = self._by_call_id.get(call_id)
        if step is None:
            self._add({
                "id": call_id,
                "tool": _cap(str(event.get("tool") or ""), MAX_STORED_NAME_CHARS),
                "args": _capped_args(event.get("args")),
                "status": _STATUS_RUNNING,
            })
            return
        # 核准後的 tool_start：同一步升級成執行中，參數換成完整的（核准卡只有預覽）
        step["status"] = _STATUS_RUNNING
        if event.get("args") is not None:
            step["args"] = _capped_args(event["args"])

    def _awaiting(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("call_id") or "")
        step = self._by_call_id.get(call_id)
        if step is not None:  # 前端會另外新增一步；同一個 id 只留一步（現在的事件順序不會發生）
            step["status"] = _STATUS_AWAITING
            return
        self._add({
            "id": call_id,
            "tool": _cap(str(event.get("tool") or ""), MAX_STORED_NAME_CHARS),
            "args": _capped_args(event.get("args")),
            "status": _STATUS_AWAITING,
        })

    def _result(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("id") or "")
        step = self._by_call_id.get(call_id)
        if step is None:
            return
        step["status"] = _STATUS_ERROR if event.get("status") == _STATUS_ERROR else _STATUS_SUCCESS
        result = event.get("result")
        if result is None:
            step.pop("result", None)
        else:
            step["result"] = _capped_value(result, MAX_STORED_RESULT_CHARS)
        # 跟著這次的結果：事件沒帶種類就不留舊的（同前端）
        step.pop("errorClass", None)
        if isinstance(event.get("error_class"), str):
            step["errorClass"] = event["error_class"]

    def _stream(self, event: dict[str, Any]) -> None:
        step = self._by_call_id.get(str(event.get("id") or ""))
        chunk = event.get("chunk")
        if step is None or not isinstance(chunk, str):
            return
        current = step.get("result")
        # 每接一段都截回上限：長輸出不會一直變大（截斷後再接的部分會被截掉）
        step["result"] = _cap((current if isinstance(current, str) else "") + chunk,
                              MAX_STORED_RESULT_CHARS)
