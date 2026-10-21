"""3b-U1：Claude 主迴圈上游串流，用真的 AsyncAnthropic＋httpx2.MockTransport（ADR-0248）。

- C1 常駐：``messages.stream()`` 彙整出的最終 Message 與同內容的 ``create()`` 完全相同；涵蓋 ping、
  redacted_thinking、多個 tool_use、空 input；送出內容＝非串流內容＋``stream: true``。
- 附註 7：完整＝``message_stop``；沒送就斷線、串流中的 error（overloaded）事件都轉 RoundError，
  下游沒有半截文字。
- 附註 18：Claude 錯誤文字格式（``str(e)``）直接字串釘住。
- D1／D6：預設非串流（complete_once）；規則表 ``stream`` 關掉時主迴圈改走 ``create``。
- 主迴圈 client（main.py）裝了 httpx2 位元組計時 hook（附註 27）。

不連網：MockTransport 不開 socket，目的地是 ``.test`` 網域。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src import main as proxy_main
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import claude_adapter
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks
from src.handlers.claude_handler import handle_claude_request
from src.models.identity import AgentIdentity
from src.services.models.model_rules import claude_rules

_GUARD_S = 5.0  # 每個串流測試的外層期限：突變時變紅而不是卡住（專案沒有 pytest-timeout）
_SSE = {"content-type": "text/event-stream"}
_MODEL = "claude-opus-5-5"
_USAGE_START = {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 100}
_FULL = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": _MODEL,
    "content": [
        {"type": "thinking", "thinking": "先想想", "signature": "sig-abc"},
        {"type": "redacted_thinking", "data": "ENCRYPTED"},
        {"type": "text", "text": "我來查"},
        {"type": "tool_use", "id": "toolu_1", "name": "vault_read",
         "input": {"path": "a.md", "opts": {"n": 2}}},
        {"type": "tool_use", "id": "toolu_2", "name": "vault_list", "input": {}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {**_USAGE_START, "output_tokens": 40},
}


def _ev(kind: str, **data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data}, ensure_ascii=False)}\n\n".encode()


_PING = b'event: ping\ndata: {"type": "ping"}\n\n'
_START = _ev("message_start", message={**_FULL, "content": [], "stop_reason": None,
                                       "usage": _USAGE_START})
_STOP = _ev("message_stop")


def _full_stream() -> list[bytes]:
    return [
        _START,
        _PING,
        _ev("content_block_start", index=0,
            content_block={"type": "thinking", "thinking": "", "signature": ""}),
        _ev("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "先想"}),
        _ev("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "想"}),
        _ev("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "sig-abc"}),
        _ev("content_block_stop", index=0),
        _ev("content_block_start", index=1,
            content_block={"type": "redacted_thinking", "data": "ENCRYPTED"}),
        _ev("content_block_stop", index=1),
        _PING,
        _ev("content_block_start", index=2, content_block={"type": "text", "text": ""}),
        _ev("content_block_delta", index=2, delta={"type": "text_delta", "text": "我來"}),
        _ev("content_block_delta", index=2, delta={"type": "text_delta", "text": "查"}),
        _ev("content_block_stop", index=2),
        _ev("content_block_start", index=3, content_block={
            "type": "tool_use", "id": "toolu_1", "name": "vault_read", "input": {}}),
        _ev("content_block_delta", index=3,
            delta={"type": "input_json_delta", "partial_json": '{"path": "a.md", '}),
        _ev("content_block_delta", index=3,
            delta={"type": "input_json_delta", "partial_json": '"opts": {"n": 2}}'}),
        _ev("content_block_stop", index=3),
        _ev("content_block_start", index=4, content_block={
            "type": "tool_use", "id": "toolu_2", "name": "vault_list", "input": {}}),
        _ev("content_block_stop", index=4),
        _ev("message_delta", delta={"stop_reason": "tool_use", "stop_sequence": None},
            usage={"output_tokens": 40}),
        _STOP,
    ]


class _Wire:
    """MockTransport 的 handler：記下送出內容（從 request.content 解析）並回預排的回應。"""

    def __init__(self, *responses: httpx2.Response | list[bytes]) -> None:
        self.sent: list[dict] = []
        self._responses = list(responses)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(json.loads(request.content))
        reply = self._responses.pop(0)
        if isinstance(reply, list):
            return httpx2.Response(200, headers=_SSE, content=b"".join(reply))
        return reply

    def client(self) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key="fake-key", base_url="https://api.anthropic.test", max_retries=0,
            http_client=DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self), follow_redirects=False,
                event_hooks=liveness_event_hooks(),
            ),
        )


def _request(text: str = "讀 a.md") -> OpenAIChatRequest:
    return OpenAIChatRequest(model=_MODEL, max_tokens=1024,
                             messages=[OpenAIMessage(role="user", content=text)])


async def _one_round(adapter: ClaudeAdapter, wire: _Wire,
                     request: OpenAIChatRequest | None = None) -> tuple[Any, Any]:
    client = wire.client()
    try:
        state = await adapter.prepare_request(request or _request(), {"anthropic_client": client})
        async with asyncio.timeout(_GUARD_S):
            items = [item async for item in adapter.send_round(state)]
    finally:
        await client.close()
    assert len(items) == 1  # 串流只在最後交出一項（不回半輪）
    return state, items[0]


# ── C1：最終 Message 與 create() 相同 ─────────────────────────────────────────


async def test_streamed_message_equals_create_message():
    created_wire = _Wire(httpx2.Response(200, json=_FULL))
    _state, created = await _one_round(ClaudeAdapter(), created_wire)
    streamed_wire = _Wire(_full_stream())
    state, streamed = await _one_round(ClaudeAdapter(stream=True), streamed_wire)

    assert streamed.model_dump() == created.model_dump()
    assert streamed_wire.sent[0].pop("stream") is True  # 只多了 stream: true
    assert streamed_wire.sent == created_wire.sent
    assert "stream" not in created_wire.sent[0]  # 非串流（complete_once 的預設）沒有 stream
    assert "stream" not in state.kwargs  # D2：旗標不寫進 kwargs（錄製台快照不變）

    turn = ClaudeAdapter().parse_round(state, streamed)
    assert turn.reasoning_parts == ["先想想"]
    assert turn.text_parts == ["我來查"]
    assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [
        ("toolu_1", "vault_read", {"path": "a.md", "opts": {"n": 2}}),
        ("toolu_2", "vault_list", {}),  # 空 input
    ]
    assert [b.type for b in turn.raw_assistant] == [
        "thinking", "redacted_thinking", "text", "tool_use", "tool_use",
    ]
    # 完整的一輪由核心照常入帳；下一輪還沒開始就被取消時不能把這一輪再記一次
    assert ClaudeAdapter().partial_usage(state) is None


# ── 附註 7：完整訊號與串流中的錯誤 ──────────────────────────────────────────────


async def test_disconnect_without_message_stop_is_round_error():
    body = _full_stream()[:-1]  # 什麼都收到了，就是沒有 message_stop
    _state, result = await _one_round(ClaudeAdapter(stream=True), _Wire(body))
    assert result == RoundError("❌ **API 發生錯誤**\n\n`上游串流沒送完就中斷（缺 message_stop）`")


async def test_mid_stream_overloaded_event_is_round_error_with_sdk_text():
    body = [_START, _ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            _ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "半截"}),
            _ev("error", error={"type": "overloaded_error", "message": "Overloaded"})]
    _state, result = await _one_round(ClaudeAdapter(stream=True), _Wire(body))
    assert result == RoundError(
        "❌ **API 發生錯誤**\n\n"
        "`{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}`"
    )


@pytest.mark.parametrize("stream", [False, True], ids=["create", "stream"])
async def test_http_status_error_text_is_pinned(stream):
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    wire = _Wire(httpx2.Response(400, json=body))
    _state, result = await _one_round(ClaudeAdapter(stream=stream), wire)
    assert result == RoundError(
        "❌ **API 發生錯誤**\n\n`Error code: 400 - "
        "{'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'bad'}}`"
    )
    assert len(wire.sent) == 1


def _ctx(client: Any, rid: str) -> dict:
    return {
        "anthropic_client": client, "protocol_manager": MagicMock(), "tool_registry": {},
        "memory_tool_registry": {}, "plugin_registry": {}, "request_id": rid, "t0": 0.0,
        "agent_id": "Sumi", "identity": AgentIdentity.for_role("Sumi", "Owner"),
        "model": _MODEL,
    }


async def test_main_loop_sends_no_partial_text_downstream(monkeypatch):
    monkeypatch.setattr(react_core, "accumulate_token_usage", MagicMock())
    body = [_START, _ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            _ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "半截回答"})]
    wire = _Wire(body)
    client = wire.client()
    try:
        async with asyncio.timeout(_GUARD_S):
            chunks = [c async for c in handle_claude_request(_request(), _ctx(client, "req-u1"))]
    finally:
        await client.close()
    joined = "".join(chunks)
    assert wire.sent[0]["stream"] is True  # 主迴圈明確打開串流（handler）
    assert "半截回答" not in joined
    assert "缺 message_stop" in joined
    assert chunks[-1] == "data: [DONE]\n\n"


# ── D1／D6：預設非串流；規則表開關 ───────────────────────────────────────────


@pytest.mark.parametrize("model", [
    "claude-3-5-sonnet", "claude-opus-4-20250514", "claude-haiku-4-5", "claude-opus-4-8",
    "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-preview", "claude-unrecognized",
])
def test_claude_family_streams_by_default(model):
    assert claude_rules(model).stream is True


async def test_rules_switch_off_keeps_main_loop_on_create(monkeypatch):
    real = claude_rules
    monkeypatch.setattr(claude_adapter, "claude_rules", lambda m: replace(real(m), stream=False))
    monkeypatch.setattr(react_core, "accumulate_token_usage", MagicMock())
    created = await _one_round(ClaudeAdapter(), _Wire(httpx2.Response(200, json={
        **_FULL, "content": [{"type": "text", "text": "好"}], "stop_reason": "end_turn"})))
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=created[1])
    client.messages.stream = MagicMock(side_effect=AssertionError("stream must stay off"))
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in handle_claude_request(_request(), _ctx(client, "req-off"))]
    assert client.messages.create.await_count == 1
    assert "stream" not in client.messages.create.call_args.kwargs
    assert client.messages.stream.call_count == 0
    assert '"content": "好"' in "".join(chunks)


async def test_main_loop_without_streaming_caps_max_tokens_for_the_sdk(monkeypatch):
    """附註 4：主迴圈關掉串流時送出當下壓到 21,333；不壓的話 SDK（預設逾時）會丟例外。"""
    real = claude_rules
    monkeypatch.setattr(claude_adapter, "claude_rules", lambda m: replace(real(m), stream=False))
    reply = {**_FULL, "content": [{"type": "text", "text": "好"}], "stop_reason": "end_turn"}
    wire = _Wire(httpx2.Response(200, json=reply))
    request = OpenAIChatRequest(model=_MODEL, max_tokens=32768,
                                messages=[OpenAIMessage(role="user", content="hi")])
    state, result = await _one_round(ClaudeAdapter(stream=True), wire, request)
    assert not isinstance(result, RoundError)
    assert wire.sent[0]["max_tokens"] == 21333
    assert "stream" not in wire.sent[0]
    assert state.kwargs["max_tokens"] == 32768  # 不寫回 kwargs（錄製台快照不變）


def test_main_loop_client_times_stream_bytes():
    hooks = proxy_main.anthropic_client._client.event_hooks["response"]
    assert liveness_event_hooks()["response"][0] in hooks
    assert proxy_main.anthropic_client._client.follow_redirects is False
