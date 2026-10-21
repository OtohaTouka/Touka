"""DA23: account suspension must actually be enforced.

The `status` field existed (admin could set "suspended"), but NO auth path read it —
so suspended accounts kept logging in AND existing access/refresh tokens kept working.
The fix adds a `User.is_active` SSOT property and rejects non-active users at every
user-resolving gate: login (authenticate_user), the Bearer gates (verify_token /
get_current_user), and token refresh. (DA11–16 adversarial review.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import jwt
import pytest
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.models.user import User
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.user_service import UserService
from src.utils.time_utils import get_current_epoch


def _access_token(sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "type": "access", "exp": int(get_current_epoch()) + 3600},
        AuthSecretManager.get_secret(),
        algorithm="HS256",
    )


class TestUserIsActive:
    def test_active_user_is_active(self):
        assert User(id="1", email="a@b.c", hashed_password="h", status="active").is_active is True

    def test_suspended_user_is_not_active(self):
        u = User(id="1", email="a@b.c", hashed_password="h", status="suspended")
        assert u.is_active is False

    def test_default_status_is_active(self):
        # backward-compat: users without an explicit status default to active
        assert User(id="1", email="a@b.c", hashed_password="h").is_active is True


class TestLoginEnforcement:
    def test_suspended_user_cannot_login(self, tmp_path):
        svc = AuthService(users_file_path=str(tmp_path / "users.json"))
        user = svc.create_user("u@test.com", "StrongPass1!", role="normal")
        # works while active
        assert svc.authenticate_user("u@test.com", "StrongPass1!")["access_token"]

        UserService(auth_service=svc).admin_update_user(user.id, {"status": "suspended"})
        with pytest.raises(ToukaException):
            svc.authenticate_user("u@test.com", "StrongPass1!")


class TestBearerGateEnforcement:
    def test_verify_token_rejects_suspended(self, monkeypatch):
        suspended = User(id="u1", email="x@t.com", hashed_password="h", status="suspended")
        monkeypatch.setattr(
            "src.services.auth_service.AuthService.get_user_by_id",
            lambda self, uid: suspended,
        )
        assert identity_service.verify_token(_access_token("u1")) is None

    def test_verify_token_allows_active(self, monkeypatch):
        active = User(
            id="u1", email="x@t.com", hashed_password="h", role="admin", status="active"
        )
        monkeypatch.setattr(
            "src.services.auth_service.AuthService.get_user_by_id",
            lambda self, uid: active,
        )
        identity = identity_service.verify_token(_access_token("u1"))
        assert identity is not None
        assert identity.agent_id == "u1"

    def test_get_current_user_rejects_suspended(self):
        suspended = User(id="u1", email="x@t.com", hashed_password="h", status="suspended")
        mock = MagicMock()
        mock.get_all_users.return_value = [suspended]
        with pytest.raises(ToukaException):
            get_current_user(token=_access_token("u1"), auth_service=mock)


class TestRefreshEnforcement:
    def test_suspended_user_cannot_refresh(self, tmp_path):
        svc = AuthService(users_file_path=str(tmp_path / "users.json"))
        user = svc.create_user("r@test.com", "StrongPass1!", role="normal")
        refresh = svc.authenticate_user("r@test.com", "StrongPass1!")["refresh_token"]

        UserService(auth_service=svc).admin_update_user(user.id, {"status": "suspended"})
        with pytest.raises(ToukaException):
            svc.refresh_access_token(refresh)
