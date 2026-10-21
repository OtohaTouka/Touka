import pytest
from fastapi.testclient import TestClient
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
    # In a real setup, we might want to override settings.config_dir to tmp_path
    # but we will rely on integration testing here or mock.
    with TestClient(app) as c:
        yield c

def test_register_user(client, auth_service):
    # Act
    response = client.post("/auth/register", json={
        "email": "newuser@touka.io",
        "password": "strongpassword",
        "role": "normal"
    })
    
    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "newuser@touka.io"
    assert "hashed_password" not in data
    assert data["role"] == "normal"
    assert "id" in data

def test_login_user_returns_tokens(client):
    # Setup
    client.post("/auth/register", json={
        "email": "loginuser@touka.io",
        "password": "loginpassword"
    })
    
    # Act
    response = client.post("/auth/login", json={
        "email": "loginuser@touka.io",
        "password": "loginpassword"
    })
    
    # Assert
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    
    # Check Cookie for refresh_token
    assert "refresh_token" in response.cookies

def test_refresh_token(client):
    # Setup
    client.post("/auth/register", json={
        "email": "refreshuser@touka.io",
        "password": "loginpassword"
    })
    login_resp = client.post("/auth/login", json={
        "email": "refreshuser@touka.io",
        "password": "loginpassword"
    })
    refresh_cookie = login_resp.cookies.get("refresh_token")
    
    # Act
    client.cookies.set("refresh_token", refresh_cookie)
    resp = client.post("/auth/refresh")

    # Assert
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


def _register_and_login(client, email, password="strongpassword"):
    client.post("/auth/register", json={"email": email, "password": password, "role": "normal"})
    resp = client.post("/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


def test_update_own_profile_normal_user(client):
    """P3: a NORMAL user can self-edit their own profile via PATCH /v1/auth/me (no 403)."""
    token = _register_and_login(client, "profile_self@touka.io")
    resp = client.patch(
        "/v1/auth/me",
        json={"display_name": "Nyan", "avatar_url": "https://example.com/a.png", "bio": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["display_name"] == "Nyan"
    assert data["avatar_url"] == "https://example.com/a.png"
    assert data["bio"] == "hi"
    assert data["role"] == "normal"  # NOT escalated


def test_update_own_profile_rejects_privileged_fields(client):
    """Security: extra='forbid' rejects role/email/etc. (422); role stays unchanged."""
    token = _register_and_login(client, "profile_sec@touka.io")
    resp = client.patch(
        "/v1/auth/me",
        json={"display_name": "X", "role": "admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["role"] == "normal"


def test_update_own_profile_requires_auth(client):
    """禁令 25: no Bearer token → 401."""
    resp = client.patch("/v1/auth/me", json={"display_name": "X"})
    assert resp.status_code == 401
