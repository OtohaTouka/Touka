import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.admin import get_user_service
from unittest.mock import patch, MagicMock

@pytest.fixture
def client():
    """使用 Context Manager 確保觸發 lifespan 事件 (初始化 app.state.context)"""
    with TestClient(app) as c:
        yield c

def test_admin_access_users_success(client):
    """驗證管理員可存取使用者管理介面。"""
    admin_identity = AgentIdentity.for_role(
        agent_id="admin-id", 
        role="admin", 
        is_human=True, 
        owned_agents=["Alice"],
        capabilities=["heartbeat_awakening"]
    )
    
    with patch("src.services.identity_router.identity_service.verify_token", return_value=admin_identity), \
         patch("src.routers.admin.get_user_service") as mock_service:
        
        mock_user_service = MagicMock()
        mock_user_service.get_users.return_value = []
        mock_service.return_value = mock_user_service
        
        response = client.get("/v1/admin/users", headers={"Authorization": "Bearer admin_token"})
        assert response.status_code == 200

def test_admin_delete_user_clears_owned_projects(client):
    """F-fix: deleting a user must also remove that user's owned project rows
    (mirrors self-service clear_my_data), else owner_user_id dangles and the
    projects become inaccessible to non-admins."""
    admin_identity = AgentIdentity.for_role(
        agent_id="admin-id", role="admin", is_human=True, capabilities=["heartbeat_awakening"]
    )
    cleared = {}

    async def _clear(uid):
        cleared["uid"] = uid
        return 3

    mock_user_service = MagicMock()
    mock_user_service.delete_user.return_value = None

    # Depends(get_user_service) binds the original ref at registration, so a module
    # patch is ignored — use FastAPI's dependency_overrides to inject the mock.
    app.dependency_overrides[get_user_service] = lambda: mock_user_service
    vt = "src.services.identity_router.identity_service.verify_token"
    clr = "src.utils.async_db.session_service.clear_projects_owned_by"
    try:
        with patch(vt, return_value=admin_identity), patch(clr, _clear):
            resp = client.delete(
                "/v1/admin/users/u_target",
                headers={"Authorization": "Bearer t"},
            )
            assert resp.status_code == 200
            # user removed first (last-admin guard runs), THEN owned projects cleared
            mock_user_service.delete_user.assert_called_once_with("u_target")
            assert cleared.get("uid") == "u_target"
    finally:
        app.dependency_overrides.pop(get_user_service, None)


def test_normal_user_access_users_denied(client):
    """驗證一般使用者無法存取管理端點。"""
    user_identity = AgentIdentity.for_role(
        agent_id="user-id",
        role="normal",
        is_human=True,
        owned_agents=["Alice"]
    )

    with patch("src.services.identity_router.identity_service.verify_token", return_value=user_identity):
        response = client.get("/v1/admin/users", headers={"Authorization": "Bearer user_token"})
        assert response.status_code == 403
        assert "Administrative privileges required" in response.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# 早期守衛缺口 E06/E07（家族④）：admin 等級的行為級佐證
#
# 源碼守衛 check_admin_level（guard_endpoint_auth.py）確保這些端點在源碼上帶
# admin 級 Depends（廣度：24 端點全覆蓋 + 降級即 FAIL）。以下行為級測試以真實
# TestClient 驅動代表性端點，證明**已認證但非 admin 的一般使用者實際被 403 擋下**
# ——正是舊守衛（只驗「有認證」）放行、ADR-0035/0053 揭露的降級漏洞的執行期反證。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/system/sliding_window"),   # ADR-0035（E06）
        ("post", "/v1/admin/setup/apply"),        # ADR-0053（E07）
        ("get", "/v1/admin/setup/embedding/download-status"),  # ADR-0053（E07）
        ("post", "/v1/identity"),                 # identity map
        ("get", "/v1/permissions"),               # 權限矩陣
    ],
)
def test_admin_endpoints_deny_non_admin(client, method, path):
    """已認證的一般使用者對 admin 端點一律 403（require_admin 族攔截）。"""
    user_identity = AgentIdentity.for_role(
        agent_id="user-id", role="normal", is_human=True, owned_agents=["Alice"],
    )
    with patch(
        "src.services.identity_router.identity_service.verify_token",
        return_value=user_identity,
    ):
        response = client.request(
            method.upper(), path,
            headers={"Authorization": "Bearer user_token"},
            json={} if method == "post" else None,
        )
        assert response.status_code == 403, (
            f"{method.upper()} {path} 應對非 admin 回 403，實得 {response.status_code}"
        )

def test_human_assume_owned_agent(client):
    """驗證人類使用者可切換至其擁有的代理人身分。"""
    user_identity = AgentIdentity.for_role(
        agent_id="user-id", 
        role="normal", 
        is_human=True, 
        owned_agents=["Alice"]
    )
    
    # ADR-0245：X-Agent-ID 只能切進靈魂；Alice 在此視為靈魂。
    with patch("src.services.identity_router.identity_service.verify_token", return_value=user_identity), \
         patch("src.services.identity_router.soul_service.is_toukasoul", return_value=True), \
         patch("src.routers.proxy.process_proxy_request") as mock_proxy:
        
        mock_proxy.return_value = MagicMock()
        
        # Human tries to act as "Alice" (owned)
        response = client.post(
            "/v1/messages", 
            json={"messages": [], "model": "claude-3-opus"}, 
            headers={
                "Authorization": "Bearer user_token",
                "X-Agent-ID": "Alice"
            }
        )
        assert response.status_code == 200

def test_human_assume_unowned_agent_denied(client):
    """驗證人類使用者無法存取未經授權的代理人身分。"""
    user_identity = AgentIdentity.for_role(
        agent_id="user-id", 
        role="normal", 
        is_human=True, 
        owned_agents=["Alice"] # Doesn't own "Bob"
    )
    
    with patch("src.services.identity_router.identity_service.verify_token", return_value=user_identity):
        # Human tries to act as "Bob" (unowned)
        response = client.post(
            "/v1/messages", 
            json={"messages": [], "model": "claude-3-opus"}, 
            headers={
                "Authorization": "Bearer user_token",
                "X-Agent-ID": "Bob"
            }
        )
        assert response.status_code == 403
        assert "Header Agent-ID does not match" in response.json()["detail"]["message"]
