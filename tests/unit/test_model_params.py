"""ADR-0111 — 原生模型 per-model 取樣參數編輯（TDD）。

Covers:
  - ModelSettingsUpdate 取樣參數驗證（範圍）
  - patch_model_settings 持久化 temperature/top_p/top_k
  - NativeModelLayer：fill-when-absent、不覆蓋客戶端明確值、跳過 custom/proxy、未知模型 no-op
  - Claude 轉譯器自來源請求轉發取樣參數（修硬寫 temperature=1.0）
  - Gemini 轉譯器不再組 generationConfig（model-p0：取樣參數一律不送）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.adapters.openai_to_claude import convert_openai_to_anthropic
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.middleware.layers.native_model_layer import NativeModelLayer
from src.models.identity import AgentIdentity
from src.routers import models as models_router
from src.routers.models import ModelSettingsUpdate
from src.services.dispatchers.utils import convert_to_openai
from src.services.models import ModelManager, model_manager
from src.services.providers import registry


def _patch_native_get_models(monkeypatch, models):
    """為 NativeModelLayer 覆寫 model_manager.get_models（**patch 類別方法**）。

    嚴禁改用 ``monkeypatch.setattr(<實例>, "get_models", …)``：``get_models`` 是繼承自
    ``ModelManager`` 的類別方法，monkeypatch 還原時以 ``setattr(舊值)`` 而非 ``delattr``，
    會在**共用單例** ``model_manager`` 上殘留一個實例屬性、永久遮蔽類別方法 → 污染後續
    測試（曾致 ``test_group_chat.py::test_toukagroup_visibility_in_models`` 在某些
    pytest-randomly 次序下 ``/v1/models`` 回空、ToukaGroup 消失）。patch 類別方法時該屬性
    本就存在於類別，monkeypatch 能正確還原、不留殘影。
    """
    monkeypatch.setattr(
        ModelManager, "get_models", lambda self, _include_offline=True: list(models)
    )


def _req(**kw) -> OpenAIChatRequest:
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return OpenAIChatRequest(**base)


# ── ModelSettingsUpdate 驗證 ──────────────────────────────────────────────────

def test_settings_update_rejects_out_of_range():
    with pytest.raises(ValidationError):
        ModelSettingsUpdate(temperature=3.0)
    with pytest.raises(ValidationError):
        ModelSettingsUpdate(top_p=1.5)
    with pytest.raises(ValidationError):
        ModelSettingsUpdate(top_k=0)


def test_settings_update_accepts_valid():
    s = ModelSettingsUpdate(temperature=0.3, top_p=0.9, top_k=40)
    assert (s.temperature, s.top_p, s.top_k) == (0.3, 0.9, 40)


# ── patch_model_settings 持久化 ───────────────────────────────────────────────

def test_patch_persists_sampling_params(monkeypatch):
    monkeypatch.setattr(model_manager, "_save_config", lambda: None)
    monkeypatch.setattr(
        model_manager, "_models_cache",
        [{"id": "gpt-4o", "owned_by": "openai", "enabled": True}],
    )
    ok = model_manager.patch_model_settings(
        AgentIdentity(agent_id="gpt-4o"),
        {"temperature": 0.3, "top_p": 0.8, "top_k": 20},
    )
    assert ok
    m = next(x for x in model_manager.get_models() if x["id"] == "gpt-4o")
    assert (m["temperature"], m["top_p"], m["top_k"]) == (0.3, 0.8, 20)


def test_patch_clears_param_with_explicit_none(monkeypatch):
    monkeypatch.setattr(model_manager, "_save_config", lambda: None)
    monkeypatch.setattr(
        model_manager, "_models_cache",
        [{"id": "gpt-4o", "owned_by": "openai", "temperature": 0.3}],
    )
    model_manager.patch_model_settings(AgentIdentity(agent_id="gpt-4o"), {"temperature": None})
    m = next(x for x in model_manager.get_models() if x["id"] == "gpt-4o")
    assert m["temperature"] is None  # 顯式 None＝清除回退預設


# ── NativeModelLayer ──────────────────────────────────────────────────────────

async def test_native_layer_fills_absent_params(monkeypatch):
    _patch_native_get_models(monkeypatch, [
        {"id": "gpt-4o", "owned_by": "openai", "temperature": 0.3, "top_p": 0.8, "top_k": 20},
    ])
    req = _req()  # 客戶端未設 temp/top_p/top_k
    assert "temperature" not in req.model_fields_set
    await NativeModelLayer().process(req, {})
    assert (req.temperature, req.top_p, req.top_k) == (0.3, 0.8, 20)


async def test_native_layer_does_not_override_explicit(monkeypatch):
    _patch_native_get_models(monkeypatch, [
        {"id": "gpt-4o", "owned_by": "openai", "temperature": 0.3},
    ])
    req = _req(temperature=0.95)  # 客戶端明確設定
    await NativeModelLayer().process(req, {})
    assert req.temperature == 0.95  # 不被覆蓋


async def test_native_layer_skips_custom(monkeypatch):
    _patch_native_get_models(monkeypatch, [
        {"id": "Alice", "owned_by": "custom", "temperature": 0.3},
    ])
    req = OpenAIChatRequest(model="Alice", messages=[{"role": "user", "content": "hi"}])
    await NativeModelLayer().process(req, {})
    assert req.temperature == 1.0  # schema 預設；custom 由 CustomModelLayer 處理


async def test_native_layer_noop_unknown_model(monkeypatch):
    _patch_native_get_models(monkeypatch, [])
    req = _req()
    await NativeModelLayer().process(req, {})
    assert req.temperature == 1.0


# ── 轉譯器轉發 ─────────────────────────────────────────────────────────────────

def test_claude_converter_forwards_sampling_params():
    anthropic = convert_openai_to_anthropic(_req(temperature=0.3, top_p=0.8, top_k=20))
    assert (anthropic.temperature, anthropic.top_p, anthropic.top_k) == (0.3, 0.8, 20)


def test_gemini_converter_sends_only_output_limits():
    """model-p0（Owner 決定 A）：取樣參數一律不送；3b-U3：generationConfig 只放輸出上限與
    停止字串（請求沒帶就整段不組）。"""
    gemini_req = convert_openai_to_gemini(_req(temperature=0.3, top_p=0.8, top_k=20))
    assert "generationConfig" not in gemini_req
    gemini_req = convert_openai_to_gemini(
        _req(temperature=0.3, top_p=0.8, top_k=20, max_tokens=50, stop="END"))
    assert gemini_req["generationConfig"] == {"maxOutputTokens": 50, "stopSequences": ["END"]}


# ── 複審修復 ───────────────────────────────────────────────────────────────────

def test_convert_to_openai_forwards_top_p_k():
    """複審 #1（High）：Anthropic→OpenAI 轉換須帶 top_p/top_k（預設 UI /messages 路徑）。"""
    req = AnthropicRequest(
        model="gemini-2.5-flash", max_tokens=100,
        messages=[{"role": "user", "content": "hi"}], top_p=0.5, top_k=20,
    )
    out = convert_to_openai(req, False, "")
    assert out.top_p == 0.5
    assert out.top_k == 20


