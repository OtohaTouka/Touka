"""3b-U1：Claude 串流的兩段逾時、整輪上限、取消與關上游（ADR-0248 D3、附註 6／16／27）。

確定性做法沿用 test_core_keepalive：把三個逾時常數縮小，上游用 Event 或短暫等待控制時序；
每個測試外層包 ``asyncio.timeout``，突變時變紅而不是卡住（專案沒有 pytest-timeout）。
上游是真的 AsyncAnthropic＋httpx2.MockTransport（⑦ 用 loopback 小伺服器），閒置計時看
httpx2 層收到的位元組：anthropic SDK 在內部丟掉 ping，adapter 收不到（附註 27）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import stream_timeout
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks

_FIRST, _IDLE, _ROUND = 0.6, 0.2, 1.2  # 縮小後的首事件／閒置／整輪（秒）
_GUARD_S = 5.0
_SSE = {"content-type": "text/event-stream"}
_MODEL = "claude-opus-5-5"


def _ev(kind: str, **data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data}, ensure_ascii=False)}\n\n".encode()


_PING = b'event: ping\ndata: {"type": "ping"}\n\n'
_START = _ev("message_start", message={
    "id": "msg_t", "type": "message", "role": "assistant", "model": _MODEL, "content": [],
    "stop_reason": None, "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 1}})
_TEXT = [_ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
         _ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "好"})]
_TAIL = [_ev("content_block_stop", index=0),
         _ev("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None},
             usage={"output_tokens": 2}),
         _ev("message_stop")]


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", _FIRST)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", _IDLE)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", _ROUND)


class _Upstream(httpx2.AsyncByteStream):
    """腳本化的上游位元組：bytes＝送出、float＝等幾秒、Event＝停住等它；腳本跑完＝連線結束。"""

    def __init__(self, script: list) -> None:
        self._script = script
        self.waiting = asyncio.Event()  # 前面的位元組都被讀走、停在 Event 上了
        self.closed = asyncio.Event()

    async def __aiter__(self) -> Any:
        for step in self._script:
            if isinstance(step, bytes):
                yield step
            elif isinstance(step, asyncio.Event):
                self.waiting.set()
                await step.wait()
            else:
                await asyncio.sleep(step)

    async def aclose(self) -> None:
        self.closed.set()


def _client(upstream: _Upstream) -> AsyncAnthropic:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers=_SSE, stream=upstream)

    return AsyncAnthropic(
        api_key="fake-key", base_url="https://api.anthropic.test", max_retries=0,
        http_client=DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handle), follow_redirects=False,
            event_hooks=liveness_event_hooks(),
        ),
    )


async def _state(client: AsyncAnthropic) -> Any:
    request = OpenAIChatRequest(model=_MODEL, max_tokens=1024,
                                messages=[OpenAIMessage(role="user", content="hi")])
    return await ClaudeAdapter(stream=True).prepare_request(request, {"anthropic_client": client})


async def _run(script: list) -> tuple[Any, _Upstream, float]:
    upstream = _Upstream(script)
    client = _client(upstream)
    loop = asyncio.get_running_loop()
    try:
        state = await _state(client)
        started = loop.time()
        async with asyncio.timeout(_GUARD_S):
            (result,) = [item async for item in ClaudeAdapter(stream=True).send_round(state)]
        return result, upstream, loop.time() - started
    finally:
        await client.close()


def _timeout_error(phase: str) -> RoundError:
    return RoundError(f"❌ **API 發生錯誤**\n\n`上游串流逾時：{phase}`")


# ── 兩段逾時本身（附註 6） ────────────────────────────────────────────────────


async def test_first_byte_slower_than_idle_but_within_first_event_succeeds():
    result, _up, _elapsed = await _run([0.35, _START, *_TEXT, *_TAIL])  # 0.2 < 0.35 < 0.6
    assert not isinstance(result, RoundError)
    assert [b.text for b in result.content] == ["好"]


async def test_no_first_byte_within_first_event_times_out():
    result, upstream, elapsed = await _run([_FIRST + 0.5, _START, *_TEXT, *_TAIL])
    assert result == _timeout_error("等不到第一個回應")
    assert _FIRST - 0.05 <= elapsed < _FIRST + 0.4
    assert upstream.closed.is_set()  # 取消後關了上游


async def test_pings_alone_keep_the_stream_alive():
    """只有 ping 的 0.8 秒（超過閒置也超過首事件）不逾時：SDK 丟掉 ping，計時看位元組。"""
    pings = [step for _ in range(16) for step in (0.05, _PING)]
    result, _up, elapsed = await _run([_START, *pings, *_TEXT, *_TAIL])
    assert not isinstance(result, RoundError)
    assert elapsed >= 0.8


async def test_pings_forever_hit_the_round_cap():
    pings = [step for _ in range(200) for step in (0.05, _PING)]
    result, upstream, elapsed = await _run([_START, *pings])
    assert result == _timeout_error("整輪超過上限")
    assert _ROUND - 0.05 <= elapsed < _ROUND + 0.4
    assert upstream.closed.is_set()


# ── 時序案例（附註 16：①②④⑦；③ 取消見下） ────────────────────────────────────


async def test_stalled_stream_hits_idle_timeout_and_closes_upstream():
    """①：收到一些事件後停住 → 閒置值＋ε 內 RoundError、上游已關、不回半輪。"""
    result, upstream, elapsed = await _run([_START, *_TEXT, asyncio.Event()])
    assert result == _timeout_error("閒置太久")
    assert _IDLE - 0.05 <= elapsed < _IDLE + 0.4
    assert upstream.closed.is_set()


async def test_slow_but_alive_stream_succeeds():
    """②：事件間隔＝閒置值的 1/4、總長 ≥ 4 倍閒置值，要成功（逐事件換成總逾時就會紅）。"""
    deltas = [step for i in range(16) for step in (_IDLE / 4, _ev(
        "content_block_delta", index=0, delta={"type": "text_delta", "text": str(i % 10)}))]
    result, _up, elapsed = await _run([_START, _TEXT[0], *deltas, *_TAIL])
    assert not isinstance(result, RoundError)
    assert result.content[0].text == "0123456789012345"
    assert elapsed >= 4 * _IDLE


async def test_slow_consumer_after_the_round_is_not_cancelled():
    """④：逾時區塊不跨過 yield——交出結果後消費端慢，不會被期限取消。"""
    upstream = _Upstream([_START, *_TEXT, *_TAIL])
    client = _client(upstream)
    try:
        state = await _state(client)
        async with asyncio.timeout(_GUARD_S):
            rounds = ClaudeAdapter(stream=True).send_round(state)
            first = await rounds.__anext__()
            await asyncio.sleep(_FIRST + 0.1)  # 比首事件、閒置都久
            with pytest.raises(StopAsyncIteration):
                await rounds.__anext__()
    finally:
        await client.close()
    assert not isinstance(first, RoundError)


async def test_cancel_waits_until_upstream_is_closed():
    """③：外部取消 → task 被取消、沒有 RoundError；pump 等那一輪真的關掉上游才往外拋。"""
    upstream = _Upstream([_START, *_TEXT, asyncio.Event()])
    client = _client(upstream)
    items: list = []

    async def consume() -> None:
        state = await _state(client)
        async for item in react_core.pump_send_round(ClaudeAdapter(stream=True).send_round(state)):
            items.append(item)

    try:
        async with asyncio.timeout(_GUARD_S):
            task = asyncio.create_task(consume())
            await upstream.waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert upstream.closed.is_set()  # 取消傳回來時上游已經關了（沒有孤兒任務）
    finally:
        await client.close()
    assert task.cancelled()
    assert not any(isinstance(item, RoundError) for item in items)
    assert all(isinstance(item, str) for item in items)  # 只有心跳，沒有半輪結果


async def test_cancel_on_loopback_server_disconnects_and_pool_still_works(monkeypatch):
    """⑦：真 TCP（127.0.0.1）：取消後伺服器看到斷線，同一個 client 的下一個請求照常成功。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                 "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    first_open = asyncio.Event()
    disconnected = asyncio.Event()
    served: list[int] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        length = next(int(line.split(":")[1]) for line in head.split("\r\n")
                      if line.lower().startswith("content-length:"))
        await reader.readexactly(length)
        served.append(len(served) + 1)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Connection: close\r\n\r\n" + _START)
        await writer.drain()
        if len(served) == 1:
            first_open.set()
            if await reader.read(1) == b"":  # 客戶端關連線＝EOF
                disconnected.set()
        else:
            writer.write(b"".join([*_TEXT, *_TAIL]))
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = AsyncAnthropic(
        api_key="fake-key", base_url=f"http://127.0.0.1:{port}", max_retries=0,
        http_client=DefaultAsyncHttpxClient(follow_redirects=False,
                                            event_hooks=liveness_event_hooks()),
    )
    try:
        async with asyncio.timeout(_GUARD_S):
            task = asyncio.create_task(_drain_round(client))
            await first_open.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 1.0)
            result = await _drain_round(client)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert not isinstance(result, RoundError)
    assert [b.text for b in result.content] == ["好"]
    assert served == [1, 2]


