"""model-p3a U1：每模型規則表（model_rules；ADR-0247，併入並取代 model-p0 的 claude_model_rules）。

廠商規則（claude-api skill 2026-10-06）：adaptive 自 4.6 起；Haiku 4.5 與更舊只收
budget_tokens；budget_tokens 在 4.7／4.8／5.x／Fable／Mythos 一律 400；強制 tool_choice
在 Fable 5.1／Mythos 5.1／Opus 5.5／Sonnet 5.5 400，其餘 5.x 預設開思考、同樣不能強制。
快取門檻 512／1024／2048／4096 依型號；中途 system 只有 Opus 4.8、Opus 5／5.5、Sonnet 5.5、
Haiku 5.5、Fable、Mythos。非串流且用 SDK 預設逾時時 SDK 拒絕超過約 21,333（Opus 4／4.1 為
8,192）的 max_tokens：3b 起只在主迴圈關掉串流時於送出當下壓（ADR-0248 附註 4），主迴圈串流與
complete_once（有傳 timeout）不壓。GPT-6 兩列取自模型層研究 [S36]。
"""

from __future__ import annotations

import ast
import logging
from functools import cache
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import AsyncAnthropic
from anthropic._constants import MODEL_NONSTREAMING_TOKENS
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.handlers.adapters import claude_adapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.services.dispatchers.route_family import native_family
from src.services.guard_service import guard_service
from src.services.models.model_rules import (
    CLAUDE_EFFORT_LEVELS,
    ClaudeThinkingPlan,
    allows_forced_tool_choice,
    claude_nonstream_cap,
    claude_rules,
    claude_send_max_tokens,
    claude_thinking_plan,
    normalize_thinking,
    rules_for,
)

from tests.helpers.claude_stream import delegate_stream_to_create

_MSGS = [{"role": "user", "content": "hi"}]
_BRAG = "根據過去的記憶系統內容，你上次說喜歡紅茶。"
_SRC = Path(__file__).resolve().parents[2] / "ToukaProxy" / "src"


@pytest.mark.parametrize(("model", "rejects"), [
    ("claude-opus-4-8", True),
    ("claude-opus-4-7", True),
    ("claude-opus-5", True),
    ("claude-opus-5-5", True),
    ("claude-sonnet-5-5", True),
    ("claude-haiku-5-5", True),
    ("claude-fable-5-1", True),
    ("claude-mythos-5-1", True),
    ("claude-unrecognized", True),  # 認不出 → 保守（adaptive）
    ("claude-opus-4-6", False),  # < 4.7 仍收
    ("claude-haiku-4-5", False),
    ("claude-haiku-4-5-20251001", False),  # 日期段不當版本
    ("claude-opus-4-20250514", False),
    ("claude-3-7-sonnet-latest", False),
    ("claude-3-5-sonnet", False),
])
def test_budget_tokens_forbidden(model, rejects):
    assert ("budget_tokens" in claude_rules(model).forbidden_params) is rejects


@pytest.mark.parametrize(("model", "has_thinking", "allowed"), [
    ("claude-3-5-sonnet", False, True),
    ("claude-haiku-4-5", False, True),
    ("claude-opus-4-8", False, True),  # 4.8 不帶 thinking 時不思考 → 可強制
    ("claude-opus-4-8", True, False),
    ("claude-opus-5", False, False),  # 5.x 預設開思考
    ("claude-sonnet-5-5", False, False),
    ("claude-haiku-5-5", False, False),
    ("claude-opus-5-5", False, False),
    ("claude-fable-5-1", False, False),
    ("claude-mythos-5-1", False, False),
    ("claude-unrecognized", False, False),
])
def test_allows_forced_tool_choice(model, has_thinking, allowed):
    assert allows_forced_tool_choice(model, has_thinking) is allowed


@pytest.mark.parametrize(("model", "thinking", "expected"), [
    ("claude-opus-4-8", {"type": "enabled", "budget_tokens": 4000}, {"type": "adaptive"}),
    ("claude-haiku-4-5", {"type": "enabled", "budget_tokens": 4000},
     {"type": "enabled", "budget_tokens": 4000}),
    ("claude-opus-4-8", {"type": "adaptive", "budget_tokens": 4000}, {"type": "adaptive"}),
    ("claude-haiku-4-5", {"type": "disabled", "budget_tokens": 4000}, {"type": "disabled"}),
])
def test_normalize_thinking(model, thinking, expected):
    assert normalize_thinking(model, thinking) == expected


