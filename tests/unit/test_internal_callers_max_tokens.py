"""O20 不波及內部呼叫端：子代理、評分卡送出的輸出上限和這批修改前一樣（ADR-0254）.

O20 讓 ``convert_to_openai`` 轉送「呼叫端明確給的」``max_tokens``，給的是 IDE／Chat 選
anthropic 協定時的 Anthropic 形狀入口。程式內部也有兩處在 ``AnthropicRequest`` 上寫了上限，
但這批修改前只有 Claude 收得到（轉成其他家時一律丟掉）：

- 子代理（``dispatch_subagent``）：Claude 8192；Gemini、OpenAI 相容不帶上限。
- 評分卡：Claude 用情境的值（512／1024）；其他家不帶上限（推理模型被截斷會誤判失敗）。

路由照 factory：Claude 原生直通，其他家先經 ``convert_to_openai``。攔截各家 adapter 實際送出
的內容（假上游，不打 API）。評分卡的模型可能是靈魂，要到 SoulLayer 才知道送哪一家，所以上限
的處理不能依呼叫當下的模型名判斷家族。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import AnthropicRequest
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.identity import AgentIdentity
from src.services.dispatchers.utils import convert_to_openai
from src.services.scorecard import runner as scorecard_runner
from src.services.scorecard.models import Scenario
from src.tools import subagent_tools

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply, stream_reply

_MODELS = {"claude": "claude-sonnet-4-6", "openai": "grok-4", "gemini": "gemini-2.5-flash"}
_SCENARIO_CAP = 1024
_DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


# ── 三家 adapter：攔截實際送出內容（路由照 factory） ─────────────────────────────


async def _claude_wire(req: AnthropicRequest) -> dict:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=MagicMock())
    delegate_stream_to_create(client)
    adapter = ClaudeAdapter(stream=True)
    state = await adapter.prepare_request(req, {"anthropic_client": client})
    async for _ in adapter.send_round(state):
        pass
    return client.messages.create.call_args.kwargs


async def _openai_wire(req: AnthropicRequest) -> dict:
    ctx = {"url": "http://wire.test", "provider": "Grok", "api_key": "k",
           "custom_connection": False}
    reply = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1}}
    upstream = FakeUpstream([stream_reply(reply, "one")])
    with pytest.MonkeyPatch.context() as monkeypatch:
        upstream.install(monkeypatch)
        adapter = OpenAIAdapter(stream=True)
        state = await adapter.prepare_request(convert_to_openai(req, False, ""), ctx)
        async for _ in adapter.send_round(state):
            pass
        await adapter.close(state)
    (sent,) = upstream.sent
    return sent


async def _gemini_wire(req: AnthropicRequest) -> dict:
    reply = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    upstream = FakeUpstream([gemini_stream_reply(reply, "one")])
    with pytest.MonkeyPatch.context() as monkeypatch:
        upstream.install(monkeypatch)
        adapter = GeminiAdapter(stream=True)
        state = await adapter.prepare_request(
            convert_to_openai(req, False, ""), {"gemini_api_key": "k", "agent_id": "Alice"}
        )
        async for _ in adapter.send_round(state):
            pass
        await adapter.close(state)
    return upstream.sent[0]


_WIRES = {"claude": _claude_wire, "openai": _openai_wire, "gemini": _gemini_wire}


def _output_cap(family: str, wire: dict) -> int | None:
    """這一家送出的輸出上限；沒帶回 None。"""
    if family == "gemini":
        return (wire.get("generationConfig") or {}).get("maxOutputTokens")
    return wire.get("max_tokens")


# ── 兩個內部呼叫端組出的請求 ─────────────────────────────────────────────────


class _NoopPipeline:
    async def execute(self, request: Any, ctx: Any) -> None:
        return None


async def _subagent_request(family: str, monkeypatch: pytest.MonkeyPatch) -> AnthropicRequest:
    monkeypatch.setattr("src.services.proxy_service.pipeline", _NoopPipeline())
    return await subagent_tools._assemble_subagent_request(
        goal="測試輸出上限",
        sub_identity=AgentIdentity.for_role(agent_id="__subagent_cap__", role="Subagent"),
        base_ctx={},
        model=_MODELS[family],
    )


async def _scorecard_request(family: str, monkeypatch: pytest.MonkeyPatch) -> AnthropicRequest:
    captured: dict[str, Any] = {}

    async def _process(request: Any, _ctx: Any, is_anthropic: bool = True) -> Any:
        captured["request"] = request
        return SimpleNamespace(body=b"")

    monkeypatch.setattr(scorecard_runner, "process_proxy_request", _process)
    monkeypatch.setattr(scorecard_runner.cost_meter, "measure", lambda _identity: (0.0, 0))
    scenario = Scenario(id="cap", surface="chat", prompt="hi", max_tokens=_SCENARIO_CAP)
    await scorecard_runner._drive_scenario(_MODELS[family], scenario, {}, _DEBUGGER)
    return captured["request"]


# ── 子代理：Claude 8192；其他家不帶上限 ─────────────────────────────────────────


async def test_subagent_claude_still_sends_8192(monkeypatch):
    req = await _subagent_request("claude", monkeypatch)
    assert _output_cap("claude", await _claude_wire(req)) == 8192


@pytest.mark.parametrize("family", ["gemini", "openai"])
async def test_subagent_non_claude_sends_no_output_cap(family, monkeypatch):
    req = await _subagent_request(family, monkeypatch)
    wire = await _WIRES[family](req)
    assert _output_cap(family, wire) is None
    if family == "gemini":
        assert "generationConfig" not in wire  # 沒有其他東西時連整個欄位都不送（和修改前一樣）


# ── 評分卡：Claude 用情境的值；其他家不帶上限 ─────────────────────────────────


async def test_scorecard_claude_still_sends_the_scenario_cap(monkeypatch):
    req = await _scorecard_request("claude", monkeypatch)
    assert _output_cap("claude", await _claude_wire(req)) == _SCENARIO_CAP


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5"])
async def test_scorecard_claude_5x_gets_the_default_on_floor(monkeypatch, model):
    """r55：情境上限（不標 fields_set）碰到 5.x 一樣補到 8,192，免得思考吃光額度而判失敗。"""
    monkeypatch.setitem(_MODELS, "claude", model)
    req = await _scorecard_request("claude", monkeypatch)
    assert "max_tokens" not in req.model_fields_set
    assert _output_cap("claude", await _claude_wire(req)) == 8192


@pytest.mark.parametrize("family", ["gemini", "openai"])
async def test_scorecard_non_claude_sends_no_output_cap(family, monkeypatch):
    req = await _scorecard_request(family, monkeypatch)
    wire = await _WIRES[family](req)
    assert _output_cap(family, wire) is None
    if family == "gemini":
        assert "generationConfig" not in wire


async def test_scorecard_cap_does_not_depend_on_the_model_name(monkeypatch):
    """靈魂名稱（SoulLayer 之後才換成主要模型）也一樣：值照設、不算呼叫端明確給。"""
    captured: dict[str, Any] = {}

    async def _process(request: Any, _ctx: Any, is_anthropic: bool = True) -> Any:
        captured["request"] = request
        return SimpleNamespace(body=b"")

    monkeypatch.setattr(scorecard_runner, "process_proxy_request", _process)
    monkeypatch.setattr(scorecard_runner.cost_meter, "measure", lambda _identity: (0.0, 0))
    scenario = Scenario(id="cap", surface="chat", prompt="hi", max_tokens=_SCENARIO_CAP)
    await scorecard_runner._drive_scenario("Alice", scenario, {}, _DEBUGGER)
    req = captured["request"]
    assert req.max_tokens == _SCENARIO_CAP
    assert "max_tokens" not in req.model_fields_set


# ── 對照：IDE／Chat 的 Anthropic 入口照 O20 帶上限（已核准行為，不動） ─────────────


@pytest.mark.parametrize("family", ["gemini", "openai"])
async def test_explicit_caller_cap_still_reaches_non_claude(family):
    req = AnthropicRequest(model=_MODELS[family], max_tokens=100,
                           messages=[{"role": "user", "content": "hi"}])
    assert _output_cap(family, await _WIRES[family](req)) == 100
