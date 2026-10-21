"""Unit tests for Logout logic and activity tracking (SSOT)."""


import pytest
from src.core.exceptions import ToukaException
from src.services.auth_service import AuthService


@pytest.fixture
def auth_service(tmp_path):
    """建立模擬的 AuthService 並指向暫存 users.json。"""
    users_file = tmp_path / "users.json"
    return AuthService(users_file_path=str(users_file))

def test_login_updates_last_active_at(auth_service):
    """驗證登入成功後，會自動更新 last_active_at 欄位。"""
    email = "test@touka.io"
    password = "password123"
    auth_service.create_user(email, password)
    
    # 執行第一次登入
    auth_service.authenticate_user(email, password)
    
    user = auth_service.get_user_by_email(email)
    assert user.last_active_at is not None
    first_active = user.last_active_at
    
    # 這裡可以透過模擬時間來驗證更新，但目前先驗證其存在性
    assert first_active > 0

def test_logout_removes_refresh_token_exp(auth_service):
    """驗證登出後，會移除伺服器端紀錄的 refresh_token_exp 以失效該 Token。"""
    email = "logout@touka.io"
    password = "password123"
    auth_service.create_user(email, password)
    
    # 登入獲取 Refresh Token 效期
    auth_service.authenticate_user(email, password)
    user = auth_service.get_user_by_email(email)
    assert user.refresh_token_exp is not None
    
    # 執行登出 (這裡測試 Service 層邏輯)
    auth_service.revoke_user_tokens(user.id)
    
    logged_out_user = auth_service.get_user_by_id(user.id)
    assert logged_out_user.refresh_token_exp is None

def test_refresh_token_fails_after_logout(auth_service):
    """驗證登出後，使用舊的 Refresh Token 進行 refresh 會失敗。"""
    email = "fail@touka.io"
    password = "password123"
    auth_service.create_user(email, password)
    
    tokens = auth_service.authenticate_user(email, password)
    refresh_token = tokens["refresh_token"]
    
    # 登出
    user = auth_service.get_user_by_email(email)
    auth_service.revoke_user_tokens(user.id)
    
    # 嘗試重新整理 Token，應拋出異常
    with pytest.raises(ToukaException):
        auth_service.refresh_access_token(refresh_token)