def test_anthropic_request_top_p_defaults_none():
    """複審 #4（Med）：AnthropicRequest.top_p 預設改 None，不再對每個 Claude 請求硬塞 1.0。"""
    req = AnthropicRequest(
        model="claude", max_tokens=100, messages=[{"role": "user", "content": "hi"}],
    )
    assert req.top_p is None


def test_patch_skips_sampling_for_non_native(monkeypatch):
    """複審 #8（Low）：取樣參數不持久化到 custom/proxy/soul（避免被忽略的 dead data）。"""
    cache = [{"id": "Alice", "owned_by": "custom", "enabled": True}]
    monkeypatch.setattr(model_manager, "_save_config", lambda: None)
    monkeypatch.setattr(model_manager, "_models_cache", cache)
    model_manager.patch_model_settings(
        AgentIdentity(agent_id="Alice"), {"temperature": 0.3, "enabled": False},
    )
    assert "temperature" not in cache[0]  # 取樣參數被略過
    assert cache[0]["enabled"] is False  # enabled 仍生效


# ── Phase 2：Registry 衍生取樣參數能力（provider-aware UI 用） ──────────────────

def test_param_caps_anthropic_caps_temperature_and_supports_top_k():
    editable, supports_top_k, temp_max = registry.model_param_caps("anthropic")
    assert (editable, supports_top_k, temp_max) == (True, True, 1.0)


