"""model-p0 U1：取樣參數一律不送（三家 adapter × 各入口；攔截實際送出內容）。

Owner 決定 A：temperature／top_p／top_k 三家一律不送（用廠商預設）。所有主對話
入口都收斂到三個 adapter，故各入口組出的請求逐一走 adapter 的 prepare_request＋
send_round，攔截送出內容。Pydantic 欄位與舊設定照常載入、值被忽略（不遷移）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.sampling import SAMPLING_PARAM_NAMES
from src.middleware.layers import custom_model_layer, native_model_layer
from src.middleware.layers.custom_model_layer import CustomModelLayer
from src.middleware.layers.native_model_layer import NativeModelLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.routers.models import ModelSettingsUpdate
from src.services.dispatchers.utils import convert_to_openai
from src.services.soul_service import SoulService
from src.utils.async_db import SessionSnapshot

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply, stream_reply

_MSGS = [{"role": "user", "content": "hi"}]
_SAMPLING = {"temperature": 0.3, "top_p": 0.9, "top_k": 40}
_MODELS = {"claude": "claude-sonnet-4-6", "openai": "grok-4", "gemini": "gemini-2.5-flash"}


def _soul_service_stub() -> SoulService:
    svc = object.__new__(SoulService)  # 不污染 singleton；設定＝SoulConfig 預設 0.7/1.0/40
    svc.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    return svc


async def _no_chunks():
    for _ in ():
        yield ""


# ── 入口：各自組出帶取樣參數的請求 ─────────────────────────────────────────────


async def _explicit(family: str, _mp: pytest.MonkeyPatch) -> Any:
    return OpenAIChatRequest(model=_MODELS[family], messages=_MSGS, **_SAMPLING)


async def _soul_applied(family: str, _mp: pytest.MonkeyPatch) -> Any:
    req = OpenAIChatRequest(model=_MODELS[family], messages=_MSGS)
    _soul_service_stub().apply_soul_to_request(req, "Alice")
    return req


async def _native_anthropic(family: str, _mp: pytest.MonkeyPatch) -> Any:
    req = AnthropicRequest(model=_MODELS[family], max_tokens=100, messages=_MSGS, **_SAMPLING)
    # factory 同款：Claude 原生直通，其他家經 convert_to_openai
    return req if family == "claude" else convert_to_openai(req, False, "")


async def _heartbeat(family: str, mp: pytest.MonkeyPatch) -> Any:
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured["request"] = kwargs["request"]
        return MagicMock(body_iterator=_no_chunks())

    session = {"id": "s", "title": "t", "messages": list(_MSGS)}
    mp.setattr(
        heartbeat, "session_service",
        MagicMock(read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev"))),
    )
    mp.setattr(heartbeat, "dispatch_request", _dispatch)
    mp.setattr(heartbeat, "autonomy_service", MagicMock())
    mp.setattr(heartbeat, "rhythm_service", MagicMock())
    mp.setattr(heartbeat, "soul_service", _soul_service_stub())
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    await heartbeat._execute_single_pulse(
        "s", identity, _MODELS[family], PipelineContext(), 1, 0.0, ""
    )
    return captured["request"]


async def _native_layer(family: str, mp: pytest.MonkeyPatch) -> Any:
    meta = {"id": _MODELS[family], "owned_by": "openai", **_SAMPLING}
    mp.setattr(native_model_layer, "model_manager", MagicMock(get_models=lambda: [meta]))
    req = OpenAIChatRequest(model=_MODELS[family], messages=_MSGS)
    await NativeModelLayer().process(req, PipelineContext())
    return req


async def _custom_layer(family: str, mp: pytest.MonkeyPatch) -> Any:
    service = MagicMock(get_model_data=MagicMock(return_value=("", dict(_SAMPLING))))
    mp.setattr(custom_model_layer, "custom_model_service", service)
    mp.setattr(custom_model_layer, "prompt_assembler",
               MagicMock(assemble_capabilities=MagicMock(return_value="")))
    req = OpenAIChatRequest(model=_MODELS[family], messages=_MSGS)
    await CustomModelLayer().process(req, PipelineContext())
    return req


_ENTRIES = {
    "explicit": _explicit,
    "soul": _soul_applied,
    "native_anthropic": _native_anthropic,
    "heartbeat": _heartbeat,
    "native_layer": _native_layer,
    "custom_layer": _custom_layer,
}


# ── 三家 adapter：攔截送出內容 ───────────────────────────────────────────────


async def _claude_wire(req: Any) -> dict:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=MagicMock())
    delegate_stream_to_create(client)
    adapter = ClaudeAdapter(stream=True)  # 主迴圈路徑（3b-U1：messages.stream()）
    state = await adapter.prepare_request(req, {"anthropic_client": client})
    async for _ in adapter.send_round(state):
        pass
    return client.messages.create.call_args.kwargs


async def _openai_wire(req: Any) -> dict:
    """主迴圈路徑（3b-U2：已知廠商走自寫 SSE 串流）；送出內容從 request.content 解析。"""
    ctx = {"url": "http://wire.test", "provider": "Grok", "api_key": "k",
           "custom_connection": False}  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
    reply = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1}}
    upstream = FakeUpstream([stream_reply(reply, "one")])
    with pytest.MonkeyPatch.context() as monkeypatch:
        upstream.install(monkeypatch)
        adapter = OpenAIAdapter(stream=True)
        state = await adapter.prepare_request(req, ctx)
        async for _ in adapter.send_round(state):
            pass
        await adapter.close(state)
    (sent,) = upstream.sent
    assert sent["stream"] is True
    return sent


async def _gemini_wire(req: Any) -> dict:
    """主迴圈路徑（3b-U3：自寫 SSE 串流）；送出內容從 request.content 解析。"""
    reply = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    upstream = FakeUpstream([gemini_stream_reply(reply, "one")])
    with pytest.MonkeyPatch.context() as monkeypatch:
        upstream.install(monkeypatch)
        adapter = GeminiAdapter(stream=True)
        state = await adapter.prepare_request(req, {"gemini_api_key": "k", "agent_id": "Alice"})
        async for _ in adapter.send_round(state):
            pass
        await adapter.close(state)
    (request,) = upstream.requests
    assert str(request.url).endswith(":streamGenerateContent?alt=sse")
    return upstream.sent[0]


_WIRES = {"claude": _claude_wire, "openai": _openai_wire, "gemini": _gemini_wire}


def test_sampling_param_names_ssot():
    assert frozenset({"temperature", "top_p", "top_k"}) == SAMPLING_PARAM_NAMES


@pytest.mark.parametrize("family", list(_WIRES))
@pytest.mark.parametrize("entry", list(_ENTRIES))
async def test_no_sampling_params_on_wire(entry, family, monkeypatch):
    req = await _ENTRIES[entry](family, monkeypatch)
    assert req.top_k == 40  # 前提：入口確實帶了取樣參數
    wire = await _WIRES[family](req)
    assert not SAMPLING_PARAM_NAMES & set(wire)
    if family == "gemini":  # R-3b ③ L2：寫死。generationConfig 只放輸出上限、不放取樣參數；
        # 只有原生 Anthropic 入口帶 max_tokens=100，經 convert_to_openai 帶過去（O20）
        expected = {"maxOutputTokens": 100} if entry == "native_anthropic" else None
        assert wire.get("generationConfig") == expected


def test_legacy_sampling_settings_still_load():
    """舊靈魂設定與模型設定照常載入（欄位保留、值被忽略、不遷移）。"""
    legacy = {"temperature": 0.7, "top_p": 1.0, "top_k": 40}
    soul = SoulConfig.model_validate({"base_model": "claude-opus-4-8", **legacy})
    assert (soul.temperature, soul.top_p, soul.top_k) == (0.7, 1.0, 40)
    assert ModelSettingsUpdate.model_validate(legacy).top_k == 40
