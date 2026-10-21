"""r55：ClaudeAdapter 送出點的思考強度、原生入口與停止字串（T2）。

- adapter 只讀 context 的專用鍵 ``thinking_profile``（不讀標頭）；有沒有它，送出的 kwargs 只差
  thinking／output_config／max_tokens 三鍵；只對 Claude 5.5 三型號生效。
- 原生 ``/v1/messages``：照實際 max_tokens 補 default_on 下限（不看 model_fields_set，計分卡
  刻意不標）；``output_config`` 只轉送 effort（其他鍵只記鍵名）；``thinking.disabled`` 依型號改寫。
- OpenAI 入口的 ``stop`` 以 ``stop_sequences`` 送出（濾掉空字串與純空白、保序去重）；原生入口不送。

全部離線：client 是 MagicMock，只看 prepare_request 組出的 kwargs。
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from src.adapters.openai_to_claude import claude_stop_sequences
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters.base import THINKING_PROFILE_KEY
from src.handlers.adapters.claude_adapter import ClaudeAdapter

_MSGS = [{"role": "user", "content": "hi"}]
_TOOLS = [{"type": "function", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
}}]
_PROFILE_KEYS = {"thinking", "output_config", "max_tokens"}


async def _kwargs(request, profile: str | None = None, **ctx) -> dict:
    context = {"anthropic_client": MagicMock(), **ctx}
    if profile is not None:
        context[THINKING_PROFILE_KEY] = profile
    return (await ClaudeAdapter().prepare_request(request, context)).kwargs


def _ui(model: str, **extra) -> OpenAIChatRequest:
    messages = [{"role": "system", "content": "妳是 Sumi。"}, {"role": "user", "content": "補全"}]
    return OpenAIChatRequest(model=model, messages=messages, tools=_TOOLS, max_tokens=128,
                             stop=["<|FIM_PREFIX|>", "```"], temperature=0.2, **extra)


def _changed(a: dict, b: dict) -> set[str]:
    return {key for key in a.keys() | b.keys() if a.get(key) != b.get(key)}


# ── OpenAI 入口：有無 profile 只差三鍵 ────────────────────────────────────────


@pytest.mark.parametrize(("model", "thinking", "effort", "max_tokens"), [
    ("claude-haiku-5-5", {"type": "disabled"}, "low", 128),
    ("claude-sonnet-5-5", {"type": "between_tools"}, "low", 128),
    ("claude-opus-5-5", None, "low", 8192),  # 關不掉：保留 8,192 下限
])
async def test_openai_entry_profile_changes_only_thinking_keys(model, thinking, effort,
                                                               max_tokens):
    plain = await _kwargs(_ui(model))
    minimal = await _kwargs(_ui(model), "minimal")
    assert _changed(plain, minimal) <= _PROFILE_KEYS
    assert (minimal.get("thinking"), minimal["output_config"], minimal["max_tokens"]) == (
        thinking, {"effort": effort}, max_tokens)
    assert (plain.get("thinking"), plain.get("output_config"), plain["max_tokens"]) == (
        None, None, 8192)


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-5",
                                   "claude-sonnet-5", "claude-fable-5-1", "claude-mythos-5-1",
                                   "claude-unrecognized", "claude-opus-4-8-thinking"])
async def test_profile_is_a_no_op_outside_5_5(model):
    assert await _kwargs(_ui(model), "minimal") == await _kwargs(_ui(model))


async def test_thinking_suffix_is_respected_under_profile():
    """客戶端明確要思考：thinking 照舊 adaptive、max_tokens 照舊下限，只補 effort。"""
    kwargs = await _kwargs(_ui("claude-haiku-5-5-thinking"), "minimal")
    assert (kwargs["thinking"], kwargs["output_config"], kwargs["max_tokens"]) == (
        {"type": "adaptive"}, {"effort": "low"}, 8192)


async def test_adapter_reads_only_the_dedicated_key_not_headers():
    """原始標頭不是來源：只有 headers 的 context 不會少想。"""
    headers = {"x-touka-purpose": "inline-completion"}
    assert await _kwargs(_ui("claude-haiku-5-5"), headers=headers) == await _kwargs(
        _ui("claude-haiku-5-5"))


@pytest.mark.parametrize("profile", ["judge", "MINIMAL", "", "low"])
async def test_unknown_profile_value_is_ignored(profile):
    assert await _kwargs(_ui("claude-haiku-5-5"), profile) == await _kwargs(
        _ui("claude-haiku-5-5"))


# ── 停止字串 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("stop", "expected"), [
    (["", "  ", "\n", "END", "END", 3], ["END"]),
    (["b", "a", "b", "\t", "a"], ["b", "a"]),
    ("X", ["X"]),
    (" ", None),
    (None, None),
    ([], None),
    (["", " \n\t "], None),
    (["\n\nEND", "```", "\n\nEND"], ["\n\nEND", "```"]),  # 含空白但不是純空白的照送
])
def test_claude_stop_sequences(stop, expected):
    assert claude_stop_sequences(stop) == expected


@pytest.mark.parametrize(("stop", "sent"), [
    (["", "  ", "\n", "END", "END"], ["END"]),
    ("X", ["X"]),
    (["  "], None),
    (None, None),
])
async def test_openai_entry_forwards_stop_as_stop_sequences(stop, sent):
    request = OpenAIChatRequest(model="claude-haiku-4-5", messages=_MSGS, stop=stop)
    assert (await _kwargs(request)).get("stop_sequences") == sent


async def test_native_entry_never_forwards_stop_sequences():
    """原生入口 schema 沒有 stop_sequences（現狀；收尾候選）：多帶的欄位被忽略、不外送。"""
    request = AnthropicRequest.model_validate(
        {"model": "claude-haiku-4-5", "messages": _MSGS, "stop_sequences": ["END"]})
    assert "stop_sequences" not in await _kwargs(request)


# ── 原生入口：輸出上限 ────────────────────────────────────────────────────────


def _native(model: str, **fields) -> AnthropicRequest:
    return AnthropicRequest(model=model, messages=_MSGS, **fields)


def _unmarked(request: AnthropicRequest) -> AnthropicRequest:
    request.model_fields_set.discard("max_tokens")  # 和計分卡 runner 同一種做法
    return request


@pytest.mark.parametrize(("request_factory", "sent"), [
    (lambda: _native("claude-opus-5-5", max_tokens=4096), 8192),
    (lambda: _native("claude-opus-5-5", max_tokens=512), 8192),
    (lambda: _native("claude-sonnet-5", max_tokens=1024), 8192),
    (lambda: _native("claude-opus-5-5"), 8192),  # 沒給：schema 預設 8,192
    (lambda: _native("claude-opus-5-5", max_tokens=32768), 32768),
    # 計分卡的做法：值照設但不標 fields_set（ADR-0254）——一樣補下限
    (lambda: _unmarked(_native("claude-opus-5-5", max_tokens=512)), 8192),
    (lambda: _native("claude-haiku-4-5", max_tokens=512), 512),  # 舊型號照舊
    (lambda: _native("claude-opus-4-8", max_tokens=123), 123),
    (lambda: _native("claude-haiku-5-5", max_tokens=512,
                     thinking={"type": "disabled"}), 512),  # 思考真的關了：免下限
])
async def test_native_entry_applies_default_on_floor_to_actual_value(request_factory, sent):
    assert (await _kwargs(request_factory()))["max_tokens"] == sent


# ── 原生入口：effort 與 output_config ─────────────────────────────────────────


@pytest.mark.parametrize(("model", "effort", "sent"), [
    ("claude-opus-5-5", "low", {"effort": "low"}),
    ("claude-sonnet-5-5", "max", {"effort": "max"}),
    ("claude-haiku-5-5", "xhigh", {"effort": "xhigh"}),
    ("claude-haiku-4-5", "low", None),  # 舊型號送了會 400：不送
    ("claude-opus-5", "high", None),
    ("claude-opus-5-5", "turbo", None),  # 未知等級：丟掉（不 422）
])
async def test_native_effort_only_for_models_that_take_it(model, effort, sent):
    kwargs = await _kwargs(_native(model, output_config={"effort": effort}))
    assert kwargs.get("output_config") == sent


async def test_native_output_config_forwards_effort_only_and_logs_key_names(caplog):
    schema = {"type": "object", "properties": {"SECRET_FIELD_9d1": {"type": "string"}}}
    request = _native("claude-opus-5-5", output_config={
        "effort": "low", "format": {"type": "json_schema", "schema": schema}, "task_budget": 1})
    with caplog.at_level(logging.WARNING):
        kwargs = await _kwargs(request)
    assert kwargs["output_config"] == {"effort": "low"}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "format" in logged and "task_budget" in logged
    assert "SECRET_FIELD_9d1" not in logged and "json_schema" not in logged


async def test_native_output_config_effort_only_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        await _kwargs(_native("claude-opus-5-5", output_config={"effort": "low"}))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("effort", ["x" * 17])
def test_native_effort_length_is_bounded(effort):
    with pytest.raises(ValueError):
        _native("claude-opus-5-5", output_config={"effort": effort})


# ── 原生入口：thinking.disabled 改寫 ──────────────────────────────────────────


@pytest.mark.parametrize(("model", "effort", "thinking", "max_tokens"), [
    ("claude-opus-5-5", None, None, 8192),  # 關不掉：不送 disabled
    ("claude-opus-5-5", "low", None, 8192),
    ("claude-sonnet-5-5", None, {"type": "between_tools"}, 1024),
    ("claude-sonnet-5-5", "high", {"type": "between_tools"}, 1024),
    ("claude-sonnet-5-5", "xhigh", None, 8192),  # between_tools 在 xhigh 會 400：不送
    ("claude-haiku-5-5", "medium", {"type": "disabled"}, 1024),
    ("claude-haiku-5-5", "max", None, 8192),
    ("claude-haiku-4-5", None, {"type": "disabled"}, 1024),  # 舊型號照舊
])
async def test_native_disabled_thinking_is_rewritten_per_model(model, effort, thinking,
                                                              max_tokens):
    fields = {"output_config": {"effort": effort}} if effort else {}
    kwargs = await _kwargs(_native(model, max_tokens=1024, thinking={"type": "disabled"},
                                   **fields))
    assert (kwargs.get("thinking"), kwargs["max_tokens"]) == (thinking, max_tokens)


async def test_native_profile_also_only_lowers_thinking():
    """原生入口帶 profile（標頭）一樣只讓它少想：只差三鍵。"""
    plain = await _kwargs(_native("claude-haiku-5-5", max_tokens=4096, tools=_TOOLS))
    minimal = await _kwargs(_native("claude-haiku-5-5", max_tokens=4096, tools=_TOOLS), "minimal")
    assert _changed(plain, minimal) == _PROFILE_KEYS
    assert (minimal["thinking"], minimal["output_config"], minimal["max_tokens"]) == (
        {"type": "disabled"}, {"effort": "low"}, 4096)
