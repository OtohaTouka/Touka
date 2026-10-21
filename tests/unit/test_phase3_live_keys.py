"""TDD — Phase 3：金鑰即時生效（免重啟）＋ 存檔探測 ＋ 反向生命週期。

涵蓋：apply_runtime_env 使 registry.get_key 立即讀到新值；probe_provider_keys 回
valid/invalid:code/cleared 並自動上架模型；POST /config/env 存 provider 金鑰回傳 key_probe；
清除金鑰 → get_models 自動將該供應商模型標離線（反向生命週期）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import src.config as cfg
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.models.fetcher import DiscoveryError
from src.services.models.manager import ModelManager
from src.services.providers import registry
from src.utils.time_utils import get_current_epoch


def _make_manager(cache: list | None = None) -> ModelManager:
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = list(cache or [])
    mgr._blacklist = []
    mgr._discovery_errors = {}
    return mgr


# --- apply_runtime_env：金鑰即時生效（免重啟） ---

def test_apply_runtime_env_makes_key_live(monkeypatch):
    ds = registry.spec_by_id("deepseek")
    # 讓 monkeypatch 先記錄三處原值，apply_runtime_env 變更後由 teardown 還原
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", None, raising=False)
    monkeypatch.setattr(cfg.settings, "DEEPSEEK_API_KEY", None, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    assert registry.get_key(ds) == ""
    cfg.apply_runtime_env({"DEEPSEEK_API_KEY": "sk-live"})
    assert registry.get_key(ds) == "sk-live"      # 免重啟，立即生效
    cfg.apply_runtime_env({"DEEPSEEK_API_KEY": ""})
    assert registry.get_key(ds) == ""             # 清除亦即時


# --- probe_provider_keys：valid / invalid / cleared ---

@pytest.mark.asyncio
async def test_probe_valid_discovers_models(monkeypatch):
    mgr = _make_manager()
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "sk-good", raising=False)
    with patch(
        "src.services.models.manager.fetch_deepseek_models",
        new_callable=AsyncMock,
        return_value=[{"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"}],
    ), patch("src.services.models.manager.config_utils"):
        res = await mgr.probe_provider_keys({"DEEPSEEK_API_KEY"})
    assert res["DEEPSEEK_API_KEY"] == "valid"
    assert any(m["id"] == "deepseek-chat" for m in mgr._models_cache)  # 自動上架


@pytest.mark.asyncio
async def test_probe_invalid_auth(monkeypatch):
    mgr = _make_manager()
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "sk-bad", raising=False)
    with patch(
        "src.services.models.manager.fetch_deepseek_models",
        new_callable=AsyncMock,
        side_effect=DiscoveryError("auth", "DeepSeek 模型清單 HTTP 401"),
    ), patch("src.services.models.manager.config_utils"):
        res = await mgr.probe_provider_keys({"DEEPSEEK_API_KEY"})
    assert res["DEEPSEEK_API_KEY"] == "invalid:auth"


@pytest.mark.asyncio
async def test_probe_cleared(monkeypatch):
    mgr = _make_manager()
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", None, raising=False)
    res = await mgr.probe_provider_keys({"DEEPSEEK_API_KEY"})
    assert res["DEEPSEEK_API_KEY"] == "cleared"


# --- 反向生命週期：清除金鑰 → 模型自動離線 ---

def test_cleared_key_marks_models_offline(monkeypatch):
    cache = [{"id": "deepseek-chat", "owned_by": "deepseek", "last_seen": get_current_epoch()}]
    mgr = _make_manager(cache)
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", None, raising=False)
    models = mgr.get_models()
    ds = next(m for m in models if m["id"] == "deepseek-chat")
    assert ds["status"] == "offline"  # 金鑰缺失 → health_key_missing → 離線


# --- POST /config/env：存 provider 金鑰回傳 key_probe ---

def test_config_env_save_provider_key_returns_probe(monkeypatch, tmp_path):
    admin = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True, capabilities=[])
    app.dependency_overrides[get_verified_identity] = lambda: admin
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))  # SSOT：config 端點改走 resolve_env_file_path()
    monkeypatch.setattr("src.routers.config.apply_runtime_env", lambda _updates: None)
    monkeypatch.setattr(
        model_manager, "probe_provider_keys",
        AsyncMock(return_value={"DEEPSEEK_API_KEY": "valid"}),
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/config/env", json={"settings": {"DEEPSEEK_API_KEY": "sk-new"}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["key_probe"]["DEEPSEEK_API_KEY"] == "valid"
        assert "sk-new" in env.read_text(encoding="utf-8")  # 真值落盤
    finally:
        app.dependency_overrides.clear()


def test_config_env_non_provider_key_no_probe(monkeypatch, tmp_path):
    """存非 provider 設定 → 不觸發探測（無 key_probe），訊息維持「需重啟」。"""
    admin = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True, capabilities=[])
    app.dependency_overrides[get_verified_identity] = lambda: admin
    env = tmp_path / ".env"
    env.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))  # SSOT：config 端點改走 resolve_env_file_path()
    try:
        with TestClient(app) as client:
            resp = client.post("/config/env", json={"settings": {"LOG_LEVEL": "DEBUG"}})
        assert resp.status_code == 200
        assert "key_probe" not in resp.json()
    finally:
        app.dependency_overrides.clear()
