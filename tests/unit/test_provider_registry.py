"""TDD — Provider Registry SSOT（Phase 1）。

驗證路由、金鑰解析、發現清單、健康白名單全部由 `registry` 衍生，且與重構前行為等價
（Grok 回完整端點、Ollama 用字面金鑰、未知供應商乾淨報錯、官方供應商缺金鑰→離線）。
"""

from __future__ import annotations

import pytest
from src.core.exceptions import ToukaException
from src.services.dispatchers.utils import get_provider_info
from src.services.providers import registry

_ALL_KEY_ATTRS = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "LITELLM_API_KEY",
)


# --- resolve_provider 路由（關鍵字 → 供應商；具體先於泛用）---

@pytest.mark.parametrize(
    ("model", "expected_id"),
    [
        ("grok-2", "grok"),
        ("some-xai-model", "grok"),
        ("ollama-llama3.1", "ollama"),
        ("litellm-proxy", "litellm"),
        ("deepseek-chat", "deepseek"),
        ("deepseek-reasoner", "deepseek"),
        ("gpt-4o", "openai"),
        ("openai-o3", "openai"),
    ],
)
def test_resolve_provider_routes_by_keyword(model, expected_id):
    spec = registry.resolve_provider(model)
    assert spec is not None
    assert spec.id == expected_id


def test_resolve_provider_unknown_returns_none():
    assert registry.resolve_provider("totally-unknown-xyz") is None


def test_resolve_provider_native_protocols_not_routed():
    """gemini/claude 由 factory 原生協定處理器負責，不經 get_provider_info。"""
    assert registry.resolve_provider("gemini-2.5-flash") is None
    assert registry.resolve_provider("claude-opus-4") is None


# --- get_provider_info 行為等價 ---

def test_get_provider_info_unknown_raises():
    with pytest.raises(ToukaException):
        get_provider_info("nope-unknown-model")


def test_get_provider_info_grok_returns_full_endpoint(monkeypatch):
    monkeypatch.setattr("src.config.GROK_API_KEY", "gk", raising=False)
    url, key, label = get_provider_info("grok-2")
    assert label == "Grok"
    assert url.endswith("/chat/completions")  # Grok 回完整端點（形狀保留）
    assert key == "gk"


def test_get_provider_info_ollama_literal_key():
    url, key, label = get_provider_info("ollama-llama3")
    assert label == "Ollama"
    assert key == "ollama"  # 字面金鑰
    assert url  # base url 非空


def test_get_provider_info_deepseek_strips_key(monkeypatch):
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "  ds-key  ", raising=False)
    url, key, label = get_provider_info("deepseek-chat")
    assert label == "DeepSeek"
    assert key == "ds-key"  # 已去除前後空白
    assert "deepseek" in url


# --- chat 端點正規化（修：路由打到基底 .../v1 而非 /chat/completions → API Error）---

def test_normalize_chat_url_v1_base_appends_path():
    assert (registry.normalize_chat_url("https://api.deepseek.com/v1")
            == "https://api.deepseek.com/v1/chat/completions")
    assert (registry.normalize_chat_url("https://api.openai.com/v1/")
            == "https://api.openai.com/v1/chat/completions")


def test_normalize_chat_url_keeps_full_endpoint():
    full = "https://api.x.ai/v1/chat/completions"
    assert registry.normalize_chat_url(full) == full


def test_normalize_chat_url_bare_host_gets_v1():
    assert (registry.normalize_chat_url("http://localhost:4000")
            == "http://localhost:4000/v1/chat/completions")


def test_normalize_chat_url_keeps_ollama_native():
    native = "http://localhost:11434/api/chat"
    assert registry.normalize_chat_url(native) == native


def test_normalize_chat_url_empty():
    assert registry.normalize_chat_url("") == ""


def test_get_provider_info_deepseek_returns_chat_endpoint(monkeypatch):
    """回歸：DeepSeek 路由必須回完整 chat 端點（修 POST 到基底 → API Error）。"""
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "ds", raising=False)
    url, _key, label = get_provider_info("deepseek-v4-flash")
    assert label == "DeepSeek"
    assert url.endswith("/chat/completions")
    assert "/v1/v1/" not in url  # 不重複 /v1


# --- get_key 解析 ---

def test_get_key_grok_falls_back_to_xai(monkeypatch):
    monkeypatch.setattr("src.config.GROK_API_KEY", None, raising=False)
    monkeypatch.setattr("src.config.XAI_API_KEY", "xai-key", raising=False)
    grok = registry.spec_by_owned_by("xai")
    assert registry.get_key(grok) == "xai-key"


def test_get_key_empty_when_unset(monkeypatch):
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", None, raising=False)
    ds = registry.spec_by_owned_by("deepseek")
    assert registry.get_key(ds) == ""


# --- 發現清單 ---

def test_discoverable_specs_set():
    ids = {s.id for s in registry.discoverable_specs()}
    assert ids == {"gemini", "openai", "grok", "anthropic", "deepseek"}


# --- 健康白名單（owned_by → 缺金鑰即離線）---

def test_health_key_missing_official_no_key(monkeypatch):
    for attr in _ALL_KEY_ATTRS:
        monkeypatch.setattr(f"src.config.{attr}", None, raising=False)
    for owned_by in ("google", "openai", "xai", "anthropic", "deepseek"):
        assert registry.health_key_missing(owned_by) is True


def test_health_key_present_not_missing(monkeypatch):
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "k", raising=False)
    assert registry.health_key_missing("openai") is False


def test_health_endpoint_providers_not_gated():
    """Ollama/LiteLLM 為端點型供應商，不因缺金鑰被強制離線（與重構前白名單一致）。"""
    assert registry.health_key_missing("ollama") is False
    assert registry.health_key_missing("litellm") is False


def test_health_unknown_owned_by():
    assert registry.health_key_missing("touka") is False
    assert registry.health_key_missing("") is False


# --- SSOT 不變式 ---

def test_owned_by_unique():
    owned = [s.owned_by for s in registry.PROVIDER_SPECS]
    assert len(owned) == len(set(owned))


def test_generic_openai_routed_last():
    """泛用 gpt/openai 必須排在所有具體供應商之後，避免誤搶路由。"""
    routed = [s.id for s in registry.PROVIDER_SPECS if s.route_keywords]
    assert routed[-1] == "openai"
