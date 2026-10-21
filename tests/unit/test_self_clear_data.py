"""ADR-0107 — user self-service data clearing + project ownership (TDD).

Covers:
  - SessionService.get_project_owner / clear_projects_owned_by (service, init_db)
  - _can_access_project / _can_modify_project explicit-owner path (mocked)
  - POST /auth/clear-my-data endpoint: auth required, password gate, happy path
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.auth import get_current_user
from src.routers.ui_sessions import _can_access_project, _can_modify_project
from src.services import avatar_service
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.user_service import UserService
from src.utils.async_db import SessionService, get_async_db, init_db_async

_MSGS = {"messages": {}, "currentId": None}


def _clear():
    app.dependency_overrides.clear()


# ── service: project ownership + clear ─────────────────────────────────────

async def test_clear_projects_owned_by_deletes_settings_keeps_conversation(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        async with get_async_db() as db:
            await db.execute(
                "INSERT INTO projects (id,name,root_path,description,owner_user_id,updatedAt) "
                "VALUES (?,?,?,?,?,?)",
                ("p1", "P1", "/root1", None, "U1", 1),
            )
            await db.execute(
                "INSERT INTO projects (id,name,root_path,description,owner_user_id,updatedAt) "
                "VALUES (?,?,?,?,?,?)",
                ("p2", "P2", "/root2", None, "U2", 1),
            )
            await db.commit()
        # a conversation in p1 (agent-keyed)
        await SessionService.upsert_session(
            "s1", "t1", _MSGS, identity=AgentIdentity(agent_id="A"), project_id="p1"
        )

        cleared = await SessionService.clear_projects_owned_by("U1")
        assert cleared == 1
        # U1's project row gone; U2's untouched
        assert await SessionService.get_project_owner("p1") is None
        assert await SessionService.get_project_owner("p2") == "U2"
        # the conversation survives but is un-linked from the deleted project
        async with get_async_db() as db, db.execute(
            "SELECT project_id FROM user_sessions WHERE id=?", ("s1",)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None  # conversation kept
        assert row["project_id"] is None  # un-linked


async def test_clear_projects_owned_by_none_for_unowned(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        assert await SessionService.clear_projects_owned_by("nobody") == 0
        assert await SessionService.get_project_owner("missing") is None


# ── access predicates: explicit-owner path ─────────────────────────────────

async def test_owner_access_and_modify_path(monkeypatch):
    async def fake_owner(pid):
        return "U1" if pid == "owned" else None

    async def fake_agents(_pid):
        return []

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_owner", fake_owner)
    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.get_project_agent_ids", fake_agents
    )
    owner = AgentIdentity.for_role(agent_id="U1", role="normal", is_human=True)
    other = AgentIdentity.for_role(agent_id="U2", role="normal", is_human=True)

    assert await _can_access_project(owner, "owned") is True
    assert await _can_access_project(other, "owned") is False
    assert await _can_modify_project(owner, "owned") is True
    assert await _can_modify_project(other, "owned") is False  # owned ≠ empty-ownerless


# ── endpoint: auth + password gate + happy path ────────────────────────────

def test_clear_my_data_requires_auth():
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/clear-my-data", json={"current_password": "x"})
    assert resp.status_code in (401, 403), f"got {resp.status_code}"


def test_clear_my_data_wrong_password():
    user = AuthService().create_user("clr_wrong@touka.io", "correct_pw_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/clear-my-data", json={"current_password": "WRONG"})
    _clear()
    assert resp.status_code == 401, f"got {resp.status_code}"
    assert resp.json()["detail"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_clear_my_data_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar_service, "avatars_dir", lambda: tmp_path / "avatars")
    user = AuthService().create_user("clr_happy@touka.io", "correct_pw_123", role="normal")
    UserService().update_user_profile(user.id, {"display_name": "Name", "bio": "Bio"})
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as c:  # lifespan initializes the async DB
        resp = c.post("/auth/clear-my-data", json={"current_password": "correct_pw_123"})
    _clear()
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body["status"] == "cleared"
    assert body["cleared"]["profile"] is True
    # profile fields cleared in the user store
    updated = AuthService().get_user_by_id(user.id)
    assert updated.display_name == ""
    assert updated.bio == ""


def test_clear_my_data_avatar_failure_does_not_block_profile_clear(monkeypatch, tmp_path):
    """A locked/failing avatar file delete must NOT 500 or leave the profile half-cleared."""
    monkeypatch.setattr(avatar_service, "avatars_dir", lambda: tmp_path / "avatars")

    def _raise(*_a, **_k):
        raise OSError  # simulate a locked/unremovable avatar file

    monkeypatch.setattr("src.routers.auth.delete_avatar", _raise)
    user = AuthService().create_user("clr_avfail@touka.io", "correct_pw_123", role="normal")
    UserService().update_user_profile(user.id, {"display_name": "X", "bio": "Y"})
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as c:
        resp = c.post("/auth/clear-my-data", json={"current_password": "correct_pw_123"})
    _clear()
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    updated = AuthService().get_user_by_id(user.id)
    assert updated.display_name == ""  # profile cleared despite avatar delete failure
    assert updated.avatar_url is None


def test_list_projects_strips_owner_user_id(monkeypatch):
    """owner_user_id is an internal authz field — must not leak in the projects list."""
    async def _projects():
        return [{
            "id": "p1", "name": "P1", "root_path": "r", "description": None,
            "owner_user_id": "U-secret", "updatedAt": 1,
        }]

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_all_projects", _projects)
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="Admin", role="admin", is_human=True
    )
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/ui/projects/list")
    _clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body and all("owner_user_id" not in p for p in body)
