"""TDD — A-fix: a project's root_path must stay inside the workspace jail.

DA3 already scopes /ui/projects* to the owner (IDOR). The residual hole (A):
`save_project` never validated `root_path`, and `get_project_files` scanned it
raw — so an authenticated NON-admin could register a project whose root_path is
ANY absolute host path (e.g. C:\\Users) and enumerate that directory tree
(filenames + sizes) via GET /ui/projects/{id}/files.

Fix (owner decision: confine non-admins only):
  * SSOT primitive  resource_manager.is_within_workspace(path) — resolves the
    path (incl. symlinks) and checks it stays under the configured workspace_root.
  * Policy          _project_root_allowed(caller, root_path) — admin/Debugger
    bypass (trusted, own the workspace_root setting), everyone else is confined.
  * Enforced at save_project (reject, 422) and get_project_files (deny, 403 —
    defense in depth for legacy / externally-inserted rows).
  * scan_dir also skips symlinks so an in-jail root cannot be used to follow a
    junction/symlink out of the workspace.

DB is the per-session sandbox (conftest MEMORY_APP_DATA_DIR); no real data touched.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import ui_sessions
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])
DEBUGGER = AgentIdentity.for_role(agent_id="Dbg", role="Debugger", is_human=True, capabilities=[])
AGENT_A = AgentIdentity.for_role(agent_id="agentA", role="Base", is_human=False, capabilities=[])


@pytest.fixture
def jailed_workspace(tmp_path, monkeypatch):
    """Point the workspace jail at an isolated tmp dir (deterministic boundary)."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    async def _fake_ws():
        return ws

    monkeypatch.setattr(resource_manager, "_get_workspace_root", _fake_ws)
    monkeypatch.setattr(resource_manager, "_is_docker", False)
    return ws


@pytest.mark.asyncio
async def test_jailed_workspace_owns_native_mode_under_container_parent(
    monkeypatch, request, tmp_path
):
    """The host-path fixture must not inherit the runner's Docker remap mode."""
    monkeypatch.setattr(resource_manager, "_is_docker", True)
    monkeypatch.setattr(resource_manager, "_container_mount", tmp_path / "container-mount")
    workspace = request.getfixturevalue("jailed_workspace")
    actual = await resource_manager.translate_path(str(workspace / "project"))
    assert actual == str(workspace / "project")


@pytest.mark.asyncio
async def test_docker_translation_uses_owned_mount_and_leaves_outside_unchanged(
    jailed_workspace, monkeypatch, tmp_path
):
    """Exercise the real Docker branch with two distinct sandbox roots."""
    mount = tmp_path / "container-mount"
    mount.mkdir()
    monkeypatch.setattr(resource_manager, "_is_docker", True)
    monkeypatch.setattr(resource_manager, "_container_mount", mount)
    inside = jailed_workspace / "project" / "file.txt"
    expected = (mount / "project/file.txt").as_posix()
    assert await resource_manager.translate_path(str(inside)) == expected
    outside = tmp_path / "outside"
    assert await resource_manager.translate_path(str(outside)) == str(outside)


# --------------------------------------------------------------------------- #
# SSOT primitive: resource_manager.is_within_workspace
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_is_within_workspace_primitive(jailed_workspace):
    ws = jailed_workspace
    assert await resource_manager.is_within_workspace(str(ws / "proj" / "src")) is True
    assert await resource_manager.is_within_workspace(str(ws)) is True
    assert await resource_manager.is_within_workspace(str(ws.parent / "outside")) is False
    # `..` traversal must not escape the jail
    assert await resource_manager.is_within_workspace(str(ws / ".." / "evil")) is False
    assert await resource_manager.is_within_workspace("") is False


# --------------------------------------------------------------------------- #
# Policy: _project_root_allowed (admin/Debugger bypass; others confined)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_project_root_allowed_policy(jailed_workspace):
    ws = jailed_workspace
    inside = str(ws / "code")
    outside = str(ws.parent / "elsewhere")
    # admin / Debugger bypass even for an out-of-workspace path
    assert await ui_sessions._project_root_allowed(ADMIN, outside) is True
    assert await ui_sessions._project_root_allowed(DEBUGGER, outside) is True
    # non-admin: inside allowed, outside denied, empty denied
    assert await ui_sessions._project_root_allowed(AGENT_A, inside) is True
    assert await ui_sessions._project_root_allowed(AGENT_A, outside) is False
    assert await ui_sessions._project_root_allowed(AGENT_A, "") is False


# --------------------------------------------------------------------------- #
# save_project endpoint: reject out-of-workspace root for non-admins
# --------------------------------------------------------------------------- #


def _client(identity):
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app)


