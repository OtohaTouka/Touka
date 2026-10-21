"""TDD: UserRecord is_temp_password field, ensure_admin_user bootstrap, and password change clearing."""

import json
import logging

import pytest

from src.config import get_initial_admin_password_file
from src.models.user import User
from src.services.auth_service import AuthService
from src.services.user_service import UserService


# ---------------------------------------------------------------------------
# 1. Model field defaults
# ---------------------------------------------------------------------------

def test_user_model_is_temp_password_defaults_false():
    """is_temp_password must default to False for any newly-constructed User."""
    user = User(id="test-id", email="test@example.com", hashed_password="hash")
    assert user.is_temp_password is False


def test_user_model_is_temp_password_can_be_set_true():
    """is_temp_password can be set to True."""
    user = User(id="test-id", email="test@example.com", hashed_password="hash", is_temp_password=True)
    assert user.is_temp_password is True


# ---------------------------------------------------------------------------
# 2. Backward-compatibility: old users.json without the field
# ---------------------------------------------------------------------------

def test_user_model_backward_compat_missing_field(tmp_path):
    """Old users.json records without is_temp_password load with False (default)."""
    users_file = tmp_path / "users.json"
    users_file.write_text(json.dumps({
        "users": [{
            "id": "abc123",
            "email": "admin@touka.io",
            "hashed_password": "$2b$12$somehash",
            "role": "admin",
        }]
    }))
    service = AuthService(users_file_path=str(users_file))
    users = service.get_all_users()
    assert len(users) == 1
    assert users[0].is_temp_password is False


# ---------------------------------------------------------------------------
# 3. ensure_admin_user sets is_temp_password=True
# ---------------------------------------------------------------------------

def test_ensure_admin_user_sets_temp_password_flag(tmp_path):
    """ensure_admin_user must create admin with is_temp_password=True."""
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    service.ensure_admin_user([])

    admin = next((u for u in service.get_all_users() if u.role == "admin"), None)
    assert admin is not None
    assert admin.is_temp_password is True


def test_ensure_admin_user_is_idempotent(tmp_path):
    """Calling ensure_admin_user twice must not duplicate the admin record."""
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    service.ensure_admin_user([])
    service.ensure_admin_user([])

    admins = [u for u in service.get_all_users() if u.role == "admin"]
    assert len(admins) == 1


def test_ensure_admin_user_writes_password_file_not_log(tmp_path, caplog):
    """DA21: the initial admin password must go to a one-time file, NOT be logged in
    plaintext to the persistent log. The WARNING may name the file path only."""
    pw_file = get_initial_admin_password_file()
    pw_file.unlink(missing_ok=True)  # clean slate (config dir is the test sandbox)

    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    with caplog.at_level(logging.WARNING, logger="src.services.auth_service"):
        service.ensure_admin_user([])

    # 1. one-time file exists and holds a password that actually authenticates
    assert pw_file.exists()
    content = pw_file.read_text(encoding="utf-8")
    pw = next(
        line.split(":", 1)[1].strip()
        for line in content.splitlines()
        if line.lower().startswith("password")
    )
    admin = next(u for u in service.get_all_users() if u.role == "admin")
    assert service.verify_password(pw, admin.hashed_password)

    # 2. the plaintext password must NOT appear in the persistent log
    log_text = "\n".join(r.message for r in caplog.records)
    assert pw not in log_text
    # 3. the log should still point the operator at the file
    assert "INITIAL_ADMIN_PASSWORD" in log_text

    pw_file.unlink(missing_ok=True)  # cleanup


def test_admin_password_change_deletes_initial_file(tmp_path):
    """DA21: changing the admin password deletes the one-time bootstrap file."""
    pw_file = get_initial_admin_password_file()
    pw_file.unlink(missing_ok=True)

    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    auth_service.ensure_admin_user([])
    assert pw_file.exists()  # written on bootstrap

    admin = next(u for u in auth_service.get_all_users() if u.role == "admin")
    user_service = UserService(auth_service=auth_service)
    user_service.admin_update_user(admin.id, {"password": "NewStrongPass1!"})

    assert not pw_file.exists()  # auto-deleted on first password change


def test_ensure_admin_does_not_overwrite_existing_admin(tmp_path):
    """ensure_admin_user must skip creation if an admin already exists."""
    users_file = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users_file))
    service.ensure_admin_user([])

    original_admin = next(u for u in service.get_all_users() if u.role == "admin")
    original_hash = original_admin.hashed_password

    # Second call — must not change anything
    service.ensure_admin_user([])
    admin_after = next(u for u in service.get_all_users() if u.role == "admin")
    assert admin_after.hashed_password == original_hash


# ---------------------------------------------------------------------------
# 4. Password change clears is_temp_password
# ---------------------------------------------------------------------------

def test_admin_password_change_clears_temp_flag(tmp_path):
    """admin_update_user with a new password must set is_temp_password=False."""
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    auth_service.ensure_admin_user([])

    admin = next(u for u in auth_service.get_all_users() if u.role == "admin")
    assert admin.is_temp_password is True

    user_service = UserService(auth_service=auth_service)
    user_service.admin_update_user(admin.id, {"password": "new_secure_password_123"})

    updated = auth_service.get_user_by_id(admin.id)
    assert updated is not None
    assert updated.is_temp_password is False


def test_non_password_update_preserves_temp_flag(tmp_path):
    """Updating non-password fields must NOT touch is_temp_password."""
    users_file = tmp_path / "users.json"
    auth_service = AuthService(users_file_path=str(users_file))
    auth_service.ensure_admin_user([])

    admin = next(u for u in auth_service.get_all_users() if u.role == "admin")
    assert admin.is_temp_password is True

    user_service = UserService(auth_service=auth_service)
    user_service.admin_update_user(admin.id, {"display_name": "Admin User"})

    updated = auth_service.get_user_by_id(admin.id)
    assert updated is not None
    assert updated.is_temp_password is True  # flag preserved
