"""Unit tests for identity router robustness and alignment (TDD).

Checks that:
1. get_optional_identity returns None on missing token from non-local network.
2. get_optional_identity raises 401 (AUTH_INVALID_TOKEN) on corrupt token.
3. get_verified_identity raises 401 (AUTH_INVALID_TOKEN) on missing token.
4. get_verified_identity raises 401 on corrupt token.
5. Local Graceful Downgrade (LDH) functions normally under both dependencies.
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_optional_identity, get_verified_identity


@pytest.mark.asyncio
async def test_get_optional_identity_missing_token_non_local():
    """get_optional_identity returns None when token is missing and request is non-local."""
    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "198.51.100.1"  # Public non-local IP
    req.state = MagicMock()

    # is_request_from_trusted_local should return False
    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=False):
        res = await get_optional_identity(request=req, auth=None)
        assert res is None


@pytest.mark.asyncio
async def test_get_optional_identity_corrupt_token_non_local():
    """get_optional_identity raises AUTH_INVALID_TOKEN when token is corrupt."""
    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "198.51.100.1"
    req.state = MagicMock()
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials="corrupt-token")

    # identity_service.verify_token returns None for corrupt token
    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=False), \
         patch("src.services.identity_router.identity_service.verify_token", return_value=None):
        with pytest.raises(ToukaException) as exc_info:
            await get_optional_identity(request=req, auth=auth)
        assert exc_info.value.code == ErrorCode.AUTH_INVALID_TOKEN


@pytest.mark.asyncio
async def test_get_verified_identity_missing_token_non_local():
    """get_verified_identity raises AUTH_INVALID_TOKEN when token is missing."""
    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "198.51.100.1"
    req.state = MagicMock()

    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=False):
        with pytest.raises(ToukaException) as exc_info:
            await get_verified_identity(request=req, auth=None)
        assert exc_info.value.code == ErrorCode.AUTH_INVALID_TOKEN


@pytest.mark.asyncio
async def test_get_verified_identity_valid_token():
    """get_verified_identity returns the resolved AgentIdentity when valid."""
    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "198.51.100.1"
    req.state = MagicMock()
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-token")
    mock_identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])

    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=False), \
         patch("src.services.identity_router.identity_service.verify_token", return_value=mock_identity):
        res = await get_verified_identity(request=req, auth=auth)
        assert res == mock_identity


@pytest.mark.asyncio
async def test_local_invalid_token_returns_401_not_ldh():
    """ADR-0131（批3 第6刀）：本地信任來源帶「失效 token」→ 一律 401，嚴禁 LDH 降級發卡。

    舊行為「本地壞卡當無卡 → LDH 降級成 DebugAgent＋X-New-Auth-Token 發新卡」會把
    過期人類會話無聲挾持成 DebugAgent 身分（會話錯記其名下、前端整個 app 被降級卡
    接管）。改為 fail-loud 401，讓合法客戶端走 /auth/refresh 續期；LDH 僅保留給
    「真正無卡」的本機請求。
    """
    req = MagicMock(spec=Request)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    auth = HTTPAuthorizationCredentials(scheme="Bearer", credentials="expired-or-corrupt")

    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=True), \
         patch("src.services.identity_router.identity_service.verify_token", return_value=None), \
         patch("src.services.identity_router.identity_service.issue_token") as mock_issue:
        with pytest.raises(ToukaException) as exc_info:
            await get_optional_identity(request=req, auth=auth)
        assert exc_info.value.code == ErrorCode.AUTH_INVALID_TOKEN
        mock_issue.assert_not_called()  # 不得發任何降級卡


@pytest.mark.asyncio
async def test_ldh_graceful_downgrade_optional_identity():
    """get_optional_identity performs Local Graceful Downgrade on trusted local networks.

    修復 3（ADR-0144）：LDH 僅限「系統未認領」——顯式清空沙箱 users.json，
    避免繼承其他測試建立的真密碼使用者（已認領→LDH 關閉）造成順序依賴。
    """
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001

    req = MagicMock(spec=Request)
    req.headers = {"X-Agent-ID": "DebugAgent"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()

    with patch("src.services.identity_router.is_request_from_trusted_local", return_value=True), \
         patch("src.services.identity_router.permission_service.get_role_key", return_value="Owner"), \
         patch("src.services.identity_router.permission_service.get_capabilities_by_id", return_value=[]), \
         patch("src.services.identity_router.identity_service.issue_token", return_value="mock-token"):
        res = await get_optional_identity(request=req, auth=None)
        assert res is not None
        assert res.agent_id == "DebugAgent"
        assert res.display_role == "Owner"
        assert req.state.new_auth_token == "mock-token"