def test_save_rejects_outside_workspace_for_non_admin(jailed_workspace, monkeypatch):
    async def _no_agents(_pid):
        return []

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _no_agents)
    outside = str(jailed_workspace.parent / "evil")
    try:
        with _client(AGENT_A) as c:
            resp = c.post(
                "/ui/projects/save",
                json={"id": "p_new", "name": "X", "root_path": outside, "description": ""},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == ErrorCode.VALIDATION_ERROR.value
    finally:
        app.dependency_overrides.clear()


def test_save_allows_inside_workspace_for_non_admin(jailed_workspace, monkeypatch):
    async def _no_agents(_pid):
        return []

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _no_agents)
    inside = str(jailed_workspace / "my_code")
    try:
        with _client(AGENT_A) as c:
            resp = c.post(
                "/ui/projects/save",
                json={"id": "p_in", "name": "X", "root_path": inside, "description": ""},
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_save_allows_outside_workspace_for_admin_bypass(jailed_workspace):
    outside = str(jailed_workspace.parent / "anywhere")
    try:
        with _client(ADMIN) as c:
            resp = c.post(
                "/ui/projects/save",
                json={"id": "p_adm", "name": "X", "root_path": outside, "description": ""},
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# get_project_files: deny out-of-workspace root for non-admins (the A vector)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_project_files_denies_outside_root_for_non_admin(
    jailed_workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")

    async def _agents(pid):
        return ["agentA"] if pid == "p_own" else []

    async def _root(_pid):
        return str(outside)

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _agents)
    monkeypatch.setattr("src.routers.ui_sessions.get_project_root_path", _root, raising=False)

    with pytest.raises(ToukaException) as ei:
        await ui_sessions.get_project_files("p_own", AGENT_A)
    assert ei.value.code == ErrorCode.AUTH_PERMISSION_DENIED


@pytest.mark.asyncio
async def test_get_project_files_allows_inside_root(jailed_workspace, monkeypatch):
    proj = jailed_workspace / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("print('hi')", encoding="utf-8")

    async def _agents(pid):
        return ["agentA"] if pid == "p_in" else []

    async def _root(_pid):
        return str(proj)

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _agents)
    monkeypatch.setattr("src.routers.ui_sessions.get_project_root_path", _root, raising=False)

    result = await ui_sessions.get_project_files("p_in", AGENT_A)
    names = {n["name"] for n in result["tree"]}
    assert "main.py" in names


@pytest.mark.asyncio
async def test_scan_dir_skips_symlink_escape(jailed_workspace, tmp_path, monkeypatch):
    """An in-jail root must not enumerate an out-of-tree dir via a symlink/junction."""
    proj = jailed_workspace / "proj"
    proj.mkdir()
    (proj / "real.py").write_text("x", encoding="utf-8")
    secret = tmp_path / "secret_outside"
    secret.mkdir()
    (secret / "leak.txt").write_text("leak", encoding="utf-8")
    try:
        os.symlink(secret, proj / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    async def _agents(pid):
        return ["agentA"] if pid == "p_sym" else []

    async def _root(_pid):
        return str(proj)

    monkeypatch.setattr("src.routers.ui_sessions.session_service.get_project_agent_ids", _agents)
    monkeypatch.setattr("src.routers.ui_sessions.get_project_root_path", _root, raising=False)

    result = await ui_sessions.get_project_files("p_sym", AGENT_A)
    names = {n["name"] for n in result["tree"]}
    assert "real.py" in names
    assert "link" not in names  # symlink skipped → its target never enumerated


# --------------------------------------------------------------------------- #
# project_tools path: _resolve_project_path / get_project_path must also confine
# non-admins (the agent-facing read/write/list/grep vector — worse than /files).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_project_path_denies_outside_root_for_non_admin(
    jailed_workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "outside_proj"
    outside.mkdir()

    async def _root(_pid):
        return str(outside)

    monkeypatch.setattr("src.utils.async_db.get_project_root_path", _root)
    # non-admin → denied (None) → project_tools reports "Project root not found"
    assert await resource_manager.get_project_path("p_x", AGENT_A) is None


@pytest.mark.asyncio
async def test_get_project_path_allows_outside_root_for_admin_bypass(
    jailed_workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "outside_proj"
    outside.mkdir()

    async def _root(_pid):
        return str(outside)

    monkeypatch.setattr("src.utils.async_db.get_project_root_path", _root)
    # admin → bypass → resolves to a path
    assert await resource_manager.get_project_path("p_x", ADMIN) == str(outside)


@pytest.mark.asyncio
async def test_get_project_path_allows_inside_root_for_non_admin(jailed_workspace, monkeypatch):
    inside = jailed_workspace / "proj_in"
    inside.mkdir()

    async def _root(_pid):
        return str(inside)

    monkeypatch.setattr("src.utils.async_db.get_project_root_path", _root)
    assert await resource_manager.get_project_path("p_in", AGENT_A) == str(inside)


# --------------------------------------------------------------------------- #
# B-fix: project root resolution must be fresh — no stale per-project path cache.
# (The cache was vestigial after A's fresh-read resolver; B removes it so a
#  root_path change is always reflected and no stale path can be served.)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_project_path_reflects_root_change_no_stale(jailed_workspace, monkeypatch):
    first = jailed_workspace / "old_root"
    first.mkdir()
    second = jailed_workspace / "new_root"
    second.mkdir()
    current = {"root": str(first)}

    async def _root(_pid):
        return current["root"]

    monkeypatch.setattr("src.utils.async_db.get_project_root_path", _root)

    assert await resource_manager.get_project_path("p_move", AGENT_A) == str(first)
    # owner repoints the project root → the next resolve must return the NEW path,
    # never a stale cached one (the pre-A cache short-circuit returned `first`).
    current["root"] = str(second)
    assert await resource_manager.get_project_path("p_move", AGENT_A) == str(second)


def test_project_path_cache_field_removed():
    # B-fix: the stale-prone per-project path cache is gone entirely, so no code
    # path can serve a memoized (and possibly outside-workspace) root.
    assert not hasattr(resource_manager, "_project_path_cache")
