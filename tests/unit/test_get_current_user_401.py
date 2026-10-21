"""TDD: get_current_user 必須對無效 token 回傳 401 AUTH_INVALID_TOKEN，而非 500 SYSTEM_ERROR。

涵蓋四個場景：
- 過期 token (ExpiredSignatureError)
- 完全非法 token (InvalidTokenError)
- Token 缺少 sub claim
- 合法 token 但對應 user 不存在
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from src.core.security import AuthSecretManager
from src.utils.time_utils import get_current_epoch
from src.main import app
from src.routers.auth import get_auth_service
from src.services.auth_service import AuthService


@pytest.fixture
def auth_service(tmp_path):
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    app.dependency_overrides[get_auth_service] = lambda: service
    yield service
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _make_token(payload: dict) -> str:
    return jwt.encode(payload, AuthSecretManager.get_secret(), algorithm="HS256")


def test_expired_token_returns_401_not_500(client, auth_service):
    """過期 token 必須回 401 AUTH_INVALID_TOKEN，不得回 500。"""
    expired_token = _make_token({"sub": "Alice", "exp": int(get_current_epoch()) - 3600})

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_invalid_token_returns_401_not_500(client, auth_service):
    """完全非法 token 必須回 401 AUTH_INVALID_TOKEN，不得回 500。"""
    response = client.get("/auth/me", headers={"Authorization": "Bearer totally.invalid.token"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_token_missing_sub_returns_401_not_500(client, auth_service):
    """Token 缺少 sub claim 必須回 401 AUTH_INVALID_TOKEN，不得回 500。"""
    token_no_sub = _make_token({"data": "no_sub_here"})

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token_no_sub}"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_valid_token_unknown_user_returns_401_not_500(client, auth_service):
    """合法 token 但 user 不存在必須回 401 AUTH_INVALID_TOKEN，不得回 500。"""
    token = _make_token({"sub": "ghost_user_that_does_not_exist"})

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"
