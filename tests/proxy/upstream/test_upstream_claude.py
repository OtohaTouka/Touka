"""Claude 上游請求契約（model-p1a U1）。

凍結 ClaudeAdapter 經 run_react_loop 每一輪交給 SDK 的 kwargs：模型名與 max_tokens（3a 起
不再改寫別名、max_tokens 照請求值否則規則表預設，ADR-0247）、UI 入口白名單、thinking 與
temperature（D-3）、快取斷點輪替與 4 個上限、誠實矯正與收尾輪的 tool_choice、多則 system
依序合併（KF-10 已修，ADR-0247）。
期望值全部寫死；之後哪一段改了，會指出是哪一輪、哪個欄位。
"""

from __future__ import annotations

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters.claude_adapter import ClaudeAdapter

from .harness import claude_text, claude_tools, drive, shape

_TOOLS = [
    {"type": "function", "function": {
        "name": "vault_tools", "description": "檔案",
        "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "memory_tools", "description": "記憶",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }},
]
_HEADERS = None  # 3a-U2 拿掉 2024 年的 prompt-caching beta 標頭（快取已 GA，不再送任何標頭）
_BASE_KEYS = ["max_tokens", "messages", "model", "system", "tools"]
_CORRECTION = (
    "Warning: You mentioned memories or file contents without actually using tools. "
    "Verify first — query memory with `memory_tools` (action=\"query\") "
    "or read files with `read`/`grep` — then answer honestly."
)
_WRAP_UP = (
    "（系統）本次行動的工具輪額度已用盡，最後一輪的工具調用未執行。"
    "請根據目前已取得的結果直接總結收尾，不要再嘗試調用工具。"
)


def _ui(model: str = "claude-4-5-sonnet", history: list | None = None,
        **extra) -> OpenAIChatRequest:
    messages = history or [
        {"role": "system", "content": "妳是 Sumi。"},
        {"role": "user", "content": "查一下"},
    ]
    return OpenAIChatRequest(model=model, messages=messages, tools=_TOOLS, **extra)


async def _run(monkeypatch, request, responses, tag, **ctx_extra):
    return await drive(monkeypatch, ClaudeAdapter(), request, responses,
                       tag=f"claude-{tag}", ctx_extra=ctx_extra or None)


@pytest.mark.parametrize(("requested", "sent", "max_tokens"), [
    ("claude-4-6-opus-20260205", "claude-4-6-opus-20260205", 16000),  # 舊別名不再改寫
    ("claude-4-5-sonnet", "claude-4-5-sonnet", 16000),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5-20250929", 16000),
    ("claude-4-5-haiku-thinking", "claude-4-5-haiku", 16000),  # 只去 -thinking 尾碼
    ("claude-opus-4-1-20250805", "claude-opus-4-1-20250805", 8192),  # SDK 非串流上限
    ("claude-3-5-sonnet", "claude-3-5-sonnet", 8192),
    ("Claude-Opus-5-5", "claude-opus-5-5", 16000),  # 照原樣小寫
    ("claude-opus-5-5-thinking", "claude-opus-5-5", 16000),
    ("claude-unrecognized", "claude-unrecognized", 8192),  # 認不出版本：保守預設
])
@pytest.mark.asyncio
async def test_model_name_mapping(monkeypatch, requested, sent, max_tokens):
    run = await _run(monkeypatch, _ui(requested), [claude_text("好")], "map")
    kwargs = run.snapshots[0]["kwargs"]
    assert (kwargs["model"], kwargs["max_tokens"]) == (sent, max_tokens)


@pytest.mark.asyncio
async def test_ui_entry_sends_only_whitelisted_keys(monkeypatch):
    """UI 入口：取樣參數、Touka 內部欄位不外送；max_tokens 照請求；工具轉 Anthropic 形狀。

    r55：``stop`` 改以 ``stop_sequences`` 送出（以前 Claude 路徑丟掉；預期的行為改變）。
    """
    request = _ui(
        temperature=0.3, top_p=0.9, top_k=40, max_tokens=123, stop=["END"],
        incognito=True, deep_research=True, mcp_dispatcher=True, session_id="s1",
        project_id="p1", auth_level="SafeAuto", ide_context={"file": "a.py"},
        safety_mode="off", safety_settings=[{"category": "x", "threshold": "y"}],
    )
    run = await _run(monkeypatch, request, [claude_text("好")], "keys")
    assert run.snapshots[0]["kwargs"]["stop_sequences"] == ["END"]
    assert [shape(s) for s in run.snapshots] == [{
        "keys": sorted([*_BASE_KEYS, "stop_sequences"]), "model": "claude-4-5-sonnet",
        "max_tokens": 123,
        "headers": _HEADERS, "tool_choice": None, "thinking": None, "system": "text*",
        "tools": ["vault_tools", "memory_tools*"], "messages": ["user:text*"],
    }]
    kwargs = run.snapshots[0]["kwargs"]
    assert kwargs["system"] == [
        {"type": "text", "text": "妳是 Sumi。", "cache_control": {"type": "ephemeral"}},
    ]
    assert kwargs["tools"][0] == {
        "name": "vault_tools", "description": "檔案",
        "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}},
    }


@pytest.mark.parametrize(("requested", "sent", "thinking"), [
    ("claude-sonnet-4-6-thinking", "claude-sonnet-4-6", {"type": "adaptive"}),  # 4.6 起 adaptive
    ("claude-haiku-4-5-thinking", "claude-haiku-4-5", {"type": "enabled", "budget_tokens": 4000}),
    ("claude-opus-5-5-thinking", "claude-opus-5-5", {"type": "adaptive"}),
])
@pytest.mark.asyncio
async def test_d3_thinking_requests_send_no_temperature(monkeypatch, requested, sent, thinking):
    """D-3 守門：UI 預設 temperature 0.7 與 -thinking 同送時，temperature 不外送。"""
    run = await _run(monkeypatch, _ui(requested, temperature=0.7), [claude_text("好")], "d3")
    kwargs = run.snapshots[0]["kwargs"]
    assert sorted(kwargs) == ["max_tokens", "messages", "model", "system", "thinking", "tools"]
    assert (kwargs["model"], kwargs["max_tokens"], kwargs["thinking"]) == (sent, 16000, thinking)


@pytest.mark.asyncio
async def test_two_tool_rounds_rotate_message_breakpoint(monkeypatch):
    """有歷史的對話：最後一則 user 有斷點；每個工具輪後斷點輪替到最新的 tool_result。"""
    history = [
        {"role": "system", "content": "妳是 Sumi。"},
        {"role": "user", "content": "前情"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "查一下"},
    ]
    run = await _run(monkeypatch, _ui(history=history), [
        claude_tools(("vault_tools", {"action": "list"})),
        claude_tools(("memory_tools", {"query": "紅茶"})),
        claude_text("查好了。"),
    ], "tools2")
    assert [shape(s)["messages"] for s in run.snapshots] == [
        ["user:text", "assistant:text", "user:text*"],
        ["user:text", "assistant:text", "user:text", "assistant:tool_use", "user:tool_result*"],
        ["user:text", "assistant:text", "user:text", "assistant:tool_use", "user:tool_result",
         "assistant:tool_use", "user:tool_result*"],
    ]
    assert {shape(s)["tools"] == ["vault_tools", "memory_tools*"] for s in run.snapshots} == {True}
    second = run.snapshots[1]["kwargs"]["messages"]
    assert second[3:] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "vault_tools",
             "input": {"action": "list"}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu_1",
            "content": [{"type": "text", "text": "Tool Success"}],
            "cache_control": {"type": "ephemeral"},
        }]},
    ]


