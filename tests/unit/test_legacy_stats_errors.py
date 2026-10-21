"""Legacy stats and sync endpoints report failures as errors; viewing creates nothing."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from fastapi.testclient import TestClient
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

ADMIN = AgentIdentity.for_role(agent_id="admin_user", role="admin", is_human=True)


class _SyntheticSyncFailure(Exception):
    """A failure raised by the patched sync service."""


@pytest.fixture(autouse=True)
def _as_admin() -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    yield
    app.dependency_overrides.clear()


def _sync() -> Any:
    return create_autospec(LegacySyncService, instance=True)


def _patched_manager(sync: Any) -> Any:
    manager = create_autospec(MemoryManager, instance=True)
    manager.get_legacy_sync_service.return_value = sync
    return patch("memory_server.services.manager.MemoryManager", return_value=manager)


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_stats_legacy_error_not_200() -> None:
    sync = _sync()
    sync.get_store.side_effect = _SyntheticSyncFailure
    with _patched_manager(sync):
        response = _client().get("/v1/stats/legacy/DebugAgent")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"


def test_sync_exception_not_200() -> None:
    sync = _sync()
    sync.sync_all_json_to_db.side_effect = _SyntheticSyncFailure
    with _patched_manager(sync):
        response = _client().post("/v1/stats/legacy/DebugAgent/sync")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "SYNC_FAILED"


def test_sync_partial_failure_not_200() -> None:
    result = {"synced": 1, "errors": 1, "total_in_db": {"session_count": 1, "message_count": 1}}
    sync = _sync()
    sync.sync_all_json_to_db.return_value = result
    with _patched_manager(sync):
        response = _client().post("/v1/stats/legacy/DebugAgent/sync")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "SYNC_FAILED"
    assert (detail["synced"], detail["errors"]) == (1, 1)


def test_stats_get_does_not_create_legacy_dir_or_db() -> None:
    agent = f"StatsProbe{uuid.uuid4().hex[:8]}"
    legacy_dir = Path(resource_manager.get_vault_path("legacy", AgentIdentity.from_str(agent)))

    response = _client().get(f"/v1/stats/legacy/{agent}")

    assert response.status_code == 200
    body = response.json()
    assert (body["message_count"], body["session_count"]) == (0, 0)
    assert not legacy_dir.exists()
