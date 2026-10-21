"""修復 3 — LDH 首裝限定（07-11 事故清單 #3；實作前＝紅）。

事故餘波：兩個 admin 被空密碼覆蓋鎖死後，瀏覽器重載 → 無卡本機請求觸發 LDH
（Local Graceful Downgrade）→ 後端鑄發 7 天 DebugAgent 卡 → 前端「未登入採納」
接手 ＝ 殭屍身分：人類面認證失敗卻換到 agent 面的卡（ADR-0141 語言的跨面越界），
整個 app 被降級卡挾持、看起來像登入卻不是任何人。

修法（ADR-0131「帶壞卡→401」的續篇；fail-loud 哲學）：
- 後端：LDH 僅在「系統未認領」時可用——尚無任何完成真密碼設定的使用者
  （bootstrap 暫時密碼 admin 不算認領，首裝精靈流程不受影響）。一旦認領，
  無卡本機請求不再降級鑄卡，get_optional_identity 回 None（強制閘 401 fail-loud）。
- 前端（vitest 另測）：曾登入過的瀏覽器永不採納 X-New-Auth-Token 降級卡。

判準 SSOT＝AuthService.any_claimed_user()。全走 conftest 沙箱 users.json；
各測試先清空再自建狀態（LDH 行為自此依賴使用者狀態，必須顯式指定）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.models.user import User
from src.services.auth_service import AuthService
from src.services.identity_router import get_optional_identity, get_verified_identity

REAL_PW = "RealPassword123!"
_PERM = "src.services.permission_service.permission_service"


def _wipe_users(auth: AuthService) -> None:
    auth._atomic_users_write(lambda users: users.clear())  # noqa: SLF001


@pytest.fixture(autouse=True)
def _users_hygiene():
    """測後清空 users.json：本檔會建立「已認領」使用者，不得漏給 session 沙箱裡
    其他仰賴 LDH（未認領）的測試（順序依賴防護；測前狀態由各測試自行建立）。"""
    yield
    _wipe_users(AuthService())


def _seed_temp_admin(auth: AuthService) -> None:
    """模擬 ensure_admin_user 的首裝產物：暫時密碼 admin（未認領）。"""
    def _mutate(users: list[User]) -> None:
        users.append(User(
            id=str(uuid.uuid4()),
            email="bootstrap-admin@touka.io",
            role="admin",
            hashed_password=auth.hash_password("TempBootstrap123!"),
            is_temp_password=True,
        ))
    auth._atomic_users_write(_mutate)  # noqa: SLF001


def _mk_request(headers: dict | None = None, host: str = "127.0.0.1") -> MagicMock:
    """模擬 FastAPI Request；state 用 SimpleNamespace 讓「未發卡」可被斷言
    （MagicMock 屬性存取會自動生成，驗不出 hasattr=False）。"""
    req = MagicMock(spec=Request)
    req.client.host = host
    req.headers = headers or {}
    req.state = SimpleNamespace()
    return req


# ── any_claimed_user 判準本體 ─────────────────────────────────────────────────────


def test_any_claimed_user_semantics():
    """無使用者＝未認領；僅暫時密碼＝未認領；任一真密碼使用者＝已認領。"""
    auth = AuthService()
    _wipe_users(auth)
    assert auth.any_claimed_user() is False

    _seed_temp_admin(auth)
    assert auth.any_claimed_user() is False, "bootstrap 暫時密碼 admin 不算認領（首裝精靈相容）"

    auth.create_user("claimed@test.com", REAL_PW)
    assert auth.any_claimed_user() is True


# ── 未認領（首裝）：LDH 照舊 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ldh_mints_debug_identity_when_unclaimed():
    """首裝（無任何使用者）：無卡本機請求仍優雅降級並發卡（精靈/開發首領卡相容）。"""
    auth = AuthService()
    _wipe_users(auth)
    req = _mk_request(host="127.0.0.71")

    with patch(f"{_PERM}.get_role_key") as mock_role, \
         patch(f"{_PERM}.get_capabilities_by_id") as mock_caps:
        mock_role.return_value = "Debugger"
        mock_caps.return_value = []
        ident = await get_optional_identity(req, None)

    assert ident is not None and ident.agent_id == "DebugAgent"
    assert isinstance(getattr(req.state, "new_auth_token", None), str)


@pytest.mark.asyncio
async def test_ldh_still_available_with_only_temp_password_admin():
    """僅 bootstrap 暫時密碼 admin 存在＝仍屬首裝，LDH 可用。"""
    auth = AuthService()
    _wipe_users(auth)
    _seed_temp_admin(auth)
    req = _mk_request(host="127.0.0.72")

    with patch(f"{_PERM}.get_role_key") as mock_role, \
         patch(f"{_PERM}.get_capabilities_by_id") as mock_caps:
        mock_role.return_value = "Debugger"
        mock_caps.return_value = []
        ident = await get_optional_identity(req, None)

    assert ident is not None and ident.agent_id == "DebugAgent"


# ── 已認領：LDH 永久關閉（fail-loud）────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ldh_refused_once_claimed():
    """已認領：無卡本機請求不再降級、不再發卡（殭屍身分根因對釘）。"""
    auth = AuthService()
    _wipe_users(auth)
    auth.create_user("claimed-a@test.com", REAL_PW)
    req = _mk_request(host="127.0.0.73")

    ident = await get_optional_identity(req, None)

    assert ident is None
    assert not hasattr(req.state, "new_auth_token"), "已認領絕不得再鑄降級卡"


@pytest.mark.asyncio
async def test_ldh_refused_with_x_agent_id_once_claimed():
    """已認領：帶 X-Agent-ID 的無卡請求同樣拒絕（Header 不是憑證）。"""
    auth = AuthService()
    _wipe_users(auth)
    auth.create_user("claimed-b@test.com", REAL_PW)
    req = _mk_request(headers={"X-Agent-ID": "DebugAgent"}, host="127.0.0.74")

    ident = await get_optional_identity(req, None)

    assert ident is None
    assert not hasattr(req.state, "new_auth_token")


@pytest.mark.asyncio
async def test_get_verified_identity_401_once_claimed():
    """強制閘：已認領＋無卡 → 401 fail-loud（不再靜默降級）。"""
    auth = AuthService()
    _wipe_users(auth)
    auth.create_user("claimed-c@test.com", REAL_PW)
    req = _mk_request(host="127.0.0.75")

    with pytest.raises(ToukaException) as exc:
        await get_verified_identity(req, None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_path_unaffected_once_claimed():
    """已認領只關 LDH——正規帶卡驗證照常運作。"""
    auth = AuthService()
    _wipe_users(auth)
    auth.create_user("claimed-d@test.com", REAL_PW)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner", capabilities=[])
    creds = MagicMock()
    creds.credentials = "valid_token"
    req = _mk_request(host="127.0.0.76")

    with patch("src.services.identity_service.identity_service.verify_token") as mock_verify:
        mock_verify.return_value = identity
        result = await get_optional_identity(req, creds)

    assert result == identity