@pytest.mark.asyncio
async def test_first_tool_round_of_new_conversation_marks_tool_result(monkeypatch):
    """只有一則 user 時：第 1 個工具輪後斷點也輪替到 tool_result（3a 修正，ADR-0247；
    以前訊息數≤2 不加，第 2 輪沒有任何訊息斷點）。"""
    run = await _run(monkeypatch, _ui(), [
        claude_tools(("vault_tools", {"action": "list"})),
        claude_tools(("vault_tools", {"action": "read"})),
        claude_text("好了。"),
    ], "fresh")
    assert [shape(s)["messages"] for s in run.snapshots] == [
        ["user:text*"],
        ["user:text", "assistant:tool_use", "user:tool_result*"],
        ["user:text", "assistant:tool_use", "user:tool_result", "assistant:tool_use",
         "user:tool_result*"],
    ]


def _breakpoints(obj) -> int:
    """遞迴數 ``cache_control`` 標記（SDK 會原樣外送，每一個都算進 Anthropic 的上限）。"""
    if isinstance(obj, dict):
        return ("cache_control" in obj) + sum(_breakpoints(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_breakpoints(v) for v in obj)
    return 0


_MULTI_SYSTEM = [
    {"role": "system", "content": ("<persona>妳是 Sumi。</persona>\n"
                                   "<conversation_summary>上次聊到紅茶</conversation_summary>")},
    {"role": "system", "content": "<tool_availability>可用：vault_tools</tool_availability>"},
]
_PRIOR = [{"role": "user", "content": "前情"}, {"role": "assistant", "content": "好"}]


def _native(messages: list) -> AnthropicRequest:
    return AnthropicRequest(
        model="claude-sonnet-4-6", max_tokens=1024, messages=messages,
        system="妳是 Sumi。\n<conversation_summary>上次聊到紅茶</conversation_summary>",
        tools=[{"name": n, "description": n, "input_schema": {"type": "object"}}
               for n in ("vault_tools", "memory_tools")],
    )


_NATIVE = _native([{"role": "user", "content": [{"type": "text", "text": "查一下"}]}])


@pytest.mark.parametrize(("request_", "ctx", "expected"), [
    (_ui(history=[*_MULTI_SYSTEM, {"role": "user", "content": "查一下"}]), {},
     [(2, 1, 1, 4)] * 4),  # 新對話
    (_ui(history=[*_MULTI_SYSTEM, *_PRIOR, {"role": "user", "content": "查一下"}]),
     {"system_degraded": True, "degraded_reason": "記憶離線。"},
     [(2, 1, 1, 4)] * 4),  # 有歷史＋降級警告
    (_NATIVE, {}, [(2, 1, 0, 3)] + [(2, 1, 1, 4)] * 3),  # 原生入口：user 不加斷點
], ids=["ui-fresh", "ui-history-degraded", "native-fresh"])
@pytest.mark.asyncio
async def test_breakpoints_never_exceed_four(monkeypatch, request_, ctx, expected):
    """多則 system（合併後切兩塊）＋工具＋3 個工具輪：每輪 (system, tools, 訊息, 總數)
    固定，總數不超過 Anthropic 每次請求 4 個 cache_control 的上限。"""
    run = await _run(monkeypatch, request_, [
        claude_tools(("vault_tools", {"action": "list"})),
        claude_tools(("memory_tools", {"query": "紅茶"})),
        claude_tools(("vault_tools", {"action": "read"})),
        claude_text("好了。"),
    ], "ceiling", **ctx)
    kw = [s["kwargs"] for s in run.snapshots]
    counts = [(_breakpoints(k["system"]), _breakpoints(k["tools"]),
               _breakpoints(k["messages"]), _breakpoints(k)) for k in kw]
    assert counts == expected
    assert max(c[-1] for c in counts) <= 4


def _text(text: str, *, mark: bool = False) -> dict:
    block = {"type": "text", "text": text}
    return {**block, "cache_control": {"type": "ephemeral"}} if mark else block


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": [_text("查一下", mark=True), _text("（附註）")]}],
    [{"role": "user", "content": [_text("前情", mark=True), _text("補充")]},
     {"role": "assistant", "content": [_text("好")]},
     {"role": "user", "content": [_text("查一下")]}],
    [{"role": "user", "content": [_text("查")]},
     {"role": "assistant", "content": [
         {"type": "tool_use", "id": "toolu_0", "name": "vault_tools", "input": {}}]},
     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_0",
                                   "content": [_text("舊結果", mark=True)]}]},
     {"role": "assistant", "content": [_text("好")]},
     {"role": "user", "content": [_text("再查")]}],
], ids=["fresh-not-last-block", "history-not-last-block", "history-inside-tool-result"])
@pytest.mark.asyncio
async def test_caller_message_breakpoints_rotate_within_four(monkeypatch, messages):
    """原生入口：呼叫端把斷點放在 user 訊息不是最後一塊（或 tool_result 內層）時，工具輪
    會清掉先前所有 user 訊息的斷點，只留最新 tool_result 一個；system 與 tools 不變。"""
    run = await _run(monkeypatch, _native(messages), [
        claude_tools(("vault_tools", {"action": "list"})),
        claude_tools(("memory_tools", {"query": "紅茶"})),
        claude_text("好了。"),
    ], "caller-bp")
    kw = [s["kwargs"] for s in run.snapshots]
    counts = [(_breakpoints(k["system"]), _breakpoints(k["tools"]),
               _breakpoints(k["messages"]), _breakpoints(k)) for k in kw]
    assert counts == [(2, 1, 1, 4)] * 3
    assert [shape(s)["messages"][-1] for s in run.snapshots[1:]] == ["user:tool_result*"] * 2


