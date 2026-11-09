"""回覆失敗時交給畫面的原因（路線圖第 8 項 PR 1）。

畫面以前只有一句固定文字。這裡釘住：
- 原因走進度事件，串流結束事件的欄位不動（前端逐欄比對）；
- 廠商原文只有人類 admin 的事件才帶，別的情況事件裡根本沒有這個鍵；
- 消毒與限長在 ``RoundError`` 建立時就做，只取本文的 ``error.message``；
- 非 UI 路徑靠 ``user_message`` 的文字判斷失敗（scorecard、memory_eval），一個字都不能變。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest
from anthropic import APIStatusError
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import responses_adapter, retry
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.claude_adapter import _sdk_error
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.identity import AgentIdentity
from src.models.session_persistence import StreamTerminalEvent, UiGenerationStopped
from src.utils.mcp_secrets import DIAGNOSTIC_DISPLAY_CHARS
from src.utils.progress_events import GENERATION_FAILURE_KIND

from tests.helpers.stream_transport import FakeUpstream, json_reply

_SOUL = AgentIdentity.for_role("FailureProbe", "Owner")
_ADMIN = AgentIdentity(agent_id="admin-id", human_role="admin", is_human=True)
_NORMAL = AgentIdentity(agent_id="user-id", human_role="normal", is_human=True)
_IMPERSONATOR = AgentIdentity(agent_id="soul-id", human_role="admin", is_human=False)  # admin 代打
_GUARD_S = 5.0
_VENDOR_TEXT = "The model `grok-old` has been retired"
_BODY = json.dumps({"error": {"message": _VENDOR_TEXT, "type": "not_found_error"}})


def _request(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


def _loop_ctx(**extra: Any) -> dict[str, Any]:
    return {
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": "r-fail", "t0": 0.0, "agent_id": _SOUL.agent_id, "identity": _SOUL,
        "session_id": "s-fail", "provider": "Grok", "api_key": "fake-key",
        "url": "https://api.x.test/v1/chat/completions", "custom_connection": False,
        **extra,
    }  # fmt: skip


@pytest.fixture
def no_wait(monkeypatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(react_core, "_retry_sleep", fake_sleep)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *_a, **_k: None)


async def _ui_chunks(adapter: Any, model: str, ctx: dict[str, Any]) -> list[str]:
    """跑一輪會失敗的 UI 回合：收集丟出 ``UiGenerationStopped`` 之前的所有輸出。"""
    chunks: list[str] = []
    async with asyncio.timeout(_GUARD_S):
        with pytest.raises(UiGenerationStopped) as caught:
            async for chunk in react_core.run_react_loop(adapter, _request(model), ctx):
                chunks.append(chunk)
    assert caught.value.generation == "error"
    return chunks


def _failure_events(chunks: list[str]) -> list[dict[str, Any]]:
    events = []
    for chunk in chunks:
        if not chunk.startswith("data: {"):
            continue
        event = json.loads(chunk[len("data: "):])
        if event.get("type") == "touka_progress" and event.get("kind") == GENERATION_FAILURE_KIND:
            events.append(event)
    return events


# ── 從廠商本文取原文 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        _BODY,
        json.loads(_BODY),
        {"type": "error", "error": {"type": "not_found_error", "message": _VENDOR_TEXT}},
        {"error": _VENDOR_TEXT},
        {"message": _VENDOR_TEXT},
        _BODY.encode("utf-8"),
    ],
)
def test_the_vendor_message_is_read_from_the_body(body):
    assert retry.error_message(body) == _VENDOR_TEXT


@pytest.mark.parametrize(
    "body",
    [None, "", "not json", "<html>nope</html>", [], {}, {"error": {}}, {"error": {"message": 3}},
     {"error": None}, 3, b"\xff\xfe"],
)
def test_a_body_without_a_vendor_message_gives_nothing(body):
    assert retry.error_message(body) is None


# ── RoundError ───────────────────────────────────────────────────


def test_the_detail_is_cleaned_when_the_error_is_built():
    error = RoundError("x", detail="bad request api_key=sk-abcdefghijklmnop123456")
    assert "sk-abcdefghijklmnop123456" not in (error.detail or "")


def test_the_detail_has_a_length_limit():
    assert len(RoundError("x", detail="字" * 5000).detail or "") <= DIAGNOSTIC_DISPLAY_CHARS


def test_no_detail_stays_none():
    assert RoundError("x").detail is None


def test_the_detail_does_not_change_equality():
    assert RoundError("x", detail="a") == RoundError("x", detail="b")


# ── 四個非 200 的地方都取得到原文 ─────────────────────────────────


async def test_openai_compatible_error_carries_the_vendor_message(monkeypatch, no_wait):
    FakeUpstream([json_reply(_BODY, 404)]).install(monkeypatch)
    ctx = _loop_ctx(ui_authorized=True, ui_requester=_ADMIN)
    events = _failure_events(await _ui_chunks(OpenAIAdapter(stream=False), "grok-4", ctx))
    assert events == [{"type": "touka_progress", "kind": GENERATION_FAILURE_KIND,
                       "error_type": "model_not_found", "detail": _VENDOR_TEXT}]


async def test_gemini_error_carries_the_vendor_message(monkeypatch, no_wait):
    body = json.dumps(
        {"error": {"code": 400, "message": _VENDOR_TEXT, "status": "INVALID_ARGUMENT"}}
    )
    FakeUpstream([json_reply(body, 400)]).install(monkeypatch)
    ctx = _loop_ctx(ui_authorized=True, ui_requester=_ADMIN, gemini_api_key="fake-key")
    events = _failure_events(await _ui_chunks(GeminiAdapter(stream=True), "gemini-3.8-flash", ctx))
    assert [(e["error_type"], e["detail"]) for e in events] == [("invalid_request", _VENDOR_TEXT)]


def test_claude_error_carries_the_vendor_message():
    response = httpx2.Response(404, request=httpx2.Request("POST", "https://api.anthropic.test"))
    body = {"type": "error", "error": {"type": "not_found_error", "message": _VENDOR_TEXT}}
    result = _sdk_error(APIStatusError("boom", response=response, body=body))
    assert (result.error_type, result.detail) == ("model_not_found", _VENDOR_TEXT)


async def test_responses_error_carries_the_vendor_message(monkeypatch):
    transport = httpx2.MockTransport(lambda _req: httpx2.Response(400, json=json.loads(_BODY)))

    def http_client(**kwargs: Any) -> Any:
        return openai.DefaultAsyncHttpxClient(transport=transport, **kwargs)

    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(
        OpenAIChatRequest(model="gpt-6-astra", messages=[{"role": "user", "content": "hi"}]),
        {"provider": "OpenAI", "base_url": "https://api.openai.test/v1", "api_key": "fake-key"},
    )
    try:
        items = [item async for item in adapter.send_round(state)]
    finally:
        await adapter.close(state)
    assert len(items) == 1 and isinstance(items[0], RoundError)
    assert (items[0].error_type, items[0].detail) == ("invalid_request", _VENDOR_TEXT)


def test_an_error_inside_a_200_stream_carries_the_vendor_message():
    body = {"type": "api_error", "message": _VENDOR_TEXT}
    result = retry.on_error_body(RoundError("x"), body, "Claude")
    assert result.detail == _VENDOR_TEXT


# ── 誰看得到廠商原文（事件本身，不是前端藏起來）─────────────────────


@pytest.mark.parametrize(
    ("requester_ctx", "has_detail"),
    [
        ({"ui_requester": _ADMIN}, True),
        ({"ui_requester": _NORMAL}, False),
        ({}, False),
        ({"ui_requester": "admin"}, False),
        ({"ui_requester": _IMPERSONATOR}, False),
    ],
)
async def test_only_a_human_admin_event_carries_the_vendor_text(
    monkeypatch, no_wait, requester_ctx, has_detail
):
    FakeUpstream([json_reply(_BODY, 404)]).install(monkeypatch)
    ctx = _loop_ctx(ui_authorized=True, **requester_ctx)
    events = _failure_events(await _ui_chunks(OpenAIAdapter(stream=False), "grok-4", ctx))
    assert len(events) == 1
    assert events[0]["error_type"] == "model_not_found"
    assert ("detail" in events[0]) is has_detail


async def test_a_soul_identity_does_not_stand_in_for_the_requester(monkeypatch, no_wait):
    """``identity`` 是換過的靈魂身分，不能拿來判斷看的人是不是 admin。"""
    FakeUpstream([json_reply(_BODY, 404)]).install(monkeypatch)
    ctx = _loop_ctx(ui_authorized=True, identity=_ADMIN)
    events = _failure_events(await _ui_chunks(OpenAIAdapter(stream=False), "grok-4", ctx))
    assert "detail" not in events[0]


# ── 別的路徑與合約不動 ────────────────────────────────────────────


async def test_the_non_ui_path_has_no_event_and_keeps_its_text(monkeypatch, no_wait):
    FakeUpstream([json_reply(_BODY, 404)]).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in react_core.run_react_loop(
            OpenAIAdapter(stream=False), _request("grok-4"), _loop_ctx(ui_requester=_ADMIN))]
    assert _failure_events(chunks) == []
    joined = "".join(chunks)
    assert "API Error (404)" in joined
    assert chunks[-1] == "data: [DONE]\n\n"


def test_the_stream_terminal_event_fields_are_unchanged():
    """前端逐欄比對（connections.ts）：多一欄，所有回覆的結束事件都會被丟掉。"""
    assert sorted(StreamTerminalEvent.model_fields) == sorted(
        ["type", "session_id", "stream_id", "assistant_node_id", "user_parent_id", "generation",
         "persistence"]
    )
