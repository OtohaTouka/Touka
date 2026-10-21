"""TDD: Refresh Token Rotation 並發 Grace Period 修復。

多 Tab 同時發出 refresh 請求時，所有請求攜帶相同舊 JTI。
第一個請求成功輪換後，後續在 AUTH_REFRESH_GRACE_PERIOD_SEC 內的並發請求
不應被誤判為 Replay Attack 而導致 session 撤銷。

Grace period 過期後，舊 JTI 仍會觸發撤銷（安全邊界不變）。

設計注意：
- 登入（authenticate_user）不 mock 時間，使 JWT exp 基於真實系統時鐘（30 天後到期）
- refresh_access_token 呼叫才 mock get_current_epoch，控制「now_sec」的業務邏輯時間
- 兩者相互獨立：jwt.decode 的 exp 驗證使用真實時鐘，業務邏輯使用 mock 時鐘
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src.config import AUTH_REFRESH_GRACE_PERIOD_SEC
from src.core.exceptions import ErrorCode, ToukaException
from src.services.auth_service import AuthService
from src.utils.time_utils import get_current_epoch


def _make_service(tmp_path):
    return AuthService(users_file_path=str(tmp_path / "users.json"))


def _login(svc: AuthService) -> dict:
    """登入使用真實時間，確保 JWT exp 對真實系統時鐘有效（30 天後到期）。"""
    svc.create_user("tab@test.io", "Password123!")
    return svc.authenticate_user("tab@test.io", "Password123!")


def _now_plus(offset: int) -> float:
    """回傳當前 epoch + offset（秒），用於 mock get_current_epoch。"""
    return get_current_epoch() + offset


def test_normal_sequential_rotation_still_works(tmp_path):
    """正常單一 Tab 順序輪換：T+1 輪換後，T+2 繼續用新 token 輪換，鏈式操作正常。"""
    svc = _make_service(tmp_path)
    tokens = _login(svc)
    old_refresh = tokens["refresh_token"]

    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(1)):
        new_tokens = svc.refresh_access_token(old_refresh)
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens

    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(2)):
        final_tokens = svc.refresh_access_token(new_tokens["refresh_token"])
    assert "access_token" in final_tokens


def test_concurrent_second_request_within_grace_succeeds(tmp_path):
    """第二個並發請求在 grace 內攜帶相同舊 JTI，不應觸發 Replay 撤銷，應正常取得新 token。"""
    svc = _make_service(tmp_path)
    tokens = _login(svc)
    old_refresh = tokens["refresh_token"]

    # Tab 1：T+1 輪換成功
    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(1)):
        svc.refresh_access_token(old_refresh)

    # Tab 2：T+5（grace 內）帶同一舊 JTI → 應成功
    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(5)):
        tab2_tokens = svc.refresh_access_token(old_refresh)

    assert "access_token" in tab2_tokens
    assert "refresh_token" in tab2_tokens

    user = svc.get_user_by_email("tab@test.io")
    assert user.refresh_token_exp is not None, "Session must NOT be revoked for concurrent request"


def test_three_concurrent_tabs_within_grace_all_succeed(tmp_path):
    """3 個並發 Tab 在 grace 內帶同一舊 JTI，全部應成功取得新 token。"""
    svc = _make_service(tmp_path)
    tokens = _login(svc)
    old_refresh = tokens["refresh_token"]

    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(1)):
        svc.refresh_access_token(old_refresh)

    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(2)):
        tokens2 = svc.refresh_access_token(old_refresh)
    assert "access_token" in tokens2

    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(3)):
        tokens3 = svc.refresh_access_token(old_refresh)
    assert "access_token" in tokens3

    user = svc.get_user_by_email("tab@test.io")
    assert user.refresh_token_exp is not None


def test_previous_jti_after_grace_triggers_reuse(tmp_path):
    """Grace period 過期後，舊 JTI 應被視為 Replay 攻擊並撤銷 session。"""
    svc = _make_service(tmp_path)
    tokens = _login(svc)
    old_refresh = tokens["refresh_token"]

    # 合法輪換（T+1）— 設定 previous_jti 與 rotated_at
    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(1)):
        svc.refresh_access_token(old_refresh)

    # Grace 過期後重用舊 JTI（T+1+GRACE+1）
    with patch(
        "src.services.auth_service.get_current_epoch",
        return_value=_now_plus(1 + AUTH_REFRESH_GRACE_PERIOD_SEC + 1),
    ), pytest.raises(ToukaException) as exc_info:
        svc.refresh_access_token(old_refresh)

    assert exc_info.value.code == ErrorCode.AUTH_REFRESH_TOKEN_REUSE

    user = svc.get_user_by_email("tab@test.io")
    assert user.refresh_token_exp is None, "Session must be revoked after grace-expired replay"


def test_replay_from_unrelated_old_jti_triggers_reuse(tmp_path):
    """與 previous_jti 無關的真正舊 token（J0，但 current=J2, previous=J1）應立即觸發撤銷。"""
    svc = _make_service(tmp_path)
    tokens = _login(svc)
    stale_refresh = tokens["refresh_token"]  # JTI = J0

    # 輪換鏈：J0 → J1
    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(1)):
        round1 = svc.refresh_access_token(stale_refresh)

    # 輪換鏈：J1 → J2（previous 改為 J1，J0 已不在 previous 中）
    with patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(2)):
        svc.refresh_access_token(round1["refresh_token"])

    # 攻擊者重播 J0（current=J2, previous=J1，J0 不符）→ 撤銷
    with (
        patch("src.services.auth_service.get_current_epoch", return_value=_now_plus(3)),
        pytest.raises(ToukaException) as exc_info,
    ):
        svc.refresh_access_token(stale_refresh)

    assert exc_info.value.code == ErrorCode.AUTH_REFRESH_TOKEN_REUSE

    user = svc.get_user_by_email("tab@test.io")
    assert user.refresh_token_exp is None, "Session must be revoked for stale JTI replay"
