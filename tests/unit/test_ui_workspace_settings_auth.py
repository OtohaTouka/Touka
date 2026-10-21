"""TDD — DA4-followup: POST /ui/settings/workspace must require admin.

The global workspace root (where the server reads project files) was gated only
by `_require_authenticated`, so any authenticated user could repoint it. The
write is now admin-only; the read stays available to any authenticated identity
(the project UI needs it), mirroring /system/sliding_window.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
OWNER = AgentIdentity.for_role(agent_id="OwnerUser", role="Owner", capabilities=[])


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def owner_client():
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_update_workspace_non_admin_denied(owner_client: TestClient):
    """A non-admin (even Owner) must not change the global workspace root."""
    resp = owner_client.post(
        "/ui/settings/workspace", json={"workspace_root": "ws-test-root"}
    )
    assert resp.status_code == 403


def test_update_workspace_admin_allowed(admin_client: TestClient):
    """admin may update it — write back the current value (no-op) to avoid
    polluting other tests' workspace root."""
    current = admin_client.get("/ui/settings/workspace").json()["workspace_root"]
    resp = admin_client.post(
        "/ui/settings/workspace", json={"workspace_root": current}
    )
    assert resp.status_code == 200


def test_get_workspace_stays_authenticated(owner_client: TestClient):
    """Read stays available to any authenticated identity (UI needs it)."""
    resp = owner_client.get("/ui/settings/workspace")
    assert resp.status_code == 200
