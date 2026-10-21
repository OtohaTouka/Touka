import pytest
# This test file ensures middleware and specific routers are protected.
from fastapi.testclient import TestClient
from src.main import app
from src.services.auth_service import AuthService
from src.routers.auth import get_auth_service

@pytest.fixture
def auth_service(tmp_path):
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    
    # Initialize admin to simulate migration
    service.create_user("admin@touka.io", "adminpass", "admin")
    
    app.dependency_overrides[get_auth_service] = lambda: service
    yield service
    app.dependency_overrides.clear()

@pytest.fixture
def client(auth_service):
    with TestClient(app) as c:
        yield c

def test_protected_agents_endpoint_without_token(client):
    response = client.get("/v1/agents")
    assert response.status_code == 401

def test_protected_agents_endpoint_with_admin(client, auth_service):
    # Setup login
    tokens = auth_service.authenticate_user("admin@touka.io", "adminpass")
    
    # Act
    response = client.get("/v1/agents", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    
    # Assert
    assert response.status_code == 200
    # Admins should see the full list
    assert isinstance(response.json(), list)

def test_protected_agents_endpoint_with_normal(client, auth_service):
    # Setup normal user
    auth_service.create_user("normal@touka.io", "normalpass", "normal")
    tokens = auth_service.authenticate_user("normal@touka.io", "normalpass")
    
    # Act
    response = client.get("/v1/agents", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    
    # Assert
    assert response.status_code == 200
    data = response.json()
    # Normal user should see filtered list
    assert isinstance(data, list)
    # Check that they don't see "soul" agents unless they own them (which is empty by default)
    for agent in data:
        assert agent.get("type") not in ("soul", "custom")
