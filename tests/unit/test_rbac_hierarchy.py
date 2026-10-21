import json

import pytest
from src.services.permission_service import PermissionService


@pytest.fixture
def temp_permission_service(tmp_path):
    """建立包含當前邏輯的臨時權限服務 (SSOT)"""
    PermissionService.reset_singleton()
    
    config_path = tmp_path / "permissions.json"
    identity_path = tmp_path / "identity_map.json"
    policy_path = tmp_path / "auth_policies.json"
    
    # 預錄目前的權限狀態 (改為新版 Role 格式)
    config_data = {
        "roles": {
            "Owner": {
                "allowed_tools": ["*"],
                "allowed_mcp_servers": ["*"]
            },
            "Subagent": {
                "inherits": ["Base"],
                "allowed_tools": ["read_file", "project_tools"],
                "allowed_mcp_servers": ["web_search"]
            },
            "Base": {
                "allowed_tools": [],
                "allowed_mcp_servers": []
            }
        }
    }
    
    # 建立身分映射 (SSOT)
    identity_data = {
        "agent_overrides": {
            "Alice": {"role": "Owner", "capabilities": []},
            "MyModel": {"role": "Subagent", "capabilities": []}
        },
        "tag_rules": {},
        "default_role": "Base"
    }

    # 建立安全政策
    policy_data = {
        "levels": {
            "ReadOnly": {"icon": "🔒", "desc": "唯讀模式"},
            "Manual": {"icon": "👤", "desc": "手動模式"}
        },
        "policies": {
            "safe_whitelist": [],
            "critical_blacklist": ["^rm -rf /"]
        },
        "tier_defaults": {}
    }
    
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    identity_path.write_text(json.dumps(identity_data), encoding="utf-8")
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")
        
    service = PermissionService(
        config_path=str(config_path), 
        identity_path=str(identity_path),
        policy_path=str(policy_path)
    )
    service.disable_auto_reload()
    
    yield service

def test_owner_is_sovereign(temp_permission_service, mock_identity):
    """驗證 Owner 角色是否自動取得最高權限"""
    alice = mock_identity(agent_id="Alice", role="Owner")
    allowed = temp_permission_service.get_allowed_tools(alice)
    assert allowed.open_ended is True
    assert temp_permission_service.is_mcp_server_allowed("any_mcp", alice) is True

def test_subagent_inheritance(temp_permission_service, mock_identity):
    """驗證 Subagent 角色的繼承與限制（ADR-0245：取代 Guest）"""
    my_model = mock_identity(agent_id="MyModel", role="Subagent")
    
    # ADR-0245：identity_map 的覆寫不套用到非靈魂 ID（Subagent 只由子代理執行期直接指派），
    # 所以 MyModel 的舊覆寫被略過、解析成 Base；以下授權斷言針對 Subagent 角色本身。
    role = temp_permission_service.get_role_key(my_model)
    assert role == "Base"
    
    allowed = temp_permission_service.get_allowed_tools(my_model)

    assert allowed.open_ended is False
    assert allowed.allows("read_file") is True
    assert temp_permission_service.is_mcp_server_allowed("web_search", my_model) is True
    assert temp_permission_service.is_mcp_server_allowed("secret_mcp", my_model) is False

def test_base_is_restricted(temp_permission_service, mock_identity):
    """驗證 Base 角色是否被嚴格限制"""
    unknown = mock_identity(agent_id="UnknownUser", role="Base")
    allowed = temp_permission_service.get_allowed_tools(unknown)
    assert allowed.is_empty is True
    assert temp_permission_service.is_mcp_server_allowed("web_search", unknown) is False


def test_auth_level_readonly_interception(temp_permission_service, mock_identity):
    """退役的 ReadOnly 走到引擎當 Manual（ADR-0246；H1：工具風險以 tool_risk 分類）"""
    ctx = {"auth_level": "ReadOnly"}
    agent = mock_identity(agent_id="test_agent", role="Base")

    # 未分類工具（default dangerous）：舊 DENY → 要人核准（事先核准的變寬 ③）
    res = temp_permission_service.get_execution_strategy("write_file", {}, agent, ctx)
    assert res == "CONFIRM"

    # read_only 類工具應被 ALLOW（全等級免 HITL；舊「工具塞指令白名單」做法已汰換）
    temp_permission_service.policies.tool_risk.tools["read_file"] = "read_only"
    res = temp_permission_service.get_execution_strategy("read_file", {}, agent, ctx)
    assert res == "ALLOW"
def test_auth_level_manual_confirmation(temp_permission_service, mock_identity):
    """驗證 Manual 與 Auto 模式下的決策邏輯（H1 決策表；ADR-0246 三模式）"""
    ctx = {"auth_level": "Manual"}
    agent = mock_identity(agent_id="test_agent", role="Base")

    # 1. Manual 模式下，safe 類工具也應回傳 CONFIRM（read_only 以外一律問人）
    temp_permission_service.policies.tool_risk.tools["query_memory"] = "safe"
    res = temp_permission_service.get_execution_strategy("query_memory", {}, agent, ctx)
    assert res == "CONFIRM"

    # 2. Auto 模式下，safe 類應回傳 ALLOW
    ctx_safe = {"auth_level": "Auto"}
    res = temp_permission_service.get_execution_strategy("query_memory", {}, agent, ctx_safe)
    assert res == "ALLOW"

    # 3. Auto 模式下，終端指令（一律 dangerous）交給審查員（REVIEW，U2c-3）
    res = temp_permission_service.get_execution_strategy(
        "execute_terminal",
        {"command": "ls"},
        agent,
        ctx_safe,
    )
    assert res == "REVIEW"
