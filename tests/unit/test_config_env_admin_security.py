"""TDD — DA1: /config/env must require admin (was: any get_current_user).

GET/POST /config/env expose/rewrite the live .env (incl. TOUKA_AUTH_SECRET,
the JWT signing key). Before the fix they were gated only by get_current_user
(no role check), so any authenticated non-admin could read the signing secret.
After the fix both endpoints require role == "admin".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.config import _MASK, _mask_value
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
NORMAL = AgentIdentity.for_role(
    agent_id="NormalUser", role="normal", is_human=True, capabilities=[]
)


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def normal_client():
    app.dependency_overrides[get_verified_identity] = lambda: NORMAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def no_auth_client():
    app.dependency_overrides[get_verified_identity] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- GET /config/env ----

def test_config_env_get_admin_allowed(admin_client: TestClient):
    """admin may read config (read-only)."""
    assert admin_client.get("/config/env").status_code == 200


def test_config_env_get_normal_user_denied(normal_client: TestClient):
    """A normal authenticated user must NOT read .env secrets."""
    assert normal_client.get("/config/env").status_code == 403


def test_config_env_get_no_auth_denied(no_auth_client: TestClient):
    assert no_auth_client.get("/config/env").status_code in (401, 403)


# ---- POST /config/env (deny-only; we never write the real .env in tests) ----

def test_config_env_post_normal_user_denied(normal_client: TestClient):
    resp = normal_client.post("/config/env", json={"settings": {"X": "1"}})
    assert resp.status_code == 403


def test_config_env_post_no_auth_denied(no_auth_client: TestClient):
    resp = no_auth_client.post("/config/env", json={"settings": {}})
    assert resp.status_code in (401, 403)


# ---- 遮罩純函式 _mask_value（#1：GET 不再明文洩漏金鑰） ----

@pytest.mark.parametrize(
    "key",
    [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "TOUKA_AUTH_SECRET",
        "DISPATCH_TOKEN",
        "DB_PASSWORD",
        "api_key",
    ],
)
def test_mask_value_masks_secret_when_set(key):
    """secret 類 key（API_KEY/SECRET/TOKEN/PASSWORD）有值 → 遮罩。"""
    assert _mask_value(key, "super-secret-value") == _MASK


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "TOUKA_AUTH_SECRET"])
def test_mask_value_empty_not_masked(key):
    """未設定（空值）→ 維持空（前端據此判斷未設定）。"""
    assert _mask_value(key, "") == ""


@pytest.mark.parametrize("key", ["EMBEDDING_MODEL", "ENABLE_DYNAMIC_MODELS", "LOG_LEVEL"])
def test_mask_value_non_secret_passthrough(key):
    """非 secret 設定值 → 原樣回傳（明文）。"""
    assert _mask_value(key, "some-value") == "some-value"


def _write_env_pair(tmp_path, monkeypatch, env_lines: str, example_lines: str):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    env.write_text(env_lines, encoding="utf-8")
    example.write_text(example_lines, encoding="utf-8")
    # root-cause-A 修復：config 端點改走 resolve_env_file_path()（SSOT），不再用硬編碼的
    # cwd 相對 ".env"。以 TOUKA_ENV_FILE 顯式覆寫指向 tmp .env；.env.example 為其同目錄 sibling。
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))
    return env


# ---- GET 遮罩 secret 值（整合，tmp env） ----

def test_config_env_get_masks_secrets(admin_client: TestClient, tmp_path, monkeypatch):
    _write_env_pair(
        tmp_path,
        monkeypatch,
        env_lines="ANTHROPIC_API_KEY=sk-secret-123\nEMBEDDING_MODEL=multilingual-e5\n",
        example_lines=(
            "# --- API Keys ---\nANTHROPIC_API_KEY=\n"
            "# --- Models ---\nEMBEDDING_MODEL=\n"
        ),
    )
    defs = {d["key"]: d["value"] for d in admin_client.get("/config/env").json()["definitions"]}
    assert defs["ANTHROPIC_API_KEY"] == _MASK             # 金鑰遮罩
    assert "sk-secret-123" not in defs["ANTHROPIC_API_KEY"]
    assert defs["EMBEDDING_MODEL"] == "multilingual-e5"   # 非 secret 維持明文


# ---- POST 跳過遮罩值（縱深防禦：遮罩被回送不覆寫真金鑰） ----

def test_config_env_post_skips_mask_token(admin_client: TestClient, tmp_path, monkeypatch):
    env = _write_env_pair(
        tmp_path,
        monkeypatch,
        env_lines="ANTHROPIC_API_KEY=sk-real-existing\n",
        example_lines="# --- API Keys ---\nANTHROPIC_API_KEY=\nOPENAI_API_KEY=\n",
    )
    # 新的 provider 金鑰會觸發即時探測（真的連 api.openai.com）；探測不是本測試的對象。
    probe = AsyncMock(return_value={"OPENAI_API_KEY": "valid"})
    monkeypatch.setattr("src.routers.config.model_manager.probe_provider_keys", probe)
    resp = admin_client.post(
        "/config/env",
        json={"settings": {"ANTHROPIC_API_KEY": _MASK, "OPENAI_API_KEY": "sk-new"}},
    )
    assert resp.status_code == 200
    probe.assert_awaited_once_with({"OPENAI_API_KEY"})
    written = env.read_text(encoding="utf-8")
    assert "sk-real-existing" in written       # 遮罩值未覆寫既有真金鑰
    assert _MASK not in written                # 遮罩值從不落盤
    assert "OPENAI_API_KEY=sk-new" in written  # 真正新值有寫入


# ---- root-cause-A 回歸：POST 寫入 resolve_env_file_path()（可經 TOUKA_ENV_FILE 指向
#      持久化路徑），而非硬編碼 cwd 相對 '.env'（docker 下＝容器暫存 /app/ToukaProxy/.env、
#      宿主 .env 永不更新、容器重建即金鑰蒸發） ----

def test_config_env_post_writes_to_resolved_env_path(
    admin_client: TestClient, tmp_path, monkeypatch
):
    """金鑰/設定寫入 resolve_env_file_path() 解析出的持久化路徑，讀寫同源。"""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")  # 檔案存在但空
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))

    # 用非 provider 設定鍵（避開即時套用/探測網路路徑，純驗寫入目標）。
    resp = admin_client.post(
        "/config/env",
        json={"settings": {"EMBEDDING_MODEL": "multilingual-e5-base"}},
    )
    assert resp.status_code == 200
    assert "EMBEDDING_MODEL=multilingual-e5-base" in env.read_text(encoding="utf-8")
