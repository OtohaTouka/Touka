from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ToukaHeartbeat.awaken import app
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_config(tmp_path):
    """為每個測試準備獨立的暫存配置。"""
    test_config_file = tmp_path / "config.json"
    # 注入新的 storage 實體至 app
    test_storage = StorageService(test_config_file, env_token="synthetic-api-shared")
    provider = StorageTokenProvider(test_storage)

    with (
        patch("ToukaHeartbeat.awaken.storage", test_storage),
        patch("ToukaHeartbeat.awaken.token_provider", provider),
        patch("ToukaHeartbeat.awaken.proxy_client", ProxyClient(provider)),
    ):
        yield test_storage


def _schedule_headers(storage_svc) -> dict[str, str]:
    """回傳包含正確 Bearer token 的標頭字典 (C9 fix: schedule endpoints require auth)。"""
    token = storage_svc.get_token()
    return {"Authorization": f"Bearer {token}"}


def test_list_schedules_empty(setup_test_config):
    """驗證初始狀態下排程列表為空。"""
    headers = _schedule_headers(setup_test_config)
    response = client.get("/v1/schedules", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_upsert_schedule(setup_test_config):
    """驗證新增排程 API。"""
    headers = _schedule_headers(setup_test_config)
    payload = {"agent_id": "DebugAgent", "mode": "interval", "interval_minutes": 30, "active": True}
    response = client.post("/v1/schedules", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == "DebugAgent"
    assert data["id"] is not None

    # 驗證列表已更新
    list_resp = client.get("/v1/schedules", headers=headers)
    assert len(list_resp.json()) == 1


def test_handle_manual_trigger(setup_test_config):
    """驗證手動觸發接口與 Token 驗證。"""
    # 1. 測試 Token 錯誤
    fail_payload = {"token": "wrong_token", "agent_id": "DebugAgent"}
    resp_fail = client.post("/trigger", json=fail_payload)
    assert resp_fail.status_code == 401

    # 2. 測試 Token 正確 (Mock 外部調用)
    # The real shared resolver must authorize the manual and management ingress alike.
    actual_token = setup_test_config.get_token()

    mock_path = "ToukaHeartbeat.awaken.proxy_client.trigger_heartbeat"
    with patch(mock_path, new_callable=AsyncMock) as mock_trigger:
        success_payload = {"token": actual_token, "agent_id": "DebugAgent", "pulse_count": 2}
        resp_ok = client.post("/trigger", json=success_payload)
        assert resp_ok.status_code == 200
        assert resp_ok.json()["status"] == "dispatched"

        # 驗證非同步任務是否被呼叫 (由於是 asyncio.create_task，可能需要等一下或直接驗證 mock)
        # 這裡我們驗證它被調用過
        mock_trigger.assert_called()


def test_get_status():
    """驗證狀態接口。"""
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert "time_taipei" in data
