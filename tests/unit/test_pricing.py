"""TDD — 批次0 費率 SSOT：pricing 查表（仿 context_window_registry）。

機制測試（0a，零數字變化階段）：Rate 成本計算、override>builtin、最長子字串匹配、
未知模型→**非零** fallback（security-1：0 會使預算護欄失效）、讀檔失敗永不拋例外、
rate_source 來源標記、JSON _default 覆蓋。

具體各模型費率／本地免費供應商＝0／cache 實際計價在 0b 補。
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from src.services.cost_guard import worst_case_input_price
from src.services.providers import pricing
from src.services.providers.pricing import Rate, get_pricing, resolve


def _patch_override(monkeypatch, data) -> None:
    """直接以記憶體 dict 模擬 config/model_pricing.json 內容（避免檔案 IO）。"""
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _fn: data)


# ---- Rate 成本計算 ----

def test_rate_cost_input_output():
    r = Rate(input=3.0, output=15.0)
    assert r.cost_usd(1_000_000, 1_000_000) == 18.0  # 3 + 15


def test_rate_cache_none_uses_input_rate():
    """cache 費率未設(None) → 照 input 價計（用量已正規化，prompt 不含快取；ADR-0244）。"""
    r = Rate(input=3.0, output=15.0)
    assert r.cost_usd(0, 0, cache_read_tokens=1_000_000) == 3.0
    assert r.cost_usd(0, 0, cache_write_tokens=1_000_000) == 3.0


def test_rate_cache_uses_explicit_rate_when_set():
    """設了 cache 費率（Anthropic 獨立計費）→ 另外加。"""
    r = Rate(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    assert r.cost_usd(0, 0, cache_read_tokens=1_000_000) == 0.3
    assert r.cost_usd(0, 0, cache_write_tokens=1_000_000) == 3.75


# ---- 查表 ----

def test_get_pricing_returns_rate(monkeypatch):
    _patch_override(monkeypatch, None)
    assert isinstance(get_pricing("anything"), Rate)


def test_unknown_model_fallback_is_nonzero(monkeypatch):
    """security-1：未知模型 fallback 不可為 0（否則 is_over_budget 恆 False）。"""
    _patch_override(monkeypatch, None)
    r = get_pricing("totally-unknown-model-xyz")
    assert r.input > 0 and r.output > 0


def test_override_beats_builtin(monkeypatch):
    _patch_override(monkeypatch, {"pricing": {"deepseek-chat": {"input": 0.27, "output": 1.1}}})
    r = get_pricing("deepseek-chat")
    assert r.input == 0.27 and r.output == 1.1


def test_longest_substring_match(monkeypatch):
    _patch_override(monkeypatch, {"pricing": {
        "gpt-4": {"input": 30, "output": 60},
        "gpt-4o": {"input": 2.5, "output": 10},
    }})
    # gpt-4o-2024-08-06 同時含 "gpt-4" 與 "gpt-4o" → 命中較長者
    assert get_pricing("gpt-4o-2024-08-06").input == 2.5


def test_corrupt_config_never_raises(monkeypatch):
    def _boom(_fn):
        raise ValueError("corrupt")
    monkeypatch.setattr(pricing.config_utils, "read_config_json", _boom)
    assert isinstance(get_pricing("claude-x"), Rate)  # 降級、不拋例外


def test_none_model_returns_fallback(monkeypatch):
    _patch_override(monkeypatch, None)
    r, src = resolve(None)
    assert isinstance(r, Rate) and src == "fallback"


def test_resolve_reports_source(monkeypatch):
    _patch_override(monkeypatch, {"pricing": {"deepseek-chat": {"input": 0.27, "output": 1.1}}})
    assert resolve("deepseek-chat")[1] == "override"
    _patch_override(monkeypatch, None)
    assert resolve("unknown-zzz")[1] == "fallback"


def test_json_default_overrides_code_default(monkeypatch):
    _patch_override(monkeypatch, {"_default": {"input": 5.0, "output": 20.0}, "pricing": {}})
    r = get_pricing("unknown-zzz")
    assert r.input == 5.0 and r.output == 20.0


def test_malformed_entry_skipped_not_fatal(monkeypatch):
    # 缺 output 的壞項應被略過，回退 fallback，而非拋例外
    _patch_override(monkeypatch, {"pricing": {"bad-model": {"input": 1.0}}})
    assert isinstance(get_pricing("bad-model"), Rate)


# ---- 0b：內建實際費率 ----

def test_builtin_specific_rates(monkeypatch):
    """各供應商不再一律套 Claude 價：DeepSeek 遠低於 Claude Opus。"""
    _patch_override(monkeypatch, None)  # 無覆蓋 → 用內建表
    deepseek = get_pricing("deepseek-chat")
    opus = get_pricing("claude-opus-4-8[1m]")  # 含系統自身模型 ID 形態
    assert deepseek.input == 0.27 and deepseek.output == 1.10
    assert opus.input == 5.0 and opus.output == 25.0  # P24：Opus 4.8（R-3a 前誤套 $15/$75）
    assert deepseek.input < opus.input  # DeepSeek 便宜得多


def test_builtin_deepseek_v4_names(monkeypatch):
    """DeepSeek 2026 新名 v4-flash/v4-pro 有對應費率（最長匹配勝過泛用 deepseek）。"""
    _patch_override(monkeypatch, None)
    assert get_pricing("deepseek-v4-flash").input == 0.30
    assert get_pricing("deepseek-v4-pro").input == 1.32


def test_builtin_source_label(monkeypatch):
    """命中內建表時來源標 builtin（非 fallback）。"""
    _patch_override(monkeypatch, None)
    assert resolve("gpt-4o")[1] == "builtin"
    assert resolve("gemini-2.5-flash")[1] == "builtin"


def test_anthropic_cache_charged_separately(monkeypatch):
    """Anthropic 模型的 cache 為獨立計費項（與 prompt 相加）。"""
    _patch_override(monkeypatch, None)
    sonnet = get_pricing("claude-sonnet-4-6")
    assert sonnet.cache_read == 0.30
    # 1M input + 1M cache_read = 3.0 + 0.30 = 3.30
    assert sonnet.cost_usd(1_000_000, 0, cache_read_tokens=1_000_000) == 3.30


def test_gemini_cache_read_has_its_own_price(monkeypatch):
    """官方有列快取價就照列（ADR-0311）；prompt 已扣快取，不重複（ADR-0244）。"""
    _patch_override(monkeypatch, None)
    flash = get_pricing("gemini-2.5-flash")
    assert flash.cache_read == 0.03
    # 0.5M 未快取 × $0.30 ＋ 0.5M 快取讀 × $0.03
    assert flash.cost_usd(500_000, 0, cache_read_tokens=500_000) == 0.165


def test_local_model_marked_free_via_override(monkeypatch):
    """使用者可於 model_pricing.json 將自架本地模型顯式標 0（合法的免費）。"""
    _patch_override(monkeypatch, {"pricing": {"llama3.1": {"input": 0.0, "output": 0.0}}})
    r = get_pricing("llama3.1")
    assert r.input == 0.0 and r.output == 0.0
    assert r.cost_usd(5_000_000, 5_000_000) == 0.0


# ---- model-p3a（R-3a）：規則表涵蓋的新 Claude 型號各拿自己的價格 ----
# 來源：claude-api skill shared/models.md、model-migration.md（2026-10-06 快取）；快取沿用 1b
# 規則（廠商有寫就照寫，否則讀 0.1×、寫 1.25× input）。Opus 4.6／4.7 取自 skill 主文 Current
# Models 表。r55 改兩列（官方 https://platform.claude.com/docs/en/about-claude/pricing ，
# 2026-10-16）：Haiku 5.5 由「一律 >100K 檔」改成兩檔；Sonnet 5.5 快取命中 0.20→0.10（另列）。
_HAIKU_5_5_LONG = Rate(0.50, 2.50, cache_read=0.05, cache_write=0.625)
_HAIKU_5_5 = Rate(0.10, 0.50, cache_read=0.01, cache_write=0.125,
                  long_prompt=(100_000, _HAIKU_5_5_LONG))


@pytest.mark.parametrize(("model", "rate"), [
    ("claude-opus-4-8", Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25)),
    ("claude-opus-5", Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25)),
    ("claude-opus-5-5", Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0)),
    ("anthropic.claude-opus-5-5", Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0)),
    ("claude-sonnet-5", Rate(2.0, 10.0, cache_read=0.20, cache_write=2.50)),
    ("claude-sonnet-5-5", Rate(2.0, 10.0, cache_read=0.10, cache_write=2.50)),
    ("claude-sonnet-5-5-20261001", Rate(2.0, 10.0, cache_read=0.10, cache_write=2.50)),
    ("claude-haiku-5-5", _HAIKU_5_5),
    ("claude-fable-5", Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50)),
    ("claude-mythos-5", Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50)),
    ("claude-fable-5-1", Rate(10.0, 50.0, cache_read=0.25, cache_write=12.50)),
    ("claude-mythos-5-1", Rate(10.0, 50.0, cache_read=0.25, cache_write=12.50)),
    ("claude-opus-4-6", Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25)),  # skill 主文表
    ("claude-opus-4-7", Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25)),
    # 2026-11-10 官方價格頁：Opus 4.5 列出來了；Fable／Mythos 5.2 是新價
    ("claude-opus-4-5-20251101", Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25)),
    ("claude-fable-5-2", Rate(8.0, 40.0, cache_read=0.20, cache_write=10.0)),
    ("claude-mythos-5-2", Rate(8.0, 40.0, cache_read=0.20, cache_write=10.0)),
    ("claude-opus-4-1-20250805", Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75)),
    ("claude-sonnet-4-6", Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75)),
    ("claude-haiku-4-5", Rate(1.0, 5.0, cache_read=0.10, cache_write=1.25)),
])
def test_new_claude_models_get_their_own_price(monkeypatch, model, rate):
    """最長子字串比對：不落到舊的 claude-opus-4（$15/$75）或泛用 claude（$3/$15）。"""
    _patch_override(monkeypatch, None)
    assert resolve(model) == (rate, "builtin")


# ---- r55：Haiku 5.5 依提示長度分兩檔（每個請求自己判斷）----
# 提示長度＝未快取輸入＋快取讀＋快取寫；超過 100,000 才用貴檔（官方「每請求獨立計價」）。


def test_for_prompt_picks_the_long_tier_only_above_the_threshold(monkeypatch):
    _patch_override(monkeypatch, None)
    haiku = get_pricing("claude-haiku-5-5")
    assert haiku.for_prompt(0) is haiku
    assert haiku.for_prompt(100_000) is haiku
    assert haiku.for_prompt(100_001) == _HAIKU_5_5_LONG
    plain = get_pricing("claude-opus-5-5")
    assert plain.for_prompt(10_000_000) is plain  # 沒分檔的型號永遠是自己


@pytest.mark.parametrize(("usage", "expected"), [
    # 剛好 100,000（含快取讀寫）＝便宜檔：9.0 萬×0.10＋5 千×0.01＋5 千×0.125
    ((90_000, 0, 5_000, 5_000), 0.009675),
    # 多 1 個快取寫入 token＝貴檔：9.0 萬×0.50＋5 千×0.05＋5,001×0.625
    ((90_000, 0, 5_000, 5_001), 0.048376),
    # 未快取只有 6 萬，加上快取讀 5 萬＝11 萬 → 貴檔（長度要含快取）
    ((60_000, 0, 50_000, 0), 0.0325),
    # 輸出不算進提示長度：10 萬輸入＋20 萬輸出仍是便宜檔
    ((100_000, 200_000, 0, 0), 0.11),
])
def test_haiku_5_5_cost_uses_the_tier_of_that_request(monkeypatch, usage, expected):
    _patch_override(monkeypatch, None)
    pt, ct, cr, cw = usage
    cost = get_pricing("claude-haiku-5-5").cost_usd(pt, ct, cache_read_tokens=cr,
                                                    cache_write_tokens=cw)
    assert cost == pytest.approx(expected, abs=1e-6)


def test_sonnet_5_5_cache_hit_is_cheaper_than_sonnet_5(monkeypatch):
    """Sonnet 5.5 快取命中 0.10；Sonnet 5 照舊 0.20。"""
    _patch_override(monkeypatch, None)
    assert get_pricing("claude-sonnet-5-5").cache_read == 0.10
    assert get_pricing("claude-sonnet-5").cache_read == 0.20
    assert get_pricing("claude-sonnet-5-20260301").cache_read == 0.20


def test_override_rate_is_a_single_tier(monkeypatch):
    """model_pricing.json 覆寫仍是單一費率：長提示也照覆寫值算，不分檔。"""
    _patch_override(monkeypatch, {"pricing": {"claude-haiku-5-5": {"input": 0.2, "output": 1.0}}})
    rate = get_pricing("claude-haiku-5-5")
    assert rate.long_prompt is None
    assert rate.cost_usd(200_000, 0) == 0.04


# ---- ADR-0311：逐筆對官方價格頁（2026-11-10）----
# Anthropic platform.claude.com/docs/en/about-claude/pricing；
# OpenAI developers.openai.com/api/docs/pricing；
# Google aistudio.google.com/docs/pricing；xAI docs.x.ai/docs/models；
# DeepSeek api-docs.deepseek.com/quick_start/pricing（用尖峰價，寧高勿低）。
_NOV_10 = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
_DEC_1 = datetime(2026, 12, 1, tzinfo=UTC).timestamp()
_JAN_1 = datetime(2027, 1, 1, tzinfo=UTC).timestamp()
_OPENAI_LONG = 272_000  # 超過 272K
_GOOGLE_LONG = 200_000  # 超過 200K
_XAI_LONG = 199_999  # 官方是「達到 20 萬」；for_prompt 比的是超過


def _at(monkeypatch, epoch: float) -> None:
    monkeypatch.setattr(pricing, "get_current_epoch", lambda: epoch)


def _tiered(threshold: int, short: tuple, long_tier: tuple) -> Rate:
    """兩邊都是（輸入, 輸出, 快取讀[, 快取寫]）。"""
    return Rate(*short, long_prompt=(threshold, Rate(*long_tier)))


_GPT_HIGHEST = _tiered(_OPENAI_LONG, (30.0, 180.0, None), (60.0, 270.0, None))
_GROK_MID = _tiered(_XAI_LONG, (2.0, 6.0, 0.50), (4.0, 12.0, 1.0))
_GROK_BASE = _tiered(_XAI_LONG, (1.25, 2.50, 0.20), (2.50, 5.0, 0.40))


@pytest.mark.parametrize(("model", "rate"), [
    # OpenAI
    ("gpt-6-astra", _tiered(_OPENAI_LONG, (10.0, 50.0, 1.0, 12.50), (20.0, 75.0, 2.0, 25.0))),
    ("gpt-6-astra-law", _tiered(_OPENAI_LONG, (10.0, 50.0, 1.0, 12.50), (20.0, 75.0, 2.0, 25.0))),
    ("gpt-6.1-sol", _tiered(_OPENAI_LONG, (2.0, 10.0, 0.10, 2.50), (4.0, 15.0, 0.20, 5.0))),
    ("gpt-6.1-terra", _tiered(_OPENAI_LONG, (1.0, 5.0, 0.05, 1.25), (2.0, 7.50, 0.10, 2.50))),
    ("gpt-6-luna", _tiered(_OPENAI_LONG, (0.10, 0.50, 0.01, 0.125), (0.20, 0.75, 0.02, 0.25))),
    ("gpt-6-sol", _tiered(_OPENAI_LONG, (2.0, 10.0, 0.20, 2.50), (4.0, 15.0, 0.40, 5.0))),
    ("gpt-5.6-sol", _tiered(_OPENAI_LONG, (4.0, 20.0, 0.40, 5.0), (8.0, 30.0, 0.80, 10.0))),
    ("gpt-5.6-terra", _tiered(_OPENAI_LONG, (2.0, 12.0, 0.20, 2.50), (4.0, 18.0, 0.40, 5.0))),
    ("gpt-5.6-luna", _tiered(_OPENAI_LONG, (0.20, 1.20, 0.02, 0.25), (0.40, 1.80, 0.04, 0.50))),
    ("gpt-5.5-cyber-preview", Rate(12.50, 75.0, cache_read=1.25)),
    ("gpt-5.5", _tiered(_OPENAI_LONG, (5.0, 30.0, 0.50), (10.0, 45.0, 1.0))),
    ("gpt-5.5-pro", _GPT_HIGHEST),
    ("gpt-5.4", _tiered(_OPENAI_LONG, (2.50, 15.0, 0.25), (5.0, 22.50, 0.50))),
    ("gpt-5.4-pro", _GPT_HIGHEST),
    ("gpt-5.4-mini", Rate(0.75, 4.50, cache_read=0.075)),
    ("gpt-5.4-nano", Rate(0.20, 1.25, cache_read=0.02)),
    ("gpt-5.3-codex", Rate(1.75, 14.0, cache_read=0.175)),
    ("gpt-5.2", Rate(1.75, 14.0, cache_read=0.175)),
    ("gpt-5.2-pro", Rate(21.0, 168.0)),
    ("gpt-5.1", Rate(1.25, 10.0, cache_read=0.125)),
    ("gpt-5", Rate(1.25, 10.0, cache_read=0.125)),
    ("gpt-5-mini", Rate(0.25, 2.0, cache_read=0.025)),
    ("gpt-5-nano", Rate(0.05, 0.40, cache_read=0.005)),
    ("gpt-5-pro", Rate(15.0, 120.0)),
    ("chat-latest", Rate(5.0, 30.0, cache_read=0.50)),
    ("gpt-4.1", Rate(2.0, 8.0, cache_read=0.50)),
    ("gpt-4.1-mini", Rate(0.40, 1.60, cache_read=0.10)),
    ("gpt-4o", Rate(2.50, 10.0, cache_read=1.25)),
    ("gpt-4o-mini", Rate(0.15, 0.60, cache_read=0.075)),
    ("o3", Rate(2.0, 8.0, cache_read=0.50)),
    ("o3-pro", Rate(20.0, 80.0)),
    # Anthropic：Mythos Preview 不在價格頁，anthropic.com/glasswing 寫 $25/$125（快取照一般倍率）
    ("claude-mythos-preview", Rate(25.0, 125.0, cache_read=2.50, cache_write=31.25)),
    # Google
    ("gemini-4-argon", Rate(2.0, 10.0, cache_read=0.10)),
    ("gemini-3.8-flash", Rate(0.75, 3.75, cache_read=0.075)),
    ("gemini-3.6-flash", Rate(0.75, 3.75, cache_read=0.075)),
    ("gemini-3.5-flash-lite", Rate(0.30, 2.50, cache_read=0.03)),
    ("gemini-3.1-flash-lite", Rate(0.25, 1.50, cache_read=0.025)),
    ("gemini-3.1-pro-preview", _tiered(_GOOGLE_LONG, (2.0, 12.0, 0.20), (4.0, 18.0, 0.40))),
    ("gemini-3-flash-preview", Rate(0.50, 3.0, cache_read=0.05)),
    ("gemini-2.5-pro", _tiered(_GOOGLE_LONG, (1.25, 10.0, 0.125), (2.50, 15.0, 0.25))),
    ("gemini-2.5-flash", Rate(0.30, 2.50, cache_read=0.03)),
    ("gemini-2.5-flash-lite", Rate(0.10, 0.40, cache_read=0.01)),
    # xAI
    ("grok-4.7", _GROK_MID),
    ("grok-4.7-latest", _GROK_MID),
    ("grok-4.6", _GROK_MID),
    ("grok-4.5", _tiered(_XAI_LONG, (2.0, 6.0, 0.30), (4.0, 12.0, 0.60))),
    ("grok-4.3", _GROK_BASE),
    ("grok-4.20-0309-reasoning", _GROK_BASE),
    ("grok-4.20-0309-non-reasoning", _GROK_BASE),
    ("grok-4.20-multi-agent-0309", _GROK_BASE),
    ("grok-build-0.1", _tiered(_XAI_LONG, (1.0, 2.0, 0.20), (2.0, 4.0, 0.40))),
    # DeepSeek
    ("deepseek-flash", Rate(0.30, 1.20, cache_read=0.006)),
    ("deepseek-v4-flash", Rate(0.30, 1.20, cache_read=0.006)),
    ("deepseek-v4-pro", Rate(1.32, 3.96, cache_read=0.044)),
])
def test_official_price_of_each_model(monkeypatch, model, rate):
    _patch_override(monkeypatch, None)
    _at(monkeypatch, _NOV_10)
    assert resolve(model) == (rate, "builtin")


@pytest.mark.parametrize(("model", "threshold"), [
    ("gpt-6-astra", 272_000), ("gemini-3.1-pro-preview", 200_000),
])
def test_long_prompt_starts_above_the_threshold(monkeypatch, model, threshold):
    _patch_override(monkeypatch, None)
    rate = get_pricing(model)
    assert rate.for_prompt(threshold) is rate
    assert rate.for_prompt(threshold + 1) is rate.long_prompt[1]


def test_xai_long_prompt_starts_at_200k(monkeypatch):
    """xAI 官方是「達到 20 萬」就整個請求用高價，和其他家「超過」差一個 token。"""
    _patch_override(monkeypatch, None)
    rate = get_pricing("grok-4.7")
    assert rate.for_prompt(199_999) is rate
    assert rate.for_prompt(200_000) is rate.long_prompt[1]


@pytest.mark.parametrize(("model", "rate"), [
    ("claude", Rate(25.0, 125.0, cache_read=2.50, cache_write=31.25)),
    ("claude-new-thing", Rate(25.0, 125.0, cache_read=2.50, cache_write=31.25)),
    ("claude-mythos-9", Rate(25.0, 125.0, cache_read=2.50, cache_write=31.25)),
    ("claude-fable-6", Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50)),
    ("deepseek-v9", Rate(1.32, 3.96)),
    ("grok-9", Rate(4.0, 15.0)),
    ("grok-4.8", Rate(4.0, 15.0)),  # 不能被當成舊的 grok-4
    ("gemini-9-pro", Rate(4.0, 20.0)),
    ("gpt-9", _GPT_HIGHEST),
    ("gpt-5.7-sol", _GPT_HIGHEST),  # 不能被當成便宜的 gpt-5
    ("gpt-5-2025-08-07", _GPT_HIGHEST),
    ("gpt-6-cyber", _GPT_HIGHEST),
    # 開放權重模型沒有官方價，維持保守預設；一樣標成估的
    ("gpt-oss-120b", Rate(3.0, 15.0)),
])
def test_model_without_its_own_price_uses_the_family_highest(monkeypatch, model, rate):
    _patch_override(monkeypatch, None)
    assert resolve(model) == (rate, "family")


def _top_tier(rate: Rate) -> Rate:
    return rate.long_prompt[1] if rate.long_prompt is not None else rate


def _cache_prices(rate: Rate) -> tuple[float, float]:
    """實際的快取讀、寫價：沒列就是照 input 價。"""
    read = rate.input if rate.cache_read is None else rate.cache_read
    write = rate.input if rate.cache_write is None else rate.cache_write
    return read, write


@pytest.mark.parametrize("family", ["claude", "claude-fable", "claude-mythos", "deepseek", "gemini",
                                    "gpt", "grok"])
@pytest.mark.parametrize("epoch", [_NOV_10, _JAN_1])
def test_family_price_is_never_below_a_known_model_of_that_family(monkeypatch, family, epoch):
    """家族價要寧高勿低：表上同一家任何型號（含長檔、已公告的漲價）都不能比它貴。"""
    _patch_override(monkeypatch, None)
    _at(monkeypatch, epoch)
    top = _top_tier(resolve(family)[0])
    for key in pricing._BUILTIN_PRICING:
        if family in key:
            rate = _top_tier(resolve(key)[0])
            assert top.input >= rate.input, key
            assert top.output >= rate.output, key
            assert _cache_prices(top)[0] >= _cache_prices(rate)[0], key
            assert _cache_prices(top)[1] >= _cache_prices(rate)[1], key


def test_openai_cache_writes_cost_more_from_gpt_5_6(monkeypatch):
    """官方：GPT-5.6 起快取寫入是輸入價的 1.25 倍；送前最壞估算要取較貴的那個。"""
    _patch_override(monkeypatch, None)
    assert get_pricing("gpt-6.1-sol").cost_usd(0, 0, cache_write_tokens=100_000) == 0.25
    assert worst_case_input_price(get_pricing("gpt-6-astra")) == 12.50
    assert worst_case_input_price(get_pricing("gpt-6-astra").for_prompt(272_001)) == 25.0
    assert worst_case_input_price(get_pricing("gpt-5.5")) == 5.0  # 較早的型號沒有寫入加價


def test_a_price_override_still_matches_by_substring(monkeypatch):
    """gpt-5 只比完整名稱是內建表的事；自己寫的覆蓋照舊最長子字串。"""
    _patch_override(monkeypatch, {"pricing": {"gpt-5": {"input": 9.0, "output": 9.0}}})
    assert resolve("gpt-5.7-sol") == (Rate(9.0, 9.0), "override")


@pytest.mark.parametrize(("model", "raises_at", "before", "after"), [
    ("gemini-4-argon", _DEC_1, Rate(2.0, 10.0, cache_read=0.10), Rate(4.0, 20.0, cache_read=0.20)),
    ("gemini-3.8-flash", _JAN_1, Rate(0.75, 3.75, cache_read=0.075),
     Rate(1.50, 7.50, cache_read=0.15)),
    ("gemini-3.6-flash", _JAN_1, Rate(0.75, 3.75, cache_read=0.075),
     Rate(1.50, 7.50, cache_read=0.15)),
])
def test_announced_price_rise_applies_from_its_date(monkeypatch, model, raises_at, before, after):
    _patch_override(monkeypatch, None)
    _at(monkeypatch, raises_at - 1)
    assert resolve(model) == (before, "builtin")
    _at(monkeypatch, raises_at)
    assert resolve(model) == (after, "builtin")
    assert resolve(f"models/{model}-001") == (after, "builtin")  # 子字串比對到也一樣


def test_a_price_override_beats_an_announced_rise(monkeypatch):
    _patch_override(monkeypatch, {"pricing": {"gemini-4-argon": {"input": 1.0, "output": 2.0}}})
    _at(monkeypatch, _JAN_1)
    assert resolve("gemini-4-argon") == (Rate(1.0, 2.0), "override")


def test_every_announced_rise_has_a_current_price():
    assert set(pricing._SCHEDULED_PRICING) <= set(pricing._BUILTIN_PRICING)


@pytest.mark.parametrize(("model", "estimated"), [
    ("claude-opus-5-5", False), ("claude-fable-9", True), ("no-such-model", True), (None, True),
])
def test_is_estimated(monkeypatch, model, estimated):
    _patch_override(monkeypatch, None)
    assert pricing.is_estimated(model) is estimated


def test_is_estimated_false_for_a_model_with_an_override(monkeypatch):
    _patch_override(monkeypatch, {"pricing": {"no-such-model": {"input": 1.0, "output": 2.0}}})
    assert pricing.is_estimated("no-such-model") is False


@pytest.mark.parametrize(("config", "expected"), [
    (None, False),
    ({"pricing": {}}, False),
    ({"pricing": {"bad": {"input": 1.0}}}, False),  # 壞項會被略過，不算有覆蓋
    ({"pricing": {"x": {"input": 1.0, "output": 2.0}}}, True),
])
def test_has_overrides(monkeypatch, config, expected):
    _patch_override(monkeypatch, config)
    assert pricing.has_overrides() is expected


def test_verified_date_is_a_plain_date():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pricing.PRICING_VERIFIED_AT)
