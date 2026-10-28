"""HTTP 端點 Smoke Test — 驗證每個 Router 的 GET 端點不回傳 500。

設計原則（SSOT + SOLID + TDD）：
- SSOT：所有 Router 的 HTTP smoke test 集中於此一個檔案
- SRP：每個測試函式只驗證一個端點的基本 HTTP 健全性（不測業務邏輯）
- 使用 auth_client fixture（conftest.py SSOT，不重複定義）
- 本地 fixture 僅補充 conftest.py 中未提供的身份角色（admin / User）
"""
from __future__ import annotations

import inspect

import pytest
import src.routers.observability as _obs_module
from fastapi.testclient import TestClient
from src.main import app
from src.models.user import User
from src.routers.auth import get_current_user
from src.routers.observability import websocket_events as _websocket_events
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service

# ── 本地 Fixture（conftest.py 的補充，不替換原有定義）────────────────────────


@pytest.fixture
def admin_auth_client(mock_identity):
    """admin 角色 TestClient — 用於 _require_admin 端點（system/backups 等）。"""
    permission_service.reload_all()
    admin_identity = mock_identity(agent_id="DebugAgent", role="admin")
    identity_service.issue_token(admin_identity)
    app.dependency_overrides[get_verified_identity] = lambda: admin_identity
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def user_auth_client():
    """User 身份 TestClient — 用於 get_current_user 端點（agents, config/env）。"""
    mock_user = User(
        id="smoke-user",
        email="smoke@touka.io",
        hashed_password="hashed",  # noqa: S106
        role="admin",
        owned_agents=["DebugAgent"],
    )
    app.dependency_overrides[get_current_user] = lambda: mock_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── CORE_ROUTERS（/v1/ 前綴）────────────────────────────────────────────────

# --- agents (使用 get_current_user) ---

def test_get_agents_not_500(user_auth_client):
    """GET /v1/agents 不應回傳 500（smoke test）"""
    resp = user_auth_client.get("/v1/agents")
    assert resp.status_code != 500


