"""TDD — #1/#3: the Bearer gate must reject non-access JWT types.

Human access/refresh tokens share the HS256 secret, and verify_token /
get_current_user only checked signature+exp+sub — so a 30-day *refresh* token
presented as `Authorization: Bearer` authenticated as the user (incl. admin),
bypassing the 1h access TTL and the jti rotation/revocation that only runs at
/auth/refresh. Fix: reject any token whose `type` is not access; untyped agent
tokens (issue_token) and access tokens still pass.
"""

from __future__ import annotations

import jwt
import pytest
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.utils.time_utils import get_current_epoch

ALG = "HS256"


def _mint(**claims) -> str:
    base = {"sub": "Alice", "exp": int(get_current_epoch()) + 3600}
    base.update(claims)
    return jwt.encode(base, AuthSecretManager.get_secret(), algorithm=ALG)


# ---- verify_token: central Bearer gate ----

def test_verify_token_rejects_refresh_token():
    assert identity_service.verify_token(_mint(type="refresh", jti="x")) is None


def test_verify_token_rejects_service_token():
    assert identity_service.verify_token(_mint(sub="heartbeat", type="service")) is None


def test_verify_token_accepts_access_token():
    ident = identity_service.verify_token(_mint(sub="SomeAgent", type="access"))
    assert ident is not None and ident.agent_id == "SomeAgent"


def test_verify_token_accepts_untyped_agent_token():
    # issue_token mints untyped tokens — these must still authenticate.
    ident = identity_service.verify_token(_mint(sub="SomeAgent"))
    assert ident is not None and ident.agent_id == "SomeAgent"


# ---- get_current_user: human Bearer path ----

def test_get_current_user_rejects_refresh_but_accepts_access():
    auth = AuthService()
    user = auth.create_user(
        email="jwt-type-test@touka.io", password="pw-12345678", role="normal"
    )

    # refresh token for a REAL user → without the type check this would succeed;
    # with it, the Bearer gate rejects it.
    refresh = _mint(sub=user.id, type="refresh", jti="x")
    with pytest.raises(ToukaException):
        get_current_user(token=refresh, auth_service=auth)

    # an access token for the same user is still accepted (regression guard).
    access = _mint(sub=user.id, type="access")
    assert get_current_user(token=access, auth_service=auth).id == user.id