@pytest.mark.parametrize(("model", "thinking", "max_out", "cache_min", "mid_system"), [
    ("claude-fable-5-1", "default_on", 16000, 512, True),
    ("claude-mythos-5-1", "default_on", 16000, 512, True),
    # Mythos Preview 不在官方中途 system 名單（M9-R1a）
    ("claude-mythos-preview", "default_on", 16000, 2048, False),  # 門檻 2,048（R-3a）
    ("claude-opus-5-5", "default_on", 16000, 512, True),
    ("claude-sonnet-5-5", "default_on", 16000, 512, True),
    ("claude-haiku-5-5", "default_on", 16000, 512, True),
    ("claude-opus-5", "default_on", 16000, 512, True),
    ("claude-sonnet-5", "default_on", 16000, 1024, False),
    ("claude-opus-4-8", "adaptive", 16000, 1024, True),
    ("claude-opus-4-7", "adaptive", 16000, 2048, False),
    ("claude-opus-4-6", "adaptive", 16000, 4096, False),
    ("claude-sonnet-4-6", "adaptive", 16000, 1024, False),
    ("claude-opus-4-5-20251101", "budget", 16000, 4096, False),
    ("claude-sonnet-4-5-20250929", "budget", 16000, 1024, False),
    ("claude-haiku-4-5-20251001", "budget", 16000, 4096, False),
    ("claude-opus-4-1-20250805", "budget", 8192, 1024, False),  # SDK 非串流上限 8192
    ("claude-sonnet-4-20250514", "budget", 8192, 1024, False),
    ("claude-3-5-haiku-20241022", "budget", 8192, 2048, False),
    ("claude-3-7-sonnet-20250219", "budget", 8192, 1024, False),
    ("claude-unrecognized", "adaptive", 8192, None, False),
])
def test_claude_rule_rows(model, thinking, max_out, cache_min, mid_system):
    rules = rules_for(model)
    assert (rules.thinking, rules.default_max_output, rules.cache_min_tokens,
            rules.mid_conversation_system) == (thinking, max_out, cache_min, mid_system)
    assert (rules.uses_responses_api, rules.required_params_with_tools) == (False, ())


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "Gemini-3-Pro-Preview", "deepseek-chat",
                                   "grok-4", "gpt-4o"])
def test_non_claude_families_get_neutral_defaults(model):
    """不補輸出上限、不調思考、門檻未確認、不放中途 system；可強制工具（現行行為）。"""
    rules = rules_for(model)
    assert (rules.forbidden_params, rules.forced_tool_choice, rules.thinking,
            rules.default_max_output, rules.cache_min_tokens, rules.mid_conversation_system,
            rules.uses_responses_api, rules.required_params_with_tools) == (
        frozenset(), True, None, None, None, False, False, ())


@pytest.mark.parametrize(("model", "responses", "required"), [
    ("gpt-6-astra", True, ()),
    ("gpt-6.1-sol", True, ()),
    ("gpt-6.1-sol-2026-09-01", True, ()),
    ("gpt-6-sol", False, (("reasoning_effort", "none"),)),
    ("GPT-6-Luna", False, (("reasoning_effort", "none"),)),
])
def test_gpt6_rows(model, responses, required):
    rules = rules_for(model)
    assert (rules.uses_responses_api, rules.required_params_with_tools) == (responses, required)


@pytest.mark.parametrize(("model", "requested", "budget", "sent"), [
    ("claude-opus-5-5", None, None, 16000),
    ("claude-opus-5-5", 21333, None, 21333),
    ("claude-opus-5-5", 32768, None, 32768),  # 轉換器不壓；非串流上限在送出當下（見三種情形）
    ("claude-sonnet-4-20250514", 64000, None, 64000),
    ("claude-unrecognized", 50000, None, 50000),
    ("claude-opus-4-1-20250805", None, None, 8192),
    ("claude-opus-4-1-20250805", 32768, None, 32768),
    ("claude-opus-4-0", 16000, None, 16000),
    ("claude-haiku-4-5", 2048, 4000, 5024),  # budget 思考：至少 budget＋1,024
    ("claude-haiku-4-5", None, 4000, 16000),
    ("claude-opus-4-1-20250805", 1000, 4000, 5024),
    ("claude-opus-4-1-20250805", 32768, 4000, 32768),
    # R-3a：default_on 思考的型號請求值太小時提高到 8,192（思考會吃掉額度）；其他型號照請求值
    ("claude-opus-5-5", 128, None, 8192),
    ("claude-fable-5-1", 128, None, 8192),
    ("claude-haiku-5-5", 1, None, 8192),
    ("claude-sonnet-5", 8191, None, 8192),
    ("claude-opus-5-5", 8193, None, 8193),
    ("claude-opus-4-8", 128, None, 128),  # adaptive：不帶 thinking 就不思考
    ("claude-haiku-4-5", 128, None, 128),
    ("claude-unrecognized", 128, None, 128),
])
def test_claude_send_max_tokens(model, requested, budget, sent):
    assert claude_send_max_tokens(model, requested, budget) == sent


