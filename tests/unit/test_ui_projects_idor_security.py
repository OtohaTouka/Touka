"""TDD — DA3 follow-up: /ui/projects* and legacy export must scope to the caller.

Projects have no owner column; ownership is derived from their sessions
(`user_sessions.project_id` → `agent_id`). These endpoints previously leaked or
allowed modification of any project — `get_project_files` is the worst (any
authenticated identity could read an arbitrary directory tree on disk).

Mocked at the service boundary (`get_all_projects` / `get_project_agent_ids`) so
no DB seeding is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import _can_access_project, _can_modify_project
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
AGENT_A = AgentIdentity.for_role(agent_id="agentA", role="Base", is_human=False, capabilities=[])

PROJECTS = [
    {"id": "p_a", "name": "PA", "root_path": "pa-root"},
    {"id": "p_b", "name": "PB", "root_path": "pb-root"},
]
PROJECT_AGENTS = {"p_a": ["agentA"], "p_b": ["agentB"]}


async def _fake_all_projects():
    return [dict(p) for p in PROJECTS]


async def _fake_project_agents(project_id):
    return list(PROJECT_AGENTS.get(project_id, []))


def _patch(monkeypatch):
    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.get_all_projects", _fake_all_projects
    )
    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.get_project_agent_ids",
        _fake_project_agents,
    )


@pytest.fixture
def agentA_client(monkeypatch):
    _patch(monkeypatch)
    app.dependency_overrides[get_verified_identity] = lambda: AGENT_A
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(monkeypatch):
    _patch(monkeypatch)
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- ownership predicates ----


@pytest.mark.asyncio
async def test_project_access_predicates(monkeypatch):
    _patch(monkeypatch)
    # read scope
    assert await _can_access_project(ADMIN, "p_b") is True
    assert await _can_access_project(AGENT_A, "p_a") is True
    assert await _can_access_project(AGENT_A, "p_b") is False
    assert await _can_access_project(AGENT_A, "p_empty") is False  # no owner → admin-only
    # write scope: empty project has no owner to protect
    assert await _can_modify_project(AGENT_A, "p_empty") is True
    assert await _can_modify_project(AGENT_A, "p_b") is False
    assert await _can_modify_project(ADMIN, "p_b") is True


# ---- list filtering ----


def test_list_projects_filtered(agentA_client: TestClient):
    resp = agentA_client.get("/ui/projects/list")
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == ["p_a"]


def test_admin_lists_all_projects(admin_client: TestClient):
    resp = admin_client.get("/ui/projects/list")
    assert {p["id"] for p in resp.json()} == {"p_a", "p_b"}


# ---- overview / files IDOR (files = arbitrary directory disclosure) ----


def test_project_overview_cross_owner_denied(agentA_client: TestClient):
    assert agentA_client.get("/ui/projects/p_b/overview").status_code == 403


def test_project_files_cross_owner_denied(agentA_client: TestClient):
    assert agentA_client.get("/ui/projects/p_b/files").status_code == 403


def test_project_files_own_passes_access(agentA_client: TestClient):
    # access granted for the owned project → past the 403 gate (404: project row
    # absent in the test DB, which is fine — we only assert the gate opened).
    assert agentA_client.get("/ui/projects/p_a/files").status_code != 403


# ---- delete scoped ----


def test_delete_other_owner_project_denied(agentA_client: TestClient):
    assert agentA_client.delete("/ui/projects/p_b").status_code == 403


# ---- legacy export scoped to the target agent ----


def test_legacy_export_cross_owner_denied(agentA_client: TestClient):
    body = {"agent_id": "agentB", "filename": "x.json", "json_content": []}
    resp = agentA_client.post("/ui/sessions/legacy/export", json=body)
    assert resp.status_code == 403


# ---- E-fix: existing ownerless + sessionless project is NOT a free-for-all ----
# The `if not agent_ids: return True` branch was meant for CREATING a brand-new
# project; it also fired for an EXISTING legacy/orphaned ownerless+sessionless row,
# letting any authenticated user modify/DELETE it (and NULL other users' session
# links). Fix: that branch now only allows a not-yet-persisted id (a create);
# an existing orphan is admin/Debugger only.


async def _exists(_pid):
    return True


async def _not_exists(_pid):
    return False


async def _no_agents(_pid):
    return []


@pytest.mark.asyncio
async def test_existing_ownerless_project_not_modifiable_by_non_admin(monkeypatch):
    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.project_exists", _exists, raising=False
    )
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _no_agents)
    # owner not mocked → real (empty) DB → None. project exists + no sessions → orphan.
    assert await _can_modify_project(AGENT_A, "p_orphan") is False  # was True (IDOR)
    assert await _can_modify_project(ADMIN, "p_orphan") is True  # admin may clean up legacy


@pytest.mark.asyncio
async def test_new_project_still_creatable_by_non_admin(monkeypatch):
    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.project_exists", _not_exists, raising=False
    )
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _no_agents)
    # id not yet persisted → this is a create → any authenticated caller allowed.
    assert await _can_modify_project(AGENT_A, "p_brand_new") is True


def test_delete_existing_ownerless_project_denied_for_non_admin(monkeypatch):
    async def _all_projects():
        return []

    monkeypatch.setattr(
        "src.routers.ui_sessions.session_service.project_exists", _exists, raising=False
    )
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _no_agents)
    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_all_projects", _all_projects)
    app.dependency_overrides[get_verified_identity] = lambda: AGENT_A
    try:
        with TestClient(app) as c:
            assert c.delete("/ui/projects/p_orphan").status_code == 403
    finally:
        app.dependency_overrides.clear()


# ---- E-residual: a session may only be linked to a project the caller can access ----
# Side-door closed: without this, a user could POST /ui/sessions with their own agent
# plus a victim's orphan project_id, giving the orphan a session they own and thus
# session-derived modify/delete — bypassing the E direct-path fix. The check is on the
# ROUTE only; internal autonomous flows call upsert_session directly and are unaffected.


def _session_payload(client: TestClient, session_id: str, project_id: str) -> dict:
    snapshot = client.get(f"/ui/sessions/{session_id}?snapshot=true")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["exists"] is False
    return {
        "id": session_id,
        "title": "x",
        "messages": [],
        "updatedAt": 1,
        "project_id": project_id,
        "agent_id": AGENT_A.agent_id,
        "revision": snapshot.json()["revision"],
    }


def test_save_session_cannot_attach_to_inaccessible_project(agentA_client: TestClient):
    # p_b is owned (session-derived) by agentB; agentA must not be able to link its
    # own session to it (would be a foothold to claim/observe another's project).
    resp = agentA_client.post(
        "/ui/sessions/s_atk",
        json=_session_payload(agentA_client, "s_atk", "p_b"),
    )
    assert resp.status_code == 403


def test_save_session_attach_to_orphan_denied(agentA_client: TestClient):
    # p_empty has no owner and no sessions (a legacy/orphan row) → not claimable.
    resp = agentA_client.post(
        "/ui/sessions/s_orphan",
        json=_session_payload(agentA_client, "s_orphan", "p_empty"),
    )
    assert resp.status_code == 403


def test_save_session_can_attach_to_own_project(agentA_client: TestClient):
    # p_a is session-owned by agentA → linking is allowed (past the 403 gate).
    resp = agentA_client.post(
        "/ui/sessions/s_ok",
        json=_session_payload(agentA_client, "s_ok", "p_a"),
    )
    assert resp.status_code == 200, resp.text
    saved = agentA_client.get("/ui/sessions/s_ok")
    assert saved.status_code == 200, saved.text
    assert saved.json()["project_id"] == "p_a"
    assert resp.json()["persistence"]["status"] == "saved"
    assert resp.json()["persistence"]["representation"] == "full"
