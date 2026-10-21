"""修復 0＋修復 1 — 密碼變更安全（07-11 事故清單 #0/#1；實作前＝紅）。

事故（2026-07-11 14:08）：使用者管理頁編輯 owned_agents，表單把未觸碰的密碼欄以
``password: ""`` 送出（UI 提示「留空保持不變」——承諾沒被任何程式碼兌現）；後端
``admin_update_user`` 判準是 ``if "password" in data``（看鍵不看值）→ 兩個 admin
帳號的密碼被覆蓋成 ``bcrypt("")``＋refresh 被強制登出 → 一小時後全系統鎖死。

修法（服務層 SSOT，涵蓋所有呼叫者、openapi 零變動）：
1. ``admin_update_user``：空／缺密碼＝「保持不變」（UI 承諾成真）；非空才變更。
2. 伺服器端密碼策略 ``validate_new_password``（非空且 ≥8）：``create_user``／
   ``admin_update_user``／``change_own_password`` 三寫入點共用（自助端點的
   pydantic min_length=8 保留＝縱深防禦）。
3. 真密碼變更的既有語意保留：強制登出（refresh_token_exp=None）照舊。

修復 1（事故餘波「一小時後才鎖死」的另一半）：改密碼只清 refresh，**既有 access
token 仍可存活最長 1 小時**（TTL=3600s 的無狀態 JWT）——被害者前端毫不知情、
被竊 token 也照用。修法：access token 加 ``iat``＋``User.password_changed_at``
時間戳，兩道 Bearer 閘（routers/auth.get_current_user 與
identity_service.verify_token 人類分支）一致拒絕「簽發於改密碼之前」的 token；
無 ``iat`` 的舊制 token 在時間戳已設定時一律 fail-closed。

全走 conftest 沙箱 users.json。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.admin import require_admin
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.user_service import UserService
from src.utils.time_utils import get_current_epoch

OLD_PW = "OriginalPass123!"
NEW_PW = "BrandNewPass456!"

ADMIN_IDENTITY = AgentIdentity.for_role(agent_id="PwFixAdmin", role="admin", is_human=True)


@pytest.fixture
def services():
    auth = AuthService()
    return auth, UserService(auth_service=auth)


def _mk_user(auth: AuthService, email: str, role: str = "normal"):
    return auth.create_user(email, OLD_PW, role=role)


# ── admin_update_user：空/缺密碼＝不變更（事故直接對釘）──────────────────────────


def test_empty_password_in_update_keeps_old_password(services):
    """password="" → 密碼不變、不強制登出（「留空保持不變」成為真話）。

    現況紅：hash_password("") 覆蓋 → 舊密碼失效＋refresh 被清＝事故重演。
    """
    auth, users = services
    user = _mk_user(auth, "pw-empty@test.com")
    tokens = auth.authenticate_user("pw-empty@test.com", OLD_PW)  # 建立 refresh 狀態
    assert tokens["access_token"]

    users.admin_update_user(user.id, {"owned_agents": ["Sumi"], "password": ""})

    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(OLD_PW, refreshed.hashed_password), "空密碼不得覆蓋舊密碼"
    assert refreshed.refresh_token_exp is not None, "未變更密碼不得強制登出"
    assert refreshed.owned_agents == ["Sumi"]  # 其他欄位照常更新


def test_absent_password_key_keeps_old_password(services):
    """payload 無 password 鍵 → 不變更（既有行為回歸鎖）。"""
    auth, users = services
    user = _mk_user(auth, "pw-absent@test.com")
    users.admin_update_user(user.id, {"display_name": "新名字"})
    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(OLD_PW, refreshed.hashed_password)


def test_short_password_in_update_rejected_atomically(services):
    """短密碼（<8）→ VALIDATION_ERROR，且整筆更新原子拒絕（其他欄位也不落盤）。"""
    auth, users = services
    user = _mk_user(auth, "pw-short@test.com")
    with pytest.raises(ToukaException):
        users.admin_update_user(user.id, {"display_name": "不該落盤", "password": "abc"})
    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(OLD_PW, refreshed.hashed_password)
    assert refreshed.display_name != "不該落盤"


def test_real_password_change_still_works_and_forces_logout(services):
    """合法變更：新密碼生效、舊密碼失效、強制登出（refresh 清空）語意保留。"""
    auth, users = services
    user = _mk_user(auth, "pw-real@test.com")
    auth.authenticate_user("pw-real@test.com", OLD_PW)

    users.admin_update_user(user.id, {"password": NEW_PW})

    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(NEW_PW, refreshed.hashed_password)
    assert not auth.verify_password(OLD_PW, refreshed.hashed_password)
    assert refreshed.refresh_token_exp is None, "真密碼變更必須維持強制登出"


# ── 伺服器端密碼策略：三寫入點 ───────────────────────────────────────────────────


def test_create_user_rejects_empty_and_short_password(services):
    auth, _users = services
    with pytest.raises(ToukaException):
        auth.create_user("pw-create-empty@test.com", "")
    with pytest.raises(ToukaException):
        auth.create_user("pw-create-short@test.com", "abc")
    assert auth.get_user_by_email("pw-create-empty@test.com") is None
    assert auth.get_user_by_email("pw-create-short@test.com") is None


def test_change_own_password_rejects_short_new_password(services):
    """服務層縱深防禦（端點 pydantic min_length 之下再守一層）。"""
    auth, users = services
    user = _mk_user(auth, "pw-own@test.com")
    with pytest.raises(ToukaException):
        users.change_own_password(user.id, OLD_PW, "abc")
    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(OLD_PW, refreshed.hashed_password)


def test_change_own_password_valid_still_works(services):
    auth, users = services
    user = _mk_user(auth, "pw-own-ok@test.com")
    users.change_own_password(user.id, OLD_PW, NEW_PW)
    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(NEW_PW, refreshed.hashed_password)
    assert refreshed.refresh_token_exp is None  # 改密碼＝強制重新登入（DA25 語意保留）


# ── 端點級整合：事故的精確重演（PATCH 帶空密碼）───────────────────────────────────


def test_patch_user_with_empty_password_endpoint_keeps_login_working():
    """PATCH /v1/admin/users/{id} 帶 password:"" → 200、其他欄位更新、密碼不變。

    ＝2026-07-11 14:08 事故的端到端對釘：編輯歸屬權不再產生「一小時後鎖死」。
    """
    auth = AuthService()
    user = auth.create_user("pw-endpoint@test.com", OLD_PW, role="normal")

    app.dependency_overrides[require_admin] = lambda: ADMIN_IDENTITY
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.patch(
                f"/v1/admin/users/{user.id}",
                json={"owned_agents": ["Sumi"], "password": ""},
            )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()

    refreshed = auth.get_user_by_id(user.id)
    assert auth.verify_password(OLD_PW, refreshed.hashed_password)
    assert refreshed.owned_agents == ["Sumi"]
    # 密碼未變 → 仍可正常登入（事故的反命題）
    tokens = auth.authenticate_user("pw-endpoint@test.com", OLD_PW)
    assert tokens["access_token"]


# ── 修復 1：改密碼即撤 access token（iat < password_changed_at ⇒ 拒絕）──────────────


def _craft_access_token(user_id: str, *, iat_offset: int | None, exp_offset: int = 3600) -> str:
    """鑄造測試用人類 access token；``iat_offset=None`` ＝舊制無 iat token。

    直接以 jwt.encode 指定 iat（而非真登入）是為了決定性：iat 為秒級解析度，
    「登入→改密碼」全落在同一秒時無從分辨先後；真實簽發面另由
    test_access_tokens_carry_iat 與 test_new_login_after_change_end_to_end 對釘。
    """
    now = int(get_current_epoch())
    payload: dict = {"sub": user_id, "type": "access", "exp": now + exp_offset}
    if iat_offset is not None:
        payload["iat"] = now + iat_offset
    return jwt.encode(payload, AuthSecretManager.get_secret(), algorithm="HS256")


def _assert_token_alive(token: str, auth: AuthService, user_id: str) -> None:
    """兩道 Bearer 閘（/auth 系 get_current_user＋Gate 1 verify_token）都放行。"""
    assert get_current_user(token=token, auth_service=auth).id == user_id
    ident = identity_service.verify_token(token)
    assert ident is not None and ident.agent_id == user_id


def _assert_token_dead(token: str, auth: AuthService) -> None:
    """兩道 Bearer 閘都拒絕（401 / None）——單閘漏網＝修復不完整。"""
    with pytest.raises(ToukaException):
        get_current_user(token=token, auth_service=auth)
    assert identity_service.verify_token(token) is None


def test_access_tokens_carry_iat(services):
    """登入與 refresh 簽發的 access token 都必須帶 iat（撤銷判準的載體）。"""
    auth, _users = services
    _mk_user(auth, "iat-pin@test.com")
    tokens = auth.authenticate_user("iat-pin@test.com", OLD_PW)
    secret = AuthSecretManager.get_secret()
    payload = jwt.decode(tokens["access_token"], secret, algorithms=["HS256"])
    assert isinstance(payload.get("iat"), int)
    rotated = auth.refresh_access_token(tokens["refresh_token"])
    payload2 = jwt.decode(rotated["access_token"], secret, algorithms=["HS256"])
    assert isinstance(payload2.get("iat"), int)


def test_admin_password_change_revokes_existing_access_tokens(services):
    """admin 幫使用者改密碼 → 改前簽發的 access token 於兩道閘立即失效。

    現況紅：改密碼只清 refresh，舊 access 存活到 TTL 盡頭（1h 定時炸彈）。
    """
    auth, users = services
    user = _mk_user(auth, "revoke-admin@test.com")
    old_token = _craft_access_token(user.id, iat_offset=-100)
    _assert_token_alive(old_token, auth, user.id)

    users.admin_update_user(user.id, {"password": NEW_PW})

    _assert_token_dead(old_token, auth)


def test_own_password_change_revokes_existing_access_tokens(services):
    """自助改密碼同樣即時撤銷既有 access token（含被竊 token 情境）。"""
    auth, users = services
    user = _mk_user(auth, "revoke-own@test.com")
    old_token = _craft_access_token(user.id, iat_offset=-100)
    _assert_token_alive(old_token, auth, user.id)

    users.change_own_password(user.id, OLD_PW, NEW_PW)

    _assert_token_dead(old_token, auth)


def test_legacy_token_without_iat_fails_closed_after_change(services):
    """無 iat 的舊制 token：時間戳未設時放行（相容），已設時一律拒絕（fail-closed）。"""
    auth, users = services
    user = _mk_user(auth, "revoke-legacy@test.com")
    legacy = _craft_access_token(user.id, iat_offset=None)
    _assert_token_alive(legacy, auth, user.id)  # 改密碼前：舊制 token 照常可用

    users.change_own_password(user.id, OLD_PW, NEW_PW)

    _assert_token_dead(legacy, auth)


def test_empty_password_update_does_not_revoke_access_tokens(services):
    """修復 0 ∩ 修復 1：空密碼「保持不變」也絕不誤撤 token（誤殺＝另一種鎖死事故）。"""
    auth, users = services
    user = _mk_user(auth, "revoke-empty@test.com")
    token = _craft_access_token(user.id, iat_offset=-100)

    users.admin_update_user(user.id, {"owned_agents": ["Sumi"], "password": ""})

    _assert_token_alive(token, auth, user.id)
    assert auth.get_user_by_id(user.id).password_changed_at is None


def test_profile_only_update_does_not_revoke_access_tokens(services):
    """非密碼欄位更新（display_name 等）不觸發撤銷。"""
    auth, users = services
    user = _mk_user(auth, "revoke-profile@test.com")
    token = _craft_access_token(user.id, iat_offset=-100)

    users.admin_update_user(user.id, {"display_name": "改個名字"})

    _assert_token_alive(token, auth, user.id)
    assert auth.get_user_by_id(user.id).password_changed_at is None


def test_new_login_after_change_end_to_end(services):
    """真實流程對釘：改密碼後，舊「真簽發」token 死、新登入 token 活。

    iat 為秒級解析度——sleep 1.1s 確保改密碼落在舊 token 簽發之後的下一秒
    （同秒邊界 iat == changed_at 依設計放行，讓改完密碼立即重登不受阻）。
    """
    auth, users = services
    user = _mk_user(auth, "revoke-e2e@test.com")
    old_tokens = auth.authenticate_user("revoke-e2e@test.com", OLD_PW)
    time.sleep(1.1)

    users.change_own_password(user.id, OLD_PW, NEW_PW)

    _assert_token_dead(old_tokens["access_token"], auth)
    new_tokens = auth.authenticate_user("revoke-e2e@test.com", NEW_PW)
    _assert_token_alive(new_tokens["access_token"], auth, user.id)
