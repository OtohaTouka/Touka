"""三家 adapter 用量正規化契約（ADR-0244；model-p1b U1）。

``extract_usage`` 一律回 ``(四欄 dict, 本輪總輸入, 輸出)``：

- 四欄＝未快取輸入 ``prompt_tokens``、快取讀 ``cache_read_input_tokens``、快取寫
  ``cache_creation_input_tokens``、輸出 ``completion_tokens``（＝token_usage.json 既有鍵，
  P20 不加欄位）；
- 本輪總輸入＝三種輸入相加（context 佔用）。

廠商方言：Anthropic 的 ``input_tokens`` 本來就不含快取；OpenAI 的 ``prompt_tokens`` 含
``prompt_tokens_details.cached_tokens``；Gemini 的 ``promptTokenCount`` 含
``cachedContentTokenCount``。期望值一律寫字面值。
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.types import Usage
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.identity import AgentIdentity
from src.services.providers import pricing
from src.services.resource_manager import resource_manager
from src.services.telemetry import accumulate_token_usage

_CLAUDE_STATE = SimpleNamespace(is_native_anthropic=False)
# 費率表只寫 input／output、沒寫快取價（OpenAI／Gemini 內建表即是此形）。
_PRICING = {
    "_default": {"input": 3.0, "output": 15.0},
    "pricing": {"norm-test-model": {"input": 3.0, "output": 15.0}},
}


def _expected(prompt: int, cache_read: int, cache_write: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "completion_tokens": completion,
    }


def _stats(ident: AgentIdentity) -> dict:
    path = Path(resource_manager.get_stats_path(ident)) / "token_usage.json"
    return resource_manager.read_json(path)


# ── Claude ──────────────────────────────────────────────────────────────────


def test_claude_without_cache_sdk_none_fields_become_zero():
    """真 SDK Usage 沒有快取時欄位是 None → 一律 0。"""
    raw = SimpleNamespace(usage=Usage(input_tokens=120, output_tokens=30))
    assert ClaudeAdapter().extract_usage(_CLAUDE_STATE, raw) == (
        _expected(120, 0, 0, 30), 120, 30,
    )


def test_claude_with_cache_input_tokens_already_excludes_cache():
    """Anthropic 的 input_tokens 不含快取：不扣，總輸入＝三者相加。"""
    raw = SimpleNamespace(usage=Usage(
        input_tokens=50, cache_read_input_tokens=9000,
        cache_creation_input_tokens=1200, output_tokens=40,
    ))
    assert ClaudeAdapter().extract_usage(_CLAUDE_STATE, raw) == (
        _expected(50, 9000, 1200, 40), 10250, 40,
    )


# ── OpenAI 相容 ─────────────────────────────────────────────────────────────


def test_openai_without_cache():
    raw = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    assert OpenAIAdapter().extract_usage(None, raw) == (_expected(100, 0, 0, 20), 100, 20)


def test_openai_with_cache_subtracts_cached_tokens():
    raw = {"usage": {
        "prompt_tokens": 2000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 1536},
    }}
    assert OpenAIAdapter().extract_usage(None, raw) == (
        _expected(464, 1536, 0, 50), 2000, 50,
    )


def test_openai_null_details_counts_as_no_cache():
    """相容廠商常送 prompt_tokens_details: null。"""
    raw = {"usage": {
        "prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": None,
    }}
    assert OpenAIAdapter().extract_usage(None, raw) == (_expected(100, 0, 0, 20), 100, 20)


def test_openai_cached_never_exceeds_prompt():
    """快取量大於 prompt（異常回報）時夾在 prompt：總輸入恆等於廠商的 prompt_tokens。"""
    raw = {"usage": {
        "prompt_tokens": 100, "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 300},
    }}
    assert OpenAIAdapter().extract_usage(None, raw) == (_expected(0, 100, 0, 1), 100, 1)


def test_openai_leaves_raw_usage_untouched():
    """終端 chunk 原樣帶出 raw["usage"]（golden 把關）→ 正規化不得改寫 raw。"""
    raw = {"usage": {
        "prompt_tokens": 2000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 1536},
    }}
    before = copy.deepcopy(raw)
    OpenAIAdapter().extract_usage(None, raw)
    assert raw == before


@pytest.mark.parametrize("raw", [{}, {"usage": None}, {"usage": {}}])
def test_openai_missing_usage_skips_telemetry(raw):
    assert OpenAIAdapter().extract_usage(None, raw) == ({}, 0, 0)


# ── Gemini ──────────────────────────────────────────────────────────────────


def test_gemini_without_cache():
    raw = {"usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 12}}
    assert GeminiAdapter().extract_usage(None, raw) == (_expected(80, 0, 0, 12), 80, 12)


def test_gemini_with_implicit_cache_subtracts_cached_content():
    """Google 自動快取：promptTokenCount 含 cachedContentTokenCount，扣除後才是未快取。"""
    raw = {"usageMetadata": {
        "promptTokenCount": 5000, "cachedContentTokenCount": 4096,
        "candidatesTokenCount": 30,
    }}
    assert GeminiAdapter().extract_usage(None, raw) == (
        _expected(904, 4096, 0, 30), 5000, 30,
    )


def test_gemini_missing_usage_metadata_skips_telemetry():
    assert GeminiAdapter().extract_usage(None, {}) == ({}, 0, 0)


# ── 入帳（token_usage.json；conftest 沙盒）────────────────────────────────


def test_openai_cached_tokens_recorded_apart_and_still_billed(monkeypatch):
    """快取 token 進快取讀欄、不再混在 prompt；費率表沒寫快取價時照 input 價計，不會變免費。"""
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _fn: _PRICING)
    ident = AgentIdentity.for_role(agent_id="UsageNormOpenAI", role="Owner")
    raw = {"usage": {
        "prompt_tokens": 1_000_000, "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 400_000},
    }}
    usage, _pt, _ct = OpenAIAdapter().extract_usage(None, raw)
    accumulate_token_usage(ident, "OpenAI", usage, model="norm-test-model")

    stats = _stats(ident)["OpenAI"]
    assert stats["prompt_tokens"] == 600_000
    assert stats["cache_read_input_tokens"] == 400_000
    assert stats["cache_creation_input_tokens"] == 0
    assert stats["total_cost_usd"] == 3.0


def test_claude_sdk_usage_without_cache_is_recorded(monkeypatch):
    """真 SDK Usage（快取欄位 None）照常入帳；None 直接進累計會 TypeError、整輪漏記。"""
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _fn: _PRICING)
    ident = AgentIdentity.for_role(agent_id="UsageNormClaude", role="Owner")
    raw = SimpleNamespace(usage=Usage(input_tokens=1_000_000, output_tokens=0))
    usage, _pt, _ct = ClaudeAdapter().extract_usage(_CLAUDE_STATE, raw)
    accumulate_token_usage(ident, "Anthropic", usage, model="norm-test-model")

    stats = _stats(ident)["Anthropic"]
    assert stats["prompt_tokens"] == 1_000_000
    assert stats["cache_read_input_tokens"] == 0
    assert stats["total_cost_usd"] == 3.0
