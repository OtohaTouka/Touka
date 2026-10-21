"""TDD — DA3: UI session endpoints must scope to the caller (IDOR fix).

`/ui/sessions*` handlers were gated only by `_require_authenticated`, so any
valid identity could list / read / overwrite / delete *anyone's* sessions.
The fix scopes access via `resource_manager.is_owner` (the same predicate
`custom_models` uses): admin/Debugger see all; an identity only reaches
sessions for agents it owns / is.

Session rows use an owned SQLite fixture; authorization and strict snapshot reads stay real.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import _can_access_session
from src.services.identity_router import get_verified_identity
from src.utils import async_db

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
AGENT_A = AgentIdentity.for_role(agent_id="agentA", role="Base", is_human=False, capabilities=[])

SESS_A = {"id": "s_a", "agent_id": "agentA", "title": "A", "messages": [], "is_home": False}
SESS_B = {"id": "s_b", "agent_id": "agentB", "title": "B", "messages": [], "is_home": False}


@pytest.fixture(autouse=True)
def owned_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(async_db, "get_db_path", lambda: tmp_path / "sessions.db")

    async def seed():
        await async_db.init_db_async()
        for row in (SESS_A, SESS_B):
            identity = AgentIdentity.for_role(agent_id=row["agent_id"], role="Base")
            assert await async_db.SessionService.upsert_session(
                session_id=row["id"],
                title=row["title"],
                messages={"messages": {}, "currentId": None},
                identity=identity,
            )

    asyncio.run(seed())


@pytest.fixture
def agentA_client(monkeypatch):
    app.dependency_overrides[get_verified_identity] = lambda: AGENT_A
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(monkeypatch):
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- pure helper ----


def test_can_access_session_helper():
    assert _can_access_session(ADMIN, "agentB") is True
    assert _can_access_session(ADMIN, None) is True
    assert _can_access_session(AGENT_A, "agentA") is True
    assert _can_access_session(AGENT_A, "agentB") is False
    assert _can_access_session(AGENT_A, None) is False  # legacy/unowned → admin-only


# ---- list is filtered ----


def test_list_sessions_filtered_for_agent(agentA_client: TestClient):
    resp = agentA_client.get("/ui/sessions")
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()] == ["s_a"]  # must NOT see agentB's session


def test_admin_sees_all_sessions(admin_client: TestClient):
    resp = admin_client.get("/ui/sessions")
    assert resp.status_code == 200
    assert {s["id"] for s in resp.json()} == {"s_a", "s_b"}


# ---- single-session access is scoped ----


def test_get_other_agent_session_denied(agentA_client: TestClient):
    assert agentA_client.get("/ui/sessions/s_b").status_code == 403


def test_get_own_session_allowed(agentA_client: TestClient):
    assert agentA_client.get("/ui/sessions/s_a").status_code == 200


def test_admin_can_get_any_session(admin_client: TestClient):
    assert admin_client.get("/ui/sessions/s_b").status_code == 200


def test_delete_other_agent_session_denied(agentA_client: TestClient):
    assert agentA_client.delete("/ui/sessions/s_b").status_code == 403


@pytest.mark.parametrize(("session_id", "removed"), [("s_a", True), ("missing", False)])
def test_delete_confirmed_and_absent_keep_success_contract(
    agentA_client: TestClient, monkeypatch: pytest.MonkeyPatch, session_id: str, removed: bool
):
    snapshot = agentA_client.get(f"/v1/ui/sessions/{session_id}?snapshot=true")
    assert snapshot.status_code == 200
    assert snapshot.json()["exists"] is removed
    response = agentA_client.delete(
        f"/v1/ui/sessions/{session_id}", headers={"If-Match": snapshot.json()["revision"]}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success", "session_id": session_id}
    assert agentA_client.get(f"/v1/ui/sessions/{session_id}").status_code == 404


def test_delete_storage_error_is_not_success(
    agentA_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    revision = agentA_client.get("/v1/ui/sessions/s_a?snapshot=true").json()["revision"]
    original_execute = aiosqlite.Connection.execute

    async def fail_delete(
        connection: aiosqlite.Connection, sql: str, parameters: Any = None
    ) -> aiosqlite.Cursor:
        if sql.startswith("DELETE FROM user_sessions"):
            message = "synthetic-storage-error"
            raise RuntimeError(message)
        return await original_execute(connection, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_delete)
    response = agentA_client.delete("/v1/ui/sessions/s_a", headers={"If-Match": revision})
    assert response.status_code == 500
    assert "synthetic-storage-error" not in response.text
    assert agentA_client.get("/v1/ui/sessions/s_a").status_code == 200
