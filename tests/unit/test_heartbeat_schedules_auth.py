"""Unit tests for C9: Heartbeat schedule endpoints authentication (TDD).

C9 — GET/POST/DELETE /v1/schedules must require a valid Bearer token.
Requests without a token or with a wrong token must receive 401.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from ToukaHeartbeat import awaken
from ToukaHeartbeat.awaken import app
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

VALID_TOKEN = "test-heartbeat-token-abc123"  # noqa: S105
WRONG_TOKEN = "definitely-wrong"  # noqa: S105
MOCK_CONFIG = {
    "heartbeat_token": VALID_TOKEN,
    "schedules": [],
    "api_base_url": "http://localhost:8000",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Real owned storage and auth; no scheduler execution or outbound traffic."""
    storage = StorageService(tmp_path / "config.json", env_token=VALID_TOKEN)
    monkeypatch.setattr(awaken, "storage", storage)
    provider = StorageTokenProvider(storage)
    monkeypatch.setattr(awaken, "token_provider", provider)
    monkeypatch.setattr(awaken, "proxy_client", ProxyClient(provider))
    monkeypatch.setattr(awaken, "scheduler", AsyncIOScheduler(timezone=awaken.TAIPEI_TZ))
    with (
        # Prevent APScheduler from being started/stopped during tests
        patch("ToukaHeartbeat.awaken.scheduler.start"),
        patch("ToukaHeartbeat.awaken.scheduler.shutdown", create=True),
        TestClient(app) as c,
    ):
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# C9 — GET /v1/schedules
# ---------------------------------------------------------------------------


def test_list_schedules_requires_token(client: TestClient):
    """GET /v1/schedules without token must return 401."""
    response = client.get("/v1/schedules")
    assert response.status_code == 401


def test_list_schedules_rejects_wrong_token(client: TestClient):
    """GET /v1/schedules with wrong token must return 401."""
    response = client.get("/v1/schedules", headers=_bearer(WRONG_TOKEN))
    assert response.status_code == 401


def test_list_schedules_allows_valid_token(client: TestClient):
    """GET /v1/schedules with correct token must return 200."""
    response = client.get("/v1/schedules", headers=_bearer(VALID_TOKEN))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# C9 — POST /v1/schedules
# ---------------------------------------------------------------------------


def test_upsert_schedule_requires_token(client: TestClient):
    """POST /v1/schedules without token must return 401."""
    payload = {"agent_id": "DebugAgent", "mode": "interval", "interval_minutes": 60}
    response = client.post("/v1/schedules", json=payload)
    assert response.status_code == 401


def test_upsert_schedule_rejects_wrong_token(client: TestClient):
    """POST /v1/schedules with wrong token must return 401."""
    payload = {"agent_id": "DebugAgent", "mode": "interval", "interval_minutes": 60}
    response = client.post("/v1/schedules", json=payload, headers=_bearer(WRONG_TOKEN))
    assert response.status_code == 401


def test_upsert_schedule_allows_valid_token(client: TestClient):
    """POST /v1/schedules with correct token must return 200."""
    payload = {"agent_id": "DebugAgent", "mode": "interval", "interval_minutes": 60}
    response = client.post("/v1/schedules", json=payload, headers=_bearer(VALID_TOKEN))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# C9 — DELETE /v1/schedules/{id}
# ---------------------------------------------------------------------------


def test_delete_schedule_requires_token(client: TestClient):
    """DELETE /v1/schedules/{id} without token must return 401."""
    response = client.delete("/v1/schedules/some-id")
    assert response.status_code == 401


def test_delete_schedule_rejects_wrong_token(client: TestClient):
    """DELETE /v1/schedules/{id} with wrong token must return 401."""
    response = client.delete("/v1/schedules/some-id", headers=_bearer(WRONG_TOKEN))
    assert response.status_code == 401


def test_delete_schedule_allows_valid_token(client: TestClient):
    """DELETE /v1/schedules/{id} with correct token must return 200."""
    created = client.post(
        "/v1/schedules",
        json={"id": "some-id", "agent_id": "DebugAgent"},
        headers=_bearer(VALID_TOKEN),
    )
    assert created.status_code == 200
    response = client.delete("/v1/schedules/some-id", headers=_bearer(VALID_TOKEN))
    assert response.status_code == 200