async def _drain_round(client: AsyncAnthropic) -> Any:
    state = await _state(client)
    (result,) = [item async for item in ClaudeAdapter(stream=True).send_round(state)]
    return result


async def test_pump_closes_round_suspended_between_items():
    """收流時 round_gen 停在兩項之間（如 Gemini 的重試通知）：pump 也要 aclose 它。"""
    cleaned = asyncio.Event()

    async def two_items() -> Any:
        try:
            yield "data: retrying\n\n"
            yield {"ok": 1}
        finally:
            cleaned.set()

    pump = react_core.pump_send_round(two_items())
    assert await pump.__anext__() == "data: retrying\n\n"
    await pump.aclose()
    assert cleaned.is_set()


async def test_closing_the_main_loop_closes_upstream(monkeypatch):
    """消費端在心跳時收流（客戶端斷線）：主迴圈關掉 pump，上游立刻關，不等閒置逾時。"""
    monkeypatch.setattr(react_core, "_KEEPALIVE_S", 0.02)
    upstream = _Upstream([_START, *_TEXT, asyncio.Event()])
    client = _client(upstream)
    request = OpenAIChatRequest(model=_MODEL, messages=[OpenAIMessage(role="user", content="hi")])
    ctx = {"anthropic_client": client, "protocol_manager": None, "tool_registry": {},
           "memory_tool_registry": {}, "plugin_registry": {}, "request_id": "req-close",
           "t0": 0.0, "agent_id": "Sumi", "model": _MODEL}
    try:
        async with asyncio.timeout(_GUARD_S):
            loop = react_core.run_react_loop(ClaudeAdapter(stream=True), request, ctx)
            assert await anext(loop) == ": heartbeat\n\n"
            await upstream.waiting.wait()  # 上游停住、主迴圈停在心跳那個 yield 上
            await loop.aclose()
            assert upstream.closed.is_set()
    finally:
        await client.close()
