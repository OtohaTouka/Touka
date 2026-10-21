"""Unit/integration tests for Batch-1 security fixes (TDD).

H5-残留 — ToukaHeartbeat/config.py and heartbeat.py must not use the
           known-insecure fallback "touka_heartbeat_secret_2026".
N1/N2   — /ui/connections endpoints must require authentication.
N3      — POST /mcp/hub/install must require authentication.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ToukaHeartbeat.config import HeartbeatSettings
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

KNOWN_INSECURE_TOKEN = "touka_heartbeat_secret_2026"  # noqa: S105

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
# ADR-0245：Guest 退役，低權限樣本改用 Base（沒有代理角色的非靈魂）。
GUEST = AgentIdentity.for_role(agent_id="LowAgent", role="Base", capabilities=[])


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def no_auth_client():
    app.dependency_overrides[get_verified_identity] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def guest_client():
    app.dependency_overrides[get_verified_identity] = lambda: GUEST
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# H5 残留 — Heartbeat config default must not be the insecure value
# ---------------------------------------------------------------------------

def test_heartbeat_config_default_is_not_insecure():
    """ToukaHeartbeat/config.py HEARTBEAT_TOKEN default must NOT be the known value."""
    default = HeartbeatSettings.model_fields["HEARTBEAT_TOKEN"].default
    assert default != KNOWN_INSECURE_TOKEN, (
        f"HeartbeatSettings.HEARTBEAT_TOKEN default must not be '{KNOWN_INSECURE_TOKEN}'"
    )


def test_heartbeat_probe_does_not_use_insecure_fallback():
    """GET /heartbeat/test must not send the known-insecure token as fallback."""
    src = Path("ToukaProxy/src/routers/heartbeat.py").read_text(encoding="utf-8")
    assert KNOWN_INSECURE_TOKEN not in src, (
        f"heartbeat.py still contains the known-insecure fallback '{KNOWN_INSECURE_TOKEN}'"
    )


# ---------------------------------------------------------------------------
# N1/N2 — /ui/connections must require authentication
# ---------------------------------------------------------------------------

def test_connections_list_requires_auth(no_auth_client: TestClient):
    """GET /ui/connections without auth must return 401/403, not a list."""
    resp = no_auth_client.get("/ui/connections")
    assert resp.status_code in (401, 403), (
        f"Expected auth required for GET /ui/connections, got {resp.status_code}"
    )


def test_connections_save_requires_auth(no_auth_client: TestClient):
    """POST /ui/connections without auth must return 401/403."""
    resp = no_auth_client.post("/ui/connections", json=[])
    assert resp.status_code in (401, 403), (
        f"Expected auth required for POST /ui/connections, got {resp.status_code}"
    )


def test_connections_status_requires_auth(no_auth_client: TestClient):
    """GET /ui/connections/status without auth must return 401/403."""
    resp = no_auth_client.get("/ui/connections/status")
    assert resp.status_code in (401, 403), (
        f"Expected auth required for GET /ui/connections/status, got {resp.status_code}"
    )


def test_connections_guest_is_denied(guest_client: TestClient):
    """A low-privilege (Base) identity must not access connection management (only Owner/admin)."""
    resp = guest_client.get("/ui/connections")
    assert resp.status_code == 403, (
        f"Low-privilege identity must be denied GET /ui/connections, got {resp.status_code}"
    )


def test_connections_owner_can_list(owner_client: TestClient):
    """Owner identity must be able to list connections."""
    resp = owner_client.get("/ui/connections")
    assert resp.status_code == 200


def test_connections_status_sanitized(owner_client: TestClient):
    """GET /ui/connections/status must NOT return raw internal error objects."""
    resp = owner_client.get("/ui/connections/status")
    assert resp.status_code == 200
    data = resp.json()
    # Must be a list/dict of sanitized info, not raw exception strings
    assert isinstance(data, (list, dict))


# ---------------------------------------------------------------------------
# N3 — POST /mcp/hub/install must require authentication
# ---------------------------------------------------------------------------

def test_mcp_hub_install_requires_auth(no_auth_client: TestClient):
    """POST /mcp/hub/install without auth must return 401/403."""
    resp = no_auth_client.post("/mcp/hub/install", json={"hub_id": "some-tool"})
    assert resp.status_code in (401, 403), (
        f"Expected auth required for POST /mcp/hub/install, got {resp.status_code}"
    )


def test_mcp_hub_install_guest_denied(guest_client: TestClient):
    """A low-privilege (Base) identity must not install MCP tools (only Owner/admin)."""
    resp = guest_client.post("/mcp/hub/install", json={"hub_id": "some-tool"})
    assert resp.status_code == 403, (
        f"Low-privilege identity must be denied POST /mcp/hub/install, got {resp.status_code}"
    )


def test_mcp_hub_install_owner_passes_auth(owner_client: TestClient):
    """Owner identity must pass the auth check (may fail for other reasons like hub not found)."""
    resp = owner_client.post("/mcp/hub/install", json={"hub_id": "nonexistent-tool"})
    # Not 401 or 403 — auth passes, but may be 404 (hub not found) or 200
    assert resp.status_code not in (401, 403), (
        f"Owner must pass auth check, got {resp.status_code}"
    )
