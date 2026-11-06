"""DA25–DA27: the three remaining Low-severity items from the DA11–16 adversarial review.

DA25 — self-service password change. There was no endpoint for a user to change their OWN
       password (admin_update_user is admin-only; update_user_profile only touches profile
       fields), so a temp/bootstrap credential could never be rotated by its holder. Adds
       POST /auth/change-password: verify current password → set new → clear is_temp_password
       → revoke existing refresh tokens.
DA26 — refresh-token JTI comparisons used `==`/`!=` (non-constant-time). Now use
       secrets.compare_digest (a weak oracle since it sits behind a verified JWT signature,
       fixed for consistency with the DISPATCH/heartbeat token gates).
DA27 — resource_manager.is_protected_path used str.startswith for the soul/legacy guard,
       falsely protecting sibling dirs (e.g. soul_archive). Now is_relative_to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.config import get_initial_admin_password_file
from src.core.exceptions import ToukaException
from src.main import app
from src.models.user import User
from src.routers.auth import get_current_user
from src.services import auth_service as auth_service_module
from src.services.auth_service import AuthService
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService

from tests.support.zone_roots import point_zone_roots

# ===========================================================================
# DA25 — self-service change password
# ===========================================================================


class TestDA25ChangePassword:
    def test_change_password_clears_temp_and_revokes(self, tmp_path):
        """The bootstrap admin (is_temp_password=True) can rotate its own password;
        doing so clears the temp flag and revokes existing refresh tokens."""
        pw_file = get_initial_admin_password_file()
        pw_file.unlink(missing_ok=True)
        svc = AuthService(users_file_path=str(tmp_path / "users.json"))
        svc.ensure_admin_user([])
        admin = next(u for u in svc.get_all_users() if u.role == "admin")
        assert admin.is_temp_password is True
        old_pw = next(
            line.split(":", 1)[1].strip()
            for line in pw_file.read_text(encoding="utf-8").splitlines()
            if line.lower().startswith("password")
        )

        UserService(auth_service=svc).change_own_password(
            admin.id, old_pw, "NewStrongPass1!"
        )

        updated = svc.get_user_by_id(admin.id)
        assert updated.is_temp_password is False
        assert updated.refresh_token_exp is None  # sessions revoked
        assert svc.verify_password("NewStrongPass1!", updated.hashed_password)
        assert not svc.verify_password(old_pw, updated.hashed_password)
        pw_file.unlink(missing_ok=True)

    def test_change_password_wrong_current_raises_and_no_change(self, tmp_path):
        svc = AuthService(users_file_path=str(tmp_path / "users.json"))
        u = svc.create_user("w@test.com", "OldPass123!", role="normal")
        with pytest.raises(ToukaException):
            UserService(auth_service=svc).change_own_password(
                u.id, "WRONG-current", "NewPass456!"
            )
        # password unchanged → still authenticates with the old one
        assert svc.authenticate_user("w@test.com", "OldPass123!")["access_token"]

    def test_change_password_endpoint_rejects_short_new_password(self):
        user = User(id="x", email="x@t.com", hashed_password="h")
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            with TestClient(app) as c:
                r = c.post(
                    "/v1/auth/change-password",
                    json={"current_password": "anything", "new_password": "short"},
                )
            assert r.status_code == 422  # new_password min_length=8 enforced
        finally:
            app.dependency_overrides.clear()


# ===========================================================================
# DA26 — refresh JTI constant-time comparison
# ===========================================================================


class TestDA26RefreshJtiConstantTime:
    def test_normal_rotation_still_works(self, tmp_path):
        svc = AuthService(users_file_path=str(tmp_path / "users.json"))
        svc.create_user("j@test.com", "StrongPass1!", role="normal")
        tokens = svc.authenticate_user("j@test.com", "StrongPass1!")
        rotated = svc.refresh_access_token(tokens["refresh_token"])
        assert rotated["access_token"]
        assert rotated["refresh_token"]

    def test_source_uses_compare_digest_not_equality(self):
        src = Path(auth_service_module.__file__).read_text(encoding="utf-8")
        assert "secrets.compare_digest" in src
        assert "presented_jti == stored_jti" not in src
        assert "presented_jti == prev_jti" not in src
        assert "presented_jti != stored_jti" not in src


# ===========================================================================
# DA27 — is_protected_path: is_relative_to (no sibling-prefix false positive)
# ===========================================================================


class TestDA27IsProtectedPath:
    @pytest.fixture(autouse=True)
    def _base(self, tmp_path, monkeypatch):
        point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
        self.base = tmp_path

    def test_soul_and_legacy_subpaths_are_protected(self):
        assert resource_manager.is_protected_path(self.base / "soul" / "A" / "soul.md") is True
        assert resource_manager.is_protected_path(self.base / "legacy" / "A" / "x.json") is True

    def test_sibling_prefix_dirs_are_not_protected(self):
        # str.startswith falsely protected these (soul_archive starts with soul)
        assert resource_manager.is_protected_path(self.base / "soul_archive" / "x") is False
        assert resource_manager.is_protected_path(self.base / "legacy_backup" / "x") is False

    def test_unrelated_path_not_protected(self):
        assert resource_manager.is_protected_path(self.base / "personal_drive" / "x") is False
