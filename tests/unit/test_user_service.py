
import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.services.auth_service import AuthService
from src.services.user_service import UserService


def test_delete_nonexistent_user_raises_clean_404(user_service):
    """Deleting a missing user must raise a clean 404, not crash with AttributeError
    (the old code used ErrorCode.NOT_FOUND, which is absent from errors.json)."""
    with pytest.raises(ToukaException) as ei:
        user_service.delete_user("does-not-exist")
    assert ei.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert ei.value.status_code == 404


@pytest.fixture
def temp_users_file(tmp_path):
    f = tmp_path / "test_users.json"
    return str(f)

@pytest.fixture
def user_service(temp_users_file):
    auth = AuthService(users_file_path=temp_users_file)
    return UserService(auth_service=auth)

def test_create_user_with_extended_fields(user_service):
    user = user_service.create_user(
        email="test@example.com",
        password="password123",
        display_name="Test User",
        bio="Hello world",
        avatar_url="http://avatar.com/1.png"
    )
    
    assert user.email == "test@example.com"
    assert user.display_name == "Test User"
    assert user.bio == "Hello world"
    assert user.avatar_url == "http://avatar.com/1.png"
    assert user.status == "active"
    assert user.created_at is not None

def test_update_user_profile_safe_fields(user_service):
    user = user_service.create_user("test@example.com", "TestPass123!")
    
    updated = user_service.update_user_profile(user.id, {
        "display_name": "New Name",
        "bio": "New Bio",
        "role": "admin" # Should be ignored
    })
    
    assert updated.display_name == "New Name"
    assert updated.bio == "New Bio"
    assert updated.role == "normal" # Safety check

def test_admin_update_user(user_service):
    user = user_service.create_user("test@example.com", "TestPass123!")
    
    updated = user_service.admin_update_user(user.id, {
        "role": "admin",
        "status": "suspended",
        "owned_agents": ["Alice", "Bob"]
    })
    
    assert updated.role == "admin"
    assert updated.status == "suspended"
    assert updated.owned_agents == ["Alice", "Bob"]

def test_delete_user_prevent_last_admin(user_service):
    # Create an admin
    admin = user_service.create_user("admin@touka.io", "TestPass123!", role="admin")
    
    # Try to delete the only admin
    with pytest.raises(ToukaException) as exc:
        user_service.delete_user(admin.id)
    assert "last administrator" in exc.value.detail["message"].lower()
    
    # Create another admin
    admin2 = user_service.create_user("admin2@touka.io", "TestPass123!", role="admin")
    
    # Now can delete the first one
    user_service.delete_user(admin.id)
    users = user_service.get_users()
    assert len(users) == 1
    assert users[0].id == admin2.id

def test_admin_update_user_prevent_sole_admin_demote(user_service):
    """P1: demoting the only admin to normal is blocked (lockout prevention)."""
    admin = user_service.create_user("admin@touka.io", "TestPass123!", role="admin")
    with pytest.raises(ToukaException) as exc:
        user_service.admin_update_user(admin.id, {"role": "normal"})
    assert "last administrator" in exc.value.detail["message"].lower()


def test_admin_update_user_prevent_sole_admin_suspend(user_service):
    """P1: suspending the only admin is blocked (a suspended admin cannot log in)."""
    admin = user_service.create_user("admin@touka.io", "TestPass123!", role="admin")
    with pytest.raises(ToukaException) as exc:
        user_service.admin_update_user(admin.id, {"status": "suspended"})
    assert "last administrator" in exc.value.detail["message"].lower()


def test_admin_update_user_demote_non_sole_admin_success(user_service):
    """P1: with two admins, demoting one is allowed (not the last)."""
    a1 = user_service.create_user("admin1@touka.io", "TestPass123!", role="admin")
    user_service.create_user("admin2@touka.io", "TestPass123!", role="admin")
    updated = user_service.admin_update_user(a1.id, {"role": "normal"})
    assert updated.role == "normal"


def test_get_user_not_found(user_service):
    with pytest.raises(ToukaException) as exc:
        user_service.get_user("non-existent-id")
    assert "not found" in exc.value.detail["message"].lower()
