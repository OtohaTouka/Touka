"""3b-U2：相容廠商 Chat 主迴圈的自寫 SSE 串流（ADR-0248 附註 6／7／14／16／17；D1／D2／D6）。

- 完整＝``[DONE]``：沒送就斷線、200 之後才來的 error 物件、壞 JSON、chunk 帶 ``message`` 都轉
  RoundError，下游沒有半截文字；錯誤文字（``API Error (400)``、401 提示）直接字串釘住。
- D2：``stream``／``stream_options`` 只在送出當下合併，不寫進 payload。
- D6：規則表 ``stream`` 關掉時主迴圈改走非串流 POST。
- 兩段逾時（自寫 SSE 每收到位元組就 touch，``:`` 註解行與空行也算）、整輪上限、取消時等上游
  關閉、逾時區塊不跨過 yield。三個逾時常數縮小，外層包 ``asyncio.timeout`` 防卡死。

上游一律用 tests/helpers/stream_transport（httpx 真 Request／Response、送出內容從
request.content 解析、回應都要關）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import openai_adapter, stream_timeout
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity
from src.services.models.model_rules import rules_for

from tests.helpers.stream_transport import FakeUpstream, Reply, json_reply, sse

_GUARD_S = 5.0
_FIRST, _IDLE, _ROUND = 0.6, 0.2, 1.2
_URL = "https://api.x.test/v1/chat/completions"


def _c(delta: dict, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


_TEXT = [_c({"role": "assistant", "content": "好"}), _c({}, "stop"),
         {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}]


def _request(**extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model="grok-4", messages=[OpenAIMessage(role="user", content="hi")],
                             **extra)


def _ctx(**extra: Any) -> dict:
    return {"provider": "Grok", "url": _URL, "api_key": "fake-key",
            "custom_connection": False, **extra}  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）


async def _round(monkeypatch, replies: list[Reply], *, request: Any = None,
                 ctx: dict | None = None) -> tuple[Any, FakeUpstream, Any]:
    upstream = FakeUpstream(replies).install(monkeypatch)
    adapter = OpenAIAdapter(stream=True)
    state = await adapter.prepare_request(request or _request(), ctx or _ctx())
    try:
        async with asyncio.timeout(_GUARD_S):
            items = [item async for item in adapter.send_round(state)]
    finally:
        await adapter.close(state)
    assert len(items) == 1  # 串流只在最後交出一項（不回半輪）
    return items[0], upstream, state


# ── 完整訊號與錯誤文字 ──────────────────────────────────────────────────────


async def test_full_stream_folds_to_the_non_streaming_shape(monkeypatch):
    result, upstream, state = await _round(monkeypatch, [Reply(pieces=[sse(*_TEXT)])])
    assert result == {"choices": [{"index": 0, "message": {"content": "好", "role": "assistant"},
                                   "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    (sent,) = upstream.sent
    assert sent == {"model": "grok-4", "messages": [{"role": "user", "content": "hi"}],
                    "stream": True, "stream_options": {"include_usage": True}}
    assert "stream" not in state.payload and "stream_options" not in state.payload  # D2
    assert OpenAIAdapter().partial_usage(state) is None  # 完整且有用量：核心照常入帳，不重記
    upstream.assert_all_closed()


async def test_stream_cut_before_done_is_round_error(monkeypatch):
    result, upstream, _ = await _round(monkeypatch, [Reply(pieces=[sse(*_TEXT, done=False)])])
    assert result == RoundError("❌ **Grok 連線錯誤**\n\n`上游串流沒送完就中斷（缺 [DONE]）`")
    upstream.assert_all_closed()


async def test_error_object_after_200_is_round_error(monkeypatch):
    error = '{"error": {"message": "overloaded", "type": "server_error"}}'
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[sse(_TEXT[0], error)])])
    assert result == RoundError(f"Grok API Error (200)\n\n`{error}`")


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "post"])
async def test_status_error_text_is_pinned(monkeypatch, stream):
    body = '{"error": {"message": "bad"}}'
    upstream = FakeUpstream([json_reply(body, 400)]).install(monkeypatch)
    adapter = OpenAIAdapter(stream=stream)
    state = await adapter.prepare_request(_request(), _ctx())
    (result,) = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    assert result == RoundError(f"Grok API Error (400)\n\n`{body}`")
    assert ("stream" in upstream.sent[0]) is stream
    upstream.assert_all_closed()


async def test_missing_openai_key_hint_on_the_stream_path(monkeypatch):
    ctx = {"provider": "OpenAI", "url": _URL, "api_key": None, "custom_connection": False}
    result, _, _ = await _round(monkeypatch, [json_reply("denied", 401)],
                                request=OpenAIChatRequest(model="gpt-5", messages=[
                                    OpenAIMessage(role="user", content="hi")]), ctx=ctx)
    assert result == RoundError(
        "OpenAI Access Denied (401)\n\nMissing `OPENAI_API_KEY` in `.env`. Raw error:\n`denied`")


@pytest.mark.parametrize(("event", "kind"), [
    ("{not json", "JSONDecodeError"),
    ('{"choices": [{"index": 0, "message": {"content": "x"}}]}', "StreamShapeError"),
], ids=["bad-json", "message-key"])
async def test_malformed_chunks_are_round_errors(monkeypatch, event, kind):
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[sse(event)])])
    assert isinstance(result, RoundError)
    assert result.user_message.startswith(f"❌ **Grok 連線錯誤**\n\n`{kind}: ")


def _loop_ctx(rid: str, **extra: Any) -> dict:
    return {**_ctx(), "protocol_manager": MagicMock(), "tool_registry": {},
            "memory_tool_registry": {}, "plugin_registry": {}, "request_id": rid, "t0": 0.0,
            "agent_id": "Sumi", "identity": AgentIdentity.for_role("Sumi", "Owner"), **extra}


async def test_main_loop_sends_no_partial_text_downstream(monkeypatch):
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    half = [_c({"role": "assistant", "content": "半截回答"})]
    upstream = FakeUpstream([Reply(pieces=[sse(*half, done=False)])]).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in handle_openai_request(_request(), _loop_ctx("req-half"))]
    joined = "".join(chunks)
    assert upstream.sent[0]["stream"] is True  # handler 明確打開串流
    assert "半截回答" not in joined
    assert "缺 [DONE]" in joined
    assert chunks[-1] == "data: [DONE]\n\n"


# ── D1／D6：預設非串流；規則表開關；自訂連線 ────────────────────────────────


@pytest.mark.parametrize(("adapter", "ctx"), [
    (OpenAIAdapter(), _ctx()),  # complete_once 的預設
    (OpenAIAdapter(stream=True), _ctx(custom_connection=True)),  # 自訂連線（路由層判定）
    (OpenAIAdapter(stream=True), _ctx(provider="Ollama")),  # 不在規則表的廠商清單
], ids=["default", "custom-connection", "unlisted-provider"])
async def test_non_streaming_paths_post_once_without_stream_fields(monkeypatch, adapter, ctx):
    body = {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 1}}
    upstream = FakeUpstream([json_reply(body)]).install(monkeypatch)
    state = await adapter.prepare_request(_request(), ctx)
    (result,) = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    assert result == body
    (sent,) = upstream.sent
    assert "stream" not in sent and "stream_options" not in sent


async def test_rules_switch_off_keeps_main_loop_on_post(monkeypatch):
    real = rules_for
    monkeypatch.setattr(openai_adapter, "rules_for", lambda m: replace(real(m), stream=False))
    body = {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 1}}
    upstream = FakeUpstream([json_reply(body), json_reply(body)]).install(monkeypatch)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in handle_openai_request(_request(), _loop_ctx("req-off"))]
    assert len(upstream.requests) == 1
    assert "stream" not in upstream.sent[0]
    assert '"content": "好"' in "".join(chunks)


# ── 兩段逾時、整輪上限、取消（附註 6／16） ────────────────────────────────


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", _FIRST)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", _IDLE)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", _ROUND)


def _timeout(phase: str) -> RoundError:
    return RoundError(f"❌ **Grok 連線錯誤**\n\n`上游串流逾時：{phase}`")


_KEEPALIVE = b": keep-alive\n\n"


async def test_first_byte_slower_than_idle_but_within_first_event(monkeypatch, short):
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[0.35, sse(*_TEXT)])])
    assert result["choices"][0]["message"]["content"] == "好"


async def test_no_first_byte_within_first_event_times_out(monkeypatch, short):
    result, upstream, _ = await _round(monkeypatch, [Reply(pieces=[_FIRST + 0.5, sse(*_TEXT)])])
    assert result == _timeout("等不到第一個回應")
    upstream.assert_all_closed()


async def test_comment_lines_keep_the_stream_alive(monkeypatch, short):
    """只有 ``:`` 註解行與空行的 0.8 秒（超過閒置也超過首事件）不逾時。"""
    alive = [p for _ in range(16) for p in (0.05, _KEEPALIVE)]
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[sse(_TEXT[0], done=False),
                                                            *alive, sse(*_TEXT[1:])])])
    assert result["choices"][0]["message"]["content"] == "好"


async def test_comment_lines_forever_hit_the_round_cap(monkeypatch, short):
    alive = [p for _ in range(200) for p in (0.05, _KEEPALIVE)]
    result, upstream, _ = await _round(monkeypatch, [Reply(pieces=[sse(_TEXT[0], done=False),
                                                                   *alive])])
    assert result == _timeout("整輪超過上限")
    upstream.assert_all_closed()


async def test_stalled_stream_hits_idle_timeout(monkeypatch, short):
    result, upstream, _ = await _round(
        monkeypatch, [Reply(pieces=[sse(_TEXT[0], done=False), asyncio.Event()])])
    assert result == _timeout("閒置太久")
    upstream.assert_all_closed()


async def test_slow_consumer_after_the_round_is_not_cancelled(monkeypatch, short):
    FakeUpstream([Reply(pieces=[sse(*_TEXT)])]).install(monkeypatch)
    adapter = OpenAIAdapter(stream=True)
    state = await adapter.prepare_request(_request(), _ctx())
    async with asyncio.timeout(_GUARD_S):
        rounds = adapter.send_round(state)
        first = await rounds.__anext__()
        await asyncio.sleep(_FIRST + 0.1)
        with pytest.raises(StopAsyncIteration):
            await rounds.__anext__()
    await adapter.close(state)
    assert not isinstance(first, RoundError)


async def test_cancel_waits_until_upstream_is_closed(monkeypatch, short):
    hold = asyncio.Event()
    upstream = FakeUpstream([Reply(pieces=[sse(_TEXT[0], done=False), hold])]).install(monkeypatch)
    adapter = OpenAIAdapter(stream=True)
    state = await adapter.prepare_request(_request(), _ctx())

    async def consume() -> None:
        async for _ in react_core.pump_send_round(adapter.send_round(state)):
            pass

    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(consume())
        while not upstream.responses:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        upstream.assert_all_closed()  # 取消傳回來時上游已關
    await adapter.close(state)
    assert json.loads(upstream.requests[0].content)["stream"] is True