# ── r55：5.5 三型號的思考強度與關思考（官方 2026-10-16）──────────────────────────
# Opus 5.5 關不掉（disabled 在任何 effort 都 400）；Sonnet 5.5 只收 between_tools；Haiku 5.5 收
# disabled；後兩者都要 effort ≤ high。effort 五級只列給 5.5 三型號，其餘一律不送。
_OPUS55, _SONNET55, _HAIKU55 = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
_OFF = {"type": "disabled", "budget_tokens": 4000}  # 原生 schema 會帶預設 budget


@pytest.mark.parametrize(("model", "levels", "off_mode"), [
    (_OPUS55, CLAUDE_EFFORT_LEVELS, "unsupported"),
    (_SONNET55, CLAUDE_EFFORT_LEVELS, "between_tools"),
    (_HAIKU55, CLAUDE_EFFORT_LEVELS, "disabled"),
    ("claude-haiku-5-5-20261001", CLAUDE_EFFORT_LEVELS, "disabled"),
    ("claude-5-5", (), None),  # 5.5 但認不出系列：保守不送
    # 5.6 以後還沒查官方：照舊（＝_CLAUDE_5），不能沿用 5.5 的 effort 與關思考寫法
    ("claude-opus-5-6", (), None),
    ("claude-sonnet-6", (), None),
    ("claude-haiku-6-1", (), None),
    ("claude-opus-5", (), None),
    ("claude-sonnet-5", (), None),
    ("claude-fable-5-1", (), None),
    ("claude-mythos-5-1", (), None),
    ("claude-mythos-preview", (), None),
    ("claude-opus-4-8", (), None),
    ("claude-haiku-4-5", (), None),
    ("claude-unrecognized", (), None),
])
def test_effort_levels_and_thinking_off_only_on_5_5(model, levels, off_mode):
    rules = claude_rules(model)
    assert (rules.effort_levels, rules.thinking_off_mode) == (levels, off_mode)


@pytest.mark.parametrize("model", ["claude-opus-5-6", "claude-sonnet-6", "claude-haiku-6-1"])
def test_models_after_5_5_keep_the_old_behaviour(model):
    """未來型號：default_on、不送 effort、不改寫 disabled、8,192 下限照舊（必改 3）。"""
    assert claude_rules(model) == claude_rules("claude-opus-5")
    for minimal in (True, False):
        assert claude_thinking_plan(model, thinking=_OFF, effort="low", minimal=minimal) == (
            ClaudeThinkingPlan({"type": "disabled"}, None, False))
        assert claude_thinking_plan(model, thinking=None, effort=None, minimal=minimal) == (
            ClaudeThinkingPlan(None, None, False))
    assert claude_send_max_tokens(model, 128, None, thinking_off=True) == 8192