@pytest.mark.parametrize(("requested", "correction_choice", "after_tool_choice"), [
    ("claude-4-5-sonnet", {"type": "any"}, {"type": "auto"}),  # 4.x 未開思考：強制
    ("claude-4-5-sonnet-thinking", None, None),  # 開思考：不強制
    ("claude-opus-5-5", None, None),  # 5.x：不強制
])
@pytest.mark.asyncio
async def test_honesty_correction_tool_choice(monkeypatch, requested, correction_choice,
                                              after_tool_choice):
    run = await _run(monkeypatch, _ui(requested), [
        claude_text("根據過去的記憶系統，你喜歡紅茶。"),
        claude_tools(("memory_tools", {"query": "紅茶"})),
        claude_text("查到了。"),
    ], "honesty")
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_choice"] for s in shapes] == [None, correction_choice, after_tool_choice]
    assert shapes[1]["messages"] == ["user:text*", "assistant:text", "user:str"]
    assert run.snapshots[1]["kwargs"]["messages"][-1] == {"role": "user", "content": _CORRECTION}
    assert "誠實度警報" in "".join(run.chunks)


@pytest.mark.asyncio
async def test_wrap_up_round_after_budget_exhausted(monkeypatch):
    """額度耗盡：收尾輪 tool_choice none＋denied tool_result＋WRAP_UP 文字（同一則 user）；
    斷點留在上一輪的 tool_result（3a 起新對話首輪也標，ADR-0247）。"""
    run = await _run(monkeypatch, _ui(), [
        claude_tools(("vault_tools", {"action": "list"})),
        claude_tools(("vault_tools", {"action": "read"})),
        claude_text("總結。"),
    ], "wrap", enable_autonomy=True, pulse_rounds=1)
    shapes = [shape(s) for s in run.snapshots]
    assert [s["tool_choice"] for s in shapes] == [None, None, {"type": "none"}]
    assert shapes[2]["messages"] == [
        "user:text", "assistant:tool_use", "user:tool_result*",
        "assistant:tool_use", "user:tool_result,text",
    ]
    assert run.snapshots[2]["kwargs"]["messages"][-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": [{"type": "text", "text": "（未執行：工具輪額度已盡（1 輪））"}]},
        {"type": "text", "text": _WRAP_UP},
    ]}


