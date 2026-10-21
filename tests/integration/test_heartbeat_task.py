"""Integration test for Heartbeat and Autonomous Tasks."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from src.models.user import User
from src.routers.heartbeat import identity_service, permission_service
from src.services.auth_service import AuthService

# P23 fix: use a test-only token instead of the cleared .env value
_TEST_HEARTBEAT_TOKEN = "test_heartbeat_token_for_ci"


@pytest.mark.asyncio
async def test_heartbeat_triggers_autonomous_pulse(auth_client, mock_identity, monkeypatch):
    """驗證 /v1/heartbeat/awaken 能成功啟動帶有正確身分的背景任務。"""
    # P23 fix: patch HEARTBEAT_TOKEN so the cleared .env value doesn't break the test
    monkeypatch.setattr("src.routers.heartbeat.HEARTBEAT_TOKEN", _TEST_HEARTBEAT_TOKEN)

    # 1. Mock 核心執行函數
    mock_run_pulse = AsyncMock(return_value=1)
    monkeypatch.setattr("src.routers.heartbeat._process_heartbeat_loop", mock_run_pulse)

    # 2. Mock 權限與身分
    debug_id = mock_identity(agent_id="DebugAgent", capabilities=["heartbeat_awakening"])
    monkeypatch.setattr(identity_service, "get_identity", lambda x: debug_id)
    monkeypatch.setattr(permission_service, "can_awaken", lambda x: True)
    # ADR-0250 A3：沒有主要模型的靈魂 /awaken 回 409；這裡測的是背景任務，先給主要模型
    monkeypatch.setattr("src.routers.heartbeat.get_active_brain", lambda _identity: "claude-x")

    # 2.5 Mock AuthService
    mock_users = [
        User(id="owner_id", email="owner@test.com", hashed_password="...", owned_agents=["DebugAgent"], refresh_token_exp=9999999999)
    ]
    monkeypatch.setattr(AuthService, "get_all_users", lambda self: mock_users)

    # 3. 發送喚醒請求
    payload = {
        "token": _TEST_HEARTBEAT_TOKEN,
        "agent_id": "DebugAgent",
        "pulse_count": 1,
        "init_prompt": "Wake up!"
    }
    
    response = auth_client.post("/v1/heartbeat/awaken", json=payload)
    assert response.status_code == 202
    
    await asyncio.sleep(0.1)
    assert mock_run_pulse.called

def test_heartbeat_unauthorized_agent(auth_client, mock_identity, monkeypatch):
    """驗證不具備 heartbeat_awakening 能力的身分無法啟動心跳。"""
    monkeypatch.setattr("src.routers.heartbeat.HEARTBEAT_TOKEN", _TEST_HEARTBEAT_TOKEN)
    civilian = mock_identity(agent_id="Civilian", role="Base", capabilities=[])
    monkeypatch.setattr(identity_service, "get_identity", lambda x: civilian)
    monkeypatch.setattr(permission_service, "can_awaken", lambda x: False)

    # 2.5 Mock AuthService
    mock_users = [
        User(id="owner_id", email="owner@test.com", hashed_password="...", owned_agents=["Civilian"], refresh_token_exp=9999999999)
    ]
    monkeypatch.setattr(AuthService, "get_all_users", lambda self: mock_users)

    payload = {
        "token": _TEST_HEARTBEAT_TOKEN,
        "agent_id": "Civilian",
        "pulse_count": 1
    }
    
    response = auth_client.post("/v1/heartbeat/awaken", json=payload)
    
    assert response.status_code == 403
    assert "未獲得心跳喚醒授權" in response.json()["detail"]["message"]