def test_effort_levels_low_to_high():
    assert CLAUDE_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize(("model", "thinking", "effort", "minimal", "plan"), [
    # 用途少想（minimal）：能關的關、effort 補最低；舊型號不變
    (_HAIKU55, None, None, True, ({"type": "disabled"}, "low", True)),
    (_SONNET55, None, None, True, ({"type": "between_tools"}, "low", True)),
    (_OPUS55, None, None, True, (None, "low", False)),
    ("claude-haiku-4-5", None, None, True, (None, None, False)),
    ("claude-opus-5", None, None, True, (None, None, False)),
    ("claude-fable-5-1", None, None, True, (None, None, False)),
    ("claude-unrecognized", None, None, True, (None, None, False)),
    # 客戶端自己給的 effort 優先；≤ high 才關得掉
    (_HAIKU55, None, "high", True, ({"type": "disabled"}, "high", True)),
    (_HAIKU55, None, "xhigh", True, (None, "xhigh", False)),
    (_SONNET55, None, "max", True, (None, "max", False)),
    # 客戶端明確要思考（-thinking）：尊重，只補 effort
    (_HAIKU55, {"type": "adaptive"}, None, True, ({"type": "adaptive"}, "low", False)),
    (_HAIKU55, {"type": "enabled", "budget_tokens": 4000}, None, True,
     ({"type": "adaptive"}, "low", False)),
    # 沒有 minimal：不主動改
    (_HAIKU55, None, None, False, (None, None, False)),
    (_OPUS55, None, "low", False, (None, "low", False)),
    # 原生 disabled：Opus 5.5 不送；Sonnet 5.5 ≤ high 改 between_tools；Haiku 5.5 ≤ high 照送
    (_OPUS55, _OFF, None, False, (None, None, False)),
    (_OPUS55, _OFF, "high", False, (None, "high", False)),
    (_OPUS55, _OFF, "xhigh", False, (None, "xhigh", False)),
    (_OPUS55, _OFF, "max", False, (None, "max", False)),
    (_SONNET55, _OFF, None, False, ({"type": "between_tools"}, None, True)),
    (_SONNET55, _OFF, "high", False, ({"type": "between_tools"}, "high", True)),
    (_SONNET55, _OFF, "xhigh", False, (None, "xhigh", False)),
    (_SONNET55, _OFF, "max", False, (None, "max", False)),
    (_HAIKU55, _OFF, "medium", False, ({"type": "disabled"}, "medium", True)),
    (_HAIKU55, _OFF, None, False, ({"type": "disabled"}, None, True)),
    (_HAIKU55, _OFF, "max", False, (None, "max", False)),
    ("claude-haiku-4-5", _OFF, None, False, ({"type": "disabled"}, None, False)),  # 舊行為
    # effort 只送給收它的型號；未知等級丟掉
    ("claude-haiku-4-5", None, "low", False, (None, None, False)),
    ("claude-sonnet-4-5", None, "high", True, (None, None, False)),
    (_HAIKU55, None, "turbo", False, (None, None, False)),
    (_HAIKU55, None, "turbo", True, ({"type": "disabled"}, "low", True)),
])
def test_claude_thinking_plan(model, thinking, effort, minimal, plan):
    assert claude_thinking_plan(model, thinking=thinking, effort=effort, minimal=minimal) == (
        ClaudeThinkingPlan(*plan))


@pytest.mark.parametrize(("model", "thinking", "effort", "expected"), [
    (_OPUS55, _OFF, None,
     "Claude claude-opus-5-5 cannot turn thinking off; thinking.disabled is not sent "
     "(it still thinks and bills output)"),
    (_SONNET55, _OFF, "xhigh",
     "Claude claude-sonnet-5-5 turns thinking off only at effort high or below; "
     "thinking.disabled is not sent at effort 'xhigh'"),
    (_HAIKU55, _OFF, "max",
     "Claude claude-haiku-5-5 turns thinking off only at effort high or below; "
     "thinking.disabled is not sent at effort 'max'"),
    ("claude-haiku-4-5", None, "low",
     "Claude effort 'low' is not sent to claude-haiku-4-5 (the model does not take it)"),
    (_HAIKU55, None, "turbo\nINJECTED",
     "Claude effort 'turbo\\nINJECTED' is not sent to claude-haiku-5-5 "
     "(the model does not take it)"),
])
def test_thinking_plan_warns_with_names_only(caplog, model, thinking, effort, expected):
    with caplog.at_level(logging.WARNING, logger="src.services.models.model_rules"):
        claude_thinking_plan(model, thinking=thinking, effort=effort, minimal=False)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [expected]


def test_thinking_plan_quiet_when_nothing_is_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger="src.services.models.model_rules"):
        claude_thinking_plan(_HAIKU55, thinking=None, effort="high", minimal=True)
        claude_thinking_plan("claude-haiku-4-5", thinking=None, effort=None, minimal=True)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(("model", "requested", "thinking_off", "sent"), [
    (_HAIKU55, 128, True, 128),  # 思考真的關了：免 8,192 下限
    (_SONNET55, 128, True, 128),
    (_OPUS55, 128, True, 8192),  # Opus 5.5 關不掉：永遠有下限
    (_HAIKU55, None, True, 16000),  # 沒給：規則表預設
    (_HAIKU55, 128, False, 8192),  # 照舊
    ("claude-opus-5", 128, True, 8192),  # 5.0 沒有關思考的規則：照舊
    ("claude-haiku-4-5", 128, True, 128),
])
def test_send_max_tokens_floor_waived_only_when_thinking_is_off(model, requested,
                                                               thinking_off, sent):
    assert claude_send_max_tokens(model, requested, None, thinking_off=thinking_off) == sent


