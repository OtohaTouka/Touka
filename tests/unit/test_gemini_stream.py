"""3b-U3：Gemini 主迴圈的自寫 SSE 串流（ADR-0248「Gemini 自寫串流的彙整規則」；D1／D3／D6）。

- 彙整規則本身在 test_sse_stream.py；這裡驗下一輪每個簽章只回送一次。
- 完整＝任一 candidate 有 ``finishReason``，或 ``promptFeedback.blockReason``（只有 blockReason＝
  safety，不是 RoundError）；沒有訊號就斷線、200 之後的 error 物件、壞 JSON 都轉 RoundError，
  下游沒有半截文字。錯誤文字直接字串釘住。
- D1：adapter 預設非串流（complete_once）；D6：規則表 ``stream`` 關掉時主迴圈改走非串流 POST。
- 兩段逾時（每收到位元組就 touch）、取消時等上游關閉；逾時與半截不重試（重試搬家是 U5a）。
- 沒完成或沒回報用量的一輪照 U1b 規則記；串流沒開始不記。

估算值：請求 ``{"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}`` 的 JSON 是 59 個
非 CJK 字 → 59÷3.5＝16.9 → 17；「abcdefg」7÷3.5 → 2；「好」1 個 CJK → 1。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import gemini_adapter, stream_timeout
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.gemini_handler import handle_gemini_request
from src.models.identity import AgentIdentity
from src.services.models.model_rules import rules_for

from tests.helpers.stream_transport import FakeUpstream, Reply, sse, status_reply

_GUARD_S = 5.0
_FIRST, _IDLE, _ROUND = 0.6, 0.2, 1.2
_BASE = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash"
_ZERO = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def _chunk(*parts: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], **({"usageMetadata": usage} if usage else {})}


# ── adapter：完整訊號、錯誤文字、網址 ────────────────────────────────────────


def _request(**extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model="gemini-2.5-flash",
                             messages=[OpenAIMessage(role="user", content="hi")], **extra)


_CTX = {"gemini_api_key": "fake-key", "agent_id": "Sumi"}
_TEXT = [_chunk({"text": "你"}, usage={"promptTokenCount": 3, "candidatesTokenCount": 1}),
         _chunk({"text": "好"}, finish="STOP",
                usage={"promptTokenCount": 3, "candidatesTokenCount": 2})]


async def _round(monkeypatch, replies: list[Reply], *,
                 stream: bool | None = True) -> tuple[Any, FakeUpstream, Any]:
    upstream = FakeUpstream(replies).install(monkeypatch)
    adapter = GeminiAdapter() if stream is None else GeminiAdapter(stream=stream)  # None＝預設
    state = await adapter.prepare_request(_request(), dict(_CTX))
    try:
        async with asyncio.timeout(_GUARD_S):
            items = [item async for item in adapter.send_round(state)]
    finally:
        await adapter.close(state)
    assert len(items) == 1  # 只在最後交出一項（不回半輪）
    return items[0], upstream, state


async def test_full_stream_folds_to_the_non_streaming_shape(monkeypatch):
    result, upstream, state = await _round(monkeypatch, [Reply(pieces=[sse(*_TEXT, done=False)])])
    assert result == {"candidates": [{"content": {"role": "model", "parts": [{"text": "你好"}]},
                                      "index": 0, "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}
    (request,) = upstream.requests
    assert str(request.url) == f"{_BASE}:streamGenerateContent?alt=sse"
    assert upstream.sent == [state.payload] == [
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}]
    assert GeminiAdapter().partial_usage(state) is None  # 完整且有用量：核心照常入帳，不重記
    upstream.assert_all_closed()


async def test_stream_cut_before_finish_reason_is_round_error(monkeypatch):
    result, upstream, _ = await _round(monkeypatch, [Reply(pieces=[sse(_TEXT[0], done=False)])])
    assert result == RoundError("API Error (200)\n\n`上游串流沒送完就中斷（缺 finishReason）`")
    upstream.assert_all_closed()


async def test_error_object_after_200_is_round_error(monkeypatch):
    error = '{"error": {"code": 500, "message": "internal", "status": "INTERNAL"}}'
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[sse(_TEXT[0], error, done=False)])])
    assert result == RoundError(f"API Error (200)\n\n`{error}`")


@pytest.mark.parametrize(("event", "kind"), [
    ("{not json", "JSONDecodeError"), ("[1]", "StreamShapeError"),
    ('{"candidates": [{"content": {"parts": ["x"]}}]}', "StreamShapeError"),
], ids=["bad-json", "not-object", "part-not-object"])
async def test_malformed_chunks_are_round_errors(monkeypatch, event, kind):
    result, _, _ = await _round(monkeypatch, [Reply(pieces=[sse(event, done=False)])])
    assert isinstance(result, RoundError)
    assert result.user_message.startswith(f"API Error (200)\n\n`{kind}: ")


@pytest.mark.parametrize(("stream", "endpoint"), [
    (True, ":streamGenerateContent?alt=sse"), (False, ":generateContent")], ids=["stream", "post"])
async def test_status_error_text_is_pinned(monkeypatch, stream, endpoint):
    result, upstream, _ = await _round(monkeypatch, [status_reply(400, "bad")], stream=stream)
    assert result == RoundError("API Error (400)\n\n`bad`")
    assert str(upstream.requests[0].url) == _BASE + endpoint
    upstream.assert_all_closed()


def _loop_ctx(rid: str, **extra: Any) -> dict:
    return {**_CTX, "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
            "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
            "request_id": rid, "t0": 0.0, "identity": AgentIdentity.for_role("Sumi", "Owner"),
            **extra}


async def _main_loop(monkeypatch, replies: list[Reply], rid: str) -> tuple[list, FakeUpstream]:
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    upstream = FakeUpstream(replies).install(monkeypatch)
    loop = react_core.run_react_loop(GeminiAdapter(stream=True), _request(), _loop_ctx(rid))
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in loop]
    return chunks, upstream


async def test_block_reason_only_is_safety_not_round_error(monkeypatch):
    blocked = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
               "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 4}}
    result, _, state = await _round(monkeypatch, [Reply(pieces=[sse(blocked, done=False)])])
    assert result == blocked
    assert GeminiAdapter().stop_reason(state, result) == "safety"
    chunks, _ = await _main_loop(monkeypatch, [Reply(pieces=[sse(blocked, done=False)])],
                                 "req-g-block")
    assert "API Error" not in "".join(chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_signatures_are_replayed_exactly_once(monkeypatch):
    call = {"functionCall": {"name": "vault_tools", "args": {"action": "list"}}}
    tool_round = sse(_chunk({"text": "我來查"}),
                     _chunk({"text": "一下", "thoughtSignature": "sigA"},
                            {**call, "thoughtSignature": "sigB"}),
                     _chunk({"text": "", "thoughtSignature": "sigC"}, finish="STOP"), done=False)
    done = sse(_chunk({"text": "好了"}, finish="STOP"), done=False)
    monkeypatch.setattr(react_core, "ToolExecutor", MagicMock(return_value=MagicMock(
        execute_to_queue=AsyncMock(side_effect=_finish_tool))))
    _, upstream = await _main_loop(monkeypatch, [Reply(pieces=[tool_round]), Reply(pieces=[done])],
                                   "req-g-sig")
    assert upstream.sent[1]["contents"][1] == {"role": "model", "parts": [
        {"text": "我來查一下", "thoughtSignature": "sigA"}, {**call, "thoughtSignature": "sigB"},
        {"text": "", "thoughtSignature": "sigC"}]}
    body = upstream.requests[1].content
    assert [body.count(s) for s in (b"sigA", b"sigB", b"sigC")] == [1, 1, 1]


async def _finish_tool(_name: str, _args: Any, _rid: str, _ctx: Any, queue: Any,
                       **_kw: Any) -> str:
    await queue.put(None)
    return "OK"


# ── D1／D6：預設非串流；規則表開關；503 照舊重試 ────────────────────────────


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview"])
def test_gemini_family_streams_by_default(model):
    assert rules_for(model).stream is True


@pytest.mark.parametrize("stream", [None, True], ids=["default", "rules-off"])
async def test_non_streaming_paths_post_once_without_alt_sse(monkeypatch, stream):
    if stream:  # 主迴圈打開串流，但規則表關掉
        real = rules_for
        monkeypatch.setattr(gemini_adapter, "rules_for", lambda m: replace(real(m), stream=False))
    body = {"candidates": [{"content": {"parts": [{"text": "好"}]}}]}
    reply = Reply(200, [json.dumps(body).encode()], {"content-type": "application/json"})
    result, upstream, _ = await _round(monkeypatch, [reply], stream=stream)
    assert result == body
    assert str(upstream.requests[0].url) == f"{_BASE}:generateContent"


async def test_main_loop_adapter_marks_503_for_core_retry(monkeypatch):
    """3b-U5a：主迴圈的 adapter 只送一次、不發狀態也不等；503 標成可重試交給 react_core。"""
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_adapter, "asyncio", SimpleNamespace(sleep=sleep))
    upstream = FakeUpstream([status_reply(503, "busy"), Reply(pieces=[sse(*_TEXT, done=False)])])
    upstream.install(monkeypatch)
    adapter = GeminiAdapter(stream=True)
    state = await adapter.prepare_request(_request(), dict(_CTX))
    items = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    assert items == [RoundError("API Error (503)\n\n`busy`")]
    assert (items[0].retry_note, items[0].retry_after) == ("Gemini busy (503)", None)
    assert len(upstream.requests) == 1
    sleep.assert_not_awaited()
    upstream.assert_all_closed()


# ── 兩段逾時、取消（D3） ─────────────────────────────────────────────────────


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", _FIRST)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", _IDLE)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", _ROUND)


def _timeout(phase: str) -> RoundError:
    return RoundError(f"API Error (Timeout)\n\n`上游串流逾時：{phase}`")


async def test_no_first_byte_times_out_without_retry(monkeypatch, short):
    result, upstream, _ = await _round(monkeypatch, [
        Reply(pieces=[_FIRST + 0.5, sse(*_TEXT, done=False)]),
        Reply(pieces=[sse(*_TEXT, done=False)])])  # 第二個回應只用來抓重試
    assert result == _timeout("等不到第一個回應")
    assert len(upstream.requests) == 1
    upstream.assert_all_closed()


async def test_stalled_stream_hits_idle_timeout(monkeypatch, short):
    result, upstream, _ = await _round(
        monkeypatch, [Reply(pieces=[sse(_TEXT[0], done=False), asyncio.Event()])])
    assert result == _timeout("閒置太久")
    upstream.assert_all_closed()


async def test_bytes_keep_the_stream_alive(monkeypatch, short):
    """每 0.1 秒來一小段（短於閒置）、總共 0.9 秒（長於首事件）：不逾時。"""
    body = sse(*_TEXT, done=False)
    pieces = [p for n in range(9) for p in (0.1, body[n * len(body) // 9:(n + 1) * len(body) // 9])]
    result, _, _ = await _round(monkeypatch, [Reply(pieces=pieces)])
    assert result["candidates"][0]["content"]["parts"] == [{"text": "你好"}]


async def test_cancel_waits_until_upstream_is_closed(monkeypatch, short):
    hold = asyncio.Event()
    upstream = FakeUpstream([Reply(pieces=[sse(_TEXT[0], done=False), hold])]).install(monkeypatch)
    adapter = GeminiAdapter(stream=True)
    state = await adapter.prepare_request(_request(), dict(_CTX))

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


# ── 用量（U1b 規則；裁定 5／6） ──────────────────────────────────────────────


@pytest.fixture
def booked(monkeypatch) -> list:
    calls: list = []

    def book(_identity: Any, _provider: Any, usage: dict, *_rest: Any, **_kw: Any) -> None:
        if usage:  # run 結束的 E-Index 那一筆用量是空的，不算
            calls.append(usage)

    monkeypatch.setattr(react_core, "accumulate_token_usage", book)
    monkeypatch.setattr(gemini_adapter, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    return calls


async def _loop(monkeypatch, replies: list[Reply], rid: str) -> list[dict]:
    FakeUpstream(replies).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in react_core.run_react_loop(
            GeminiAdapter(stream=True), _request(), _loop_ctx(rid))]
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e for e in events if e.get("kind") == "token_usage"]


_REPORTED = {"promptTokenCount": 30, "cachedContentTokenCount": 10, "candidatesTokenCount": 4}


@pytest.mark.parametrize(("chunks", "usage", "estimated"), [
    ([_chunk({"text": "abcdefg"})], {"prompt_tokens": 17, **_ZERO, "completion_tokens": 2}, True),
    ([_chunk({"text": "好"}, finish="STOP")],
     {"prompt_tokens": 17, **_ZERO, "completion_tokens": 1}, True),
    ([_chunk({"text": "好"}, usage=_REPORTED)], {"prompt_tokens": 20, "cache_read_input_tokens": 10,
                                                "cache_creation_input_tokens": 0,
                                                "completion_tokens": 4}, False),
    ([_chunk({"text": "好"}, finish="STOP", usage=_REPORTED)],
     {"prompt_tokens": 20, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0,
      "completion_tokens": 4}, False),
], ids=["half", "done-without-usage", "usage-without-finish", "done-with-usage"])
async def test_round_usage_is_booked_once(monkeypatch, booked, chunks, usage, estimated):
    events = await _loop(monkeypatch, [Reply(pieces=[sse(*chunks, done=False)])], "req-g-u")
    assert booked == [usage]
    assert ("estimated" in events[0], events[0].get("estimated")) == (
        (True, True) if estimated else (False, None))


async def test_cancelled_stream_books_an_estimate(monkeypatch, booked):
    hold = asyncio.Event()
    upstream = FakeUpstream([Reply(pieces=[sse(_chunk({"text": "abcdefg"}), done=False), hold])])
    upstream.install(monkeypatch)
    loop = react_core.run_react_loop(GeminiAdapter(stream=True), _request(), _loop_ctx("req-g-c"))
    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(anext(loop))
        while not upstream.responses:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert booked == [{"prompt_tokens": 17, **_ZERO, "completion_tokens": 2}]


async def test_stream_that_never_opened_books_nothing(monkeypatch, booked):
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())  # 503 會重試（3b-U5a）
    events = await _loop(monkeypatch, [status_reply(503, "busy")] * 3, "req-g-503")
    assert booked == []
    assert events == []


# ── handler 打開串流（主迴圈）；規則表關掉時走非串流（D6） ─────────────────


async def _handler_loop(monkeypatch, replies: list[Reply], rid: str) -> tuple[list, FakeUpstream]:
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    upstream = FakeUpstream(replies).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in handle_gemini_request(_request(), _loop_ctx(rid))]
    return chunks, upstream


async def test_main_loop_sends_no_partial_text_downstream(monkeypatch):
    half = _chunk({"text": "半截回答"})
    chunks, upstream = await _handler_loop(monkeypatch, [Reply(pieces=[sse(half, done=False)])],
                                           "req-g-half")
    joined = "".join(chunks)
    assert str(upstream.requests[0].url).endswith(":streamGenerateContent?alt=sse")  # handler 打開
    assert "半截回答" not in joined
    assert "缺 finishReason" in joined
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_rules_switch_off_keeps_main_loop_on_post(monkeypatch):
    real = rules_for
    monkeypatch.setattr(gemini_adapter, "rules_for", lambda m: replace(real(m), stream=False))
    body = {"candidates": [{"content": {"parts": [{"text": "好"}]}, "finishReason": "STOP"}]}
    reply = Reply(200, [json.dumps(body).encode()], {"content-type": "application/json"})
    chunks, upstream = await _handler_loop(monkeypatch, [reply], "req-g-off")
    assert [str(r.url) for r in upstream.requests] == [f"{_BASE}:generateContent"]
    assert '"content": "好"' in "".join(chunks)
