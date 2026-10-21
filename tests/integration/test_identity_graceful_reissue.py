"""測試 JWT 認證失效 token 的 fail-loud 行為（ADR-0131 / 禁令 60）.

行為翻轉（刻意，ADR-0131）：舊版驗證「本地信任請求持壞 Token 時重新發卡而非拒絕」
——該降級會把過期人類會話無聲挾持成 DebugAgent 身分（會話錯記其名下、前端整個
app 被降級卡接管）。現在帶失效 token 一律 401（不分本地/外部），讓合法客戶端走
/auth/refresh 續期；LDH 發卡僅保留給「真正無卡」的本機請求。
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity


def _reset_to_unclaimed() -> None:
    """修復 3（ADR-0144）後，LDH 行為依賴「系統認領狀態」——測試必須顯式指定。

    清空沙箱 users.json＝首裝（未認領）；不清就會繼承 session 沙箱裡其他測試
    建立的真密碼使用者（已認領→LDH 關閉），變成順序依賴的 flaky。
    """
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001


@pytest.fixture
def mock_request_local():
    """模擬來自本地信任網路的請求。"""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = {}
    req.state = MagicMock()
    return req


@pytest.fixture
def mock_request_external():
    """模擬來自外部網路的請求。"""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "8.8.8.8"
    req.headers = {}
    req.state = MagicMock()
    return req


@pytest.fixture
def mock_bad_auth():
    """模擬一個無效的 Bearer Token。"""
    auth = MagicMock()
    auth.credentials = "expired_or_wrong_secret_token"
    return auth


@pytest.mark.asyncio
async def test_local_stale_token_401_no_reissue(mock_request_local, mock_bad_auth):
    """本地信任請求 + 壞 Token → 401 且不得發降級卡（ADR-0131 翻轉舊「重發卡」行為）。"""
    with patch("src.services.identity_router.identity_service") as mock_svc:
        mock_svc.verify_token.return_value = None  # Token 驗證失敗

        with pytest.raises(HTTPException) as exc_info:
            await get_verified_identity(mock_request_local, mock_bad_auth)

        assert exc_info.value.status_code == 401
        mock_svc.issue_token.assert_not_called()  # 嚴禁對失效卡發降級卡


@pytest.mark.asyncio
async def test_local_no_token_still_downgrades(mock_request_local):
    """本地信任 + 完全無卡 +（修復 3）系統未認領 → LDH 照舊降級發卡（首裝精靈相容）。

    已認領後的無卡行為＝拒絕，見 test_ldh_first_run_gate.py（ADR-0144）。
    """
    _reset_to_unclaimed()
    with patch("src.services.identity_router.identity_service") as mock_svc:
        mock_svc.issue_token.return_value = "new_valid_token"

        identity = await get_verified_identity(mock_request_local, None)

        assert identity is not None
        assert isinstance(identity, AgentIdentity)
        assert identity.agent_id == "DebugAgent"
        assert hasattr(mock_request_local.state, "new_auth_token")


@pytest.mark.asyncio
async def test_external_stale_token_rejects(mock_request_external, mock_bad_auth):
    """外部請求 + 壞 Token → 應嚴格 401（行為不變）。"""
    with patch("src.services.identity_router.identity_service") as mock_svc:
        mock_svc.verify_token.return_value = None  # Token 驗證失敗

        with pytest.raises(HTTPException) as exc_info:
            await get_verified_identity(mock_request_external, mock_bad_auth)

        assert exc_info.value.status_code == 401
