"""P1 工具事件封套 SSOT 單元測試（提案 `p1-telemetry-envelope-plan.md`）——TDD.

「一份事實、兩個投影」：`ToolCallEvent`（屬性名對齊 OTel gen_ai）為規範事實，
`to_result_sse()` 是人面投影（wire 欄位凍結：前端 connections.ts / stream_tree.ts
依賴 id/tool/args/status/result/chunk），新屬性（duration_ms / error_class）一律
additive。builder 為純函式（每次新 dict）。
"""

from __future__ import annotations

from src.services import tool_errors
from src.utils.tool_events import (
    OPERATION_EXECUTE_TOOL,
    TOOL_ERROR,
    TOOL_RESULT,
    TOOL_START,
    TOOL_STREAM,
    ToolCallEvent,
    tool_error_event,
    tool_start_event,
    tool_stream_event,
)


def test_wire_type_constants():
    """wire 型別常量＝前端 customEventTypes 凍結契約。"""
    assert TOOL_START == "tool_start"
    assert TOOL_STREAM == "tool_stream"
    assert TOOL_RESULT == "tool_result"
    assert TOOL_ERROR == "error"


def test_tool_start_event_shape():
    ev = tool_start_event(call_id="c1", tool="read", args={"scope": "project", "path": "a"})
    assert ev == {
        "type": "tool_start",
        "id": "c1",
        "tool": "read",
        "args": {"scope": "project", "path": "a"},
    }


def test_tool_stream_event_shape():
    ev = tool_stream_event(call_id="c1", chunk="line\n")
    assert ev == {"type": "tool_stream", "id": "c1", "chunk": "line\n"}


def test_tool_error_event_shape():
    ev = tool_error_event(call_id="c1", message="工具執行失敗：X")
    assert ev == {"type": "error", "id": "c1", "message": "工具執行失敗：X"}


def test_result_sse_success_frozen_keys():
    """成功事件：鍵集恰為 wire 凍結四鍵（無 additive 時不得多鍵）。"""
    e = ToolCallEvent(
        call_id="c1",
        tool_name="read",
        arguments={"scope": "project"},
        status="ok",
        result_text="hello",
    )
    assert e.to_result_sse() == {
        "type": "tool_result",
        "id": "c1",
        "result": "hello",
        "status": "success",
    }


def test_result_sse_error_carries_error_class():
    e = ToolCallEvent(
        call_id="c2",
        tool_name="read",
        arguments={},
        status="error",
        error_class=tool_errors.PERMISSION_DENIED,
        result_text='{"error_class": "permission_denied"}',
    )
    ev = e.to_result_sse()
    assert ev["type"] == "tool_result"
    assert ev["id"] == "c2"
    assert ev["status"] == "error"
    assert ev["error_class"] == "permission_denied"


def test_result_sse_additive_duration_keeps_frozen_keys():
    e = ToolCallEvent(
        call_id="c3",
        tool_name="read",
        arguments={},
        status="ok",
        result_text="ok",
        duration_ms=12.5,
    )
    ev = e.to_result_sse()
    assert ev["duration_ms"] == 12.5
    # 凍結鍵仍完整
    assert ev["type"] == "tool_result"
    assert ev["id"] == "c3"
    assert ev["result"] == "ok"
    assert ev["status"] == "success"


def test_status_wire_mapping():
    assert ToolCallEvent("c", "t", {}, status="ok").status_wire() == "success"
    assert ToolCallEvent("c", "t", {}, status="error").status_wire() == "error"


def test_record_default_operation_is_execute_tool():
    e = ToolCallEvent("c", "t", {}, status="ok")
    assert e.operation == OPERATION_EXECUTE_TOOL == "execute_tool"


def test_builders_return_new_dict_each_call():
    a = tool_start_event(call_id="c", tool="t", args={})
    b = tool_start_event(call_id="c", tool="t", args={})
    assert a == b
    assert a is not b