def test_get_agents_returns_list(user_auth_client):
    """GET /v1/agents 應回傳 list"""
    resp = user_auth_client.get("/v1/agents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --- autonomy ---

def test_get_autonomy_not_500(auth_client):
    """GET /v1/autonomy 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/autonomy")
    assert resp.status_code != 500


def test_get_autonomy_returns_200(auth_client):
    """GET /v1/autonomy 應回傳 200"""
    resp = auth_client.get("/v1/autonomy")
    assert resp.status_code == 200


# --- bots（回傳 BotsData Pydantic model，序列化高風險端點）---

def test_get_bots_not_500(auth_client):
    """GET /v1/bots 不應回傳 500（smoke test；BotsData Pydantic model 序列化驗證）"""
    resp = auth_client.get("/v1/bots")
    assert resp.status_code != 500


def test_get_bots_has_discord_telegram(auth_client):
    """GET /v1/bots 應包含 discord / telegram 欄位"""
    resp = auth_client.get("/v1/bots")
    assert resp.status_code == 200
    data = resp.json()
    assert "discord" in data
    assert "telegram" in data


# --- config（使用 get_current_user）---

def test_get_config_env_not_500(admin_auth_client):
    """GET /v1/config/env 不應回傳 500（smoke test；DA1: admin-only）"""
    resp = admin_auth_client.get("/v1/config/env")
    assert resp.status_code != 500


def test_get_config_env_returns_200(admin_auth_client):
    """GET /v1/config/env 應回傳 200（DA1: admin-only）"""
    resp = admin_auth_client.get("/v1/config/env")
    assert resp.status_code == 200


# --- custom_models ---

def test_get_custom_models_not_500(auth_client):
    """GET /v1/ui/custom_models 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/ui/custom_models")
    assert resp.status_code != 500


def test_get_custom_models_has_models_key(auth_client):
    """GET /v1/ui/custom_models 應包含 models 欄位"""
    resp = auth_client.get("/v1/ui/custom_models")
    assert resp.status_code == 200
    assert "models" in resp.json()


# --- models ---

def test_get_models_not_500(auth_client):
    """GET /v1/models 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/models")
    assert resp.status_code != 500


def test_get_models_returns_200(auth_client):
    """GET /v1/models 應回傳 200"""
    resp = auth_client.get("/v1/models")
    assert resp.status_code == 200


# --- permissions ---

def test_get_permissions_not_500(auth_client):
    """GET /v1/permissions 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/permissions")
    assert resp.status_code != 500


def test_get_permissions_returns_200(admin_auth_client):
    """GET /v1/permissions 應回傳 200（需 admin 角色）"""
    resp = admin_auth_client.get("/v1/permissions")
    assert resp.status_code == 200


# --- skills ---

def test_get_skills_list_not_500(auth_client):
    """GET /v1/skills/list 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/skills/list")
    assert resp.status_code != 500


def test_get_skills_list_has_skills(auth_client):
    """GET /v1/skills/list 應包含 skills 欄位"""
    resp = auth_client.get("/v1/skills/list")
    assert resp.status_code == 200
    assert "skills" in resp.json()


# --- souls ---

def test_get_souls_not_500(auth_client):
    """GET /v1/souls 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/souls")
    assert resp.status_code != 500


def test_get_souls_has_souls_key(auth_client):
    """GET /v1/souls 應包含 souls 欄位（回傳 {"souls": [...]}）"""
    resp = auth_client.get("/v1/souls")
    assert resp.status_code == 200
    assert "souls" in resp.json()


# --- stats ---

def test_get_stats_not_500(auth_client):
    """GET /v1/stats 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/stats")
    assert resp.status_code != 500


def test_get_stats_returns_200(auth_client):
    """GET /v1/stats 應回傳 200"""
    resp = auth_client.get("/v1/stats")
    assert resp.status_code == 200


# --- system/sliding_window ---

def test_get_system_sliding_window_not_500(auth_client):
    """GET /v1/system/sliding_window 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/system/sliding_window")
    assert resp.status_code != 500


def test_get_system_sliding_window_returns_200(auth_client):
    """GET /v1/system/sliding_window 應回傳 200"""
    resp = auth_client.get("/v1/system/sliding_window")
    assert resp.status_code == 200


# --- system/backups（回傳 list[BackupInfo] Pydantic model，序列化高風險端點）---

def test_get_system_backups_not_500(admin_auth_client):
    """GET /v1/system/backups 不應回傳 500（smoke test；list[BackupInfo] 序列化驗證）"""
    resp = admin_auth_client.get("/v1/system/backups")
    assert resp.status_code != 500


def test_get_system_backups_returns_list(admin_auth_client):
    """GET /v1/system/backups 應回傳 list（BackupInfo Pydantic model 序列化正確）"""
    resp = admin_auth_client.get("/v1/system/backups")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --- observability ---

def test_get_system_health_not_500(auth_client):
    """GET /v1/system_health 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/system_health")
    assert resp.status_code != 500


def test_get_memory_stats_not_500(auth_client):
    """GET /v1/memory 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/memory")
    assert resp.status_code != 500


def test_get_tools_not_500(auth_client):
    """GET /v1/tools 不應回傳 500（smoke test）"""
    resp = auth_client.get("/v1/tools")
    assert resp.status_code != 500


def test_get_tools_returns_200(auth_client):
    """GET /v1/tools 應回傳 200"""
    resp = auth_client.get("/v1/tools")
    assert resp.status_code == 200


# ── APP_ROUTERS（無 /v1 前綴）───────────────────────────────────────────────

# --- ui/sessions ---

def test_get_ui_sessions_not_500(auth_client):
    """GET /ui/sessions 不應回傳 500（smoke test）"""
    resp = auth_client.get("/ui/sessions")
    assert resp.status_code != 500


def test_get_ui_sessions_returns_list(auth_client):
    """GET /ui/sessions 應回傳 list"""
    resp = auth_client.get("/ui/sessions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_workspace_settings_not_500(auth_client):
    """GET /ui/settings/workspace 不應回傳 500（smoke test）"""
    resp = auth_client.get("/ui/settings/workspace")
    assert resp.status_code != 500


def test_get_workspace_settings_returns_200(auth_client):
    """GET /ui/settings/workspace 應回傳 200"""
    resp = auth_client.get("/ui/settings/workspace")
    assert resp.status_code == 200


def test_get_ui_projects_list_not_500(auth_client):
    """GET /ui/projects/list 不應回傳 500（smoke test）"""
    resp = auth_client.get("/ui/projects/list")
    assert resp.status_code != 500


def test_get_ui_projects_list_returns_list(auth_client):
    """GET /ui/projects/list 應回傳 list"""
    resp = auth_client.get("/ui/projects/list")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_ui_mission_not_500(auth_client):
    """GET /ui/mission 不應回傳 500（smoke test；無任務時回 404，不應崩潰）"""
    resp = auth_client.get("/ui/mission")
    assert resp.status_code != 500
    # 測試沙盒中無 mission，預期 404（SESSION_NOT_FOUND）
    assert resp.status_code in {200, 404}


# --- terminal ---

def test_get_terminal_history_not_500(auth_client):
    """GET /terminal/history 不應回傳 500（smoke test）"""
    resp = auth_client.get("/terminal/history")
    assert resp.status_code != 500


def test_get_terminal_history_returns_200(auth_client):
    """GET /terminal/history 應回傳 200"""
    resp = auth_client.get("/terminal/history")
    assert resp.status_code == 200


# ── observability.py Bug 修復驗證（TDD）────────────────────────────────────
# 驗證 F821（time 未定義）與 datetime import 順序問題已修復

def test_observability_module_has_asyncio():
    """observability 模組必須在頂層 import asyncio（WebSocket handler 需要）"""
    assert hasattr(_obs_module, "asyncio"), "asyncio 必須在模組頂層 import"


def test_observability_module_has_datetime_at_module_level():
    """datetime 必須在模組頂層 import，不能在函式體內（避免 import 順序 bug）"""
    assert hasattr(_obs_module, "datetime"), "datetime 必須在模組頂層 import"


def test_websocket_handler_uses_asyncio_sleep():
    """websocket_events 應使用 asyncio.sleep 而非未定義的 time.sleep"""
    source = inspect.getsource(_websocket_events)
    assert "asyncio.sleep" in source, "WebSocket handler 必須使用 asyncio.sleep"
    assert "time.sleep" not in source, "WebSocket handler 不能使用未定義的 time.sleep"


def test_get_monitor_summary_not_500(auth_client):
    """GET /monitor/summary 不應回傳 500（smoke test）"""
    resp = auth_client.get("/monitor/summary")
    assert resp.status_code != 500


def test_get_monitor_summary_returns_200(auth_client):
    """GET /monitor/summary 應回傳 200 且包含 requests_last_hour（int）"""
    resp = auth_client.get("/monitor/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "requests_last_hour" in data
    assert isinstance(data["requests_last_hour"], int)
