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
    with TestClient(app) as c:
        yield c


def test_public_registration_enforces_normal_role(client, auth_service):
    """TDD Test: Verify that registering via public /auth/register enforces the 'normal' role.

    Even when an attacker specifies role='admin' in the JSON body, the system must ignore
    or override it to 'normal' to prevent privilege escalation.
    """
    # Act: Attempt to register with role="admin"
    response = client.post("/auth/register", json={
        "email": "malicious_admin@touka.io",
        "password": "strongpassword123",
        "role": "admin"
    })
    
    # Assert
    assert response.status_code == 200
    data = response.json()
    
    # The returned payload must have role="normal"
    assert data["role"] == "normal"
    assert data["email"] == "malicious_admin@touka.io"
    
    # Verify the database (SSOT) entry also enforces the "normal" role
    db_user = auth_service.get_user_by_email("malicious_admin@touka.io")
    assert db_user is not None
    assert db_user.role == "normal"
