"""上游錄製台自身的保證（model-p1a U1）。

- L：RecordingAdapter 換掉真的 adapter 後，run_react_loop 的下游 SSE 逐字相同。
- 快照在送出當下深拷貝：第 1 輪快照不會被之後幾輪的原地修改改掉。
- 三家快照正規化都能用（OpenAI、Gemini 各一個最小情境，供 U2／U3 沿用）；3b-U4a 加 Responses。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from characterization.harness import (
    UPSTREAM_S,
    drain,
    install_clock,
    make_clock,
    make_ctx,
    make_mock_executor,
)
from src.adapters.schemas import OpenAIChatRequest
from src.handlers.adapters.base import ProviderAdapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.react_core import run_react_loop

from .harness import (
    CORE,
    SNAPSHOTTERS,
    RecordingAdapter,
    claude_text,
    claude_tools,
    drive,
    gemini_text,
    gemini_tools,
    openai_text,
    openai_tools,
    reset_rhythm,
    shape,
    to_plain,
)

_TOOL = {"type": "function", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
}}


def _claude_request() -> OpenAIChatRequest:
    return OpenAIChatRequest(model="claude-4-5-sonnet", tools=[_TOOL], messages=[
        {"role": "system", "content": "妳是 Sumi。"},
        {"role": "user", "content": "前情"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "查一下"},
    ])


def _claude_responses() -> list:
    return [
        claude_tools(("vault_tools", {"action": "list"}), ("vault_tools", {"action": "read"})),
        claude_tools(("vault_tools", {"action": "stat"})),
        claude_text("兩件事都查好了。", thinking="先想一下"),
    ]


async def _run_with_real_adapter(monkeypatch, responses: list) -> list[str]:
    """對照組：原 ClaudeAdapter＋假 SDK client（同一假時鐘、同一 request_id）。"""
    clock = make_clock()
    install_clock(monkeypatch, CORE, clock)
    rid, sid = "req-up-L", "sess-up-L"
    reset_rhythm(rid, sid)
    queue = list(responses)

    async def create(**_kwargs):
        clock["now"] += UPSTREAM_S
        return queue.pop(0)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    ctx = make_ctx(rid, sid, anthropic_client=client)
    try:
        with patch(f"{CORE}.ToolExecutor") as executor:
            executor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            return await drain(run_react_loop(ClaudeAdapter(), _claude_request(), ctx))
    finally:
        reset_rhythm(rid, sid)


@pytest.mark.asyncio
async def test_recording_adapter_is_substitutable_for_claude(monkeypatch):
    """L：同一情境，原 adapter＋假 SDK client 與 RecordingAdapter 的下游 SSE 逐字相同。"""
    real = await _run_with_real_adapter(monkeypatch, _claude_responses())
    run = await drive(monkeypatch, ClaudeAdapter(), _claude_request(), _claude_responses(),
                      tag="L")
    assert run.chunks == real
    joined = "".join(real)  # 防空比較：確實走過兩個工具輪、思考頻道與 token 用量
    assert joined.count("[vault_tools done]") == 3
    assert '"reasoning_content": "先想一下"' in joined
    assert "context_breakdown" in joined
    assert len(run.snapshots) == 3


def test_recording_adapter_forwards_every_protocol_member():
    """Protocol 每個成員＋核心實際會叫的 resolve_provider_label 都由 RecordingAdapter 定義。"""
    members = {n for n in vars(ProviderAdapter) if not n.startswith("_")}
    members |= set(ProviderAdapter.__annotations__)
    members.add("resolve_provider_label")
    assert {"send_round", "emits_context_breakdown", "close"} <= members
    inner = ClaudeAdapter()
    recorder = RecordingAdapter(inner, [])
    not_forwarded = sorted(
        n for n in members - {"send_round"} if getattr(recorder, n) != getattr(inner, n)
    )
    assert not_forwarded == []
    assert recorder.send_round != inner.send_round  # 唯一被換掉的成員
    assert recorder.emits_context_breakdown is True


@pytest.mark.asyncio
async def test_first_snapshot_is_not_mutated_by_later_rounds(monkeypatch):
    """第 1 輪快照在後面幾輪跑完後不變（adapter 會原地刪掉舊斷點、追加歷史）。"""
    run = await drive(monkeypatch, ClaudeAdapter(), _claude_request(), _claude_responses(),
                      tag="snap")
    first = run.snapshots[0]["kwargs"]["messages"]
    assert first == [
        {"role": "user", "content": [{"type": "text", "text": "前情"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "好"}]},
        {"role": "user", "content": [
            {"type": "text", "text": "查一下", "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    assert shape(run.snapshots[2])["messages"] == [
        "user:text", "assistant:text", "user:text",
        "assistant:tool_use,tool_use", "user:tool_result,tool_result",
        "assistant:tool_use", "user:tool_result*",
    ]


def test_to_plain_rejects_magicmock():
    """SDK block 不可用 MagicMock：快照拒收，而不是存進一個會跟著變的參照。"""
    with pytest.raises(AssertionError, match="MagicMock"):
        to_plain({"content": [MagicMock()]})


@pytest.mark.asyncio
async def test_openai_snapshot_normalizer(monkeypatch):
    request = OpenAIChatRequest(model="grok-4", tools=[_TOOL],
                                messages=[{"role": "user", "content": "查一下"}])
    run = await drive(monkeypatch, OpenAIAdapter(), request,
                      [openai_tools(("vault_tools", {"action": "list"})), openai_text("好了")],
                      tag="oa", ctx_extra={"provider": "Grok", "url": "http://grok.test/v1"})
    assert [shape(s) for s in run.snapshots] == [  # KF-8a 已修正：內部欄位不外送（3b-U2）
        {"url": "http://grok.test/v1", "headers": ["Authorization", "Content-Type"],
         "keys": ["messages", "model", "tools"],
         "tool_choice": None, "tools": ["vault_tools"], "messages": ["user:str"]},
        {"url": "http://grok.test/v1", "headers": ["Authorization", "Content-Type"],
         "keys": ["messages", "model", "tools"],
         "tool_choice": None, "tools": ["vault_tools"],
         "messages": ["user:str", "assistant:none+tool_calls", "tool:str"]},
    ]
    assert run.snapshots[1]["payload"]["messages"][2] == {
        "role": "tool", "tool_call_id": "call_1", "name": "vault_tools", "content": "Tool Success",
    }


@pytest.mark.asyncio
async def test_gemini_snapshot_normalizer(monkeypatch):
    request = OpenAIChatRequest(model="gemini-2.5-flash", tools=[_TOOL],
                                messages=[{"role": "user", "content": "查一下"}])
    run = await drive(monkeypatch, GeminiAdapter(), request,
                      [gemini_tools(("vault_tools", {"action": "list"})), gemini_text("好了")],
                      tag="gm")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent")
    assert [shape(s) for s in run.snapshots] == [
        {"url": url, "keys": ["contents", "tools"], "tool_config": None,
         "tools": ["vault_tools"], "contents": ["user:text"]},
        {"url": url, "keys": ["contents", "tools"], "tool_config": None,
         "tools": ["vault_tools"],
         "contents": ["user:text", "model:functionCall", "user:functionResponse"]},
    ]
    assert run.snapshots[1]["payload"]["contents"][2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "vault_tools", "response": {"result": "Tool Success"}}},
    ]}


async def test_responses_snapshot_normalizer():
    """3b-U4a：base URL＋create 參數的純資料深拷貝；SDK client 不進快照。"""
    request = OpenAIChatRequest(model="gpt-6-astra", messages=[
        {"role": "system", "content": "妳是助理。"},
        {"role": "system", "content": "用繁體中文。"},
        {"role": "user", "content": "讀 a.md"}])
    state = await ResponsesAdapter().prepare_request(request,
                                                     {"base_url": "https://api.openai.test/v1"})
    snapshot = SNAPSHOTTERS[ResponsesAdapter](state)
    state.kwargs["input"].append({"role": "user", "content": "後來加的"})
    assert snapshot == {"wire": "responses", "base_url": "https://api.openai.test/v1", "kwargs": {
        "model": "gpt-6-astra", "input": [{"role": "user", "content": "讀 a.md"}],
        "store": False, "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "touka-6e340b9cffb37a989ca544e6bb780a2c",  # 沒有 agent／session
        "instructions": "妳是助理。\n用繁體中文。"}}
