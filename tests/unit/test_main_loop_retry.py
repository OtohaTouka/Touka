"""3b-U5a：主迴圈的退避重試、錯誤分類、被放棄那次的用量、finish_reason 與拒答提示。

（ADR-0248；DEV-A 附註 3、18、29、30；master R2、R3。）

- 重試在 react_core，不在 adapter：只接可重試的狀態碼（429／500／502／503／504／529）與首事件、
  閒置逾時；整輪上限逾時、400、一般例外、連線中斷都不重試。retry-after（秒、HTTP 日期、
  ``retry-after-ms``）照辦，看不懂就用預設階梯（2、4 秒），太大截到 30 秒。
- 每次重試先發一則 touka_status（「…, retrying... (attempt N)」），再等一次；失敗那次的錯誤
  不送到下游。等待中被取消照常往外傳，送出次數維持 1。
- 被放棄那次已收到的部分照裁定 6 記帳（有回報照記、沒回報就估），四家（含 Responses）各一案。
- 下游終端 chunk 的 finish_reason 照實回報；拒答給可讀提示（含模型自己的拒答文字）。

上游一律 mock：相容 Chat／Gemini 用 tests/helpers/stream_transport，Claude／Responses 用真 SDK＋
httpx2.MockTransport。等待一律換成記錄用的假 sleep（不真睡）。估算值同各家 U1b 測試：
相容 Chat 19、Gemini 17、Responses 55 個輸入 token，「abcdefg」→ 2 個輸出 token。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import httpx2
import openai
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src import main as proxy_main
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import (
    claude_adapter,
    gemini_adapter,
    responses_adapter,
    retry,
    stream_timeout,
)
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.adapters.stream_timeout import RoundClock, liveness_event_hooks
from src.models.identity import AgentIdentity

from tests.helpers.stream_transport import (
    FakeUpstream,
    Reply,
    gemini_stream_reply,
    json_reply,
    sse,
    status_reply,
    stream_reply,
)

_GUARD_S = 5.0
_IDENTITY = AgentIdentity.for_role("Sumi", "Owner")
_SSE = {"content-type": "text/event-stream"}
_ZERO = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(react_core, "_retry_sleep", fake_sleep)
    return waited


@pytest.fixture
def booked(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def book(_identity: Any, _provider: Any, usage: dict, *_rest: Any, **_kw: Any) -> None:
        if usage:  # 空用量不算
            calls.append(usage)

    monkeypatch.setattr(react_core, "accumulate_token_usage", book)
    return calls


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", 0.6)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", 1.2)


@pytest.fixture
def roomy(monkeypatch):
    """閒置期限照樣短，首事件與整輪期限放寬到機器再慢也碰不到。

    SDK 第一次解析串流事件會同步建 pydantic schema（OpenAI 的 ResponseStreamEvent 在 Windows
    開發機實測約 0.55 秒），這段時間 event loop 不動；首事件只給 0.6 秒時，冷啟動就被誤判成逾時。
    """
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", 30.0)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", 60.0)


def _request(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


def _ctx(rid: str, **extra: Any) -> dict:
    return {"protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
            "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
            "request_id": rid, "t0": 0.0, "agent_id": "Sumi", "identity": _IDENTITY, **extra}


async def _run(adapter: Any, model: str, ctx: dict) -> list[str]:
    async with asyncio.timeout(_GUARD_S):
        return [c async for c in react_core.run_react_loop(adapter, _request(model), ctx)]


def _statuses(chunks: list[str]) -> list[str]:
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e["status"] for e in events if e.get("type") == "touka_status" and "status" in e]


def _terminal(chunks: list[str]) -> dict:
    return json.loads(chunks[-2][6:])


# ── 錯誤分類 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("headers", "seconds"), [
    ({"retry-after": "7"}, 7.0),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, 12.0),
    ({"retry-after": "soon"}, None),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "9999"}, 30.0),
    ({"retry-after": "-3"}, 0.0),
    ({}, None),
    (None, None),
])
def test_retry_after_parsing(monkeypatch, headers, seconds):
    monkeypatch.setattr(retry, "get_current_epoch", lambda: 1792567668.0)  # 那個日期前 12 秒
    assert retry.retry_after_s(headers) == seconds


def test_classification_rules():
    error = RoundError("x")
    assert [retry.on_status(error, code, None, "busy").retry_note
            for code in (400, 401, 404, 408, 409, 429, 500, 501, 502, 503, 504, 529)] == [
        None, None, None, "busy", "busy", "busy", "busy", "busy", "busy", "busy", "busy",
        "busy"]
    clock = RoundClock()
    clock.phase = "idle"
    assert retry.on_timeout(error, clock).retry_note == "上游串流逾時：閒置太久"
    clock.phase = "round"
    assert retry.on_timeout(error, clock).retry_note is None  # 整輪上限不重試（附註 29）


# ── Gemini：503、503、200（附註 30） ─────────────────────────────────────────

_GEMINI_OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "你好"}]},
                              "finishReason": "STOP"}],
              "usageMetadata": {"promptTokenCount": 30, "cachedContentTokenCount": 10,
                                "candidatesTokenCount": 4}}


def _gemini_ctx(rid: str) -> dict:
    return _ctx(rid, gemini_api_key="fake-key")


async def test_gemini_503_503_200_status_order_and_one_wait_each(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([status_reply(503), status_reply(503),
                             gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-g-ok"))
    assert _statuses(chunks) == ["Gemini busy (503), retrying... (attempt 1)",
                                 "Gemini busy (503), retrying... (attempt 2)"]
    assert sleeps == [2.0, 4.0]  # 每次重試只等一次
    joined = "".join(chunks)
    assert "API Error (503)" not in joined  # 失敗那次的 RoundError 不送到下游
    assert '"content": "你好"' in joined
    assert len(upstream.requests) == 3
    assert booked == [{"prompt_tokens": 20, "cache_read_input_tokens": 10,
                       "cache_creation_input_tokens": 0, "completion_tokens": 4}]


async def test_gemini_503_until_the_cap_shows_the_last_error(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([status_reply(503, "busy")] * 3).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-g-503"))
    assert _statuses(chunks) == ["Gemini busy (503), retrying... (attempt 1)",
                                 "Gemini busy (503), retrying... (attempt 2)"]
    assert sleeps == [2.0, 4.0]
    assert "".join(chunks).count("API Error (503)") == 1
    assert len(upstream.requests) == 3
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_cancel_during_backoff_keeps_one_send(monkeypatch, booked):
    started = asyncio.Event()

    async def hanging_sleep(_seconds: float) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(react_core, "_retry_sleep", hanging_sleep)
    upstream = FakeUpstream([status_reply(503)] * 3).install(monkeypatch)
    loop = react_core.run_react_loop(GeminiAdapter(stream=True), _request("gemini-2.5-flash"),
                                     _gemini_ctx("r-g-cancel"))

    async def drain() -> None:
        async for _ in loop:
            pass

    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(drain())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(upstream.requests) == 1


# ── 不重試的：400、一般例外；Claude 的 str(e) 格式 ───────────────────────────


def _compat_ctx(rid: str) -> dict:
    return _ctx(rid, provider="Grok", url="https://api.x.test/v1/chat/completions",
                api_key="fake-key", custom_connection=False)


_COMPAT_OK = {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 30, "completion_tokens": 4,
                        "prompt_tokens_details": {"cached_tokens": 10}}}


async def test_compat_400_is_not_retried(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([json_reply("bad", 400), stream_reply(_COMPAT_OK, "one")])
    upstream.install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-400"))
    assert "Grok API Error (400)\\n\\n`bad`" in "".join(chunks)
    assert (len(upstream.requests), sleeps, _statuses(chunks)) == (1, [], [])


async def test_compat_429_is_retried_with_its_retry_after(monkeypatch, sleeps, booked):
    busy = Reply(429, [b"slow"], {"content-type": "text/plain", "retry-after": "3"})
    upstream = FakeUpstream([busy, stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-429"))
    assert _statuses(chunks) == ["Grok busy (429), retrying... (attempt 1)"]
    assert (len(upstream.requests), sleeps) == (2, [3.0])
    assert "API Error (429)" not in "".join(chunks)


async def test_claude_plain_exception_is_not_retried(sleeps, booked):
    client = MagicMock()
    client.messages.stream = MagicMock(side_effect=RuntimeError("boom"))
    chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                        _ctx("r-cl-boom", anthropic_client=client))
    assert "❌ **API 發生錯誤**\\n\\n`boom`" in "".join(chunks)
    assert (client.messages.stream.call_count, sleeps) == (1, [])


def _claude_events(text: str, output: int) -> bytes:
    def ev(kind: str, **data: Any) -> bytes:
        body = json.dumps({"type": kind, **data}, ensure_ascii=False)
        return f"event: {kind}\ndata: {body}\n\n".encode()

    return b"".join([
        ev("message_start", message={
            "id": "msg_r", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 5}}),
        ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        ev("content_block_delta", index=0, delta={"type": "text_delta", "text": text}),
        ev("content_block_stop", index=0),
        ev("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None},
           usage={"output_tokens": output}),
        ev("message_stop"),
    ])


class _Script(httpx2.AsyncByteStream):
    """腳本化的位元組：bytes＝送出、float＝等幾秒、Event＝停住。"""

    def __init__(self, steps: list) -> None:
        self._steps = steps

    async def __aiter__(self) -> Any:
        for step in self._steps:
            if isinstance(step, bytes):
                yield step
            elif isinstance(step, asyncio.Event):
                await step.wait()
            else:
                await asyncio.sleep(step)


def _transport(replies: list, seen: list) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):  # 還沒收到回應就斷（R-3b ② M1）
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, headers=_SSE, stream=_Script(reply), request=request)

    return httpx2.MockTransport(handle)


def _claude_client(replies: list, seen: list) -> AsyncAnthropic:
    return AsyncAnthropic(api_key="fake-key", base_url="https://api.anthropic.test", max_retries=0,
                          http_client=DefaultAsyncHttpxClient(
                              transport=_transport(replies, seen), follow_redirects=False,
                              event_hooks=liveness_event_hooks()))


async def test_claude_529_honours_retry_after_and_400_keeps_str_format(sleeps, booked):
    seen: list = []
    busy = httpx2.Response(529, headers={"retry-after": "7"},
                           json={"type": "error", "error": {"type": "overloaded_error",
                                                            "message": "busy"}})
    client = _claude_client([busy, [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-cl-529", anthropic_client=client))
    finally:
        await client.close()
    assert _statuses(chunks) == ["Claude busy (529), retrying... (attempt 1)"]
    assert (sleeps, len(seen)) == ([7.0], 2)
    assert '"content": "好"' in "".join(chunks)

    seen.clear()
    bad = httpx2.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "bad"}})
    client = _claude_client([bad, [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-cl-400", anthropic_client=client))
    finally:
        await client.close()
    assert ("❌ **API 發生錯誤**\\n\\n`Error code: 400 - {'type': 'error', 'error': "
            "{'type': 'invalid_request_error', 'message': 'bad'}}`") in "".join(chunks)
    assert len(seen) == 1


async def test_main_loop_anthropic_client_does_not_retry_inside_the_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-boot-key")
    client = proxy_main._build_anthropic_client()
    try:
        assert client.max_retries == 0  # 重試只在 react_core（3b-U5a）
    finally:
        await client.close()


# ── Responses：429 的 retry-after-ms；max_retries=0 ─────────────────────────

_RESP_USAGE = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1024},
               "output_tokens": 40}


def _responses_events(*events: tuple[str, dict]) -> bytes:
    lines = [(kind, json.dumps({"type": kind, **data}, ensure_ascii=False))
             for kind, data in events]
    return b"".join(f"event: {kind}\ndata: {body}\n\n".encode() for kind, body in lines)


def _responses_done(text: str = "好", usage: dict | None = None) -> bytes:
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
                "status": "completed", "output": [message], **({"usage": usage} if usage else {})}
    return _responses_events(("response.completed", {"sequence_number": 1, "response": response}))


def _responses_delta(text: str) -> bytes:
    return _responses_events(("response.output_text.delta", {
        "sequence_number": 0, "output_index": 0, "item_id": "msg_1", "content_index": 0,
        "delta": text, "logprobs": []}))


def _install_responses(monkeypatch, replies: list, seen: list) -> None:
    def http_client(**kwargs: Any) -> Any:
        return openai.DefaultAsyncHttpxClient(transport=_transport(replies, seen), **kwargs)

    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)


def _responses_ctx(rid: str) -> dict:
    return _ctx(rid, provider="OpenAI", base_url="https://api.openai.test/v1",
                api_key="fake-key")


async def test_responses_429_honours_retry_after_ms(monkeypatch, sleeps, booked):
    seen: list = []
    busy = httpx2.Response(429, headers={"retry-after-ms": "1500"},
                           json={"error": {"message": "slow down"}})
    _install_responses(monkeypatch, [busy, [_responses_done(usage=_RESP_USAGE)]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-r-429"))
    assert _statuses(chunks) == ["OpenAI busy (429), retrying... (attempt 1)"]
    assert (sleeps, len(seen)) == ([1.5], 2)  # SDK 自己不重送（max_retries=0）


# ── 逾時：首事件與閒置重試，整輪上限不重試（附註 29；R-3b ② M2、L3） ─────────

_COMPAT_HALF = sse({"choices": [{"index": 0, "delta": {"content": "abcdefg"},
                                 "finish_reason": None}]}, done=False)
_COMPAT_ROLE = sse({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                 "finish_reason": None}]}, done=False)
_PINGS = [step for _ in range(8) for step in (0.1, b": ping\n\n")]  # 到第 0.8 秒，過了首事件期限


@pytest.mark.parametrize(("first", "note"), [
    ([1.0], "上游串流逾時：等不到第一個回應"),
    ([_COMPAT_ROLE, *_PINGS, asyncio.Event()], "上游串流逾時：閒置太久"),  # 還沒有輸出
], ids=["first-event", "idle-before-output"])
async def test_first_event_and_idle_timeouts_are_retried(monkeypatch, short, sleeps, booked,
                                                         first, note):
    replies = [Reply(pieces=list(first)), stream_reply(_COMPAT_OK, "one")]
    upstream = FakeUpstream(replies).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-to"))
    assert _statuses(chunks) == [f"{note}, retrying... (attempt 1)"]
    assert (len(upstream.requests), sleeps) == (2, [2.0])
    assert '"content": "好"' in "".join(chunks)


_GEMINI_HALF = sse({"candidates": [{"content": {"role": "model", "parts": [{"text": "abcdefg"}]},
                                     "index": 0}]}, done=False)


@pytest.mark.parametrize("family", ["compat", "gemini"])
async def test_idle_timeout_after_output_is_not_retried(monkeypatch, short, sleeps, booked,
                                                        family):
    """R-3b ② L3（選「已有輸出就不重試」）：輸出已經計費，重送等於再付一次。"""
    if family == "compat":
        upstream = FakeUpstream([Reply(pieces=[_COMPAT_HALF, asyncio.Event()]),
                                 stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
        chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-l3"))
        estimate = {"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}
    else:
        upstream = FakeUpstream([Reply(pieces=[_GEMINI_HALF, asyncio.Event()]),
                                 gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
        chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-g-l3"))
        estimate = {"prompt_tokens": 17, **_ZERO, "completion_tokens": 2}
    assert "上游串流逾時：閒置太久" in "".join(chunks)
    assert (len(upstream.requests), sleeps, _statuses(chunks)) == (1, [], [])
    assert booked == [estimate]


async def test_round_cap_timeout_is_not_retried(monkeypatch, short, sleeps, booked):
    alive = [step for _ in range(200) for step in (0.05, b": keep-alive\n\n")]
    upstream = FakeUpstream([Reply(pieces=[_COMPAT_HALF, *alive]),
                             stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-cap"))
    assert "上游串流逾時：整輪超過上限" in "".join(chunks)
    assert (len(upstream.requests), sleeps, _statuses(chunks)) == (1, [], [])


def _responses_event(kind: str, **data: Any) -> bytes:
    return _responses_events((kind, {"sequence_number": 0, **data}))


_CREATED = _responses_event("response.created", response={
    "id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
    "status": "in_progress", "output": []})


@pytest.mark.parametrize("family", ["responses", "compat", "claude"])
async def test_shell_events_do_not_end_the_first_event_wait(monkeypatch, roomy, sleeps, booked,
                                                            family):
    """R-3b ② M2：先送的空殼事件（response.created、只有 role 的 chunk、message_start）之後
    沉默 0.4 秒——超過閒置 0.2——不算逾時。

    結果只看「閒置期限比沉默結束早到」這個先後（同一個 event loop 上兩個計時器的順序），
    不看機器快慢：空殼事件若結束了首事件階段，閒置期限一定先到而變紅；沒結束時首事件期限
    放寬到 30 秒（roomy），SDK 冷啟動或 GC 卡住都碰不到。"""
    seen: list = []
    if family == "responses":
        _install_responses(monkeypatch, [[_CREATED, 0.4, _responses_done(usage=_RESP_USAGE)]],
                           seen)
        chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-m2"))
    elif family == "compat":
        upstream = FakeUpstream([Reply(pieces=[
            _COMPAT_ROLE, 0.4, *stream_reply(_COMPAT_OK, "one").pieces])]).install(monkeypatch)
        chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-m2-c"))
        seen = upstream.requests
    else:
        full = _claude_events("好", 7)
        cut = full.index(b"event: content_block_start")
        client = _claude_client([[full[:cut], 0.4, full[cut:]]], seen)
        try:
            chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                                _ctx("r-m2-cl", anthropic_client=client))
        finally:
            await client.close()
    assert (_statuses(chunks), len(seen)) == ([], 1)
    assert '"content": "好"' in "".join(chunks)


async def test_shell_event_then_silence_times_out_at_the_first_event_limit(
        monkeypatch, short, sleeps, booked):
    seen: list = []
    _install_responses(monkeypatch, [[_CREATED, asyncio.Event()],
                                     [_responses_done(usage=_RESP_USAGE)]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-m2-to"))
    assert _statuses(chunks) == ["上游串流逾時：等不到第一個回應, retrying... (attempt 1)"]
    assert len(seen) == 2


# ── 被放棄那次的用量（裁定 6、master R2）：四家各一（中途收到忙碌錯誤而重送） ───────

_REAL = {"prompt_tokens": 20, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0,
         "completion_tokens": 4}


async def test_abandoned_compat_attempt_is_booked(monkeypatch, short, sleeps, booked):
    busy = sse({"error": {"type": "server_error", "message": "busy"}}, done=False)
    FakeUpstream([Reply(pieces=[_COMPAT_HALF, busy]),
                  stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-u-c"))
    assert _statuses(chunks) == ["Grok busy (server_error), retrying... (attempt 1)"]
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}, _REAL]


async def test_abandoned_gemini_attempt_is_booked(monkeypatch, short, sleeps, booked):
    half = sse({"candidates": [{"content": {"role": "model", "parts": [{"text": "abcdefg"}]},
                                "index": 0}]}, done=False)
    busy = sse({"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}, done=False)
    FakeUpstream([Reply(pieces=[half, busy]),
                  gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-u-g"))
    assert _statuses(chunks) == ["Gemini busy (UNAVAILABLE), retrying... (attempt 1)"]
    assert booked == [{"prompt_tokens": 17, **_ZERO, "completion_tokens": 2}, _REAL]


async def test_abandoned_responses_attempt_is_booked(monkeypatch, short, sleeps, booked):
    seen: list = []
    busy = _responses_event("error", code="server_error", message="busy", param=None)
    _install_responses(monkeypatch, [[_responses_delta("abcdefg"), busy],
                                     [_responses_done(usage=_RESP_USAGE)]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-u-r"))
    assert _statuses(chunks) == ["OpenAI busy (server_error), retrying... (attempt 1)"]
    assert booked == [{"prompt_tokens": 55, **_ZERO, "completion_tokens": 2},
                      {"prompt_tokens": 176, "cache_read_input_tokens": 1024,
                       "cache_creation_input_tokens": 0, "completion_tokens": 40}]


def _claude_error_event(error_type: str) -> bytes:
    """message_start、一段文字、然後 200 之後才來的錯誤事件。"""
    full = _claude_events("好", 7)
    error = json.dumps({"type": "error", "error": {"type": error_type, "message": "x"}})
    head = full[:full.index(b"event: content_block_stop")]
    return head + f"event: error\ndata: {error}\n\n".encode()


async def test_abandoned_claude_attempt_is_booked(short, sleeps, booked):
    seen: list = []
    client = _claude_client([[_claude_error_event("overloaded_error")],
                             [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-u-cl", anthropic_client=client))
    finally:
        await client.close()
    assert _statuses(chunks) == ["Claude busy (overloaded_error), retrying... (attempt 1)"]
    reported = {"prompt_tokens": 100, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5}
    assert booked == [{**reported, "completion_tokens": 1}, {**reported, "completion_tokens": 7}]


# ── finish_reason 實際值、拒答提示 ──────────────────────────────────────────


async def test_responses_refusal_text_is_shown(monkeypatch, booked):
    seen: list = []
    refusal = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "refusal", "refusal": "不行"}]}
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
                "status": "completed", "output": [refusal], "usage": _RESP_USAGE}
    _install_responses(monkeypatch, [[_responses_events(
        ("response.completed", {"sequence_number": 0, "response": response}))]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-ref"))
    assert "\\n\\n⚠️ 模型拒絕回答：不行" in "".join(chunks)
    assert _terminal(chunks)["choices"][0]["finish_reason"] == "content_filter"


async def test_compat_refusal_text_and_missing_finish_reason(monkeypatch, booked):
    refused = {"choices": [{"message": {"content": None, "refusal": "我不能幫忙"},
                            "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}}
    plain = {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 3}}
    FakeUpstream([stream_reply(refused, "one"), stream_reply(plain, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-ref"))
    assert "\\n\\n⚠️ 模型拒絕回答：我不能幫忙" in "".join(chunks)
    assert _terminal(chunks)["choices"][0]["finish_reason"] == "content_filter"
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-plain"))
    assert _terminal(chunks)["choices"][0]["finish_reason"] == "stop"  # 缺欄位維持 stop
    assert "⚠️" not in "".join(chunks)


async def test_unreadable_stop_reason_keeps_stop(monkeypatch, booked):
    def broken(*_args: Any) -> str:
        raise KeyError("stop")

    monkeypatch.setattr(OpenAIAdapter, "stop_reason", broken)
    FakeUpstream([stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-c-broken"))
    assert _terminal(chunks)["choices"][0]["finish_reason"] == "stop"
    assert '"content": "好"' in "".join(chunks)


# ── R-3b ② M1：還沒收到 200 就斷可以重試（沒送到廠商、不計費），開了之後斷不重試 ─────


async def test_compat_connect_error_before_200_is_retried(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([Reply(error=httpx.ConnectError("reset")),
                             stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-m1-c"))
    assert _statuses(chunks) == ["Grok connection error, retrying... (attempt 1)"]
    assert (len(upstream.requests), sleeps, booked) == (2, [2.0], [_REAL])


async def test_custom_connection_connect_error_is_retried(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([Reply(error=httpx.ConnectError("down")),
                             json_reply(_COMPAT_OK)]).install(monkeypatch)
    ctx = {**_compat_ctx("r-m1-custom"), "custom_connection": True}
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", ctx)
    assert _statuses(chunks) == ["Grok connection error, retrying... (attempt 1)"]
    assert len(upstream.requests) == 2


async def test_compat_drop_after_200_is_not_retried(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([Reply(pieces=[_COMPAT_HALF, httpx.ReadError("reset")]),
                             stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-m1-drop"))
    assert "ReadError: reset" in "".join(chunks)
    assert (len(upstream.requests), sleeps, _statuses(chunks)) == (1, [], [])
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}]


async def test_gemini_connect_error_before_200_is_retried(monkeypatch, sleeps, booked):
    upstream = FakeUpstream([Reply(error=httpx.ConnectError("down")),
                             gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-m1-g"))
    assert _statuses(chunks) == ["Gemini connection error, retrying... (attempt 1)"]
    assert (len(upstream.requests), booked) == (2, [_REAL])


@pytest.mark.parametrize("cut", [
    httpx.ReadError("reset"),  # 串流開了之後斷線
    sse({"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}, done=False),
], ids=["drop-after-200", "error-400-inside-stream"])
async def test_gemini_failures_after_200_that_are_not_retried(monkeypatch, sleeps, booked, cut):
    half = sse({"candidates": [{"content": {"role": "model", "parts": [{"text": "abcdefg"}]},
                                "index": 0}]}, done=False)
    upstream = FakeUpstream([Reply(pieces=[half, cut]),
                             gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-m1-g2"))
    assert (len(upstream.requests), sleeps, _statuses(chunks)) == (1, [], [])


async def test_claude_connect_error_before_200_is_retried(sleeps, booked):
    seen: list = []
    client = _claude_client([httpx2.ConnectError("reset"), [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-m1-cl", anthropic_client=client))
    finally:
        await client.close()
    assert _statuses(chunks) == ["Claude connection error, retrying... (attempt 1)"]
    assert (len(seen), sleeps) == (2, [2.0])


async def test_claude_non_retryable_error_inside_the_stream(sleeps, booked):
    seen: list = []
    bad = _claude_error_event("invalid_request_error")
    client = _claude_client([[bad], [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-m1-cl2", anthropic_client=client))
    finally:
        await client.close()
    assert (len(seen), sleeps, _statuses(chunks)) == (1, [], [])


async def test_responses_connect_error_before_200_is_retried(monkeypatch, sleeps, booked):
    seen: list = []
    _install_responses(monkeypatch, [httpx2.ConnectError("reset"),
                                     [_responses_done(usage=_RESP_USAGE)]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-m1-r"))
    assert _statuses(chunks) == ["OpenAI connection error, retrying... (attempt 1)"]
    assert len(seen) == 2


def test_error_body_classification():
    error = RoundError("x")
    bodies = [{"type": "overloaded_error"}, {"type": "api_error"}, {"type": "rate_limit_error"},
              {"code": 503},
              {"code": "529"}, {"status": "UNAVAILABLE", "code": 503}, {"code": "server_error"},
              {"type": "invalid_request_error"}, {"code": 400}, {"code": True}, "busy", None]
    assert [retry.on_error_body(error, body, "X").retry_note for body in bodies] == [
        "X busy (overloaded_error)", "X busy (api_error)", "X busy (rate_limit_error)",
        "X busy (503)", "X busy (529)",
        "X busy (UNAVAILABLE)", "X busy (server_error)", None, None, None, None, None]
    assert retry.on_connect(error, False, "X connection error").retry_note == "X connection error"
    assert retry.on_connect(error, True, "X connection error").retry_note is None


# ── R-3b ② L5：Gemini 讀 retry-after 與 RetryInfo ───────────────────────────


def _retry_info(delay: str, code: int) -> bytes:
    return json.dumps({"error": {"code": code, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}).encode()


@pytest.mark.parametrize(("reply", "waited"), [
    (Reply(429, [b'{"error": {"code": 429}}'], {"content-type": "application/json",
                                                "retry-after": "27"}), 27.0),
    (Reply(503, [_retry_info("12s", 503)], {"content-type": "application/json"}), 12.0),
    (Reply(429, [_retry_info("1.5s", 429)], {"content-type": "application/json"}), 1.5),
    (Reply(429, [_retry_info("90s", 429)], {"content-type": "application/json"}), 30.0),
    (Reply(503, [b"not json"], {"content-type": "text/plain"}), 2.0),
], ids=["retry-after-429", "retry-info-503", "retry-info-fraction", "retry-info-capped",
        "nothing-to-read"])
async def test_gemini_honours_retry_after_and_retry_info(monkeypatch, sleeps, booked, reply,
                                                         waited):
    FakeUpstream([reply, gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-l5"))
    assert sleeps == [waited]


# ── R-3b ② L4：形狀錯或 JSON 壞掉＝這一輪錯誤，用量照記 ─────────────────────


async def test_compat_shape_error_is_a_round_error_and_booked(monkeypatch, sleeps, booked):
    FakeUpstream([Reply(pieces=[_COMPAT_HALF, sse({"choices": ["oops"]}, done=False)])]
                 ).install(monkeypatch)
    chunks = await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-l4-c"))
    assert "StreamShapeError" in "".join(chunks)
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}]


async def test_gemini_shape_error_is_a_round_error_and_booked(monkeypatch, sleeps, booked):
    def chunk(text: Any) -> bytes:
        return sse({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                    "index": 0}]}, done=False)

    FakeUpstream([Reply(pieces=[chunk("abcdefg"), chunk(5)])]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-l4-g"))
    assert "StreamShapeError" in "".join(chunks)
    assert booked == [{"prompt_tokens": 17, **_ZERO, "completion_tokens": 2}]


@pytest.mark.parametrize("bad", [
    b"event: response.output_text.delta\ndata: {not json\n\n",
    _responses_event("response.output_item.done", output_index=0),  # 沒有 item
    _responses_event("response.failed", response="oops"),
], ids=["broken-json", "item-missing", "failed-without-object"])
async def test_responses_bad_events_are_round_errors_and_booked(monkeypatch, sleeps, booked,
                                                                bad):
    seen: list = []
    _install_responses(monkeypatch, [[_responses_delta("abcdefg"), bad]], seen)
    chunks = await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-l4-r"))
    assert "OpenAI" in chunks[-2]  # 錯誤訊息是 [DONE] 前的最後一則 delta
    assert booked == [{"prompt_tokens": 55, **_ZERO, "completion_tokens": 2}]


# ── R-3b ② L1、L2：退避期間照樣送心跳；等完先看使用者有沒有按停 ─────────────


async def test_backoff_keeps_sending_heartbeats(monkeypatch, booked):
    async def slow(_seconds: float) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(react_core, "_retry_sleep", slow)
    monkeypatch.setattr(react_core, "_KEEPALIVE_S", 0.01)
    FakeUpstream([status_reply(503), gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-l1"))
    status = next(i for i, c in enumerate(chunks) if "retrying... (attempt 1)" in c)
    assert ": heartbeat\n\n" in chunks[status + 1:status + 4]


async def test_stop_pressed_during_backoff_sends_nothing_more(monkeypatch, booked):
    stopped = {"value": False}

    async def press_stop(_seconds: float) -> None:
        stopped["value"] = True

    monkeypatch.setattr(react_core, "_retry_sleep", press_stop)
    monkeypatch.setattr(react_core.rhythm_service, "is_interrupted",
                        lambda _session: stopped["value"])
    upstream = FakeUpstream([status_reply(503)] * 2).install(monkeypatch)
    chunks = await _run(GeminiAdapter(stream=True), "gemini-2.5-flash",
                        {**_gemini_ctx("r-l2"), "session_id": "s-l2"})
    assert len(upstream.requests) == 1
    assert "⏹️ 已由使用者中斷。" in "".join(chunks)


# ── R-3b ② N1：非串流路徑只重試「連不上」；讀不到回應時請求已送出，不重送 ─────────

_FAILURES = [("ReadTimeout", 1), ("RemoteProtocolError", 1),
             ("ConnectError", 2), ("ConnectTimeout", 2), ("PoolTimeout", 2)]
_CLAUDE_MESSAGE = {"id": "msg_n", "type": "message", "role": "assistant",
                   "model": "claude-opus-5-5", "content": [{"type": "text", "text": "好"}],
                   "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 2}}


def _stream_off(monkeypatch, module: Any, name: str) -> None:
    """規則表的 stream 開關關掉（D6）：主迴圈改走非串流路徑。"""
    real = getattr(module, name)
    monkeypatch.setattr(module, name, lambda model: dataclasses.replace(real(model), stream=False))


@pytest.mark.parametrize(("name", "sends"), _FAILURES)
async def test_custom_connection_retries_only_connect_failures(monkeypatch, sleeps, booked,
                                                               name, sends):
    upstream = FakeUpstream([Reply(error=getattr(httpx, name)("x")),
                             json_reply(_COMPAT_OK)]).install(monkeypatch)
    ctx = {**_compat_ctx("r-n1-c"), "custom_connection": True}
    await _run(OpenAIAdapter(stream=True), "grok-4", ctx)
    assert len(upstream.requests) == sends


@pytest.mark.parametrize(("name", "sends"), _FAILURES)
async def test_gemini_without_stream_retries_only_connect_failures(monkeypatch, sleeps, booked,
                                                                   name, sends):
    _stream_off(monkeypatch, gemini_adapter, "rules_for")
    upstream = FakeUpstream([Reply(error=getattr(httpx, name)("x")),
                             json_reply(_GEMINI_OK)]).install(monkeypatch)
    await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-n1-g"))
    assert len(upstream.requests) == sends


@pytest.mark.parametrize(("name", "sends"), _FAILURES)
async def test_claude_without_stream_retries_only_connect_failures(monkeypatch, sleeps, booked,
                                                                   name, sends):
    _stream_off(monkeypatch, claude_adapter, "claude_rules")
    seen: list = []
    client = _claude_client([getattr(httpx2, name)("x"),
                             httpx2.Response(200, json=_CLAUDE_MESSAGE)], seen)
    try:
        await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                   _ctx("r-n1-cl", anthropic_client=client))
    finally:
        await client.close()
    assert len(seen) == sends


@pytest.mark.parametrize(("name", "sends"), _FAILURES)
async def test_responses_without_stream_retries_only_connect_failures(monkeypatch, sleeps,
                                                                      booked, name, sends):
    _stream_off(monkeypatch, responses_adapter, "rules_for")
    seen: list = []
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "好", "annotations": []}]}
    done = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
            "status": "completed", "output": [message], "usage": _RESP_USAGE}
    _install_responses(monkeypatch, [getattr(httpx2, name)("x"),
                                     httpx2.Response(200, json=done)], seen)
    await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-n1-r"))
    assert len(seen) == sends


async def test_claude_rate_limit_error_inside_the_stream_is_retried(sleeps, booked):
    seen: list = []
    client = _claude_client([[_claude_error_event("rate_limit_error")],
                             [_claude_events("好", 7)]], seen)
    try:
        chunks = await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                            _ctx("r-n1-rl", anthropic_client=client))
    finally:
        await client.close()
    assert _statuses(chunks) == ["Claude busy (rate_limit_error), retrying... (attempt 1)"]
    assert len(seen) == 2


@pytest.mark.parametrize("family", ["compat", "gemini", "claude", "responses"])
async def test_stream_path_still_retries_read_errors_before_200(monkeypatch, sleeps, booked,
                                                                family):
    """串流路徑維持原判斷（R-3b ② 第 2 次確認）：還沒收到 200 就斷，連讀取錯誤也重試。"""
    seen: list = []
    if family == "compat":
        upstream = FakeUpstream([Reply(error=httpx.RemoteProtocolError("reset")),
                                 stream_reply(_COMPAT_OK, "one")]).install(monkeypatch)
        await _run(OpenAIAdapter(stream=True), "grok-4", _compat_ctx("r-n1-sc"))
        seen = upstream.requests
    elif family == "gemini":
        upstream = FakeUpstream([Reply(error=httpx.RemoteProtocolError("reset")),
                                 gemini_stream_reply(_GEMINI_OK, "one")]).install(monkeypatch)
        await _run(GeminiAdapter(stream=True), "gemini-2.5-flash", _gemini_ctx("r-n1-sg"))
        seen = upstream.requests
    elif family == "claude":
        client = _claude_client([httpx2.RemoteProtocolError("reset"),
                                 [_claude_events("好", 7)]], seen)
        try:
            await _run(ClaudeAdapter(stream=True), "claude-opus-5-5",
                       _ctx("r-n1-scl", anthropic_client=client))
        finally:
            await client.close()
    else:
        _install_responses(monkeypatch, [httpx2.RemoteProtocolError("reset"),
                                         [_responses_done(usage=_RESP_USAGE)]], seen)
        await _run(ResponsesAdapter(stream=True), "gpt-6-astra", _responses_ctx("r-n1-sr"))
    assert len(seen) == 2


def test_cause_chain_is_walked_with_a_depth_limit():
    """R-3b ② 第 3 次 LOW：SDK 例外沿 __cause__ 最多找 8 層（循環的鏈也會停）。"""
    def wrapped(depth: int) -> BaseException:
        inner: BaseException = httpx2.ConnectError("down")
        for _ in range(depth):
            outer = RuntimeError("wrap")
            outer.__cause__ = inner
            inner = outer
        return inner

    error = RoundError("x")
    assert retry.on_post_failure(error, wrapped(7), "n").retry_note == "n"  # 第 8 層找得到
    assert retry.on_post_failure(error, wrapped(8), "n").retry_note is None  # 第 9 層就不找了
