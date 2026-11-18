"""後端記下工具步驟（ADR-0319 PR 2）.

- 收集器讀同一條 SSE 的工具事件，組成和前端 ``ToolStep`` 同形狀的步驟（配對規則同前端
  ``applyEventToTree``）；核准只存結果，MCP 表單互動不存；有存檔上限。
- 心跳：一個脈衝的步驟掛在那個脈衝最後寫入的助理節點；只用了工具也寫一則（內容空白）。
- 網頁文字版存檔（分頁關掉時）：存步驟；沒文字但有步驟也存。
- SNS：回覆連同步驟存進對話；沒文字不往外送。
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import PersistedStreamAdmission, SavedResult
from src.models.sns import SNSIncomingEvent
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services import dispatcher as dispatcher_mod
from src.services.interjection_queue import InMemoryInterjectionQueue
from src.services.sns_gateway import SNSGatewayService
from src.services.soul_service import SoulService
from src.services.stream_buffer_service import StreamBufferService
from src.utils.async_db import SessionSnapshot, init_db_async, session_service
from src.utils.history_manager import TreeMessageManager
from src.utils.secrets import MASK, mask_secret_keys
from src.utils.tool_events import (
    ToolCallEvent,
    mcp_interaction_request_event,
    mcp_interaction_resolved_event,
    tool_approval_request_event,
    tool_approval_resolved_event,
    tool_error_event,
    tool_start_event,
    tool_stream_event,
)
from src.utils.tool_records import RECORD_DETAILED, render_tool_record
from src.utils.tool_step_collector import (
    MAX_STORED_ARG_CHARS,
    MAX_STORED_ARGS_CHARS,
    MAX_STORED_NAME_CHARS,
    MAX_STORED_RESULT_CHARS,
    MAX_STORED_STEPS,
    STEPS_DROPPED_FIELD,
    TRUNCATED_NOTE,
    ToolStepCollector,
)


def _sse(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def _text(text: str) -> str:
    return _sse({"choices": [{"delta": {"content": text}}]})


def _result(call_id: str, status: str, text: str, error_class: str | None = None) -> dict:
    return ToolCallEvent(call_id, "t", {}, status, error_class=error_class,
                         result_text=text).to_result_sse()


def _approval(call_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return tool_approval_request_event(
        exec_id=f"e-{call_id}", call_id=call_id, tool=tool, args_preview=args, agent_id="A",
        risk="dangerous", timeout_s=60,
    )


def _decided(call_id: str, tool: str, decision: str) -> dict[str, Any]:
    return tool_approval_resolved_event(
        exec_id=f"e-{call_id}", call_id=call_id, tool=tool, decision=decision
    )


def _collect(*events: dict[str, Any]) -> ToolStepCollector:
    collector = ToolStepCollector()
    collector.feed_sse(_sse(*events))
    return collector


READ_STEPS = (
    tool_start_event(call_id="c1", tool="read", args={"path": "a.md"}),
    _result("c1", "ok", "內容"),
)
READ_STEP = {"id": "c1", "tool": "read", "args": {"path": "a.md"}, "status": "success",
             "result": "內容"}
# 模型寫出 NaN／Infinity 的參數：SSE 照 json.dumps 預設寫成 NaN、Infinity，存檔卻不接受
NAN_START = _sse(tool_start_event(call_id="c9", tool="calc",
                                  args={"x": float("nan"), "y": float("inf")}))


# --- 收集器 -------------------------------------------------------------------------------


def test_pairs_starts_and_results_like_the_frontend():
    collector = _collect(
        *READ_STEPS,
        tool_start_event(call_id="c2", tool="write", args={"path": "b.md"}),
        _result("c2", "error", "不行", "permission_denied"),
    )
    assert collector.steps == [
        READ_STEP,
        {"id": "c2", "tool": "write", "args": {"path": "b.md"}, "status": "error",
         "result": "不行", "errorClass": "permission_denied"},
    ]
    assert collector.node_fields() == {"steps": collector.steps}


def test_approvals_keep_only_the_decision_and_render_as_not_run():
    collector = _collect(
        _approval("c1", "exec", {"command": "rm"}),
        _decided("c1", "exec", "rejected"),
        _result("c1", "error", "拒絕", "permission_denied"),
        _approval("c2", "write", {"path": "x"}),
        _decided("c2", "write", "approved"),
        tool_start_event(call_id="c2", tool="write", args={"path": "x", "text": "全文"}),
        _result("c2", "ok", "ok"),
        _approval("c3", "exec", {}),
    )
    rejected, approved, waiting = collector.steps
    assert rejected == {
        "id": "c1", "tool": "exec", "args": {"command": "rm"}, "status": "error",
        "result": "拒絕", "errorClass": "permission_denied", "approval": {"decision": "rejected"},
    }
    assert approved["args"] == {"path": "x", "text": "全文"}
    assert (approved["status"], approved["approval"]) == ("success", {"decision": "approved"})
    # 等到這一輪結束還沒核准：沒有核准資料（畫面不會出現可按的核准卡）
    assert waiting == {"id": "c3", "tool": "exec", "args": {}, "status": "awaiting_approval"}
    lines = render_tool_record(collector.steps, RECORD_DETAILED).splitlines()
    assert lines[1:4] == [
        "1. exec（command=「rm」）→ 沒有執行（被拒絕）",
        "2. write（path=「x」、text=「全文」）→ 成功：ok",
        "3. exec → 沒有結果（這一輪先結束了）",
    ]


def test_streams_errors_and_form_interactions():
    collector = _collect(
        tool_start_event(call_id="c1", tool="exec", args={"command": "ls"}),
        tool_stream_event(call_id="c1", chunk="a"),
        tool_stream_event(call_id="c1", chunk="b"),
        tool_start_event(call_id="c2", tool="fetch", args={"url": "u"}),
        tool_error_event(call_id="c2", message="boom"),
        mcp_interaction_request_event(
            interaction_id="i1", call_id="c3", tool="form", message="填", schema={}, timeout_s=30
        ),
        mcp_interaction_resolved_event(
            interaction_id="i1", call_id="c3", tool="form", decision="accepted"
        ),
        {"type": "error", "message": "整條串流出錯"},
    )
    assert collector.steps == [
        {"id": "c1", "tool": "exec", "args": {"command": "ls"}, "status": "running",
         "result": "ab"},
        {"id": "c2", "tool": "fetch", "args": {"url": "u"}, "status": "error", "result": "boom"},
    ]


def test_a_later_result_without_error_class_drops_the_earlier_one():
    collector = _collect(
        tool_start_event(call_id="c1", tool="exec", args={}),
        _result("c1", "error", "x", "timeout_exceeded"),
        _result("c1", "error", "y"),
    )
    assert "errorClass" not in collector.steps[0]


def test_reads_sse_chunks_the_way_the_writers_get_them():
    collector = ToolStepCollector()
    collector.feed_sse("data: [DONE]\n\ndata: not json\n\ndata: [1, 2]\n\n: keep-alive\n\n")
    collector.feed_sse(_sse(READ_STEPS[0]))
    collector.feed_sse(_sse(READ_STEPS[1]) + _text("文字"))
    assert collector.steps == [READ_STEP]
    assert ToolStepCollector().node_fields() == {}


def test_events_with_nan_are_skipped_like_a_failed_json_parse_in_the_browser():
    collector = ToolStepCollector()
    collector.feed_sse(NAN_START + _sse(*READ_STEPS))
    collector.feed_sse(_sse(_result("c9", "ok", "3")))
    assert collector.steps == [READ_STEP]


def test_an_approval_request_for_an_existing_step_reuses_it():
    collector = _collect(
        tool_start_event(call_id="c1", tool="exec", args={"a": 1}),
        _approval("c1", "exec", {"a": 1}),
    )
    assert collector.steps == [
        {"id": "c1", "tool": "exec", "args": {"a": 1}, "status": "awaiting_approval"}
    ]


def test_names_keys_and_whole_args_are_capped_too():
    many = {f"k{i}": "v" * 100 for i in range(60)}
    collector = _collect(
        tool_start_event(call_id="c1", tool="t" * (MAX_STORED_NAME_CHARS + 5),
                         args={"鍵" * (MAX_STORED_NAME_CHARS + 1): 1}),
        tool_start_event(call_id="c2", tool="t", args=many),
    )
    first, second = collector.steps
    assert first["tool"] == "t" * MAX_STORED_NAME_CHARS + TRUNCATED_NOTE
    assert first["args"] == {"鍵" * MAX_STORED_NAME_CHARS + TRUNCATED_NOTE: 1}
    whole = json.dumps(many, ensure_ascii=False)
    assert second["args"] == whole[:MAX_STORED_ARGS_CHARS] + TRUNCATED_NOTE


def test_args_cut_to_a_json_string_still_hide_secret_keys():
    """截成字串後紀錄讀不回鍵名，所以收集器先遮：和前端存的（仍是 dict、照鍵名遮）一樣不外露."""
    config = {"password": "hunter2", "host": "h" * (MAX_STORED_ARG_CHARS + 100)}
    whole = {"api_token": "abc123", **{f"k{i}": "v" * 100 for i in range(60)}}
    collector = _collect(
        tool_start_event(call_id="c1", tool="t", args={"config": config}),
        tool_start_event(call_id="c2", tool="t", args=whole),
    )
    first, second = collector.steps
    assert "hunter2" not in first["args"]["config"]
    assert f'"password": "{MASK}"' in first["args"]["config"]
    assert isinstance(second["args"], str)
    assert "abc123" not in second["args"]
    assert f'"api_token": "{MASK}"' in second["args"]


def test_mask_secret_keys_walks_dicts_and_lists():
    value = {"items": [{"token": "t1"}, {"name": "n"}], "nested": {"api_key": "k"}, "plain": "p"}
    assert mask_secret_keys(value) == {
        "items": [{"token": MASK}, {"name": "n"}], "nested": {"api_key": MASK}, "plain": "p",
    }


def test_storage_caps():
    events = [
        tool_start_event(call_id="c0", tool="read", args={
            "text": "x" * (MAX_STORED_ARG_CHARS + 1), "items": list(range(400)), "n": 3,
        }),
        _result("c0", "ok", "長" * (MAX_STORED_RESULT_CHARS + 10)),
    ]
    events += [tool_start_event(call_id=f"c{i}", tool="t", args={})
               for i in range(1, MAX_STORED_STEPS + 5)]
    events.append(_result(f"c{MAX_STORED_STEPS + 2}", "ok", "太晚了"))
    collector = _collect(*events)

    first = collector.steps[0]
    assert first["result"] == "長" * MAX_STORED_RESULT_CHARS + TRUNCATED_NOTE
    assert first["args"]["text"] == "x" * MAX_STORED_ARG_CHARS + TRUNCATED_NOTE
    items = json.dumps(list(range(400)), ensure_ascii=False)
    assert first["args"]["items"] == items[:MAX_STORED_ARG_CHARS] + TRUNCATED_NOTE
    assert first["args"]["n"] == 3
    assert len(collector.steps) == MAX_STORED_STEPS
    assert collector.node_fields()[STEPS_DROPPED_FIELD] == 5


def test_streamed_output_stops_growing_at_the_cap():
    collector = _collect(
        tool_start_event(call_id="c1", tool="exec", args={}),
        *[tool_stream_event(call_id="c1", chunk="y" * 900) for _ in range(5)],
    )
    assert collector.steps[0]["result"] == "y" * MAX_STORED_RESULT_CHARS + TRUNCATED_NOTE


# --- 心跳 ---------------------------------------------------------------------------------


async def _pulse(monkeypatch, *chunks: str) -> tuple[int, list[dict[str, Any]]]:
    saved: list[dict[str, Any]] = []

    async def _chunks():
        for chunk in chunks:
            yield chunk

    async def _dispatch(**_kwargs: Any) -> Any:
        return MagicMock(body_iterator=_chunks())

    async def _save(_sid: str, _rev: str, changes: Any, _identity: Any) -> SavedResult:
        saved.append(copy.deepcopy(changes.messages))
        return SavedResult(status="saved", representation="full", revision="next")

    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "醒來")
    session = {"id": "s", "title": "t", "messages": tree}
    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    monkeypatch.setattr(heartbeat, "session_service", MagicMock(
        read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev")),
        conditional_save_session=_save,
    ))
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    turns = await heartbeat._execute_single_pulse(
        "s", identity, "claude-sonnet-4-6", PipelineContext(), 1, 0.0, ""
    )
    assistants = [] if not saved else [
        node for node in TreeMessageManager.get_linear_path(saved[-1])
        if node["role"] == "assistant"
    ]
    return turns, assistants


async def test_a_pulse_saves_its_tool_steps_on_its_reply(monkeypatch):
    turns, assistants = await _pulse(monkeypatch, _sse(*READ_STEPS), _text("完成"))
    assert turns == 1
    assert [(n["content"], n["steps"]) for n in assistants] == [("完成", [READ_STEP])]


async def test_split_turns_put_the_steps_on_the_last_reply_of_the_pulse(monkeypatch):
    split = "做事\n--- 第 1 輪 自主行動"
    turns, assistants = await _pulse(monkeypatch, _text(split), _sse(*READ_STEPS), _text("收尾"))
    assert turns == 2
    assert [n.get("steps") for n in assistants] == [None, [READ_STEP]]

    turns, assistants = await _pulse(monkeypatch, _sse(*READ_STEPS), _text(split))
    assert turns == 1
    assert [(n["content"], n.get("steps")) for n in assistants] == [(split, [READ_STEP])]


async def test_a_tools_only_pulse_still_writes_a_reply(monkeypatch):
    turns, assistants = await _pulse(monkeypatch, _sse(*READ_STEPS))
    assert turns == 1
    assert [(n["content"], n["steps"]) for n in assistants] == [("", [READ_STEP])]


async def test_a_pulse_with_nan_arguments_still_saves(monkeypatch):
    turns, assistants = await _pulse(monkeypatch, NAN_START + _sse(*READ_STEPS), _text("完成"))
    assert turns == 1
    assert [(n["content"], n["steps"]) for n in assistants] == [("完成", [READ_STEP])]


async def test_a_pulse_without_tools_adds_no_steps(monkeypatch):
    _turns, assistants = await _pulse(monkeypatch, _text("只有文字"))
    assert "steps" not in assistants[0]


# --- 網頁文字版存檔（分頁關掉時由後端存） ----------------------------------------------------

UI_OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
UI_TREE = {
    "messages": {"user": {"id": "user", "role": "user", "content": "hi", "parentId": None}},
    "currentId": "user",
}


@pytest.fixture
async def ui(tmp_path, monkeypatch) -> StreamBufferService:
    monkeypatch.setattr("src.utils.async_db.get_db_path", lambda: tmp_path / "test.db")
    await init_db_async()
    queue = InMemoryInterjectionQueue()
    monkeypatch.setattr(dispatcher_mod, "interjection_queue", queue)
    buffer = StreamBufferService(interjections=queue)
    monkeypatch.setattr(dispatcher_mod, "stream_buffer_service", buffer)
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    return buffer


async def _pump(buffer: StreamBufferService, *chunks: str) -> tuple[Any, dict[str, Any]]:
    assert await session_service.upsert_session(
        "s1", "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )
    snapshot = await session_service.read_persistence_snapshot("s1", UI_OWNER)
    admission = PersistedStreamAdmission(
        mode="persistent", revision=snapshot.revision, user_parent_id="user",
        assistant_node_id="assistant",
    )
    entry = await buffer.create("s1", identity=UI_OWNER, admission=admission)

    async def generation():
        for chunk in chunks:
            yield chunk

    await dispatcher_mod._pump_ui_stream(
        generation(), "s1", entry.stream_id, "m", UI_OWNER, admission
    )
    saved = await session_service.read_persistence_snapshot("s1", UI_OWNER)
    return entry.terminal.persistence, saved.session["messages"]["messages"].get("assistant", {})


async def test_the_text_save_keeps_the_tool_steps(ui):
    persistence, node = await _pump(ui, _sse(*READ_STEPS), _text("回答"))
    assert persistence.status == "saved"
    assert (node["content"], node["steps"]) == ("回答", [READ_STEP])


async def test_a_tools_only_turn_is_saved_instead_of_skipped(ui):
    persistence, node = await _pump(ui, _sse(*READ_STEPS))
    assert persistence.status == "saved"
    assert (node["content"], node["steps"]) == ("", [READ_STEP])


async def test_a_turn_with_nan_arguments_is_still_saved(ui):
    persistence, node = await _pump(ui, NAN_START, _sse(*READ_STEPS), _text("回答"))
    assert persistence.status == "saved"
    assert node["steps"] == [READ_STEP]


async def test_a_turn_without_text_or_tools_is_still_skipped(ui):
    persistence, node = await _pump(ui, _sse({"type": "touka_progress", "kind": "react_slots"}))
    assert (persistence.status, persistence.reason) == ("skipped", "no_text")
    assert node == {}


# --- SNS ----------------------------------------------------------------------------------


async def _sns(*chunks: str) -> tuple[str, MagicMock, MagicMock]:
    async def _chunks():
        for chunk in chunks:
            yield chunk

    async def _proxy(_request: Any, _ctx: Any, is_anthropic: bool = False) -> Any:  # noqa: ARG001
        return MagicMock(body_iterator=_chunks())

    adapter = MagicMock(agent_id="Alice", show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="c1", content="嗨")
    session = MagicMock(
        open_channel_session=AsyncMock(return_value="sns-1"),
        append_message_to_session=AsyncMock(return_value=True),
        read_execution_snapshot=AsyncMock(),  # 送出前的歸屬檢查（ADR-0322 PR 2）
    )
    with (
        patch(
            "src.services.sns_gateway.agent_service.identify_agent",
            side_effect=lambda value: {"agent_id": value, "is_toukasoul": True},
        ),
        patch(
            "src.services.sns_gateway.identity_service.get_identity",
            side_effect=lambda value: AgentIdentity.for_role(value, "Owner"),
        ),
        patch("src.services.sns_gateway.session_service", session),
        patch("src.services.sns_gateway.process_proxy_request", side_effect=_proxy),
    ):
        text = await SNSGatewayService().on_event(adapter, event)
    return text, session.append_message_to_session, adapter.send_message


def _assistant_call(append: MagicMock) -> Any:
    calls = [c for c in append.await_args_list if c.args[1] == "assistant"]
    assert len(calls) == 1
    return calls[0]


async def test_sns_saves_the_reply_with_its_tool_steps():
    text, append, send = await _sns(_sse(*READ_STEPS), _text("回覆"))
    assert text == "回覆"
    call = _assistant_call(append)
    assert call.args[:3] == ("sns-1", "assistant", "回覆")
    assert call.kwargs["extra_fields"] == {"steps": [READ_STEP]}
    send.assert_awaited_once()


async def test_sns_saves_a_tools_only_turn_without_sending_anything():
    text, append, send = await _sns(_sse(*READ_STEPS))
    assert text == ""
    call = _assistant_call(append)
    assert call.args[:3] == ("sns-1", "assistant", "")
    assert call.kwargs["extra_fields"] == {"steps": [READ_STEP]}
    send.assert_not_awaited()


async def test_append_message_stores_the_extra_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("src.utils.async_db.get_db_path", lambda: tmp_path / "test.db")
    await init_db_async()
    assert await session_service.upsert_session(
        "s1", "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )
    assert await session_service.append_message_to_session(
        "s1", "assistant", "回覆", name="Alice", identity=UI_OWNER,
        extra_fields={"steps": [READ_STEP], "content": "偷改", "parentId": None},
    )
    assert await session_service.append_message_to_session(
        "s1", "user", "再一則", identity=UI_OWNER
    )
    snapshot = await session_service.read_persistence_snapshot("s1", UI_OWNER)
    linear = TreeMessageManager.get_linear_path(snapshot.session["messages"])
    assert [(n["role"], n["content"], n.get("steps")) for n in linear] == [
        ("user", "hi", None), ("assistant", "回覆", [READ_STEP]), ("user", "再一則", None),
    ]  # 額外欄位只能加新的，不能蓋掉節點本身的欄位
