"""TDD — 不靜默失敗：發現錯誤分類 ＋ 環境金鑰稽核（Phase 2c）。

涵蓋：HTTP 狀態 → 分類碼；官方 fetcher 在非 200 時拋分類 DiscoveryError；manager
逐供應商記錄並沙化錯誤；registry 稽核未支援金鑰與供應商金鑰狀態；POST /refresh 回報。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.models.fetcher import (
    DiscoveryError,
    _classify_status,
    fetch_openai_models,
)
from src.services.models.manager import ModelManager, _sanitize_connection_error
from src.services.providers import registry


def _make_manager() -> ModelManager:
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = []
    mgr._blacklist = []
    mgr._discovery_errors = {}
    return mgr


def _mock_client(status: int) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = status
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# --- 分類碼 ---

@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limit"), (500, "http_500")],
)
def test_classify_status(status, code):
    assert _classify_status(status) == code


# --- 官方 fetcher 在非 200 時拋分類 DiscoveryError ---

@pytest.mark.asyncio
async def test_fetch_openai_raises_auth_on_401():
    with patch(
        "src.services.models.fetcher.httpx.AsyncClient", return_value=_mock_client(401)
    ), pytest.raises(DiscoveryError) as ei:
        await fetch_openai_models("sk-bad", lambda _x: False)
    assert ei.value.code == "auth"


@pytest.mark.asyncio
async def test_fetch_openai_no_key_returns_empty_not_error():
    """無金鑰 → 回 []（非錯誤）；registry 會先 gate，不該被當失敗回報。"""
    assert await fetch_openai_models("", lambda _x: False) == []


# --- manager 逐供應商記錄並沙化 ---

@pytest.mark.asyncio
async def test_run_discovery_records_classified_error():
    mgr = _make_manager()

    async def boom():
        raise DiscoveryError("auth", "OpenAI 模型清單 HTTP 401")

    await mgr._run_discovery("openai", "OpenAI", boom())
    errs = mgr.get_discovery_errors()
    assert errs["openai"]["code"] == "auth"


@pytest.mark.asyncio
async def test_run_discovery_sanitizes_credentials_in_message():
    mgr = _make_manager()

    async def boom():
        raise DiscoveryError("error", "failed with Bearer sk-supersecret123")

    await mgr._run_discovery("x", "X", boom())
    msg = mgr.get_discovery_errors()["x"]["message"]
    assert "sk-supersecret123" not in msg
    assert "REDACTED" in msg


@pytest.mark.parametrize(
    "msg",
    [
        "connect to http://admin:supersecret@host:11434/api failed",
        "Authorization: Bearer sk-leakythis123",
        "api_key=sk-anotherleak",
    ],
)
def test_sanitize_redacts_credentials_including_url_userinfo(msg):
    out = _sanitize_connection_error(msg)
    for leak in ("supersecret", "sk-leakythis123", "sk-anotherleak"):
        assert leak not in out


@pytest.mark.asyncio
async def test_run_discovery_success_clears_prior_error():
    mgr = _make_manager()
    mgr._discovery_errors["openai"] = {"code": "auth", "message": "old"}

    async def ok():
        return []

    await mgr._run_discovery("openai", "OpenAI", ok())
    assert "openai" not in mgr.get_discovery_errors()


# --- registry 稽核 ---

def test_unknown_provider_key_names():
    names = {
        "OPENAI_API_KEY", "TAVILY_API_KEY", "FOOBAR_API_KEY",
        "DEEPSEEK_API_KEY", "RANDOM_TOKEN",
    }
    unknown = registry.unknown_provider_key_names(names)
    assert "FOOBAR_API_KEY" in unknown          # 未支援供應商金鑰 → 回報
    assert "OPENAI_API_KEY" not in unknown       # 已知供應商
    assert "DEEPSEEK_API_KEY" not in unknown     # 已知供應商
    assert "TAVILY_API_KEY" not in unknown       # 已知非模型金鑰（搜尋）
    assert "RANDOM_TOKEN" not in unknown         # 非 *_API_KEY


def test_provider_key_status(monkeypatch):
    for attr in (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "GROK_API_KEY",
        "XAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    ):
        monkeypatch.setattr(f"src.config.{attr}", None, raising=False)
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "k", raising=False)
    status = registry.provider_key_status()
    assert set(status) == {"gemini", "openai", "grok", "anthropic", "deepseek"}
    assert status["openai"] is True
    assert status["deepseek"] is False


# --- POST /refresh 回報 errors + env_audit ---

def test_refresh_response_includes_errors_and_env_audit(monkeypatch):
    owner = AgentIdentity.for_role(agent_id="O", role="Owner", is_human=True, capabilities=[])
    app.dependency_overrides[get_verified_identity] = lambda: owner
    monkeypatch.setattr(model_manager, "refresh_all_dynamic_models", AsyncMock(return_value=0))
    monkeypatch.setattr(
        model_manager, "get_discovery_errors",
        lambda: {"openai": {"code": "auth", "message": "OpenAI 模型清單 HTTP 401"}},
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/models/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["errors"]["openai"]["code"] == "auth"
        assert "providers" in body["env_audit"]
        assert "unknown_keys" in body["env_audit"]
    finally:
        app.dependency_overrides.clear()
