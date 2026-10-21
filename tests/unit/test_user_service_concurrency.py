"""並發安全測試：UserService profile update 函式 (TDD)。

驗證 update_user_profile / admin_update_user 修復後，在鎖內完成原子讀改寫，
不再以 stale snapshot 覆蓋並發 token rotation 更新的 refresh_token_jti。
"""

from __future__ import annotations

import threading
import time

import pytest
from src.core.exceptions import ToukaException
from src.services.auth_service import AuthService
from src.services.user_service import UserService


@pytest.fixture
def temp_users_file(tmp_path):
    f = tmp_path / "test_users.json"
    return str(f)


@pytest.fixture
def user_service(temp_users_file):
    auth = AuthService(users_file_path=temp_users_file)
    return UserService(auth_service=auth)


@pytest.fixture
def user_with_token(user_service):
    """建立一個帶有有效 refresh_token_jti 的使用者（模擬已登入狀態）。"""
    user = user_service.create_user("race@example.com", "password123")
    sentinel_jti = "sentinel-jti-must-survive"

    def _set_token(users):
        for u in users:
            if u.id == user.id:
                u.refresh_token_jti = sentinel_jti
                u.refresh_token_exp = 9_999_999_999

    user_service.auth_service._atomic_users_write(_set_token)  # noqa: SLF001
    return user.id, sentinel_jti


def test_update_profile_preserves_token_jti(user_service, user_with_token):
    """update_user_profile 不得清除或改變已存在的 refresh_token_jti。

    這是核心回歸測試：修復前 _update_user_in_list 以舊 snapshot 完整替換使用者
    記錄，導致 jti 被回滾至讀取時的值。
    """
    user_id, expected_jti = user_with_token

    updated = user_service.update_user_profile(user_id, {"display_name": "X", "bio": "B"})

    assert updated.display_name == "X"
    assert updated.bio == "B"
    assert updated.refresh_token_jti == expected_jti, (
        f"update_user_profile 不得覆蓋 refresh_token_jti，"
        f"期望 {expected_jti!r}，實際 {updated.refresh_token_jti!r}"
    )
    assert updated.refresh_token_exp == 9_999_999_999

    reloaded = user_service.get_user(user_id)
    assert reloaded.refresh_token_jti == expected_jti
    assert reloaded.display_name == "X"


def test_update_profile_race_does_not_overwrite_jti(user_service, user_with_token):
    """競態：並發 token rotation 後，jti 必須是旋轉後的值，而非被回滾。

    Barrier 確保兩個執行緒同時啟動，sleep(0.002) 讓 rotation 在
    update_user_profile 讀取後才執行，放大舊實作的競態視窗。
    修復後讀改寫全在單一鎖內，rotation 只能在 update 完成前或完成後執行，
    不會有 stale snapshot 覆蓋問題。
    """
    user_id, _ = user_with_token
    rotated_jti = "rotated-jti-by-concurrent-refresh"
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def profile_update():
        try:
            barrier.wait()
            user_service.update_user_profile(user_id, {"display_name": "Raced"})
        except Exception as exc:
            errors.append(exc)

    def token_rotation():
        try:
            barrier.wait()
            time.sleep(0.002)  # 讓 profile_update 先讀取（放大舊實作的 race window）

            def _rotate(users):
                for u in users:
                    if u.id == user_id:
                        u.refresh_token_jti = rotated_jti

            user_service.auth_service._atomic_users_write(_rotate)  # noqa: SLF001
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=profile_update)
    t2 = threading.Thread(target=token_rotation)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"執行緒拋出例外：{errors}"

    final = user_service.get_user(user_id)
    assert final.display_name == "Raced"
    assert final.refresh_token_jti == rotated_jti, (
        f"update_user_profile 不得覆蓋並發旋轉的 jti，"
        f"期望 {rotated_jti!r}，實際 {final.refresh_token_jti!r}"
    )


def test_admin_update_preserves_token_jti(user_service, user_with_token):
    """admin_update_user（非密碼變更）不得清除已存在的 refresh_token_jti。"""
    user_id, expected_jti = user_with_token

    updated = user_service.admin_update_user(user_id, {"status": "suspended"})

    assert updated.status == "suspended"
    assert updated.refresh_token_jti == expected_jti, (
        f"admin_update_user 不得覆蓋 refresh_token_jti，"
        f"期望 {expected_jti!r}，實際 {updated.refresh_token_jti!r}"
    )
    assert updated.refresh_token_exp == 9_999_999_999

    reloaded = user_service.get_user(user_id)
    assert reloaded.refresh_token_jti == expected_jti
    assert reloaded.status == "suspended"


def test_admin_password_reset_intentionally_clears_token(user_service, user_with_token):
    """密碼重設時 refresh_token_exp 必須強制設為 None（安全不變量）。

    這是合法的 token 廢止路徑，不同於 stale-snapshot bug。
    修復後此行為必須維持不變。
    """
    user_id, _ = user_with_token

    updated = user_service.admin_update_user(user_id, {"password": "new-secure-pass-abc"})

    assert updated.refresh_token_exp is None, (
        "密碼重設必須廢止 refresh token（設 refresh_token_exp = None）"
    )
    assert updated.hashed_password is not None
    assert len(updated.hashed_password) > 20


def test_update_profile_raises_for_unknown_user(user_service):
    """update_user_profile 對不存在的 user_id 必須拋出 ToukaException。"""
    with pytest.raises(ToukaException) as exc_info:
        user_service.update_user_profile("nonexistent-id", {"display_name": "X"})
    assert "not found" in exc_info.value.detail["message"].lower()


def test_admin_update_raises_for_unknown_user(user_service):
    """admin_update_user 對不存在的 user_id 必須拋出 ToukaException。"""
    with pytest.raises(ToukaException) as exc_info:
        user_service.admin_update_user("nonexistent-id", {"status": "suspended"})
    assert "not found" in exc_info.value.detail["message"].lower()
