"""前綴穩定（model-p1a U5）。

快取前綴不能在不知不覺中被打破：
- 迴圈內：同一回合的連續工具輪，前一輪的請求是後一輪的前綴（Claude 忽略斷點標記本身）。
- 工具 schema：同樣輸入兩次，序列化結果相同、順序照輸入。
- 跨回合：Claude 原生字串入口與 UI 入口的穩定前綴都不變（KF-5 已修，ADR-0247）。
  OpenAI／Gemini 的 system 原樣放在最前面，波動區塊不搬動，照實釘住。
若之後在 system 裡加了每回合會變的內容，這裡會紅。
"""

from __future__ import annotations

import json

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter

from .harness import (
    claude_text,
    claude_tools,
    drive,
    gemini_text,
    gemini_tools,
    openai_text,
    openai_tools,
)

_SYS_A = ("<persona>妳是 Sumi。</persona>\n"
          "<conversation_summary>上次聊到紅茶</conversation_summary>\n"
          "<tool_availability>可用：vault_tools</tool_availability>")
_SYS_B = _SYS_A.replace("紅茶", "咖啡")  # 下一個回合：只有摘要變了
_NAMES = ["vault_tools", "research", "memory_tools"]  # 刻意不照字母序
_TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": f"{name} 說明",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    }}
    for name in _NAMES
]
_HISTORY = [
    {"role": "user", "content": "前情"},
    {"role": "assistant", "content": "好"},
    {"role": "user", "content": "查一下"},
]

# provider → (adapter 類別, 模型名, 快照主體鍵, 歷史鍵, 工具回應, 文字回應, ctx)
_CASES = {
    "claude": (ClaudeAdapter, "claude-4-5-sonnet", "kwargs", "messages",
               claude_tools, claude_text, {}),
    "openai": (OpenAIAdapter, "grok-4", "payload", "messages",
               openai_tools, openai_text, {"provider": "Grok", "url": "http://grok.test/v1"}),
    "gemini": (GeminiAdapter, "gemini-2.5-flash", "payload", "contents",
               gemini_tools, gemini_text, {}),
}


def _strip_breakpoints(obj):
    """去掉 Anthropic 的 cache_control 標記（比對前綴時忽略斷點本身）。"""
    if isinstance(obj, dict):
        return {k: _strip_breakpoints(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_breakpoints(v) for v in obj]
    return obj


def _ui(model: str, system: str, history: list) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, tools=_TOOLS,
                             messages=[{"role": "system", "content": system}, *history])


async def _first_round(monkeypatch, provider: str, request, tag: str) -> dict:
    adapter_cls, _m, body, _h, _tool, text, ctx = _CASES[provider]
    run = await drive(monkeypatch, adapter_cls(), request, [text("好")],
                      tag=f"prefix-{provider}-{tag}", ctx_extra=ctx or None)
    return run.snapshots[0][body]


@pytest.mark.parametrize("provider", list(_CASES))
@pytest.mark.asyncio
async def test_tool_rounds_extend_previous_request(monkeypatch, provider):
    """迴圈內：每一輪只在歷史尾端追加，其他欄位逐位元不變。"""
    adapter_cls, model, body, history_key, tool, text, ctx = _CASES[provider]
    run = await drive(monkeypatch, adapter_cls(), _ui(model, _SYS_A, _HISTORY), [
        tool(("vault_tools", {"q": "a"})),
        tool(("research", {"q": "b"})),
        text("查好了。"),
    ], tag=f"prefix-loop-{provider}", ctx_extra=ctx or None)
    bodies = [_strip_breakpoints(s[body]) for s in run.snapshots]
    assert len(bodies) == 3
    for prev, nxt in zip(bodies, bodies[1:], strict=False):
        prev_history, next_history = prev[history_key], nxt[history_key]
        assert next_history[:len(prev_history)] == prev_history
        assert len(next_history) == len(prev_history) + 2  # assistant＋工具結果
        rest = {k: v for k, v in nxt.items() if k != history_key}
        assert rest == {k: v for k, v in prev.items() if k != history_key}


@pytest.mark.parametrize("provider", list(_CASES))
@pytest.mark.asyncio
async def test_tool_schema_serialization_is_deterministic(monkeypatch, provider):
    """同樣的工具輸入跑兩次：序列化結果相同、順序照輸入（不排序、不去重）。"""
    model = _CASES[provider][1]
    sent = []
    for n in (1, 2):
        b = await _first_round(monkeypatch, provider, _ui(model, _SYS_A, _HISTORY), f"schema{n}")
        tools = b["tools"][0]["functionDeclarations"] if provider == "gemini" else b["tools"]
        sent.append(tools)
    assert json.dumps(sent[0], ensure_ascii=False) == json.dumps(sent[1], ensure_ascii=False)
    names = [t.get("name") or t.get("function", {}).get("name") for t in sent[0]]
    assert names == _NAMES


