"""P5b 失效可見化 — 工具例外細節、OpenAI 網路錯誤、degraded 三家注入。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from src.handlers.gemini_handler import handle_gemini_request
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity
from src.services.tool_executor import ToolExecutor

from tests.helpers.stream_transport import (
    FakeUpstream,
    Reply,
    gemini_stream_reply,
    stream_reply,
)


def _decode_sse_text(chunks: list[str]) -> str:
    texts = []
    for c in chunks:
        payload = c[len("data: "):].strip() if c.startswith("data: ") else ""
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
        if delta:
            texts.append(delta)
    return "".join(texts)


def _openai_context(**over):
    ctx = {
        "api_key": "fake", "url": "http://fake", "provider": "OpenAI",
        "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
        "protocol_manager": MagicMock(), "tool_registry": {}, "memory_tool_registry": {},
        "plugin_registry": {}, "request_id": "req-fv", "t0": 0.0, "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": False, "session_id": "sess-fv", "model": "gpt-4",
    }
    ctx.update(over)
    return ctx


def _openai_request():
    r = MagicMock()
    r.model = "gpt-4"
    r.pulse_slots = None
    r.model_dump.return_value = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r.messages = [MagicMock(content="hi")]
    return r


@pytest.mark.asyncio
async def test_tool_executor_error_includes_detail(monkeypatch):
    """工具拋例外 → error 事件帶真正的錯誤類別＋訊息（非籠統 Execution failed）。"""
    async def boom_tool(**kwargs):
        raise ValueError("specific boom reason")

    monkeypatch.setattr(
        "src.services.tool_executor.permission_service.evaluate_policy",
        lambda ctx: type("D", (), {"effect": "ALLOW", "reason": "ok"})(),
    )
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id",
        AsyncMock(return_value=None),
    )
    te = ToolExecutor(None, {"boom": boom_tool}, {}, {})
    ctx = {
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "session_id": "s",
        "model": "m",
    }
    queue: asyncio.Queue = asyncio.Queue()
    await te.execute_to_queue("boom", {}, "req", ctx, queue, call_id="c1")

    errs = []
    while not queue.empty():
        ev = queue.get_nowait()
        if isinstance(ev, dict) and ev.get("type") == "error":
            errs.append(ev)
    assert errs
    assert "ValueError" in errs[0]["message"]
    assert "specific boom reason" in errs[0]["message"]


@pytest.mark.asyncio
async def test_openai_handler_network_error_visible(monkeypatch):
    """OpenAI 連線錯誤 → 明確報錯 ＋ [DONE]，不再靜默斷線（3b-U2：主迴圈走串流）。

    R-3b ② M1：還沒收到 200 就連不上算可重試，主迴圈共送 3 次（等待換成假的）。"""
    monkeypatch.setattr("src.handlers.react_core._retry_sleep", AsyncMock())
    upstream = FakeUpstream([Reply(error=httpx.ConnectError("connection refused"))] * 3)
    upstream.install(monkeypatch)
    chunks = []
    async for c in handle_openai_request(_openai_request(), _openai_context()):
        chunks.append(c)
        if len(chunks) > 20:
            break
    assert "[DONE]" in "".join(chunks)
    assert "連線錯誤" in _decode_sse_text(chunks)
    assert len(upstream.requests) == 3


@pytest.mark.asyncio
async def test_openai_handler_injects_degraded_warning(monkeypatch):
    upstream = FakeUpstream([stream_reply({
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }, "semantic")]).install(monkeypatch)
    ctx = _openai_context(system_degraded=True, degraded_reason="Memory offline. ")
    chunks = []
    async for c in handle_openai_request(_openai_request(), ctx):
        chunks.append(c)
        if len(chunks) > 30:
            break
    msgs = upstream.sent[0]["messages"]
    upstream.assert_all_closed()
    sys_texts = [m.get("content", "") for m in msgs if m.get("role") == "system"]
    assert any("DEGRADED MODE" in t for t in sys_texts)


@pytest.mark.asyncio
async def test_gemini_handler_injects_degraded_warning(monkeypatch):
    upstream = FakeUpstream([gemini_stream_reply({
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    }, "semantic")]).install(monkeypatch)
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.pulse_slots = None
    request.safety_settings = None  # 假傳輸會真的編 JSON（3b-U3）
    request.messages = []
    ctx = {
        "gemini_api_key": "fake", "protocol_manager": MagicMock(), "tool_registry": {},
        "memory_tool_registry": {}, "plugin_registry": {}, "request_id": "req-fv-g", "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": False, "session_id": "sess-fv-g", "model": "gemini-2.5-flash",
        "system_degraded": True, "degraded_reason": "Memory offline. ",
    }
    chunks = []
    async for c in handle_gemini_request(request, ctx):
        chunks.append(c)
        if len(chunks) > 30:
            break
    si_text = json.dumps(upstream.sent[0].get("system_instruction"), ensure_ascii=False)
    upstream.assert_all_closed()
    assert "DEGRADED MODE" in si_text
