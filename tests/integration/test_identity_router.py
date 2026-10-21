from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity


@pytest.fixture
def mock_request():
    """模擬 FastAPI Request"""
    req = MagicMock(spec=Request)
    req.client.host = "127.0.0.1"
    req.headers = {}
    req.state = MagicMock()
    return req

@pytest.mark.asyncio
async def test_get_verified_identity_with_valid_token(mock_request):
    """驗證有效 Token 的情況"""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner", capabilities=[])
    auth_creds = MagicMock()
    auth_creds.credentials = "valid_token"
    
    with patch("src.services.identity_service.identity_service.verify_token") as mock_verify:
        mock_verify.return_value = identity
        
        result = await get_verified_identity(mock_request, auth_creds)
        assert result == identity
        assert result.agent_id == "Alice"

@pytest.mark.asyncio
async def test_get_verified_identity_spoofing_blocked(mock_request):
    """驗證身分偽造 (Header 與 Token 不符) 是否被攔截"""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner", capabilities=[])
    auth_creds = MagicMock()
    auth_creds.credentials = "alice_token"
    
    # 惡意 Header 試圖假裝是 Bob
    mock_request.headers = {"X-Agent-ID": "Bob"}
    
    with patch("src.services.identity_service.identity_service.verify_token") as mock_verify, \
         patch("src.services.permission_service.permission_service.get_role_key") as mock_role:
        
        mock_verify.return_value = identity
        mock_role.return_value = "Owner" # Bob 的角色也是 Owner
        
        with pytest.raises(HTTPException) as exc:
            await get_verified_identity(mock_request, auth_creds)
        assert exc.value.status_code == 403
        assert "Spoofing" in str(exc.value.detail) or "match" in str(exc.value.detail)

@pytest.mark.asyncio
async def test_get_verified_identity_local_handshake(mock_request):
    """驗證本地信任網路的優雅降級 (LDH)（修復 3：僅限系統未認領＝首裝）"""
    # ADR-0144：LDH 依賴認領狀態——顯式清空沙箱 users.json＝未認領，
    # 避免繼承其他測試建立的真密碼使用者（已認領→LDH 關閉）造成順序依賴。
    AuthService()._atomic_users_write(lambda users: users.clear())  # noqa: SLF001

    mock_request.client.host = "127.0.0.1"
    mock_request.headers = {"X-Agent-ID": "DebugAgent"}

    with patch("src.services.permission_service.permission_service.get_role_key") as mock_role, \
         patch("src.services.permission_service.permission_service.get_capabilities_by_id") as mock_caps, \
         patch("src.services.identity_service.identity_service.issue_token") as mock_issue:
        
        mock_role.return_value = "Debugger"
        mock_caps.return_value = ["debug"]
        mock_issue.return_value = "new_token"
        
        # 不帶 Token
        result = await get_verified_identity(mock_request, None)
        
        assert result.agent_id == "DebugAgent"
        assert result.display_role == "Debugger"
        # 應自動簽發新 Token 並存入 request.state
        assert mock_request.state.new_auth_token == "new_token"

def test_post_identity_map_no_op_returns_200(auth_client, mock_identity):
    """POST /v1/identity is admin-only (DA2); same-data round-trip is a no-op → 200."""
    current = auth_client.get("/v1/identity").json()
    app.dependency_overrides[get_verified_identity] = lambda: mock_identity(
        agent_id="DebugAgent", role="admin"
    )
    resp = auth_client.post("/v1/identity", json=current)
    assert resp.status_code != 500
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_post_identity_map_owner_denied(auth_client):
    """DA2: a non-admin Owner must NOT be able to write the identity map."""
    current = auth_client.get("/v1/identity").json()  # auth_client is Owner
    resp = auth_client.post("/v1/identity", json=current)
    assert resp.status_code == 403


def test_get_identity_agents_returns_200(auth_client):
    """GET /v1/identity/agents 應回傳 200 且為有效 JSON"""
    resp = auth_client.get("/v1/identity/agents")
    assert resp.status_code != 500
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


