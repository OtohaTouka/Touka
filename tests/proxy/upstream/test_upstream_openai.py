"""OpenAI 相容上游請求契約（model-p1a U2）。

凍結 OpenAIAdapter 經 run_react_loop 每一輪交給傳輸層的 payload／標頭／URL：內部欄位不外送
（KF-8a）、MCP 工具非標準鍵拿掉（KF-8b）、空工具陣列不送（KF-8c；KF-8 於 3b-U2 在 adapter 出口
修正，ADR-0248）、金鑰標頭、逐輪 tool_choice 與歷史形狀、Anthropic 形狀入口（KF-9）。
期望值全部寫死。

- Grok 保留 safety 參數、其他 provider 剝除：既有
  ``tests/unit/test_adapter_contract.py::test_prepare_filters_safety_params_for_non_grok``，不重寫。
- 401 提示與非 200 處理在 send_round 內（``_post_once``），錄製台碰不到：直接呼叫
  ``_post_once`` 並傳入假的 ``state.client``（不 patch httpx）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.openai_adapter import OpenAIAdapter, _State
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.dispatchers.utils import convert_to_openai
from src.services.models.manager import ModelManager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
)

from .harness import drive, openai_text, openai_tools, shape

_TOOL = {"type": "function", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
}}
_MEMORY = {"type": "function", "function": {
    "name": "memory_tools", "description": "記憶",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
}}
_MCP = {  # ToolLayer 從 protocol_manager 拿到的形狀（server／tool／category 非標準）
    "server": "notes", "tool": "search", "category": "MCP", "type": "function",
    "function": {"name": "notes__search", "description": "搜尋", "parameters": {"type": "object"}},
}
_URL = "https://api.openai.test/v1/chat/completions"
_OK = "Tool Success"  # characterization 假執行器的固定回傳
_ALWAYS = ["deep_research", "incognito", "mcp_dispatcher"]  # KF-8a：修正前預設值也外送
_CORRECTION = (
    "Warning: You mentioned memories or file contents without actually using tools. "
    "Verify first — query memory with `memory_tools` (action=\"query\") "
    "or read files with `read`/`grep` — then answer honestly."
)
_WRAP_UP = (
    "（系統）本次行動的工具輪額度已用盡，最後一輪的工具調用未執行。"
    "請根據目前已取得的結果直接總結收尾，不要再嘗試調用工具。"
)
_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", is_human=True,
                                   capabilities=[])


def _ui(tools: list | None = None, **extra) -> OpenAIChatRequest:
    history = [{"role": "system", "content": "妳是 Sumi。"}, {"role": "user", "content": "查一下"}]
    return OpenAIChatRequest(model="gpt-5", messages=history,
                             tools=[_TOOL, _MEMORY] if tools is None else tools, **extra)


async def _run(monkeypatch, request, responses, tag, **ctx_extra):
    ctx = {"provider": "OpenAI", "url": _URL, **ctx_extra}
    return await drive(monkeypatch, OpenAIAdapter(), request, responses,
                       tag=f"openai-{tag}", ctx_extra=ctx)


@pytest.mark.parametrize("provider", ["OpenAI", "Grok", "DeepSeek"])
@pytest.mark.asyncio
async def test_kf8a_default_internal_fields_are_not_sent(monkeypatch, provider):
    """KF-8 已修正（ADR-0248，3b-U2）：原 ``test_kf8a_default_internal_fields_are_sent``。

    KF-8a：什麼都沒設時，incognito／deep_research／mcp_dispatcher 不外送；temperature 預設
    1.0 與 stream 也不在 payload。三家 provider 相同。
    """
    run = await _run(monkeypatch, _ui(), [openai_text("好")], "kf8a", provider=provider)
    payload = run.snapshots[0]["payload"]
    assert sorted(payload) == ["messages", "model", "tools"]
    assert not set(_ALWAYS) & set(payload)


@pytest.mark.asyncio
async def test_kf8a_set_internal_fields_are_not_sent(monkeypatch):
    """KF-8 已修正（ADR-0248，3b-U2）：原 ``test_kf8a_set_internal_fields_are_sent``。

    KF-8a：incognito／deep_research／mcp_dispatcher／session_id／project_id／auth_level／
    ide_context 有值也不外送；thinking／stop／max_tokens 照送；取樣參數、stream 與非 Grok 的
    safety 參數剝除。
    """
    request = _ui(
        temperature=0.3, top_p=0.9, top_k=40, stream=True, max_tokens=123, stop=["END"],
        incognito=True, deep_research=True, mcp_dispatcher=True, session_id="s1",
        project_id="p1", auth_level="SafeAuto", ide_context={"file": "a.py"},
        thinking={"type": "enabled", "budget_tokens": 2048},
        safety_mode="off", safety_settings=[{"category": "x", "threshold": "y"}],
    )
    run = await _run(monkeypatch, request, [openai_text("好")], "kf8a-set")
    payload = run.snapshots[0]["payload"]
    assert {k: v for k, v in payload.items() if k not in ("messages", "tools")} == {
        "model": "gpt-5", "max_tokens": 123, "stop": ["END"],
        "thinking": {"type": "enabled", "budget_tokens": 2048},
    }


_MCP_ON_WIRE = {"type": "function", "function": {
    "name": "notes__search", "description": "搜尋", "parameters": {"type": "object"}}}


@pytest.mark.parametrize(("tools", "sent"), [
    pytest.param([_TOOL, _MCP], [_TOOL, _MCP_ON_WIRE], id="kf8b-mcp-keys"),
    pytest.param([], None, id="kf8c-empty"),
])
@pytest.mark.asyncio
async def test_kf8_tools_are_cleaned(monkeypatch, tools, sent):
    """KF-8 已修正（ADR-0248，3b-U2）：原 ``test_kf8_tools_are_sent_verbatim``。

    KF-8b：MCP 工具的 server／tool／category 拿掉，其餘原樣；KF-8c：工具全被過濾時不送 tools。
    """
    run = await _run(monkeypatch, _ui(tools=tools), [openai_text("好")], "kf8-tools")
    payload = run.snapshots[0]["payload"]
    assert payload.get("tools") == sent
    assert ("tools" in payload) is (sent is not None)
    assert "tool_choice" not in payload
    assert _MCP["server"] == "notes"  # 呼叫端（ToolLayer）的工具物件沒被改到


@pytest.mark.parametrize(("api_key", "headers"), [
    ("fake-key", {"Content-Type": "application/json", "Authorization": "Bearer fake-key"}),
    (None, {"Content-Type": "application/json"}),
    ("", {"Content-Type": "application/json"}),  # 自訂連線的空金鑰也不帶 Authorization
])
@pytest.mark.asyncio
async def test_headers_and_url(monkeypatch, api_key, headers):
    """有金鑰帶 Bearer、沒金鑰不帶 Authorization；URL 就是 ctx 給的完整端點。"""
    url = "http://localhost:11434/v1/chat/completions"
    run = await _run(monkeypatch, _ui(), [openai_text("好")], "hdr", api_key=api_key, url=url)
    assert (run.snapshots[0]["url"], run.snapshots[0]["headers"]) == (url, headers)


@pytest.mark.parametrize(("provider", "api_key", "message"), [
    ("OpenAI", None,
     "OpenAI Access Denied (401)\n\nMissing `OPENAI_API_KEY` in `.env`. Raw error:\n`bad key`"),
    ("OpenAI", "fake-key", "OpenAI API Error (401)\n\n`bad key`"),
    ("Grok", None, "Grok API Error (401)\n\n`bad key`"),
])
@pytest.mark.asyncio
async def test_post_once_401_hint_only_for_openai_without_key(provider, api_key, message):
    """provider 分岔：只有 OpenAI 且沒金鑰時，401 改成缺 OPENAI_API_KEY 的提示。"""
    client = SimpleNamespace(post=AsyncMock(
        return_value=SimpleNamespace(status_code=401, text="bad key")))
    state = _State(payload={"model": "gpt-5"}, headers={"Content-Type": "application/json"},
                   url=_URL, provider=provider, api_key=api_key, client=client)
    assert await OpenAIAdapter()._post_once(state) == RoundError(message)
    client.post.assert_awaited_once_with(
        _URL, headers={"Content-Type": "application/json"}, json={"model": "gpt-5"},
        timeout=120.0,
    )


@pytest.mark.asyncio
async def test_tool_round_writes_back_tool_messages_without_reasoning(monkeypatch):
    """工具輪：assistant 原形寫回、reasoning_content 不回送；每個呼叫一則 role:"tool"。"""
    first = openai_tools(("vault_tools", {"action": "list"}), ("memory_tools", {"query": "紅茶"}))
    first["choices"][0]["message"]["reasoning_content"] = "先想一下"
    run = await _run(monkeypatch, _ui(), [first, openai_text("查好了。")], "tools",
                     provider="DeepSeek")
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_choice"] for s in shapes] == [None, None]
    assert [s["messages"] for s in shapes] == [
        ["system:str", "user:str"],
        ["system:str", "user:str", "assistant:none+tool_calls", "tool:str", "tool:str"],
    ]
    assert run.snapshots[1]["payload"]["messages"][2:] == [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "vault_tools", "arguments": '{"action": "list"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "memory_tools", "arguments": '{"query": "紅茶"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "vault_tools", "content": _OK},
        {"role": "tool", "tool_call_id": "call_2", "name": "memory_tools", "content": _OK},
    ]


@pytest.mark.asyncio
async def test_honesty_correction_requires_then_releases_tool_choice(monkeypatch):
    """誠實矯正：矯正輪 "required"，下一個工具輪後改回 "auto"。"""
    run = await _run(monkeypatch, _ui(), [
        openai_text("根據過去的記憶系統，你喜歡紅茶。"),
        openai_tools(("memory_tools", {"query": "紅茶"})),
        openai_text("查到了。"),
    ], "honesty")
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_choice"] for s in shapes] == [None, "required", "auto"]
    assert shapes[2]["messages"] == [
        "system:str", "user:str", "assistant:str", "user:str",
        "assistant:none+tool_calls", "tool:str",
    ]
    assert run.snapshots[1]["payload"]["messages"][-1] == {"role": "user", "content": _CORRECTION}
    assert "誠實度警報" in "".join(run.chunks)


@pytest.mark.asyncio
async def test_wrap_up_round_after_budget_exhausted(monkeypatch):
    """額度耗盡：收尾輪 "none"＋denied tool 訊息＋WRAP_UP user 訊息。"""
    run = await _run(monkeypatch, _ui(), [
        openai_tools(("vault_tools", {"action": "list"})),
        openai_tools(("vault_tools", {"action": "read"})),
        openai_text("總結。"),
    ], "wrap", enable_autonomy=True, pulse_rounds=1)
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_choice"] for s in shapes] == [None, None, "none"]
    assert shapes[2]["messages"] == [
        "system:str", "user:str", "assistant:none+tool_calls", "tool:str",
        "assistant:none+tool_calls", "tool:str", "user:str",
    ]
    assert run.snapshots[2]["payload"]["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call_1", "name": "vault_tools",
         "content": "（未執行：工具輪額度已盡（1 輪））"},
        {"role": "user", "content": _WRAP_UP},
    ]


_TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "vault_tools",
             "input": {"action": "list"}}
_TOOL_RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}
_SYSTEM_BLOCK = {"type": "text", "text": "妳是 Sumi。", "cache_control": {"type": "ephemeral"}}


def _anthropic_entry() -> AnthropicRequest:
    return AnthropicRequest(
        model="grok-4", system=[_SYSTEM_BLOCK],
        messages=[
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": [_TOOL_USE]},
            {"role": "user", "content": [_TOOL_RESULT]},
        ],
        tools=[{"name": "vault_tools", "description": "檔案", "input_schema": {"type": "object"}}],
    )


_KF9_MESSAGES = [
    {"role": "user", "content": "查一下"},
    {"role": "assistant", "tool_calls": [{"id": "toolu_1", "type": "function", "function": {
        "name": "vault_tools", "arguments": '{"action": "list"}'}}]},
    {"role": "tool", "tool_call_id": "toolu_1", "content": "a.txt"},
]


@pytest.mark.parametrize(("degraded", "system"), [
    pytest.param(False, "妳是 Sumi。", id="list-system-as-text"),
    pytest.param(True, "妳是 Sumi。\n\n⚠️ SYSTEM WARNING: 記憶離線", id="degraded-warning-appended"),
])
@pytest.mark.asyncio
async def test_kf9_anthropic_entry_sends_tool_calls_to_openai_compatible_vendor(monkeypatch,
                                                                               degraded, system):
    """KF-9 已修正（ADR-0248，3b-U5b）。

    原 ``test_kf9_anthropic_entry_blocks_reach_openai_compatible_vendor``。
    Anthropic 形狀入口接 OpenAI 相容模型：tool_use／tool_result 轉成 tool_calls 與 role=tool 訊息
    （call id＝tool_use id）。list system 合成字串、cache_control 不外送；降級警告接在字串後面，
    中文不再被跳脫。
    """
    request = convert_to_openai(_anthropic_entry(), degraded, "記憶離線")
    run = await _run(monkeypatch, request, [openai_text("好")], "kf9", provider="Grok")
    assert run.snapshots[0]["payload"]["messages"] == [
        {"role": "system", "content": system}, *_KF9_MESSAGES]
    assert shape(run.snapshots[0])["tools"] == ["vault_tools"]


_CUSTOM = CustomRouteResolution(
    disposition=CustomRouteDisposition.ACTIVE_CUSTOM,
    route=CustomModelRoute(connection_id="c1", endpoint="http://custom.test/v1/chat/completions",
                           api_key="", provider="custom"),
)
_UNKNOWN = CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN)


@pytest.mark.parametrize(("model", "route", "handler"), [
    pytest.param("my-local-model", _CUSTOM, "handle_openai_request", id="custom"),
    pytest.param("gemini-2.5-flash", _UNKNOWN, "handle_gemini_request", id="gemini"),
    pytest.param("grok-4", _UNKNOWN, "handle_openai_request", id="openai-compatible"),
])
@pytest.mark.asyncio
async def test_kf9_every_non_claude_entry_hands_over_the_tool_history(monkeypatch, model, route,
                                                                     handler):
    """KF-9 已修正（ADR-0248，3b-U5b）。

    原 ``test_kf9_every_non_claude_entry_converts_anthropic_requests``。
    自訂連線、Gemini、一般 OpenAI 相容三個入口收到 Anthropic 形狀請求，handler 拿到的都是
    convert_to_openai 轉好的請求：字串 system、tool_calls 與 role=tool 訊息（handler patch 掉，
    不連網）。
    """
    monkeypatch.setattr(ModelManager, "get_models", lambda self: [])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda self, _model: route)
    monkeypatch.setattr(factory, "get_provider_info",
                        lambda _model: ("http://grok.test/v1", "fake-key", "Grok"))
    received = []
    monkeypatch.setattr(factory, handler, lambda req, _ctx: received.append(req))
    await factory.get_stream_generator(model, _anthropic_entry(), _IDENTITY, "rid", 0.0, {})
    assert [message.model_dump(exclude_none=True) for message in received[0].messages] == [
        {"role": "system", "content": "妳是 Sumi。"}, *_KF9_MESSAGES]