def test_send_max_tokens_thinking_off_is_keyword_only():
    """memory_eval_probe 的三參數呼叫不受影響；第四個位置參數不收。"""
    with pytest.raises(TypeError):
        claude_send_max_tokens(_HAIKU55, 128, None, True)  # type: ignore[misc]


def test_send_max_tokens_logs_when_lowered(caplog):
    with caplog.at_level(logging.INFO, logger="src.services.models.model_rules"):
        assert claude_nonstream_cap("claude-opus-5-5", 32768) == 21333
        assert claude_nonstream_cap("claude-opus-5-5", 9000) == 9000
        assert claude_nonstream_cap("claude-opus-4-1-20250805", 32768) == 8192
    assert [r.getMessage() for r in caplog.records] == [
        "Claude max_tokens 32768 lowered to non-streaming cap 21333 for claude-opus-5-5",
        "Claude max_tokens 32768 lowered to non-streaming cap 8192 for claude-opus-4-1-20250805",
    ]


async def _sent_max_tokens(monkeypatch, path: str, model: str, max_tokens: int | None) -> int:
    """三種送出路徑實際交給 SDK 的 max_tokens（主迴圈串流／主迴圈關掉串流／complete_once）。"""
    if path == "main_nonstream":
        real = claude_rules
        monkeypatch.setattr(claude_adapter, "claude_rules", lambda m: replace(real(m), stream=False))
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=MagicMock())
    delegate_stream_to_create(client)
    adapter = ClaudeAdapter(stream=path != "once")
    request = OpenAIChatRequest(model=model, messages=_MSGS, max_tokens=max_tokens)
    state = await adapter.prepare_request(request, {"anthropic_client": client})
    async for _ in adapter.send_round(state):
        pass
    assert state.stream is (path == "main_stream")
    assert client.messages.stream.call_count == (1 if path == "main_stream" else 0)
    return client.messages.create.call_args.kwargs["max_tokens"]


@pytest.mark.parametrize(("model", "requested", "streamed", "nonstream", "once"), [
    ("claude-opus-5-5", 32768, 32768, 21333, 32768),
    ("claude-opus-5-5", 21333, 21333, 21333, 21333),
    ("claude-sonnet-4-20250514", 64000, 64000, 21333, 64000),
    ("claude-unrecognized", 50000, 50000, 21333, 50000),
    ("claude-opus-4-1-20250805", 32768, 32768, 8192, 32768),  # SDK 對 Opus 4／4.1 另有 8,192
    ("claude-opus-4-0", 16000, 16000, 8192, 16000),
    ("claude-opus-4-1-20250805", None, 8192, 8192, 8192),
    ("claude-haiku-4-5-thinking", 32768, 32768, 21333, 32768),  # budget 4,000：壓低後仍大於它
    ("claude-opus-4-1-thinking", 32768, 32768, 8192, 32768),
])
async def test_nonstreaming_cap_only_when_main_loop_streaming_is_off(
        monkeypatch, model, requested, streamed, nonstream, once):
    """ADR-0248 附註 4：上限只在主迴圈非串流送出時壓（送出當下，不寫回 kwargs）。"""
    sent = {path: await _sent_max_tokens(monkeypatch, path, model, requested)
            for path in ("main_stream", "once", "main_nonstream")}
    assert sent == {"main_stream": streamed, "main_nonstream": nonstream, "once": once}


def _sdk_rejects(model: str, max_tokens: int) -> bool:
    """呼叫已安裝 SDK 自己的非串流檢查（主迴圈 client 用預設逾時時才會觸發）。"""
    client = AsyncAnthropic(api_key="fake-key")
    try:
        client._calculate_nonstreaming_timeout(max_tokens, MODEL_NONSTREAMING_TOKENS.get(model))
    except ValueError:
        return True
    return False


@pytest.mark.parametrize("model", [*sorted(MODEL_NONSTREAMING_TOKENS), "claude-opus-5-5",
                                   "claude-haiku-4-5", "claude-sonnet-4-20250514"])
def test_sdk_accepts_defaults_and_lowered_values(model):
    """預設、壓低後、budget 思考提高後都不被 SDK 拒絕；上限＋1 會被拒（常數＝SDK 界線）。"""
    sent = [claude_nonstream_cap(model, claude_send_max_tokens(model, requested, budget))
            for requested, budget in ((None, None), (10**6, None), (1000, 4000), (10**6, 4000))]
    assert [_sdk_rejects(model, value) for value in sent] == [False] * 4
    assert _sdk_rejects(model, max(sent) + 1) is True


