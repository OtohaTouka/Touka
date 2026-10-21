"""Test round-trip: POST upload, then GET serve with content verification."""
from types import SimpleNamespace
from fastapi.testclient import TestClient
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

def test_upload_then_get_avatar_full_roundtrip(tmp_path):
    """POST upload PNG, then GET serve and verify content bytes match."""
    # Setup with a real user
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    
    app.dependency_overrides[get_current_user] = None
    app.dependency_overrides.clear()
    
    c = TestClient(app, raise_server_exceptions=False)
    
    # 1. Register and login a user
    email = "avatar_test_user@touka.io"
    password = "testpass123"
    
    reg_resp = c.post("/auth/register", json={
        "email": email,
        "password": password,
        "role": "normal"
    })
    assert reg_resp.status_code == 200, f"Register failed: {reg_resp.text}"
    user_id = reg_resp.json()["id"]
    
    login_resp = c.post("/auth/login", json={
        "email": email,
        "password": password
    })
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    token = login_resp.json()["access_token"]
    
    # 2. Upload avatar with auth
    upload_resp = c.post(
        "/auth/avatar", 
        files={"file": ("test.png", PNG, "image/png")},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert upload_resp.status_code == 200, f"Upload failed: {upload_resp.status_code}, {upload_resp.text}"
    
    # 3. GET avatar (public, no auth needed) and verify content
    get_resp = c.get(f"/v1/users/{user_id}/avatar")
    assert get_resp.status_code == 200, f"GET failed: {get_resp.text}"
    
    # 4. Verify content-type header
    assert get_resp.headers.get("content-type") == "image/png", \
        f"Wrong content-type: {get_resp.headers.get('content-type')}"
    
    # 5. Verify content bytes match original
    assert get_resp.content == PNG, \
        f"Content mismatch: expected {len(PNG)} bytes, got {len(get_resp.content)} bytes"
