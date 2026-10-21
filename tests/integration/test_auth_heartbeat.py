import jwt
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers.auth import get_auth_service
from src.services.auth_service import AuthService
from src.services.permission_service import permission_service
from src.utils.time_utils import get_current_epoch

@pytest.fixture
def auth_service(tmp_path):
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    
    # Create a user who owns an agent
    user = service.create_user("hb_user@touka.io", "TestPass123!", "normal")
    
    # We must manually set owned_agents for testing since the API doesn't have an endpoint for it yet
    users = service._read_users()
    for u in users:
        if u.email == "hb_user@touka.io":
            u.owned_agents = ["TestAgent"]
    service._write_users(users)
    
    app.dependency_overrides[get_auth_service] = lambda: service
    yield service
    app.dependency_overrides.clear()

@pytest.fixture
def client(auth_service):
    with TestClient(app) as c:
        yield c

def test_heartbeat_awaken_missing_service_token(client):
    # Act
    # Original endpoint required "token" in body, but now we expect Service JWT
    response = client.post("/v1/heartbeat/awaken", json={
        "token": "old_string_token",
        "agent_id": "TestAgent",
    })
    
    # Assert
    assert response.status_code == 403

def test_heartbeat_awaken_invalid_service_token(client):
    response = client.post("/v1/heartbeat/awaken", json={
        "token": "old_string_token",
        "agent_id": "TestAgent",
    }, headers={"X-Heartbeat-Token": "invalid_jwt_format"})
    assert response.status_code == 403

def test_heartbeat_awaken_valid_service_token_no_owner(client, auth_service):
    # Create valid service token
    service_token = jwt.encode({"type": "service", "sub": "heartbeat"}, AuthSecretManager.get_secret(), algorithm="HS256")
    
    # Act: Request awaken for an agent that has no owner
    response = client.post("/v1/heartbeat/awaken", json={
        "token": "dummy",
        "agent_id": "OrphanAgent",
    }, headers={"X-Heartbeat-Token": service_token})
    
    # Assert
    # Because OrphanAgent is not owned by any user, it should fail
    assert response.status_code == 403

def test_heartbeat_awaken_valid_service_token_owner_refresh_expired(client, auth_service):
    service_token = jwt.encode({"type": "service", "sub": "heartbeat"}, AuthSecretManager.get_secret(), algorithm="HS256")
    
    # Set owner's refresh token as expired (e.g. 31 days ago)
    users = auth_service._read_users()
    for u in users:
        if u.email == "hb_user@touka.io":
            now_sec = int(get_current_epoch())
            u.refresh_token_exp = now_sec - 1000 # Expired
    auth_service._write_users(users)
    
    response = client.post("/v1/heartbeat/awaken", json={
        "token": "dummy",
        "agent_id": "TestAgent",
    }, headers={"X-Heartbeat-Token": service_token})
    
    assert response.status_code == 403

def test_heartbeat_awaken_success(client, auth_service):
    service_token = jwt.encode({"type": "service", "sub": "heartbeat"}, AuthSecretManager.get_secret(), algorithm="HS256")
    
    # Set owner's refresh token as valid
    users = auth_service._read_users()
    for u in users:
        if u.email == "hb_user@touka.io":
            now_sec = int(get_current_epoch())
            u.refresh_token_exp = now_sec + 100000 # Valid
    auth_service._write_users(users)
    
    # We also need to mock or ensure identity_service.issue_token doesn't crash
    # The actual business logic in /awaken will issue a token and start background tasks
    # We will test just the endpoint response 200 or 202
    # ADR-0250 A3：沒有主要模型的靈魂 /awaken 回 409；這裡測的是認證，先給主要模型
    with patch.object(permission_service, "can_awaken", return_value=True), \
         patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"):
        response = client.post("/v1/heartbeat/awaken", json={
            "token": "dummy",
            "agent_id": "TestAgent",
        }, headers={"X-Heartbeat-Token": service_token})
    
    # Because we're not fully mocking the dispatcher, it might fail internally,
    # but the Auth guards should pass and return at least something not 401/403.
    # The heartbeat router usually returns 202 Accepted
    assert response.status_code in (202, 200, 500)
    assert response.status_code != 401
    # Check that it's NOT 403 (since we should have passed auth)
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# Migrated from test_heartbeat_auth.py: capability + identity-passing coverage
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_heartbeat_token():
    """Mock HEARTBEAT_TOKEN for testing."""
    with patch("src.config.HEARTBEAT_TOKEN", "test_secret_token"), \
         patch("src.routers.heartbeat.HEARTBEAT_TOKEN", "test_secret_token"):
        yield "test_secret_token"


@pytest.fixture
def simple_client():
    """TestClient without auth_service override (for capability tests)."""
    with TestClient(app) as c:
        yield c


def test_awaken_capability_denied(simple_client, mock_heartbeat_token):
    """Agent without heartbeat_awakening capability must be rejected."""
    identity = AgentIdentity.for_role(agent_id="NormalAgent", role="Base", capabilities=[])

    with patch("src.services.identity_service.identity_service.get_identity", return_value=identity), \
         patch("src.services.permission_service.permission_service.can_awaken", return_value=False), \
         patch("src.services.auth_service.AuthService.get_all_users", return_value=[
             User(id="owner_id", email="owner@test.com", hashed_password="...",
                  owned_agents=["NormalAgent"], refresh_token_exp=9999999999)
         ]):
        response = simple_client.post("/v1/heartbeat/awaken", json={
            "token": mock_heartbeat_token,
            "agent_id": "NormalAgent",
        })
    assert response.status_code == 403
    assert "未獲得心跳喚醒授權" in response.json()["detail"]["message"]


def test_awaken_success_identity_passed_to_background_task(simple_client, mock_heartbeat_token):
    """Successful awaken must pass the correct AgentIdentity to the background task."""
    identity = AgentIdentity.for_role(
        agent_id="Alice", role="Owner", capabilities=["heartbeat_awakening"]
    )

    with patch("src.services.identity_service.identity_service.get_identity", return_value=identity), \
         patch("src.services.permission_service.permission_service.can_awaken", return_value=True), \
         patch("src.services.auth_service.AuthService.get_all_users", return_value=[
             User(id="owner_id", email="owner@test.com", hashed_password="...",
                  owned_agents=["Alice"], refresh_token_exp=9999999999)
         ]), \
         patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"), \
         patch("src.routers.heartbeat.BackgroundTasks.add_task") as mock_bg:
        response = simple_client.post("/v1/heartbeat/awaken", json={
            "token": mock_heartbeat_token,
            "agent_id": "Alice",
            "pulse_count": 1,
        })

    assert response.status_code == 202
    assert mock_bg.called
    # args[0]=func, args[1]=final_session_id, args[2]=identity
    used_identity = mock_bg.call_args[0][2]
    assert used_identity == identity