@pytest.mark.parametrize("rel", ["services/models/fetcher.py", "services/models/manager.py",
                                 "services/models/health.py", "routers/models.py"])
def test_model_discovery_never_reads_rules_table(rel):
    """禁令 39：規則表只用於整備送出內容，模型發現與健康檢查路徑不得讀它。"""
    assert "model_rules" not in (_SRC / rel).read_text(encoding="utf-8")


# ── 禁令 39：AST 匯入圖（R-3a）────────────────────────────────────────────────
_RULES_MODULE = ("src", "services", "models", "model_rules")
# 只有整備送出內容的模組讀規則表：openai_to_claude（OpenAI 形狀請求轉 Claude：思考參數、
# max_tokens）、claude_adapter（思考參數正規化、可否強制 tool_choice）、openai_adapter（3b-U2：
# 串流開關、帶工具必送參數、禁送參數、預設輸出上限、可否強制 tool_choice）。
_RULES_IMPORTERS = {"src.adapters.openai_to_claude", "src.handlers.adapters.claude_adapter",
                    "src.handlers.adapters.gemini_adapter", "src.handlers.adapters.openai_adapter",
                    "src.handlers.adapters.responses_adapter",
                    "src.services.dispatchers.route_family"}
# 模型發現、健康檢查與模型管理：不得匯入規則表，也不得匯入上述模組來間接取得它的符號。
_DISCOVERY = [
    p.relative_to(_SRC).as_posix() for p in sorted((_SRC / "services/models").glob("*.py"))
    if p.name != "model_rules.py"
] + ["routers/models.py", "services/model_manager.py"]


