"""認證縫修復批 P3（四功能區調查 §auth）——輸入驗證＋登出撤銷＋refresh 重武裝。

調查（2026-07-15）在 07-11 事故同族找到 9 個未修站點。本檔對釘後端修復（前端
#9/#6 於 vitest）。分區：
- §1 輸入驗證：admin PATCH 顯式 null 毒化 users.json（修了空字串沒修 null，同 07-11
  形態）／email 撞號無檢查／role·status 無枚舉／create_user 兩段寫非原子。
- §2 登出撤銷（#2 high，owner 選完整方案）：過期卡點登出＝後端撤銷被跳過（logout
  需有效 Bearer）＋登出後 access 殘活 1h（無 tokens_revoked_at）。
- §3 refresh 重武裝（#7）：舊密碼並發登入在改密碼後重新武裝 refresh／輪替不檢查水位。

全走 conftest 沙箱 users.json。
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User  # noqa: TC002
from src.routers.admin import require_admin
from src.services.auth_service import AuthService, access_token_is_stale
from src.services.user_service import UserService
from src.utils.time_utils import get_current_epoch

PW = "SeamHardPass123!"
ADMIN_IDENTITY = AgentIdentity.for_role(agent_id="SeamAdmin", role="admin", is_human=True)


@pytest.fixture(autouse=True)
def _clean_users():
    """每測試起點清空 users.json（消除順序依賴＝隨機次序不 flake，比照 ADR-0144 教訓）。"""
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001
    yield


@pytest.fixture
def auth() -> AuthService:
    return AuthService()


@pytest.fixture
def admin_client(auth):
    """TestClient 以既有真實 admin 使用者過 require_admin（末位 admin 守衛需真帳號）。"""
    admin_user = auth.create_user("seam-admin@test.com", PW, role="admin")
    app.dependency_overrides[require_admin] = lambda: ADMIN_IDENTITY
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, admin_user
    app.dependency_overrides.clear()


# ── §1 輸入驗證 ───────────────────────────────────────────────────────────────


def test_admin_patch_explicit_null_role_does_not_poison(admin_client):
    """PATCH {"role": null} 不得把 role 寫成 JSON null → 下次讀 users.json 仍成功。

    現況紅：exclude_unset 保留顯式 null＋setattr 無值驗證＋User.role 非 Optional →
    _read_users 時 User(**u) 驗證失敗 → PERSISTENCE_ERROR → 登入/refresh/認證全 500。
    """
    _client, admin_user = admin_client
    target = AuthService().create_user("seam-null@test.com", PW, role="normal")
    r = _client.patch(f"/v1/admin/users/{target.id}", json={"role": None})
    # 顯式 null 必須被拒或當作 no-op——不得毒化
    assert r.status_code in (200, 422), r.text
    # 關鍵：users.json 仍可正常讀（role 未被寫成 null）
    reread = AuthService().get_user_by_id(target.id)
    assert reread is not None
    assert reread.role == "normal"


def test_write_users_revalidates_and_fails_loud(auth):
    """_write_users 寫前逐筆重驗——毒化 user（role=None）當下 fail-loud，而非讀時全站掛。"""
    auth.create_user("seam-valid@test.com", PW)

    def _poison(users: list[User]) -> None:
        users[0].role = None  # type: ignore[assignment]

    with pytest.raises(ToukaException):
        auth._atomic_users_write(_poison)  # noqa: SLF001
    # 毒化被拒後檔案仍可讀（未落盤 null）
    assert AuthService().get_user_by_email("seam-valid@test.com") is not None


def test_admin_update_email_collision_rejected(admin_client):
    """admin 把 A 的 email 改成 B 的 email → 拒（否則 B 被 first-match 永久遮蔽）。"""
    _client, _admin = admin_client
    a = AuthService().create_user("seam-a@test.com", PW)
    AuthService().create_user("seam-b@test.com", PW)
    r = _client.patch(f"/v1/admin/users/{a.id}", json={"email": "seam-b@test.com"})
    assert r.status_code >= 400
    assert AuthService().get_user_by_id(a.id).email == "seam-a@test.com"


def test_admin_update_email_to_same_self_ok(admin_client):
    """改成自己現有 email（no-op）不得被唯一性檢查誤擋。"""
    _client, _admin = admin_client
    a = AuthService().create_user("seam-self@test.com", PW)
    r = _client.patch(f"/v1/admin/users/{a.id}", json={"email": "seam-self@test.com"})
    assert r.status_code == 200, r.text


def test_admin_update_invalid_role_rejected(admin_client):
    """role typo（如 "Admin" 大小寫錯）→ 422，不得悄悄寫入等同失權/失效的無效值。"""
    _client, _admin = admin_client
    a = AuthService().create_user("seam-role@test.com", PW, role="normal")
    r = _client.patch(f"/v1/admin/users/{a.id}", json={"role": "Admin"})
    assert r.status_code == 422, r.text
    assert AuthService().get_user_by_id(a.id).role == "normal"


def test_create_user_is_single_atomic_write(auth, monkeypatch):
    """UserService.create_user 全欄位一次原子落盤（不再 create→覆蓋兩段寫）。"""
    calls = {"n": 0}
    orig = AuthService._atomic_users_write

    def _counting(self, fn):  # noqa: ANN001
        calls["n"] += 1
        return orig(self, fn)

    monkeypatch.setattr(AuthService, "_atomic_users_write", _counting)
    UserService(auth_service=auth).create_user(
        "seam-atomic@test.com", PW, owned_agents=["X"], display_name="Y"
    )
    assert calls["n"] == 1, f"create_user 應單次原子寫入，實際 {calls['n']} 次"
    u = auth.get_user_by_email("seam-atomic@test.com")
    assert u.owned_agents == ["X"] and u.display_name == "Y"


# ── §2 登出撤銷（完整方案：tokens_revoked_at ＋ logout 憑 cookie）────────────────


def test_revoke_sets_tokens_revoked_at_and_stales_access(auth):
    """revoke_user_tokens 設 tokens_revoked_at → 既有 access token（iat 較早）即失效。"""
    user = auth.create_user("seam-revoke@test.com", PW)
    tokens = auth.authenticate_user("seam-revoke@test.com", PW)
    payload = jwt.decode(
        tokens["access_token"], AuthSecretManager.get_secret(), algorithms=["HS256"]
    )
    fresh = auth.get_user_by_id(user.id)
    assert access_token_is_stale(payload, fresh) is False  # 撤銷前有效

    auth.revoke_user_tokens(user.id)
    revoked = auth.get_user_by_id(user.id)
    assert revoked.tokens_revoked_at is not None
    # 既有 access 於登出前簽發（生產中隔一段使用時間）——iat 早於登出水位即失效。
    # 同秒邊界（iat == watermark）刻意放行以利改密碼後立即重登，故此處明示 iat 早於水位。
    aged = {**payload, "iat": revoked.tokens_revoked_at - 1}
    assert access_token_is_stale(aged, revoked) is True  # 登出即撤（不再殘活 1h）


def test_relogin_after_logout_issues_valid_access(auth):
    """登出設水位後重新登入 → 新 access（iat 較新）不得被水位判死。"""
    user = auth.create_user("seam-relogin@test.com", PW)
    auth.authenticate_user("seam-relogin@test.com", PW)
    auth.revoke_user_tokens(user.id)
    new_tokens = auth.authenticate_user("seam-relogin@test.com", PW)
    payload = jwt.decode(
        new_tokens["access_token"], AuthSecretManager.get_secret(), algorithms=["HS256"]
    )
    assert access_token_is_stale(payload, auth.get_user_by_id(user.id)) is False


def test_logout_endpoint_revokes_via_refresh_cookie_when_access_expired(auth):
    """過期 access 卡點登出：logout 端點憑 refresh cookie 撤銷（不再靜默跳過）。

    現況紅：logout 依賴 get_current_user（需有效 Bearer）→ 過期卡得 401 → revoke
    從未執行 → refresh cookie 完好 → 下次開頁 session 復活。
    """
    user = auth.create_user("seam-logout@test.com", PW)
    tokens = auth.authenticate_user("seam-logout@test.com", PW)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.cookies.set("refresh_token", tokens["refresh_token"])
        # 不帶 Authorization（模擬 access 已過期）
        r = c.post("/v1/auth/logout")
        assert r.status_code == 200, r.text
    # 後端撤銷確有執行：refresh_token_exp 清空、水位已設
    after = auth.get_user_by_id(user.id)
    assert after.refresh_token_exp is None
    assert after.tokens_revoked_at is not None


# ── §3 refresh 重武裝競態（#7）───────────────────────────────────────────────


def test_refresh_with_stale_iat_rejected_after_password_change(auth):
    """改密碼前簽發的 refresh token（iat < password_changed_at）輪替時必拒。"""
    user = auth.create_user("seam-rearm@test.com", PW)
    tokens = auth.authenticate_user("seam-rearm@test.com", PW)

    # 模擬改密碼水位設在此 refresh token 之後
    def _bump(users: list[User]) -> None:
        u = next(x for x in users if x.id == user.id)
        u.password_changed_at = int(get_current_epoch()) + 10

    auth._atomic_users_write(_bump)  # noqa: SLF001
    with pytest.raises(ToukaException):
        auth.refresh_access_token(tokens["refresh_token"])


def test_authenticate_rejects_if_password_changed_mid_verify(auth, monkeypatch):
    """驗證期間密碼被改（hashed_password 快照不符）→ 這次登入不得重新武裝 refresh。"""
    user = auth.create_user("seam-midverify@test.com", PW)
    original_verify = auth.verify_password

    def _verify_then_change(plain, hashed):  # noqa: ANN001
        result = original_verify(plain, hashed)
        # verify 通過後、_mutate 前，模擬合法使用者改密碼（改 hashed_password）
        def _change(users: list[User]) -> None:
            u = next(x for x in users if x.id == user.id)
            u.hashed_password = auth.hash_password("CompletelyNewPass789!")
        auth._atomic_users_write(_change)  # noqa: SLF001
        return result

    monkeypatch.setattr(auth, "verify_password", _verify_then_change)
    with pytest.raises(ToukaException):
        auth.authenticate_user("seam-midverify@test.com", PW)
    # 舊密碼未能重新武裝：refresh_token_jti 仍為 None（改密碼那筆 _change 沒設 jti）
    assert auth.get_user_by_id(user.id).refresh_token_jti is None