@pytest.mark.asyncio
async def test_kf10_claude_merges_system_messages_in_order(monkeypatch):
    """已知故障 KF-10，見 ADR-0242；3a 已修正（ADR-0247）。

    UI 入口帶多則 system 時依序合併成一段（換行相接，和 Gemini 轉換器一致），
    不再只留最後一則；夾在對話中間的 system 也併入，不變成訊息。
    """
    history = [
        {"role": "system", "content": "第一則 system"},
        {"role": "system", "content": "第二則 system"},
        {"role": "user", "content": "查一下"},
        {"role": "system", "content": "第三則 system"},
    ]
    run = await _run(monkeypatch, _ui(history=history), [claude_text("好")], "kf10")
    assert run.snapshots[0]["kwargs"]["system"] == [{
        "type": "text", "text": "第一則 system\n第二則 system\n第三則 system",
        "cache_control": {"type": "ephemeral"},
    }]
    assert shape(run.snapshots[0])["messages"] == ["user:text*"]


@pytest.mark.asyncio
async def test_native_anthropic_entry(monkeypatch):
    """原生 Anthropic 入口：模型名與 max_tokens 直通、內部欄位不外送、user 訊息不加斷點。"""
    request = AnthropicRequest(
        model="claude-sonnet-4-6", max_tokens=1024, temperature=0.5, top_p=0.9,
        incognito=True, session_id="s1", project_id="p1",
        system="妳是 Sumi。\n<conversation_summary>上次聊到紅茶</conversation_summary>",
        messages=[{"role": "user", "content": [{"type": "text", "text": "查一下"}]}],
        tools=[
            {"name": "vault_tools", "description": "檔案", "input_schema": {"type": "object"}},
            {"name": "memory_tools", "description": "記憶", "input_schema": {"type": "object"}},
        ],
    )
    run = await _run(monkeypatch, request, [claude_text("好")], "native")
    assert [shape(s) for s in run.snapshots] == [{
        "keys": _BASE_KEYS, "model": "claude-sonnet-4-6", "max_tokens": 1024,
        "headers": _HEADERS, "tool_choice": None, "thinking": None, "system": "text*,text*",
        "tools": ["vault_tools", "memory_tools*"], "messages": ["user:text"],
    }]
