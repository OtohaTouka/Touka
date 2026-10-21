"""Batch 5+6 P1 security tests (TDD).

Covers the P1-A backend-security trio surfaced by the settings UX review:
  - S2: path-traversal hardening on POST /system/restore/run (filename param)
  - S3: CSRF Origin guard on POST /system/restore/run and POST /system/backup/run
  - S4: SNS bots role tightened to {Owner, admin} (Debugger removed)

⚠️ The restore endpoint schedules a BackgroundTask that runs `os._exit(0)`.
In Starlette's TestClient background tasks execute synchronously after the
response, so a *successful* restore would kill pytest. Every restore test here
therefore uses an INVALID filename or an untrusted Origin, which makes the
handler raise BEFORE `background_tasks.add_task(...)` is reached — the kill path
is never armed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", capabilities=[])
OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=[])
NORMAL = AgentIdentity.for_role(agent_id="someuser", role="normal", capabilities=[])

_EVIL_ORIGIN = {"Origin": "http://evil.example.com"}


def _override(identity):
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _clear():
    app.dependency_overrides.clear()


def _client(identity):
    _override(identity)
    return TestClient(app, raise_server_exceptions=False)


# 軌1 Phase 1：restore 改為 get_current_user + 密碼閘（DA25）。以真實 admin user（已知密碼）
# 覆寫 get_current_user，並在 body 帶 current_password，讓請求通過認證/密碼閘後才觸及
# CSRF / 路徑檢查（被測的安全行為）。
_RESTORE_PW = "sec_pw_123"


def _client_pw(email):
    user = AuthService().create_user(email, _RESTORE_PW, role="admin")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# S2 — path traversal on restore filename
# ---------------------------------------------------------------------------

def test_restore_blocks_path_traversal_even_when_target_exists():
    """A `../`-escaping filename must be rejected (404), not restored.

    `../README.md` resolves to an existing file OUTSIDE backups/, so the bare
    `.exists()` check passes — only the symlink-escape guard stops it.
    """
    c = _client_pw("rsec_trav1@touka.io")
    resp = c.post(
        "/system/restore/run", json={"filename": "../README.md", "current_password": _RESTORE_PW}
    )
    _clear()
    assert resp.status_code == 404, f"traversal must be blocked, got {resp.status_code}"
    # Must NOT have been accepted for execution.
    assert resp.json().get("status") != "accepted"


def test_restore_blocks_deep_traversal():
    c = _client_pw("rsec_trav2@touka.io")
    resp = c.post(
        "/system/restore/run",
        json={"filename": "../../etc/passwd", "current_password": _RESTORE_PW},
    )
    _clear()
    assert resp.status_code == 404, f"deep traversal must be blocked, got {resp.status_code}"


def test_restore_missing_file_still_404():
    """A plain (non-escaping) but missing filename still 404s (regression guard)."""
    c = _client_pw("rsec_missing@touka.io")
    resp = c.post(
        "/system/restore/run",
        json={"filename": "nope-does-not-exist.zip", "current_password": _RESTORE_PW},
    )
    _clear()
    assert resp.status_code == 404, f"missing file must 404, got {resp.status_code}"


# ---------------------------------------------------------------------------
# S3 — CSRF Origin guard on destructive system endpoints
# ---------------------------------------------------------------------------

def test_restore_rejects_untrusted_origin():
    c = _client_pw("rsec_origin@touka.io")
    resp = c.post(
        "/system/restore/run",
        json={"filename": "nope.zip", "current_password": _RESTORE_PW},
        headers=_EVIL_ORIGIN,
    )
    _clear()
    assert resp.status_code == 403, f"untrusted origin must be blocked, got {resp.status_code}"


def test_backup_run_rejects_untrusted_origin():
    c = _client(ADMIN)
    resp = c.post("/system/backup/run", headers=_EVIL_ORIGIN)
    _clear()
    assert resp.status_code == 403, f"untrusted origin must be blocked, got {resp.status_code}"


def test_delete_rejects_untrusted_origin():
    """軌1 Phase 1：DELETE /backups/{filename} 同樣受 CSRF Origin 守衛保護。"""
    c = _client(ADMIN)
    resp = c.delete("/system/backups/whatever.zip", headers=_EVIL_ORIGIN)
    _clear()
    assert resp.status_code == 403, f"untrusted origin must be blocked, got {resp.status_code}"


def test_delete_blocks_path_traversal():
    """逃出 backups/ 的檔名必須被擋（404，未刪除）——與 restore 同一道 symlink-escape 守衛。"""
    c = _client(ADMIN)
    resp = c.delete("/system/backups/..%2f..%2fevil.zip")
    _clear()
    assert resp.status_code == 404, f"traversal must be blocked, got {resp.status_code}"
    assert resp.json().get("status") != "deleted"


def test_download_rejects_untrusted_origin():
    """ADR-0108：加密下載端點同受 CSRF Origin 守衛保護。"""
    c = _client(ADMIN)
    resp = c.post(
        "/system/backups/whatever.zip/download",
        json={"passphrase": "pass-phrase-123"},
        headers=_EVIL_ORIGIN,
    )
    _clear()
    assert resp.status_code == 403, f"untrusted origin must be blocked, got {resp.status_code}"


def test_download_blocks_path_traversal():
    """加密下載的 filename 同樣不得逃出 backups/（404）。"""
    c = _client(ADMIN)
    resp = c.post(
        "/system/backups/..%2f..%2fevil.zip/download", json={"passphrase": "pass-phrase-123"}
    )
    _clear()
    assert resp.status_code == 404, f"traversal must be blocked, got {resp.status_code}"


def test_backup_schedule_post_rejects_untrusted_origin():
    """ADR-0109：寫入排程設定的 POST 同受 CSRF Origin 守衛保護。"""
    c = _client(ADMIN)
    resp = c.post("/system/backup/schedule", json={"enabled": True}, headers=_EVIL_ORIGIN)
    _clear()
    assert resp.status_code == 403, f"untrusted origin must be blocked, got {resp.status_code}"


def test_restore_no_origin_passes_csrf_then_404_on_missing():
    """No Origin header (non-browser/API call) must pass CSRF + password gate (then
    404 on the missing file), proving the guard does not block legitimate use."""
    c = _client_pw("rsec_noorigin@touka.io")
    resp = c.post(
        "/system/restore/run", json={"filename": "nope.zip", "current_password": _RESTORE_PW}
    )
    _clear()
    assert resp.status_code == 404, f"no-origin should pass CSRF and 404, got {resp.status_code}"


# ---------------------------------------------------------------------------
# S4 — SNS bots role tightened to {Owner, admin} (Debugger removed)
# ---------------------------------------------------------------------------

def test_bots_post_debugger_forbidden():
    c = _client(DEBUGGER)
    resp = c.post("/bots", json={"discord": [], "telegram": []})
    _clear()
    assert resp.status_code == 403, f"Debugger must be forbidden, got {resp.status_code}"


def test_bots_get_debugger_forbidden():
    c = _client(DEBUGGER)
    resp = c.get("/bots")
    _clear()
    assert resp.status_code == 403, f"Debugger must be forbidden, got {resp.status_code}"


def test_bots_post_admin_passes():
    c = _client(ADMIN)
    resp = c.post("/bots", json={"discord": [], "telegram": []})
    _clear()
    assert resp.status_code not in (401, 403), f"admin must pass, got {resp.status_code}"


def test_bots_post_owner_passes():
    c = _client(OWNER)
    resp = c.post("/bots", json={"discord": [], "telegram": []})
    _clear()
    assert resp.status_code not in (401, 403), f"Owner must pass, got {resp.status_code}"


def test_bots_post_normal_forbidden():
    c = _client(NORMAL)
    resp = c.post("/bots", json={"discord": [], "telegram": []})
    _clear()
    assert resp.status_code == 403, f"normal must be forbidden, got {resp.status_code}"
