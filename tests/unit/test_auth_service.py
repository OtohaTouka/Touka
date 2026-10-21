
import pytest
from src.core.exceptions import ToukaException
from src.services.auth_service import AuthService
from src.utils.time_utils import get_current_epoch


def test_password_hashing():
    auth_service = AuthService(users_file_path="dummy.json")
    plain_password = "my_secret_password"
    
    hashed = auth_service.hash_password(plain_password)
    
    assert hashed != plain_password
    assert len(hashed) > 20
    
    assert auth_service.verify_password(plain_password, hashed) is True
    assert auth_service.verify_password("wrong_password", hashed) is False

def test_long_password_handling():
    auth_service = AuthService(users_file_path="dummy_long.json")
    # 100 characters, exceeds bcrypt's 72 bytes limit
    long_password = "A" * 100
    
    # Act
    hashed = auth_service.hash_password(long_password)
    
    # Assert
    assert auth_service.verify_password(long_password, hashed) is True
    assert auth_service.verify_password("wrong_password", hashed) is False

def test_user_json_crud(tmp_path):
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    
    users = auth_service.get_all_users()
    assert users == []
    
    new_user = auth_service.create_user(
        email="admin@touka.io",
        password="admin_password",
        role="admin"
    )
    
    assert new_user.email == "admin@touka.io"
    assert new_user.role == "admin"
    assert new_user.id is not None
    assert new_user.hashed_password != "admin_password"
    
    fetched = auth_service.get_user_by_email("admin@touka.io")
    assert fetched is not None
    assert fetched.id == new_user.id

def test_login_token_generation(tmp_path):
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    auth_service.create_user("test@touka.io", "testpass", "normal")
    
    tokens = auth_service.authenticate_user("test@touka.io", "testpass")
    
    assert tokens is not None
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    
    user = auth_service.get_user_by_email("test@touka.io")
    assert user.refresh_token_exp is not None
    assert user.refresh_token_exp > get_current_epoch()
def test_refresh_token_validation(tmp_path):
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    user = auth_service.create_user("test@touka.io", "testpass", "normal")
    
    tokens = auth_service.authenticate_user("test@touka.io", "testpass")
    refresh_token = tokens["refresh_token"]
    
    new_access_token = auth_service.refresh_access_token(refresh_token)
    assert new_access_token is not None
    
    with pytest.raises(ToukaException) as exc_info:
        auth_service.refresh_access_token("invalid_token")
    
    assert "AUTH_INVALID_TOKEN" in str(exc_info.value)
