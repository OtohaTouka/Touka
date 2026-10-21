"""TDD — 批次0 費率 SSOT：pricing 查表（仿 context_window_registry）。

機制測試（0a，零數字變化階段）：Rate 成本計算、override>builtin、最長子字串匹配、
未知模型→**非零** fallback（security-1：0 會使預算護欄失效）、讀檔失敗永不拋例外、
rate_source 來源標記、JSON _default 覆蓋。

具體各模型費率／本地免費供應商＝0／cache 實際計價在 0b 補。
"""
from __future__ import annotations

import pytest
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
    assert get_pricing("deepseek-v4-flash").input == 0.27
    assert get_pricing("deepseek-v4-pro").input == 0.55


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


def test_gemini_cache_billed_at_input_rate(monkeypatch):
    """Gemini 快取折扣未填（None）→ 快取讀照 input 價；prompt 已扣快取，不重複（ADR-0244）。"""
    _patch_override(monkeypatch, None)
    flash = get_pricing("gemini-2.5-flash")
    assert flash.cache_read is None
    # 0.5M 未快取 + 0.5M 快取讀，都照 input $0.30／1M → 0.15 + 0.15
    assert flash.cost_usd(500_000, 0, cache_read_tokens=500_000) == 0.3


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
    # 查不到價格的 Fable／Mythos：家族鍵，套同家族已知最高價（寧高勿低）
    ("claude-mythos-preview", Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50)),
    ("claude-fable-6", Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50)),
    # Opus 4.5 查不到：照舊 claude-opus-4（偏高）；舊型號不受影響
    ("claude-opus-4-5-20251101", Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75)),
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
