"""ADR-0106 avatar endpoint tests (TDD) — auth, validation, public serving.

Happy-path persistence (writes users.json + avatar file) is covered by
test_avatar_service.py; here we assert the endpoint wiring: auth required on
upload, format rejection (422), and the public GET serving + 404/traversal.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from src.main import app
from src.routers.auth import get_current_user
from src.services import avatar_service
from src.services.auth_service import AuthService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _clear():
    app.dependency_overrides.clear()


def test_upload_avatar_requires_auth():
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/avatar", files={"file": ("a.png", PNG, "image/png")})
    assert resp.status_code in (401, 403), f"got {resp.status_code}"


def test_upload_avatar_rejects_non_image_format():
    """An SVG (XSS vector) is rejected with 422 before any write/persist."""
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1")
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/avatar", files={"file": ("evil.svg", SVG, "image/svg+xml")})
    _clear()
    assert resp.status_code == 422, f"got {resp.status_code}"
    assert resp.json()["detail"]["code"] == "AVATAR_INVALID_FORMAT"


def test_get_avatar_404_when_absent():
    """Public serving endpoint: 404 for a user with no avatar (no auth required)."""
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/users/nonexistent-user/avatar")
    assert resp.status_code == 404, f"got {resp.status_code}"


def test_get_avatar_path_traversal_404():
    """A traversing user_id must not escape the avatars dir → 404 (not 200/500)."""
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/v1/users/..%2f..%2fetc%2fpasswd/avatar")
    assert resp.status_code == 404, f"got {resp.status_code}"


def test_upload_avatar_happy_path_sets_cache_busted_url(tmp_path, monkeypatch):
    """avatar-gap-4: successful upload sets avatar_url to the serving path with ?v= cache-bust."""
    monkeypatch.setattr(avatar_service, "avatars_dir", lambda: tmp_path / "avatars")
    user = AuthService().create_user("avtest@touka.io", "pw_strong_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/avatar", files={"file": ("a.png", PNG, "image/png")})
    _clear()
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:200]}"
    avatar_url = resp.json()["avatar_url"]
    assert avatar_url.startswith(f"/v1/users/{user.id}/avatar")
    assert "?v=" in avatar_url  # cache-busting param present
