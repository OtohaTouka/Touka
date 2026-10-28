"""3b-U2：相容 Chat 的用量（裁定 5／6、附註 31）與規則表欄位接上（附註 11）。

- 自訂連線（路由層判定）維持非串流：請求沒有 ``stream``／``stream_options``，用量照實記、不是 0。
- 已知廠商串流送 ``stream_options.include_usage``；最後沒有用量時照裁定 6 估算、帶估計標記
  （畫面事件 ``estimated`` 與日誌），不靜默不入帳。
- 沒完成的一輪（半截、取消、只收到用量沒收到 ``[DONE]``）照 U1b 規則記；串流沒開始不記。
- 規則表：gpt-6-sol／luna 帶工具送 ``reasoning_effort='none'``；``forbidden_params``、
  ``default_max_output``、``forced_tool_choice`` 接上相容 Chat。

估算值：請求 ``{"model": "grok-4", "messages": [{"role": "user", "content": "hi"}]}`` 的 JSON
是 68 個非 CJK 字 → 68÷3.5＝19.4 → 19；輸出「好」1 個 CJK → 1；「abcdefg」7÷3.5 → 2。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import openai_adapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.identity import AgentIdentity
from src.services.guard_service import guard_service
from src.services.models.model_rules import COMPAT_STREAM_PROVIDERS, ModelRules, rules_for

from tests.helpers.stream_transport import FakeUpstream, Reply, json_reply, sse

_GUARD_S = 5.0
_IDENTITY = AgentIdentity.for_role("Sumi", "Owner")
_ZERO = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def _c(delta: dict, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


_USAGE = {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 4,
                                    "prompt_tokens_details": {"cached_tokens": 10}}}


def _request(model: str = "grok-4", **extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")],
                             **extra)


def _ctx(rid: str, **extra: Any) -> dict:
    return {"provider": "Grok", "url": "https://api.x.test/v1/chat/completions",
            "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
            "api_key": "fake-key", "protocol_manager": MagicMock(), "tool_registry": {},
            "memory_tool_registry": {}, "plugin_registry": {}, "request_id": rid, "t0": 0.0,
            "agent_id": "Sumi", "identity": _IDENTITY, **extra}


@pytest.fixture
def booked(monkeypatch) -> list:
    calls: list = []
    def book(_identity: Any, _provider: Any, usage: dict, *_rest: Any, **_kw: Any) -> None:
        if usage:  # 空用量不算
            calls.append(usage)

    monkeypatch.setattr(react_core, "accumulate_token_usage", book)
    return calls


async def _loop(monkeypatch, replies: list[Reply], rid: str,
                **ctx: Any) -> tuple[list, FakeUpstream]:
    upstream = FakeUpstream(replies).install(monkeypatch)
    async with asyncio.timeout(_GUARD_S):
        chunks = [c async for c in react_core.run_react_loop(
            OpenAIAdapter(stream=True), _request(), _ctx(rid, **ctx))]
    return chunks, upstream


def _usage_events(chunks: list[str]) -> list[dict]:
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e for e in events if e.get("kind") == "token_usage"]


# ── 裁定 5：自訂連線非串流、已知廠商串流 ──────────────────────────────────


async def test_custom_connection_posts_without_stream_fields_and_books_usage(monkeypatch, booked):
    body = {"choices": [{"message": {"content": "好"}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 4}}
    chunks, upstream = await _loop(monkeypatch, [json_reply(body)], "req-u-custom",
                                   custom_connection=True)
    (sent,) = upstream.sent
    assert "stream" not in sent and "stream_options" not in sent
    assert booked == [{"prompt_tokens": 30, **_ZERO, "completion_tokens": 4}]
    assert "estimated" not in _usage_events(chunks)[0]


async def test_known_vendor_streams_with_usage_and_books_it(monkeypatch, booked):
    chunks, upstream = await _loop(
        monkeypatch, [Reply(pieces=[sse(_c({"content": "好"}, "stop"), _USAGE)])], "req-u-known")
    assert upstream.sent[0]["stream_options"] == {"include_usage": True}
    assert booked == [{"prompt_tokens": 20, "cache_read_input_tokens": 10,
                       "cache_creation_input_tokens": 0, "completion_tokens": 4}]
    (event,) = _usage_events(chunks)
    assert (event["total"], "estimated" in event) == (34, False)


async def test_known_vendor_without_final_usage_books_an_estimate(monkeypatch, booked, caplog):
    with caplog.at_level(logging.WARNING, logger=react_core.__name__):
        chunks, _ = await _loop(
            monkeypatch, [Reply(pieces=[sse(_c({"content": "好"}, "stop"))])], "req-u-est")
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 1}]
    (event,) = _usage_events(chunks)
    assert (event["total"], event["estimated"]) == (20, True)
    assert "estimated=True" in caplog.text


# ── 沒完成的一輪（U1b 規則） ────────────────────────────────────────────────


async def test_usage_received_but_no_done_books_reported_usage(monkeypatch, booked):
    chunks, _ = await _loop(monkeypatch, [Reply(pieces=[sse(_c({"content": "好"}), _USAGE,
                                                            done=False)])], "req-u-nodone")
    assert booked == [{"prompt_tokens": 20, "cache_read_input_tokens": 10,
                       "cache_creation_input_tokens": 0, "completion_tokens": 4}]
    assert "estimated" not in _usage_events(chunks)[0]


async def test_half_stream_books_an_estimate(monkeypatch, booked):
    chunks, _ = await _loop(monkeypatch, [Reply(pieces=[sse(_c({"content": "abcdefg"}),
                                                            done=False)])], "req-u-half")
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}]
    assert _usage_events(chunks)[0]["estimated"] is True


async def test_cancelled_stream_books_an_estimate(monkeypatch, booked):
    hold = asyncio.Event()
    upstream = FakeUpstream([Reply(pieces=[sse(_c({"content": "abcdefg"}), done=False), hold])])
    upstream.install(monkeypatch)
    loop = react_core.run_react_loop(OpenAIAdapter(stream=True), _request(), _ctx("req-u-cancel"))
    async with asyncio.timeout(_GUARD_S):
        task = asyncio.create_task(anext(loop))
        while not upstream.responses:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert booked == [{"prompt_tokens": 19, **_ZERO, "completion_tokens": 2}]


async def test_stream_that_never_opened_books_nothing(monkeypatch, booked):
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())  # 503 會重試（3b-U5a）
    chunks, _ = await _loop(monkeypatch, [json_reply("busy", 503)] * 3, "req-u-503")
    assert booked == []
    assert _usage_events(chunks) == []


# ── 規則表欄位接上（附註 11） ──────────────────────────────────────────────

_TOOL = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}


@pytest.mark.parametrize(("model", "tools", "effort"), [
    ("gpt-6-sol", [_TOOL], "none"),
    ("GPT-6-Luna", [_TOOL], "none"),
    ("gpt-6-sol", None, None),  # 不帶工具不送
    ("gpt-5", [_TOOL], None),  # 規則表沒列
])
async def test_required_params_with_tools(model, tools, effort):
    state = await OpenAIAdapter().prepare_request(_request(model, tools=tools), _ctx("r"))
    assert state.payload.get("reasoning_effort") == effort


async def test_forbidden_params_and_default_max_output(monkeypatch):
    rules = ModelRules(forbidden_params=frozenset({"stop"}), default_max_output=777, stream=True)
    monkeypatch.setattr(openai_adapter, "rules_for", lambda _m: rules)
    adapter = OpenAIAdapter()
    state = await adapter.prepare_request(_request(stop=["END"]), _ctx("r"))
    assert ("stop" in state.payload, state.payload["max_tokens"]) == (False, 777)
    state = await adapter.prepare_request(_request(max_tokens=5), _ctx("r"))
    assert state.payload["max_tokens"] == 5  # 請求有給就照請求


@pytest.mark.parametrize(("forced", "tool_choice"), [(True, "required"), (False, None)])
async def test_honesty_guard_honours_forced_tool_choice(monkeypatch, forced, tool_choice):
    rules = ModelRules(forced_tool_choice=forced, stream=True)
    monkeypatch.setattr(openai_adapter, "rules_for", lambda _m: rules)
    adapter = OpenAIAdapter()
    state = await adapter.prepare_request(_request(), _ctx("r"))
    turn = adapter.parse_round(state, {"choices": [{"message": {
        "role": "assistant", "content": "根據過去的記憶系統內容，你上次說喜歡紅茶。"}}]})
    assert adapter.mid_loop_hook(state, turn, set(), True) is not None  # 仍注入矯正提示
    assert state.payload.get("tool_choice") == tool_choice
    assert state.honesty_forced is forced
    assert guard_service.get_honesty_correction_prompt() == state.payload["messages"][-1]["content"]


@pytest.mark.parametrize("model", ["gpt-5", "grok-4", "deepseek-chat", "gpt-6-sol", "gpt-6-astra",
                                   "llama-3.3-70b"])
def test_compat_family_streams_by_default(model):
    """D6：相容 Chat 家族的規則表 ``stream`` 預設開；實際串流另限已知廠商（裁定 5）。"""
    assert rules_for(model).stream is True
    assert frozenset({"OpenAI", "DeepSeek", "Grok"}) == COMPAT_STREAM_PROVIDERS


@pytest.mark.parametrize(("flag", "streams"), [({}, False), ({"custom_connection": None}, False),
                                               ({"custom_connection": False}, True)])
async def test_missing_custom_connection_flag_means_custom(flag, streams):
    """R-3b ① L4：context 沒明說「不是自訂連線」就不串流（fail-closed）。"""
    request = OpenAIChatRequest(model="grok-4", messages=[OpenAIMessage(role="user", content="hi")])
    ctx = {"provider": "Grok", "url": "https://api.x.test/v1/chat/completions", **flag}
    adapter = OpenAIAdapter(stream=True)
    state = await adapter.prepare_request(request, ctx)
    await adapter.close(state)
    assert state.stream is streams