def test_get_identity_agents_returns_list(auth_client):
    """GET /v1/identity/agents 回傳資料應為 list"""
    resp = auth_client.get("/v1/identity/agents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_identity_map_returns_200_with_structure(auth_client):
    """GET /v1/identity 應回傳 200 且包含 tag_rules, agent_overrides, default_role"""
    resp = auth_client.get("/v1/identity")
    assert resp.status_code == 200
    data = resp.json()
    assert "tag_rules" in data
    assert "agent_overrides" in data
    assert "default_role" in data


def test_get_identity_map_returns_valid_json(auth_client):
    """回傳的 JSON 結構應可被正確解析，不應有 500 錯誤"""
    resp = auth_client.get("/v1/identity")
    assert resp.status_code != 500
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_get_verified_identity_trusted_switch(mock_request):
    """驗證受信任本地網路下，擁有 DebugAgent 的 Owner 可切換身分 (P7 fix)"""
    # 原始身分是 Alice (帶 Token)，且 owned_agents 包含 DebugAgent
    identity = AgentIdentity.for_role(
        agent_id="Alice", role="Owner", capabilities=[], owned_agents=["DebugAgent"]
    )
    auth_creds = MagicMock()
    auth_creds.credentials = "alice_token"
    
    # 試圖切換到 DebugAgent
    mock_request.headers = {"X-Agent-ID": "DebugAgent"}
    mock_request.client.host = "127.0.0.1"
    
    with patch("src.services.identity_service.identity_service.verify_token") as mock_verify, \
         patch("src.services.permission_service.permission_service.get_role_key") as mock_role:
        
        mock_verify.return_value = identity
        # DebugAgent 的角色是 Debugger
        mock_role.return_value = "Debugger"
        
        result = await get_verified_identity(mock_request, auth_creds)

        # 應允許切換
        assert result.agent_id == "DebugAgent"
        assert result.display_role == "Debugger"


# ── ADR-0141 §8-1 代打雙向測試（P5）：admin 代打過管理閘 ✓／非 admin 擁有者 ✗ ──────


@pytest.mark.asyncio
async def test_admin_assumption_is_pilot_pair_option_a(mock_request):
    """admin 代打（選項 A）：human_role='admin'、agent_role=None——僅管理權、不繼承目標
    軀體角色；管理閘經 human_role 放行（走私字串已除）。雙向測試的 admin ✓ 半邊。"""
    driver = AgentIdentity.for_role(agent_id="AdminU", role="admin", is_human=True, owned_agents=[])
    auth_creds = MagicMock()
    auth_creds.credentials = "admin_token"
    mock_request.headers = {"X-Agent-ID": "SoulSeven"}  # admin 可代打任一靈魂（ADR-0245）
    mock_request.client.host = "127.0.0.1"

    with patch("src.services.identity_service.identity_service.verify_token") as mv, \
         patch("src.services.identity_router.soul_service.is_toukasoul", return_value=True), \
         patch("src.services.permission_service.permission_service.get_capabilities_by_id") as mc:
        mv.return_value = driver
        mc.return_value = []
        result = await get_verified_identity(mock_request, auth_creds)

    assert result.agent_id == "SoulSeven"
    assert result.is_human is False
    assert result.human_role == "admin"          # 駕駛員管理權
    assert result.agent_role is None             # 不繼承目標軀體（選項 A）
    assert result.display_role == "admin"        # 有效角色（display_role），單欄位不模糊
    assert result.is_admin_authority() is True   # 管理閘放行（保權）


@pytest.mark.asyncio
async def test_owner_assumption_becomes_body_no_admin_authority(mock_request):
    """非 admin 擁有者代打＝成為目標軀體 agent（agent_role=目標角色、human_role=None），
    不具管理權。雙向測試的 normal ✗ 半邊（行為與拆分前一致）。"""
    driver = AgentIdentity.for_role(
        agent_id="OwnerU", role="normal", is_human=True, owned_agents=["MyBot"]
    )
    auth_creds = MagicMock()
    auth_creds.credentials = "owner_token"
    mock_request.headers = {"X-Agent-ID": "MyBot"}
    mock_request.client.host = "127.0.0.1"

    with patch("src.services.identity_service.identity_service.verify_token") as mv, \
         patch("src.services.identity_router.soul_service.is_toukasoul", return_value=True), \
         patch("src.services.permission_service.permission_service.get_role_key") as mr, \
         patch("src.services.permission_service.permission_service.get_capabilities_by_id") as mc:
        mv.return_value = driver
        mr.return_value = "Owner"   # MyBot（靈魂）的軀體角色
        mc.return_value = []
        result = await get_verified_identity(mock_request, auth_creds)

    assert result.agent_id == "MyBot"
    assert result.is_human is False
    assert result.agent_role == "Owner"          # 成為目標軀體
    assert result.human_role is None             # 無人類特權
    assert result.is_admin_authority() is False  # 管理閘拒絕