def _module_of(rel: str) -> tuple[str, ...]:
    parts = ("src", *rel.removesuffix(".py").split("/"))
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imported_modules(rel: str, source: str) -> set[tuple[str, ...]]:
    """一個檔案匯入的模組（相對匯入解析成絕對；from X import y 同時記 X 與 X.y）。"""
    package = _module_of(rel) if rel.endswith("__init__.py") else _module_of(rel)[:-1]
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {tuple(alias.name.split(".")) for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else ()
            module = (*base, *(node.module.split(".") if node.module else ()))
            found |= {module} | {(*module, alias.name) for alias in node.names}
    return found


def _rules_symbols() -> set[str]:
    tree = ast.parse((_SRC / "services/models/model_rules.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.ClassDef)
             and not n.name.startswith("_")}
    return names | {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name) and t.id[0].isupper()}  # 常數與型別別名


def _from_imports(rel: str, source: str) -> list[tuple[tuple[str, ...], str, str]]:
    """(來源模組, 匯入的名字, 本地綁定名)；相對匯入解析成絕對。"""
    package = _module_of(rel) if rel.endswith("__init__.py") else _module_of(rel)[:-1]
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else ()
            module = (*base, *(node.module.split(".") if node.module else ()))
            found += [(module, alias.name, alias.asname or alias.name) for alias in node.names]
    return found


def _readers_of_rules(sources: dict[str, str]) -> set[str]:
    """讀規則表的模組（遞移）：直接匯入規則表，或從讀規則表的模組匯入它帶出的規則表符號（含改名）。"""
    graph = {_module_of(rel): (_imported_modules(rel, text), _from_imports(rel, text))
             for rel, text in sources.items()}
    carried: dict[tuple[str, ...], set[str]] = {_RULES_MODULE: _rules_symbols()}
    readers: set[tuple[str, ...]] = set()
    changed = True
    while changed:
        changed = False
        for module, (modules, froms) in graph.items():
            got = {local for origin, name, local in froms if name in carried.get(origin, ())}
            if (_RULES_MODULE in modules or got) and (
                    module not in readers or not got <= carried.get(module, set())):
                readers.add(module)
                carried[module] = carried.get(module, set()) | got
                changed = True
    return {".".join(module) for module in readers}


@cache
def _src_readers() -> frozenset[str]:
    return frozenset(_readers_of_rules({p.relative_to(_SRC).as_posix(): p.read_text(
        encoding="utf-8") for p in _SRC.rglob("*.py")}))


def _discovery_leaks(rel: str, source: str, readers: frozenset[str] | set[str]) -> list[str]:
    banned = {_RULES_MODULE} | {tuple(m.split(".")) for m in readers}
    leaks = [".".join(m) for m in _imported_modules(rel, source) if m in banned]
    symbols = _rules_symbols()
    for node in ast.walk(ast.parse(source)):
        name = (node.id if isinstance(node, ast.Name) else node.attr
                if isinstance(node, ast.Attribute) else getattr(node, "name", None)
                if isinstance(node, ast.alias) else None)
        if name in symbols:
            leaks.append(name)
    return leaks


def test_only_the_send_path_imports_the_rules_table():
    assert _src_readers() == _RULES_IMPORTERS


@pytest.mark.parametrize("rel", _DISCOVERY)
def test_discovery_path_cannot_reach_rules_symbols(rel):
    assert _discovery_leaks(rel, (_SRC / rel).read_text(encoding="utf-8"), _src_readers()) == []


def test_discovery_scope_is_not_vacuous():
    assert {"services/models/fetcher.py", "services/models/manager.py",
            "services/models/health.py", "routers/models.py"} <= set(_DISCOVERY)
    assert {"claude_rules", "rules_for", "claude_send_max_tokens", "ModelRules"} <= _rules_symbols()


@pytest.mark.parametrize(("rel", "source", "leaks"), [
    ("services/models/fetcher.py",
     "from src.adapters.openai_to_claude import claude_rules\n",
     ["src.adapters.openai_to_claude", "claude_rules"]),
    ("services/models/fetcher.py", "from . import model_rules\n",
     ["src.services.models.model_rules"]),
    ("services/models/fetcher.py", "from .model_rules import rules_for as r\n",
     ["src.services.models.model_rules", "rules_for"]),
    ("routers/models.py",
     "import src.handlers.adapters.claude_adapter as c\nc.normalize_thinking\n",
     ["src.handlers.adapters.claude_adapter", "normalize_thinking"]),
    ("services/models/fetcher.py", "from src.adapters import schemas\n", []),
])
def test_discovery_leak_detector(rel, source, leaks):
    assert sorted(_discovery_leaks(rel, source, _RULES_IMPORTERS)) == sorted(leaks)


def test_two_hop_import_through_an_allowed_module_counts_as_reading():
    """R-3a 複審：helper 從轉換器以別名匯入 claude_rules，fetcher 再從 helper 匯入。"""
    sources = {
        "adapters/openai_to_claude.py":
            "from src.services.models.model_rules import claude_rules\n",
        "utils/rules_helper.py": "from src.adapters.openai_to_claude import claude_rules as pick\n",
        "services/models/fetcher.py": "from src.utils.rules_helper import pick\n",
        "services/models/health.py": "from src.adapters.openai_to_claude import convert\n",
    }
    readers = _readers_of_rules(sources)
    assert readers == {"src.adapters.openai_to_claude", "src.utils.rules_helper",
                       "src.services.models.fetcher"}
    assert _discovery_leaks("services/models/fetcher.py", sources["services/models/fetcher.py"],
                            readers) == ["src.utils.rules_helper"]


@pytest.mark.parametrize(("rel", "source", "reads"), [
    ("services/telemetry.py", "from src.services.models.model_rules import rules_for\n", True),
    ("services/models/manager.py", "from .model_rules import claude_rules\n", True),
    ("services/models/manager.py", "from . import model_rules\n", True),
    ("adapters/x.py", "import src.services.models.model_rules\n", True),
    ("adapters/x.py", "from src.services.models import routes\n", False),
])
def test_rules_reader_detector(rel, source, reads):
    assert bool(_readers_of_rules({rel: source})) is reads


@pytest.mark.parametrize(("model", "family", "thinking", "required"), [
    ("claude-opus-5-5", "anthropic", "default_on", ()),
    ("CLAUDE-gemini-gpt-6-sol", "anthropic", "adaptive", ()),  # 含 claude 就是 Claude
    ("openrouter/anthropic/claude-opus-4.8", "anthropic", "adaptive", ()),
    ("Gemini-gpt-6-sol", "gemini", None, ()),  # 含 gemini（無 claude）走 Gemini、不套 GPT 列
    ("models/gemini-2.5-pro", "gemini", None, ()),
    ("gpt-6-sol", None, None, (("reasoning_effort", "none"),)),
    ("deepseek-chat", None, None, ()),
])
def test_rules_family_matches_native_family(model, family, thinking, required):
    """rules_for 的家族判斷與 route_family.native_family 一致（兩處各自實作，靠這裡釘住）。"""
    rules = rules_for(model)
    assert (native_family(model), rules.thinking, rules.required_params_with_tools) == (
        family, thinking, required)


# ── adapter 接線（送出內容）───────────────────────────────────────────────────


async def _kwargs(request) -> dict:
    state = await ClaudeAdapter().prepare_request(request, {"anthropic_client": MagicMock()})
    return state.kwargs


@pytest.mark.parametrize(("requested", "model", "thinking"), [
    ("claude-opus-4-8-thinking", "claude-opus-4-8", {"type": "adaptive"}),
    ("claude-opus-4-6-thinking", "claude-opus-4-6", {"type": "adaptive"}),  # 4.6 起 adaptive
    ("claude-haiku-4-5-thinking", "claude-haiku-4-5", {"type": "enabled", "budget_tokens": 4000}),
])
async def test_thinking_suffix_reads_rules_table(requested, model, thinking):
    kwargs = await _kwargs(OpenAIChatRequest(model=requested, messages=_MSGS))
    assert (kwargs["model"], kwargs["thinking"], kwargs["max_tokens"]) == (model, thinking, 16000)


@pytest.mark.parametrize(("requested", "max_tokens", "sent_model", "sent_max"), [
    ("claude-opus-4-8", 123, "claude-opus-4-8", 123),  # 請求值優先
    ("claude-opus-5-5", 128, "claude-opus-5-5", 8192),  # default_on 下限（如 IDE 行內補全）
    ("claude-opus-5-5", None, "claude-opus-5-5", 16000),  # 否則規則表預設
    ("claude-opus-4-1-20250805", None, "claude-opus-4-1-20250805", 8192),
    ("claude-opus-5-5", 32768, "claude-opus-5-5", 32768),  # kwargs 不壓（送出當下才壓，附註 4）
    ("claude-opus-4-1-20250805", 32768, "claude-opus-4-1-20250805", 32768),
    ("claude-4-5-sonnet", None, "claude-4-5-sonnet", 16000),  # 舊別名不再改寫
])
async def test_max_tokens_request_value_else_rules_default(requested, max_tokens, sent_model,
                                                          sent_max):
    kwargs = await _kwargs(
        OpenAIChatRequest(model=requested, messages=_MSGS, max_tokens=max_tokens)
    )
    assert (kwargs["model"], kwargs["max_tokens"]) == (sent_model, sent_max)
    assert "thinking" not in kwargs


@pytest.mark.parametrize(("requested", "max_tokens", "sent_max"), [
    ("claude-haiku-4-5-thinking", 2048, 5024),  # 請求值不夠放 budget＋可見輸出 → 提高
    ("claude-haiku-4-5-thinking", 32768, 32768),
    ("claude-opus-4-1-thinking", 1000, 5024),
])
async def test_budget_thinking_keeps_max_tokens_above_budget(requested, max_tokens, sent_max):
    kwargs = await _kwargs(
        OpenAIChatRequest(model=requested, messages=_MSGS, max_tokens=max_tokens)
    )
    assert (kwargs["thinking"], kwargs["max_tokens"]) == (
        {"type": "enabled", "budget_tokens": 4000}, sent_max)


async def test_native_adaptive_sends_no_budget_tokens():
    """AnthropicThinking.budget_tokens 預設 4000，原生 adaptive 不得被塞進去。"""
    request = AnthropicRequest(
        model="claude-opus-4-8", max_tokens=100, messages=_MSGS, thinking={"type": "adaptive"}
    )
    assert (await _kwargs(request))["thinking"] == {"type": "adaptive"}


def _brag_raw() -> MagicMock:
    text = MagicMock()
    text.type = "text"
    text.text = _BRAG
    return MagicMock(content=[text])


async def _honesty(model: str):
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(
        OpenAIChatRequest(model=model, messages=_MSGS), {"anthropic_client": MagicMock()}
    )
    action = adapter.mid_loop_hook(state, adapter.parse_round(state, _brag_raw()), set(), True)
    return state, action


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5"])
async def test_honesty_guard_does_not_force_on_5x(model):
    state, action = await _honesty(model)
    assert action is not None  # 仍注入矯正提示
    assert state.kwargs["messages"][-1]["content"] == guard_service.get_honesty_correction_prompt()
    assert "tool_choice" not in state.kwargs
    assert state.honesty_forced is False


async def test_honesty_guard_still_forces_on_legacy_model():
    state, action = await _honesty("claude-3-5-sonnet")
    assert action is not None
    assert state.kwargs["tool_choice"] == {"type": "any"}
