"""3b-U1：沒完成那一輪的用量（3b 裁定 6、master R2；附註 28）。

取消、閒置逾時、半截串流：廠商回報的照記，沒回報的估（輸入用請求內容、輸出用已收到的片段）。
「估計」標記只進畫面事件（token_usage 的 ``estimated``）與日誌，用量紀錄檔照舊四欄（P20）。
估算只用在主迴圈串流路徑：complete_once 不呼叫，逾時與錯誤時帳本仍是空的。

估算值依 ``token_estimator``：非 CJK 每 3.5 字 1 token、CJK 每字 1 token，四捨五入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src.adapters.schemas import AnthropicMessage, AnthropicRequest, OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import claude_adapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks
from src.handlers.adapters.usage import PartialUsage, estimate_partial_usage
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import complete_once
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

_GUARD_S = 5.0
_SSE = {"content-type": "text/event-stream"}
_MODEL = "claude-opus-5-5"
_IDENTITY = AgentIdentity.for_role("Sumi", "Owner")


# ── 估算函式：三種情況（純函式） ────────────────────────────────────────────


def test_all_reported_is_recorded_as_is():
    partial = estimate_partial_usage(request={"a": "你好"}, output_text="abcdefg",
                                     reported_input=(100, 20, 5), reported_output=7)
    assert partial == PartialUsage(
        usage={"prompt_tokens": 100, "cache_read_input_tokens": 20,
               "cache_creation_input_tokens": 5, "completion_tokens": 7},
        total_input=125, estimated=False,
    )


def test_partly_reported_estimates_only_the_output():
    partial = estimate_partial_usage(request={"a": "你好"}, output_text="abcdefg",
                                     reported_input=(100, 20, 5), reported_output=None)
    assert partial == PartialUsage(
        usage={"prompt_tokens": 100, "cache_read_input_tokens": 20,
               "cache_creation_input_tokens": 5, "completion_tokens": 2},  # 7 字 ÷ 3.5
        total_input=125, estimated=True,
    )


def test_nothing_reported_estimates_input_from_the_request():
    partial = estimate_partial_usage(request={"a": "你好"}, output_text="你好嗎",
                                     reported_input=None, reported_output=None)
    # '{"a": "你好"}'：9 個非 CJK 字＋2 個 CJK → 2＋9÷3.5＝4.57 → 5；輸出 3 個 CJK → 3
    assert partial == PartialUsage(
        usage={"prompt_tokens": 5, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0, "completion_tokens": 3},
        total_input=5, estimated=True,
    )


# ── 主迴圈：沒完成的一輪照記，估計標記進畫面事件與日誌 ─────────────────────


def _ev(kind: str, **data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data}, ensure_ascii=False)}\n\n".encode()


_START = _ev("message_start", message={
    "id": "msg_p", "type": "message", "role": "assistant", "model": _MODEL, "content": [],
    "stop_reason": None, "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 20,
              "cache_creation_input_tokens": 5}})
_TEXT = [_ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
         _ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "abcdefg"})]


class _Upstream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, hold: bool = False) -> None:
        self._chunks = chunks
        self._hold = hold
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._hold:  # 停住不結束（取消測試用）
            self.waiting.set()
            await asyncio.Event().wait()


def _client(upstream: _Upstream) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="fake-key", base_url="https://api.anthropic.test", max_retries=0,
        http_client=DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(
                lambda _r: httpx2.Response(200, headers=_SSE, stream=upstream)),
            follow_redirects=False, event_hooks=liveness_event_hooks(),
        ),
    )


def _ctx(client: AsyncAnthropic, rid: str) -> dict:
    return {
        "anthropic_client": client, "protocol_manager": MagicMock(), "tool_registry": {},
        "memory_tool_registry": {}, "plugin_registry": {}, "request_id": rid, "t0": 0.0,
        "agent_id": "Sumi", "identity": _IDENTITY, "model": _MODEL,
    }


@pytest.fixture
def booked(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(react_core, "accumulate_token_usage",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


async def _main_loop(upstream: _Upstream, request: Any, rid: str) -> list[str]:
    client = _client(upstream)
    try:
        async with asyncio.timeout(_GUARD_S):
            return [c async for c in react_core.run_react_loop(
                ClaudeAdapter(stream=True), request, _ctx(client, rid))]
    finally:
        await client.close()


def _openai_request() -> OpenAIChatRequest:
    return OpenAIChatRequest(model=_MODEL, messages=[OpenAIMessage(role="user", content="hi")])


def _usage_events(chunks: list[str]) -> list[dict]:
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e for e in events if e.get("kind") == "token_usage"]


async def test_main_loop_records_fully_reported_unfinished_round(booked, caplog):
    body = [_START, *_TEXT, _ev("message_delta", delta={"stop_reason": None,
                                                        "stop_sequence": None},
                                usage={"output_tokens": 7})]  # 沒有 message_stop
    with caplog.at_level(logging.WARNING, logger=react_core.__name__):
        chunks = await _main_loop(_Upstream(body), _openai_request(), "req-p-all")
    assert booked == [((_IDENTITY, "Anthropic", {
        "prompt_tokens": 100, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5, "completion_tokens": 7}), {"model": _MODEL})]
    (event,) = _usage_events(chunks)
    assert event["total"] == 132
    assert "estimated" not in event
    assert "estimated=False" in caplog.text
    assert "abcdefg" not in "".join(chunks)  # 半截文字丟掉


async def test_main_loop_records_partly_reported_round_as_estimated(booked, caplog):
    with caplog.at_level(logging.WARNING, logger=react_core.__name__):
        chunks = await _main_loop(_Upstream([_START, *_TEXT]), _openai_request(), "req-p-part")
    assert booked == [((_IDENTITY, "Anthropic", {
        "prompt_tokens": 100, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5, "completion_tokens": 2}), {"model": _MODEL})]
    (event,) = _usage_events(chunks)
    assert (event["total"], event["estimated"]) == (127, True)
    assert "estimated=True" in caplog.text


async def test_main_loop_estimates_round_with_nothing_reported(booked, caplog):
    """HTTP 200 之後什麼事件都沒有就斷：輸入用請求估（原生入口，不送畫面事件，只進日誌）。"""
    request = AnthropicRequest(model=_MODEL, max_tokens=1024,
                               messages=[AnthropicMessage(role="user", content="hi")])
    with caplog.at_level(logging.WARNING, logger=react_core.__name__):
        chunks = await _main_loop(_Upstream([]), request, "req-p-none")
    # {"system": null, "tools": null, "messages": [{"role": "user", "content": "hi"}]}：80 字 → 23
    assert booked == [((_IDENTITY, "Anthropic", {
        "prompt_tokens": 23, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "completion_tokens": 0}), {"model": _MODEL})]
    assert _usage_events(chunks) == []
    assert "estimated=True" in caplog.text


async def test_cancelled_round_is_recorded_before_cancellation_propagates(booked):
    upstream = _Upstream([_START, *_TEXT], hold=True)
    client = _client(upstream)
    try:
        async with asyncio.timeout(_GUARD_S):
            loop = react_core.run_react_loop(
                ClaudeAdapter(stream=True), _openai_request(), _ctx(client, "req-p-cancel"))
            task = asyncio.create_task(anext(loop))
            await upstream.waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await client.close()
    assert booked == [((_IDENTITY, "Anthropic", {
        "prompt_tokens": 100, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5, "completion_tokens": 2}), {"model": _MODEL})]


async def test_round_that_never_opened_records_nothing(booked, monkeypatch):
    """串流還沒開始就失敗（HTTP 錯誤）：廠商沒收費依據，不記也不估（529 會重試，每次都不記）。"""
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())
    client = AsyncAnthropic(
        api_key="fake-key", base_url="https://api.anthropic.test", max_retries=0,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(
            lambda _r: httpx2.Response(529, json={"type": "error"})), follow_redirects=False),
    )
    try:
        async with asyncio.timeout(_GUARD_S):
            chunks = [c async for c in react_core.run_react_loop(
                ClaudeAdapter(stream=True), _openai_request(), _ctx(client, "req-p-529"))]
    finally:
        await client.close()
    assert booked == []
    assert _usage_events(chunks) == []


# ── complete_once 不估算，逾時與錯誤時帳本是空的（附註 28） ───────────────────


@pytest.fixture
def once(monkeypatch) -> dict:
    book: dict[str, list] = {"usage": [], "audit": [], "estimate": [], "sent": []}
    reply: dict[str, Any] = {}

    async def handle(request: httpx2.Request) -> httpx2.Response:
        book["sent"].append(json.loads(request.content))
        if reply.get("hang"):
            await asyncio.Event().wait()
        return httpx2.Response(reply["status"], json={"type": "error"})

    def http_client(**kwargs: Any) -> Any:
        return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(completion, "DefaultAsyncHttpxClient", http_client)
    monkeypatch.setattr(completion, "accumulate_token_usage",
                        lambda *a, **k: book["usage"].append((a, k)))
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: book["audit"].append(a))
    monkeypatch.setattr(completion.autonomy_service, "is_over_budget", lambda _i: False)
    monkeypatch.setattr(claude_adapter, "estimate_partial_usage",
                        lambda **kw: book["estimate"].append(kw))
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    book["reply"] = reply  # type: ignore[assignment]
    return book


@pytest.mark.parametrize(("reply", "reason"), [
    ({"status": 529}, "upstream"),
    ({"hang": True}, "timeout"),
], ids=["error", "timeout"])
async def test_complete_once_never_estimates_and_books_nothing(once, reply, reason):
    once["reply"].update(reply)
    async with asyncio.timeout(_GUARD_S):
        result = await complete_once(_IDENTITY, "reviewer", _MODEL, "s", "u",
                                     256, 0.3)
    assert (result.ok, result.error.reason) == (False, reason)
    assert len(once["sent"]) == 1
    assert "stream" not in once["sent"][0]
    assert (once["usage"], once["audit"], once["estimate"]) == ([], [], [])