@pytest.mark.asyncio
async def test_claude_native_string_system_keeps_stable_prefix_across_turns(monkeypatch):
    """原生字串入口：摘要搬到尾端並切成兩塊，第 N 與 N+1 回合的第一塊逐位元相同。"""
    def native(system: str, history: list) -> AnthropicRequest:
        return AnthropicRequest(
            model="claude-sonnet-4-6", system=system, tools=[
                {"name": n, "description": f"{n} 說明", "input_schema": {"type": "object"}}
                for n in _NAMES
            ],
            messages=[{"role": m["role"], "content": [{"type": "text", "text": m["content"]}]}
                      for m in history],
        )

    turn_n = await _first_round(monkeypatch, "claude", native(_SYS_A, _HISTORY[:1]), "native-n")
    turn_n1 = await _first_round(monkeypatch, "claude", native(_SYS_B, _HISTORY), "native-n1")
    stable = ("<persona>妳是 Sumi。</persona>\n\n"
              "<tool_availability>可用：vault_tools</tool_availability>\n")
    assert [b["text"] for b in turn_n["system"]] == [
        stable, "<conversation_summary>上次聊到紅茶</conversation_summary>",
    ]
    assert [b["text"] for b in turn_n1["system"]] == [
        stable, "<conversation_summary>上次聊到咖啡</conversation_summary>",
    ]
    assert turn_n1["tools"] == turn_n["tools"]
    assert turn_n1["messages"][:1] == turn_n["messages"]


@pytest.mark.asyncio
async def test_kf5_claude_ui_entry_keeps_stable_prefix_across_turns(monkeypatch):
    """已知故障 KF-5，見 ADR-0242；3a 已修正（ADR-0247）。

    UI 入口（OpenAI 形狀）的 system 以字串交給 adapter，和原生字串入口一樣：摘要搬到
    尾端並切成兩塊（各帶斷點），第 N 與 N+1 回合的第一塊逐位元相同。
    """
    turn_n = await _first_round(monkeypatch, "claude", _ui("claude-4-5-sonnet", _SYS_A,
                                                           _HISTORY[:1]), "kf5-n")
    turn_n1 = await _first_round(monkeypatch, "claude", _ui("claude-4-5-sonnet", _SYS_B,
                                                            _HISTORY), "kf5-n1")
    stable = {"type": "text", "cache_control": {"type": "ephemeral"},
              "text": ("<persona>妳是 Sumi。</persona>\n\n"
                       "<tool_availability>可用：vault_tools</tool_availability>\n")}
    assert turn_n["system"] == [stable, {
        "type": "text", "text": "<conversation_summary>上次聊到紅茶</conversation_summary>",
        "cache_control": {"type": "ephemeral"},
    }]
    assert turn_n1["system"] == [stable, {
        "type": "text", "text": "<conversation_summary>上次聊到咖啡</conversation_summary>",
        "cache_control": {"type": "ephemeral"},
    }]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.asyncio
async def test_cross_turn_system_sent_verbatim_at_front(monkeypatch, provider):
    """OpenAI／Gemini：system 原樣放在最前面、波動區塊不搬動，所以摘要一變，
    差異就出現在所有歷史之前；system 之後的歷史仍是前一回合的延伸。"""
    model, _b, history_key = _CASES[provider][1:4]
    turn_n = await _first_round(monkeypatch, provider, _ui(model, _SYS_A, _HISTORY[:1]), "x-n")
    turn_n1 = await _first_round(monkeypatch, provider, _ui(model, _SYS_B, _HISTORY), "x-n1")
    if provider == "openai":
        assert turn_n["messages"][0] == {"role": "system", "content": _SYS_A}
        assert turn_n1["messages"][0] == {"role": "system", "content": _SYS_B}
        history_n, history_n1 = turn_n["messages"][1:], turn_n1["messages"][1:]
    else:
        assert turn_n["system_instruction"] == {"parts": [{"text": _SYS_A}]}
        assert turn_n1["system_instruction"] == {"parts": [{"text": _SYS_B}]}
        history_n, history_n1 = turn_n[history_key], turn_n1[history_key]
    assert history_n1[:len(history_n)] == history_n
    assert len(history_n1) == 3