def test_param_caps_gemini_supports_top_k_full_temperature():
    editable, supports_top_k, temp_max = registry.model_param_caps("google")
    assert (editable, supports_top_k, temp_max) == (True, True, 2.0)


def test_param_caps_openai_no_top_k_full_temperature():
    """OpenAI 相容路徑會 pop top_k → 不支援；temperature 上限 2.0。"""
    editable, supports_top_k, temp_max = registry.model_param_caps("openai")
    assert (editable, supports_top_k, temp_max) == (True, False, 2.0)


def test_param_caps_non_native_not_editable():
    """custom/proxy/touka（自訂模型／代理／靈魂）無 spec → 不可在 ModelsTab 編。"""
    for owner in ("custom", "proxy", "touka", "unknown"):
        editable, supports_top_k, temp_max = registry.model_param_caps(owner)
        assert (editable, supports_top_k, temp_max) == (False, False, 2.0)


# ── 複審 #2：原生判準單一 SSOT（is_native_owner），三處同源、無 allowlist/blocklist 漂移 ──

def test_is_native_owner_matches_registry_specs():
    for owner in ("openai", "anthropic", "google", "xai", "ollama", "litellm", "deepseek"):
        assert registry.is_native_owner(owner) is True
    # 自訂模型/代理/靈魂、以及自訂連線衍生（openrouter…）或推斷（cohere/unknown）皆非原生
    for owner in ("custom", "proxy", "touka", "openrouter", "together", "cohere", "unknown"):
        assert registry.is_native_owner(owner) is False


def test_patch_and_layer_agree_on_non_spec_owner(monkeypatch):
    """複審 #2：owned_by 無 spec（如 openrouter 自訂連線）→ 注入層與持久化皆視為非原生。"""
    # patch_model_settings 不持久化取樣參數（先前 blocklist 會誤存）
    cache = [{"id": "or-x", "owned_by": "openrouter", "enabled": True}]
    monkeypatch.setattr(model_manager, "_save_config", lambda: None)
    monkeypatch.setattr(model_manager, "_models_cache", cache)
    model_manager.patch_model_settings(AgentIdentity(agent_id="or-x"), {"temperature": 0.3})
    assert "temperature" not in cache[0]


async def test_native_layer_skips_non_spec_owner(monkeypatch):
    """複審 #2：注入層對無 spec 的 owned_by 不套用預設（與 UI 不可編一致）。"""
    _patch_native_get_models(monkeypatch, [
        {"id": "or-x", "owned_by": "openrouter", "temperature": 0.3},
    ])
    req = OpenAIChatRequest(model="or-x", messages=[{"role": "user", "content": "hi"}])
    await NativeModelLayer().process(req, {})
    assert req.temperature == 1.0  # schema 預設；未被注入


# ── 複審 #3：GET /models/{id} 能力旗標與列表端點一致（自 registry 衍生，非 schema 預設） ──

async def test_get_model_endpoint_derives_caps_from_registry(monkeypatch):
    monkeypatch.setattr(
        models_router, "get_model_metadata",
        lambda mid: {"id": "claude-x", "owned_by": "anthropic", "object": "model"},
    )
    info = await models_router.get_model("claude-x")
    assert (info.params_editable, info.supports_top_k, info.temperature_max) == (True, True, 1.0)
