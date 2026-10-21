"""DA22: admin user-management endpoints must not leak sensitive User fields.

`/v1/admin/users*` returned the full `User` model — including `hashed_password` and the
refresh-token JTIs (`refresh_token_jti` / `previous_refresh_jti` / `refresh_jti_rotated_at`)
— to the admin client (list/detail/create used response_model=User; update had NONE so it
serialized the full object). The fix switches every user-returning endpoint to the
sanitized `UserOut` projection, which excludes those fields. (DA11–16 adversarial review.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers.admin import get_user_service
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="admin-id", role="admin", is_human=True, capabilities=[])

# a user record fully populated with the fields that must NOT escape
SENSITIVE_USER = User(
    id="u1",
    email="victim@test.com",
    role="normal",
    hashed_password="$2b$12$THISisABcryptHASHsecret",
    refresh_token_jti="JTI-SECRET-VALUE",
    previous_refresh_jti="PREV-JTI-SECRET",
    refresh_jti_rotated_at=1780000000,
)

SECRETS = (
    "hashed_password",
    "THISisABcryptHASHsecret",
    "refresh_token_jti",
    "JTI-SECRET-VALUE",
    "previous_refresh_jti",
    "PREV-JTI-SECRET",
    "refresh_jti_rotated_at",
)


@pytest.fixture
def admin_client():
    """Admin-authenticated client with a mocked UserService (deps overridden)."""
    mock = MagicMock()
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    app.dependency_overrides[get_user_service] = lambda: mock
    with TestClient(app) as c:
        yield c, mock
    app.dependency_overrides.clear()


def _assert_no_secrets(raw_body: str) -> None:
    for marker in SECRETS:
        assert marker not in raw_body, f"sensitive field/value leaked: {marker}"


def test_list_users_uses_userout(admin_client):
    c, mock = admin_client
    mock.get_users.return_value = [SENSITIVE_USER]
    resp = c.get("/v1/admin/users")
    assert resp.status_code == 200
    _assert_no_secrets(resp.text)
    body = resp.json()
    assert body[0]["id"] == "u1"
    assert body[0]["email"] == "victim@test.com"


def test_get_user_detail_uses_userout(admin_client):
    c, mock = admin_client
    mock.get_user.return_value = SENSITIVE_USER
    resp = c.get("/v1/admin/users/u1")
    assert resp.status_code == 200
    _assert_no_secrets(resp.text)
    assert resp.json()["id"] == "u1"


def test_create_user_uses_userout(admin_client):
    c, mock = admin_client
    mock.create_user.return_value = SENSITIVE_USER
    resp = c.post(
        "/v1/admin/users",
        json={"email": "new@test.com", "password": "StrongPass1!", "role": "normal"},
    )
    assert resp.status_code == 200
    _assert_no_secrets(resp.text)
    assert resp.json()["id"] == "u1"


def test_update_user_uses_userout(admin_client):
    # update_user previously had NO response_model → leaked the full object.
    c, mock = admin_client
    mock.admin_update_user.return_value = SENSITIVE_USER
    resp = c.patch("/v1/admin/users/u1", json={"display_name": "Renamed"})
    assert resp.status_code == 200
    _assert_no_secrets(resp.text)
    assert resp.json()["id"] == "u1"
