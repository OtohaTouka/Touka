"""Unit tests for Agent Autonomy Survival during 30-day grace period."""

from unittest.mock import MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.services.resource_manager import ResourceManager
from src.utils.time_utils import get_current_epoch


@pytest.fixture
def resource_mgr():
    return ResourceManager()

def test_system_access_within_grace_period(resource_mgr):
    """驗證 System 角色在 30 天寬限期內可以存取資源。"""
    agent_id = "Alice"
    # 模擬 10 天前的最後活躍時間
    ten_days_ago = int(get_current_epoch()) - (10 * 24 * 3600)
    
    system_identity = AgentIdentity.for_role(agent_id=agent_id, role="System", is_human=False)
    
    # 模擬使用者
    mock_user = MagicMock()
    mock_user.last_active_at = ten_days_ago
    mock_user.owned_agents = [agent_id]
    
    with patch("src.services.auth_service.AuthService.get_all_users", return_value=[mock_user]):
        # 即使沒有真人 Session，System 角色仍應通過檢查
        target = AgentIdentity.for_role(agent_id=agent_id, role="base", is_human=False)
        assert resource_mgr.is_owner(target, system_identity) is True

def test_system_access_expired_grace_period(resource_mgr):
    """驗證 System 角色在超過 30 天後失去自主權 (休眠)。"""
    agent_id = "Alice"
    # 模擬 31 天前的最後活躍時間
    thirty_one_days_ago = int(get_current_epoch()) - (31 * 24 * 3600)
    
    system_identity = AgentIdentity.for_role(agent_id=agent_id, role="System", is_human=False)
    
    mock_user = MagicMock()
    mock_user.last_active_at = thirty_one_days_ago
    mock_user.owned_agents = [agent_id]
    
    with patch("src.services.auth_service.AuthService.get_all_users", return_value=[mock_user]):
        # 超過寬限期，存取應被拒絕
        target = AgentIdentity.for_role(agent_id=agent_id, role="base", is_human=False)
        assert resource_mgr.is_owner(target, system_identity) is False

def test_admin_bypass_grace_period(resource_mgr):
    """驗證 Admin 角色不受 30 天寬限期限制。"""
    agent_id = "Alice"
    admin_identity = AgentIdentity.for_role(agent_id="admin_uuid", role="admin", is_human=True)
    
    # 超過 30 天
    thirty_one_days_ago = int(get_current_epoch()) - (31 * 24 * 3600)
    
    mock_user = MagicMock()
    mock_user.last_active_at = thirty_one_days_ago
    mock_user.role = "admin"
    mock_user.owned_agents = [agent_id]
    
    with patch("src.services.auth_service.AuthService.get_user_by_id", return_value=mock_user):
        target = AgentIdentity.for_role(agent_id=agent_id, role="base", is_human=False)
        assert resource_mgr.is_owner(target, admin_identity) is True

