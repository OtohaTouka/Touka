"""3b-U4b-1：OpenAI Responses 的送出、原始事件彙整、解析與用量（ADR-0248 D1／D3／D5；附註 7、27）。

上游一律是真的 openai SDK＋httpx2.MockTransport 吐 Responses 原始事件（SSE）。每個彙整情境跑三種
切法（1 個 chunk／一個事件一個 chunk／位元組亂切，至少一刀切在 UTF-8 字元中間），結果都要一樣。

- 完整＝``response.completed``／``response.incomplete``；沒收到就斷線、error 事件、
  ``response.failed``、資料帶 ``error``（SDK 丟例外）都轉 RoundError，下游沒有半截文字。
- completed 沒帶 output 時依 output_index 補回（測試故意對調到達順序）。
- 解析結果與非串流相同（reasoning、多個 function_call 用 call_id）；非串流送出、停止原因、錯誤文字、
  誠實守衛在 test_responses_parse.py。
- 用量扣 cached_tokens；沒完成、取消、完成卻沒用量時照 U1b 規則估（估算值見下）。
- 兩段逾時與整輪上限：閒置計時看 httpx2 層位元組，``:`` 註解行（ping）也算。
- 送出內容和 U4a fixture 一致（串流旗標只在送出當下合併）。

估算值：第一輪 kwargs 的 JSON 是 192 個非 CJK 字 → 192÷3.5＝54.9 → 55；「abcdefg」→ 2；「好」→ 1。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.handlers import react_core
from src.handlers.adapters import responses_adapter, stream_timeout
from src.handlers.adapters.base import RoundError, ToolCall
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.identity import AgentIdentity

from tests.helpers.stream_transport import byte_cuts
from tests.unit.test_responses_parse import BASE as _BASE
from tests.unit.test_responses_parse import CALL1 as _CALL1
from tests.unit.test_responses_parse import CALL2 as _CALL2
from tests.unit.test_responses_parse import CTX as _CTX
from tests.unit.test_responses_parse import MSG as _MSG
from tests.unit.test_responses_parse import REASONING as _REASONING
from tests.unit.test_responses_parse import USAGE as _USAGE
from tests.unit.test_responses_parse import final as _final

ROOT = Path(__file__).resolve().parents[2]
WIRE = json.loads((ROOT / "tests/fixtures/responses_u4a_wire.json").read_text(encoding="utf-8"))
_GUARD_S = 5.0
_FIRST, _IDLE, _ROUND = 0.6, 0.2, 1.2
_SSE = {"content-type": "text/event-stream"}
_ZERO = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
_MODES = ("one", "semantic", "bytes")
_PING = b": ping\n\n"



def _ev(kind: str, **data: Any) -> bytes:
    body = json.dumps({"type": kind, **data}, ensure_ascii=False)
    return f"event: {kind}\ndata: {body}\n\n".encode()


def _text_events(final: dict | None = None) -> list[bytes]:
    return [
        _ev("response.created", sequence_number=0, response=_BASE),
        _ev("response.output_item.added", sequence_number=1, output_index=0,
            item={**_MSG, "status": "in_progress", "content": []}),
        _ev("response.output_text.delta", sequence_number=2, output_index=0, item_id="msg_1",
            content_index=0, delta="檔案說", logprobs=[]),
        _ev("response.output_text.delta", sequence_number=3, output_index=0, item_id="msg_1",
            content_index=0, delta=" hi", logprobs=[]),
        _ev("response.output_item.done", sequence_number=4, output_index=0, item=_MSG),
        _ev("response.completed", sequence_number=5,
            response=final or _final(output=[_MSG], usage=_USAGE)),
    ]


def _tool_events() -> list[bytes]:
    return [
        _ev("response.created", sequence_number=0, response=_BASE),
        _ev("response.output_item.done", sequence_number=1, output_index=0, item=_REASONING),
        _ev("response.function_call_arguments.delta", sequence_number=2, output_index=1,
            item_id="fc_1", delta='{"action":'),
        _ev("response.function_call_arguments.delta", sequence_number=3, output_index=1,
            item_id="fc_1", delta='"list"}'),
        _ev("response.output_item.done", sequence_number=4, output_index=1, item=_CALL1),
        _ev("response.output_item.done", sequence_number=5, output_index=2, item=_CALL2),
        _ev("response.completed", sequence_number=6,
            response=_final(output=[_REASONING, _CALL1, _CALL2], usage=_USAGE)),
    ]


def _pieces(events: list[bytes], mode: str) -> list[bytes]:
    body = b"".join(events)
    return {"one": [body], "semantic": list(events), "bytes": byte_cuts(body)}[mode]


class _Upstream(httpx2.AsyncByteStream):
    """腳本化的上游位元組：bytes＝送出、float＝等幾秒、Event＝停住等它；跑完＝連線結束。"""

    def __init__(self, script: list) -> None:
        self._script = script
        self.closed = asyncio.Event()

    async def __aiter__(self) -> Any:
        for step in self._script:
            if isinstance(step, bytes):
                yield step
            elif isinstance(step, asyncio.Event):
                await step.wait()
            else:
                await asyncio.sleep(step)

    async def aclose(self) -> None:
        self.closed.set()


class _Wire:
    """httpx2.MockTransport：依序回預排的回應（list＝SSE 腳本，Response＝原樣）。"""

    def __init__(self, *replies: list | httpx2.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []
        self.upstreams: list[_Upstream] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, httpx2.Response):
            return reply
        upstream = _Upstream(reply)
        self.upstreams.append(upstream)
        return httpx2.Response(200, headers=_SSE, stream=upstream, request=request)

    def install(self, monkeypatch) -> _Wire:
        def http_client(**kwargs: Any) -> Any:
            return openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self), **kwargs)

        monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)
        return self

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


def _request(text: str = "hi", **extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model="gpt-6-astra", messages=[{"role": "user", "content": text}],
                             **extra)


async def _round(monkeypatch, *replies: list | httpx2.Response, ctx: dict | None = None,
                 request: Any = None) -> tuple[Any, _Wire, Any]:
    wire = _Wire(*replies).install(monkeypatch)
    adapter = ResponsesAdapter(stream=True)
    state = await adapter.prepare_request(request or _request(), dict(ctx or _CTX))
    try:
        async with asyncio.timeout(_GUARD_S):
            items = [item async for item in adapter.send_round(state)]
    finally:
        await adapter.close(state)
    assert len(items) == 1  # 只在最後交出一項（不回半輪）
    return items[0], wire, state


# ── 彙整與解析（三種切法） ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", _MODES)
async def test_text_round(monkeypatch, mode):
    raw, wire, state = await _round(monkeypatch, _pieces(_text_events(), mode))
    assert raw == _final(output=[_MSG], usage=_USAGE)
    adapter = ResponsesAdapter()
    turn = adapter.parse_round(state, raw)
    assert (turn.visible_text, turn.text_parts, turn.tool_calls) == ("檔案說 hi", ["檔案說 hi"], [])
    assert adapter.extract_usage(state, raw)[:2] == (
        {"prompt_tokens": 176, "cache_read_input_tokens": 1024,
         "cache_creation_input_tokens": 0, "completion_tokens": 40}, 1200)
    assert adapter.stop_reason(state, raw) == "end"
    assert adapter.partial_usage(state) is None  # 完整且有用量：核心照常入帳，不重記
    assert wire.bodies[0]["stream"] is True and "stream" not in state.kwargs  # D2
    assert [up.closed.is_set() for up in wire.upstreams] == [True]


@pytest.mark.parametrize("mode", _MODES)
async def test_reasoning_and_two_function_calls(monkeypatch, mode):
    raw, _, state = await _round(monkeypatch, _pieces(_tool_events(), mode))
    turn = ResponsesAdapter().parse_round(state, raw)
    assert turn.reasoning_parts == ["先讀檔"]
    assert turn.tool_calls == [ToolCall(id="call_1", name="vault_tools", args={"action": "list"}),
                               ToolCall(id="call_2", name="memory_tools", args={"action": "query"})]
    assert turn.raw_assistant == [_REASONING, _CALL1, _CALL2]  # 加密 reasoning 原樣留給下一輪
    assert turn.visible_text == ""


@pytest.mark.parametrize("mode", _MODES)
async def test_completed_without_output_is_rebuilt_by_output_index(monkeypatch, mode):
    events = [_ev("response.output_item.done", sequence_number=1, output_index=1, item=_CALL1),
              _ev("response.output_item.done", sequence_number=2, output_index=0, item=_REASONING),
              _ev("response.completed", sequence_number=3,
                  response={k: v for k, v in _final(usage=_USAGE).items() if k != "output"})]
    raw, _, _ = await _round(monkeypatch, _pieces(events, mode))
    assert raw["output"] == [_REASONING, _CALL1]


@pytest.mark.parametrize("mode", _MODES)
async def test_incomplete_event_is_a_complete_signal(monkeypatch, mode):
    partial = {**_MSG, "status": "incomplete",
               "content": [{"type": "output_text", "text": "截到一", "annotations": []}]}
    final = _final("incomplete", output=[partial], usage=_USAGE,
                   incomplete_details={"reason": "max_output_tokens"})
    events = [_ev("response.created", sequence_number=0, response=_BASE),
              _ev("response.output_text.delta", sequence_number=1, output_index=0,
                  item_id="msg_1", content_index=0, delta="截到一", logprobs=[]),
              _ev("response.incomplete", sequence_number=2, response=final)]
    raw, _, state = await _round(monkeypatch, _pieces(events, mode))
    assert raw == final  # 不是 RoundError：截斷照常交給主迴圈
    assert ResponsesAdapter().stop_reason(state, raw) == "max_tokens"


# ── 錯誤與半截 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", _MODES)
async def test_cut_before_completed_is_round_error(monkeypatch, mode):
    raw, wire, _ = await _round(monkeypatch, _pieces(_text_events()[:4], mode))
    assert raw == RoundError(
        "❌ **OpenAI 連線錯誤**\n\n`上游串流沒送完就中斷（缺 response.completed）`")
    assert [up.closed.is_set() for up in wire.upstreams] == [True]


async def test_error_event_mid_stream_is_round_error(monkeypatch):
    error = _ev("error", sequence_number=3, code="server_error", message="overloaded", param=None)
    raw, _, _ = await _round(monkeypatch, [*_text_events()[:3], error])
    assert raw == RoundError('OpenAI API Error (200)\n\n`{"code": "server_error", '
                             '"message": "overloaded", "param": null, "sequence_number": 3, '
                             '"type": "error"}`')


async def test_error_event_closes_the_upstream_at_once(monkeypatch):
    """error 事件後上游還掛著：這一輪交出 RoundError 時連線已關（不等 client.close）。"""
    error = _ev("error", sequence_number=1, code="server_error", message="x", param=None)
    wire = _Wire([_text_events()[0], error, asyncio.Event()]).install(monkeypatch)
    adapter = ResponsesAdapter(stream=True)
    state = await adapter.prepare_request(_request(), dict(_CTX))
    async with asyncio.timeout(_GUARD_S):
        items = [item async for item in adapter.send_round(state)]
    closed_before_client_close = wire.upstreams[0].closed.is_set()
    await adapter.close(state)
    assert isinstance(items[0], RoundError)
    assert closed_before_client_close


async def test_failed_response_is_round_error(monkeypatch):
    failed = _final("failed", error={"code": "server_error", "message": "boom"})
    raw, _, _ = await _round(monkeypatch, [_ev("response.failed", sequence_number=0,
                                               response=failed)])
    assert raw == RoundError('OpenAI API Error (200)\n\n`{"code": "server_error", '
                             '"message": "boom"}`')


async def test_error_key_in_data_is_round_error(monkeypatch):
    raw, _, _ = await _round(monkeypatch, [b'data: {"error": {"message": "bad stream"}}\n\n'])
    assert raw == RoundError("OpenAI API Error (200)\n\n`bad stream`")


# ── 送出內容和 U4a fixture 一致 ─────────────────────────────────────────


async def test_streamed_request_matches_the_u4a_wire_fixture(monkeypatch):
    request = OpenAIChatRequest(
        model="gpt-6-astra", max_tokens=512, temperature=0.3, top_p=0.9, top_k=40, stop=["END"],
        tools=[{"type": "function", "server": "touka", "category": "fs", "function": {
            "name": "vault_tools", "description": "檔案",
            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}}}}],
        messages=[{"role": "system", "content": "妳是助理。"},
                  {"role": "system", "content": "用繁體中文。"},
                  {"role": "user", "content": "讀 a.md"}])
    ctx = {**_CTX, "agent_id": "Alice", "session_id": "sess-1"}
    _, wire, _ = await _round(monkeypatch, _text_events(), ctx=ctx, request=request)
    assert wire.bodies == [WIRE["first_round"]]


# ── 兩段逾時、整輪上限、取消（D3、附註 27） ──────────────────────────────────


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setattr(stream_timeout, "FIRST_EVENT_TIMEOUT_S", _FIRST)
    monkeypatch.setattr(stream_timeout, "IDLE_TIMEOUT_S", _IDLE)
    monkeypatch.setattr(stream_timeout, "ROUND_TIMEOUT_S", _ROUND)


def _timeout(phase: str) -> RoundError:
    return RoundError(f"❌ **OpenAI 連線錯誤**\n\n`上游串流逾時：{phase}`")


async def test_pings_keep_a_slow_stream_alive(monkeypatch, short):
    """只有 ping 的 0.8 秒（超過閒置也超過首事件，沒超過整輪上限）不逾時。"""
    events = _text_events()
    alive = [step for _ in range(16) for step in (0.05, _PING)]
    raw, _, _ = await _round(monkeypatch, [events[0], *alive, *events[1:]])
    assert raw["output"] == [_MSG]


async def test_pings_forever_hit_the_round_cap(monkeypatch, short):
    alive = [step for _ in range(200) for step in (0.05, _PING)]
    raw, wire, _ = await _round(monkeypatch, [_text_events()[0], *alive])
    assert raw == _timeout("整輪超過上限")
    assert [up.closed.is_set() for up in wire.upstreams] == [True]


async def test_stall_hits_the_idle_timeout(monkeypatch, short):
    """有內容（第一段 delta）之後停住＝閒置逾時；只有空殼事件時算首事件（R-3b ② M2）。"""
    raw, wire, _ = await _round(monkeypatch, [*_text_events()[:3], asyncio.Event()])
    assert raw == _timeout("閒置太久")
    assert [up.closed.is_set() for up in wire.upstreams] == [True]


async def test_no_first_byte_times_out(monkeypatch, short):
    raw, _, _ = await _round(monkeypatch, [_FIRST + 0.5, *_text_events()])
    assert raw == _timeout("等不到第一個回應")


async def test_cancel_waits_until_upstream_is_closed(monkeypatch, short):
    wire = _Wire([_text_events()[0], asyncio.Event()]).install(monkeypatch)
    adapter = ResponsesAdapter(stream=True)
    state = await adapter.prepare_request(_request(), dict(_CTX))

    async def consume() -> None:
        async for _ in react_core.pump_send_round(adapter.send_round(state)):
            pass

    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(consume())
        while not wire.upstreams:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wire.upstreams[0].closed.is_set()  # 取消傳回來時上游已關
    await adapter.close(state)


# ── 主迴圈：沒有半截文字；用量（U1b 規則） ──────────────────────────────────


def _loop_ctx(rid: str) -> dict:
    return {**_CTX, "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
            "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
            "request_id": rid, "t0": 0.0, "agent_id": "Sumi",
            "identity": AgentIdentity.for_role("Sumi", "Owner")}


@pytest.fixture
def booked(monkeypatch) -> list:
    calls: list = []

    def book(_identity: Any, _provider: Any, usage: dict, *_rest: Any, **_kw: Any) -> None:
        if usage:  # 空用量不算
            calls.append(usage)

    monkeypatch.setattr(react_core, "accumulate_token_usage", book)
    return calls


async def _loop(monkeypatch, *replies: list | httpx2.Response,
                rid: str = "req-r") -> tuple[list[str], list[dict]]:
    _Wire(*replies).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in react_core.run_react_loop(
            ResponsesAdapter(stream=True), _request(), _loop_ctx(rid))]
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return chunks, [e for e in events if e.get("kind") == "token_usage"]


async def test_main_loop_sends_no_partial_text_downstream(monkeypatch, booked):
    half = [_ev("response.output_text.delta", sequence_number=0, output_index=0, item_id="m",
                content_index=0, delta="半截回答", logprobs=[])]
    chunks, _ = await _loop(monkeypatch, half)
    joined = "".join(chunks)
    assert "半截回答" not in joined
    assert "缺 response.completed" in joined
    assert chunks[-1] == "data: [DONE]\n\n"


def _delta(text: str) -> bytes:
    return _ev("response.output_text.delta", sequence_number=1, output_index=0, item_id="m",
               content_index=0, delta=text, logprobs=[])


_HAO = {**_MSG, "content": [{"type": "output_text", "text": "好", "annotations": []}]}


@pytest.mark.parametrize(("script", "usage", "estimated"), [
    ([_delta("abcdefg")], {"prompt_tokens": 55, **_ZERO, "completion_tokens": 2}, True),
    ([_delta("好"), _ev("response.completed", sequence_number=2, response=_final(output=[_HAO]))],
     {"prompt_tokens": 55, **_ZERO, "completion_tokens": 1}, True),
    ([_ev("response.completed", sequence_number=2, response=_final(output=[_HAO]))],
     {"prompt_tokens": 55, **_ZERO, "completion_tokens": 1}, True),  # 沒有 delta：用 output 估
    ([_delta("好"), _ev("response.completed", sequence_number=2,
                        response=_final(output=[_HAO], usage=_USAGE))],
     {"prompt_tokens": 176, "cache_read_input_tokens": 1024, "cache_creation_input_tokens": 0,
      "completion_tokens": 40}, False),
], ids=["half", "done-without-usage", "done-without-deltas", "done-with-usage"])
async def test_round_usage_is_booked_once(monkeypatch, booked, script, usage, estimated):
    _, events = await _loop(monkeypatch, script)
    assert booked == [usage]
    assert ("estimated" in events[0], events[0].get("estimated")) == (
        (True, True) if estimated else (False, None))


async def test_stream_that_never_opened_books_nothing(monkeypatch, booked):
    _, events = await _loop(monkeypatch, httpx2.Response(400, json={"error": {"message": "x"}}))
    assert (booked, events) == ([], [])


async def test_cancelled_stream_books_an_estimate(monkeypatch, booked, short):
    wire = _Wire([_delta("abcdefg"), asyncio.Event()]).install(monkeypatch)
    loop = react_core.run_react_loop(ResponsesAdapter(stream=True), _request(), _loop_ctx("r-c"))
    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(anext(loop))
        while not wire.upstreams:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert booked == [{"prompt_tokens": 55, **_ZERO, "completion_tokens": 2}]
