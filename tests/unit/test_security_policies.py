import json

import pytest
from src.services.permission_service import PermissionService


@pytest.fixture
def security_service(tmp_path):
    """建立安全政策測試服務 (SSOT)"""
    PermissionService.reset_singleton()
    
    policy_path = tmp_path / "auth_policies.json"
    ident_path = tmp_path / "identity_map.json"
    config_path = tmp_path / "permissions.json"
    
    # 建立測試政策
    policy_data = {
        "levels": {
            "Manual": {"icon": "👤", "desc": "手動模式"},
            "Auto": {"icon": "🛡️", "desc": "自動"},
            "YOLO": {"icon": "🚀", "desc": "全力開發"}
        },
        "policies": {
            # 舊檔留著的 safe_whitelist 鍵：讀得進來，但不再生效（ADR-0246）
            "safe_whitelist": ["^ls$", "^echo .*"],
            "critical_blacklist": ["^rm -rf /", "^docker-compose restart$"]
        },
        "tier_defaults": {},
        # H1：read_only_tools 已汰換為 tool_risk 分類表（未列名工具＝default dangerous）
        "tool_risk": {"default": "dangerous", "tools": {"research": "read_only"}}
    }

    ident_data = {
        "agent_overrides": {
            "Alice": {"role": "Owner", "capabilities": ["is_toukasoul"]}
        },
        "tag_rules": {
            "is_toukasoul": {"role": "Owner", "capabilities": []}
        },
        "default_role": "Base"
    }

    config_data = {
        "roles": {
            "Owner": {"allowed_tools": ["*"], "allowed_mcp_servers": ["*"]},
            "Subagent": {"allowed_tools": ["ls", "echo"], "allowed_mcp_servers": []},
            "Base": {"allowed_tools": [], "allowed_mcp_servers": []}
        }
    }
    
    policy_path.write_text(json.dumps(policy_data), encoding="utf-8")
    ident_path.write_text(json.dumps(ident_data), encoding="utf-8")
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    service = PermissionService(
        policy_path=str(policy_path), 
        identity_path=str(ident_path),
        config_path=str(config_path),
        force_init=True
    )
    service.disable_auto_reload()
    return service

def test_level_readonly(security_service, mock_identity):
    # ReadOnly 已退役（ADR-0246）：走到引擎一律當看不懂的值＝CONFIRM（＝Manual，
    # 「核准後可寫」是事先核准的變寬 ③）
    ctx = {"auth_level": "ReadOnly"}
    agent = mock_identity(agent_id="BaseAgent", role="Base")

    for command in ("ls", "mkdir test"):
        strategy = security_service.get_execution_strategy("bash", {"command": command}, agent, ctx)
        assert strategy == "CONFIRM"

def test_level_fullyolo_danger(security_service, mock_identity):
    # YOLO 模式（原 FullYOLO）
    ctx = {"auth_level": "YOLO"}
    alice = mock_identity(agent_id="Alice", role="Owner")
    
    # 一般指令應允許
    assert security_service.get_execution_strategy("bash", {"command": "mkdir test"}, alice, ctx) == "ALLOW"
    
    # 黑名單指令應攔截
    assert security_service.get_execution_strategy("bash", {"command": "rm -rf /"}, alice, ctx) == "DENY"
    
    # Docker-compose 全域指令應攔截 (核心保護)
    assert security_service.get_execution_strategy("bash", {"command": "docker-compose restart"}, alice, ctx) == "DENY"

def test_level_safeauto(security_service, mock_identity):
    # Auto 模式（對應舊 SafeAuto）
    ctx = {"auth_level": "Auto"}
    agent = mock_identity(agent_id="Anyway", role="Subagent")
    
    # 舊白名單指令不再自動放行，交給審查員（U2c-3；無人在場不送審）
    strategy = security_service.get_execution_strategy("bash", {"command": "ls"}, agent, ctx)
    assert strategy == "REVIEW"
    
    # 其他指令同樣交給審查員（REVIEW）
    assert security_service.get_execution_strategy("bash", {"command": "mkdir test"}, agent, ctx) == "REVIEW"

def test_default_is_manual(security_service, mock_identity):
    # 即使是 Owner，若未指定等級，預設也應是 Manual (或者是從角色配置讀取)
    ctx = {} # 未帶 auth_level
    alice = mock_identity(agent_id="Alice", role="Owner")
    # 注意：Owner 角色可能有自己的 default_auth_level，但策略引擎預設為 Manual
    assert security_service.get_execution_strategy("bash", {"command": "mkdir test"}, alice, ctx) == "CONFIRM"


def test_readonly_tool_allowed_all_levels(security_service, mock_identity):
    """ADR-0089：唯讀工具 research（網路搜尋）在所有 auth_level 一律 ALLOW、免 HITL。"""
    agent = mock_identity(agent_id="Anyway", role="Subagent")
    for level in ("Manual", "Auto", "YOLO"):
        ctx = {"auth_level": level}
        result = security_service.get_execution_strategy("research", {"query": "x"}, agent, ctx)
        assert result == "ALLOW", f"research 在 {level} 應 ALLOW，實得 {result}"


def test_non_readonly_tool_still_gated(security_service, mock_identity):
    """非唯讀工具（vault_tools）：Manual 仍需確認、Auto 交給審查員、退役的 ReadOnly 當 Manual。"""
    agent = mock_identity(agent_id="Anyway", role="Subagent")
    assert security_service.get_execution_strategy(
        "vault_tools", {"action": "read"}, agent, {"auth_level": "Manual"}
    ) == "CONFIRM"
    assert security_service.get_execution_strategy(
        "vault_tools", {"action": "read"}, agent, {"auth_level": "Auto"}
    ) == "REVIEW"
    assert security_service.get_execution_strategy(
        "vault_tools", {"action": "read"}, agent, {"auth_level": "ReadOnly"}
    ) == "CONFIRM"


def test_readonly_allow_not_leaked_to_bash_command(security_service, mock_identity):
    """防混淆：bash 指令字串剛好等於 'research' 不應被當唯讀工具放行（terminal_execute 排除）。"""
    agent = mock_identity(agent_id="Anyway", role="Subagent")
    # action=terminal_execute（args 有 command）→ 不走 read_only 放行 → Manual 仍需確認
    assert security_service.get_execution_strategy(
        "bash", {"command": "research"}, agent, {"auth_level": "Manual"}
    ) == "CONFIRM"
