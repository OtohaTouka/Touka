"""`denied_mcp_tools` 走**真檔案＋真繼承鏈**的 P2 行為。

P2 將角色繼承定義為「allow 向上累加、deny 僅自身生效」。跨角色禁令改由
`global_denied_mcp_tools` 明示，且會約束所有角色（含 Owner）。本檔用真設定檔釘住：

1. 父角色 deny 不傳播，子角色自己的 deny 仍優先。
2. 全域地板會傳播，且 Owner 不能繞過。
3. allow 繼承與 MCP 掛載行為維持不變。
4. 裸 `"*"` 在 deny 面成為安全的角色本地 deny-all。
"""
from __future__ import annotations

import json

import pytest
from src.services.permission_service import PermissionService

_IDENTITY_DATA = {
    "agent_overrides": {
        "Alice": {"role": "Owner", "capabilities": []},
        "SubProbe": {"role": "Subagent", "capabilities": []},
    },
    "tag_rules": {},
    "default_role": "Base",
}
_POLICY_DATA = {
    "levels": {"Manual": {"icon": "👤", "desc": "手動模式"}},
    "policies": {"safe_whitelist": [], "critical_blacklist": []},
    "tier_defaults": {},
}


@pytest.fixture
def _service_factory(tmp_path):
    """以指定設定建立讀真檔案的 PermissionService（真解析器、真載入）。"""

    def _build(
        roles: dict,
        *,
        global_denied_mcp_tools: list[str] | None = None,
    ) -> PermissionService:
        PermissionService.reset_singleton()
        cfg = tmp_path / "permissions.json"
        ident = tmp_path / "identity_map.json"
        pol = tmp_path / "auth_policies.json"
        cfg.write_text(
            json.dumps(
                {
                    "roles": roles,
                    "global_denied_mcp_tools": global_denied_mcp_tools or [],
                }
            ),
            encoding="utf-8",
        )
        ident.write_text(json.dumps(_IDENTITY_DATA), encoding="utf-8")
        pol.write_text(json.dumps(_POLICY_DATA), encoding="utf-8")
        svc = PermissionService(
            config_path=str(cfg), identity_path=str(ident), policy_path=str(pol)
        )
        svc.disable_auto_reload()
        return svc

    yield _build
    PermissionService.reset_singleton()


def _production_shaped_roles(base_denies: list[str]) -> dict:
    """三層繼承形狀（改名前的生產版面 Owner→Guest→Base），deny 寫在最底層 Base。

    ADR-0245 後生產的 Owner 不再繼承中間層；此處刻意保留三層以釘住繼承語意，中間層
    改名為 Subagent（舊名 Guest 讀取時會被略過）。
    """
    return {
        "Owner": {
            "inherits": ["Subagent"],
            "allowed_tools": ["*"],
            "allowed_mcp_servers": ["*"],
        },
        "Subagent": {"inherits": ["Base"], "allowed_tools": [], "allowed_mcp_servers": []},
        "Base": {
            "allowed_tools": [],
            "allowed_mcp_servers": [],
            "denied_mcp_tools": base_denies,
        },
    }


# ── 核心語意：deny self-only ──────────────────────────────────────────────


def test_deny_written_on_base_does_not_block_owner(_service_factory, mock_identity):
    """Base 的 deny 不再沿 Owner→Subagent→Base 反向污染 Owner。"""
    svc = _service_factory(_production_shaped_roles(["stripe/create_refund"]))
    alice = mock_identity(agent_id="Alice", role="Owner")

    assert svc.is_mcp_tool_allowed("stripe", "create_refund", alice) is True
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", alice) is True


def test_deny_on_base_applies_to_base_itself(_service_factory, mock_identity):
    """self-only 不是忽略 deny：Base 自己仍被逐工具收斂。"""
    roles = _production_shaped_roles(["stripe/create_refund"])
    roles["Base"]["allowed_mcp_servers"] = ["stripe"]
    svc = _service_factory(roles)
    base = mock_identity(agent_id="BaseProbe", role="Base")

    assert svc.is_mcp_tool_allowed("stripe", "create_refund", base) is False
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", base) is True


def test_resolved_owner_inherits_allow_without_parent_deny(_service_factory):
    """唯一解析器保留 Owner 開放式授權，但不攜帶 Base 的拒絕。"""
    svc = _service_factory(_production_shaped_roles(["stripe/create_refund"]))
    grant = svc.resolve_grants("Owner").mcp
    assert grant.open_ended is True
    assert grant.denied == frozenset()
    assert grant.deny_all is False


def test_child_own_server_wide_deny_still_wins(_service_factory, mock_identity):
    """`server/*` 寫在角色自身時仍拒絕該台全部工具。"""
    roles = _production_shaped_roles([])
    roles["Subagent"]["allowed_mcp_servers"] = ["*"]
    roles["Subagent"]["denied_mcp_tools"] = ["stripe/*"]
    svc = _service_factory(roles)
    sub_probe = mock_identity(agent_id="SubProbe", role="Subagent")

    assert svc.is_mcp_tool_allowed("stripe", "list_customers", sub_probe) is False
    assert svc.is_mcp_tool_allowed("github", "list_issues", sub_probe) is True


# ── deny 是工具級，不是伺服器級 ───────────────────────────────────────────


def test_tool_deny_does_not_block_mounting(_service_factory, mock_identity):
    """工具面 deny 不得連帶擋掉掛載——否則等於用工具級設定做了整台停用。

    整台停用有專屬機制（伺服器條目的 `enabled:false`，ADR-0188），語意與範圍都不同。
    """
    svc = _service_factory(_production_shaped_roles(["stripe/create_refund"]))
    alice = mock_identity(agent_id="Alice", role="Owner")

    assert svc.is_mcp_server_allowed("stripe", alice) is True


# ── 多條 deny 的聯集 ──────────────────────────────────────────────────────


def test_each_level_keeps_only_its_own_denies(_service_factory, mock_identity):
    """各層各寫一條時，Owner 只套用 Owner 自身拒絕。"""
    roles = _production_shaped_roles(["stripe/create_refund"])
    roles["Subagent"]["denied_mcp_tools"] = ["github/delete_repo"]
    roles["Owner"]["denied_mcp_tools"] = ["zapier/run_action"]
    svc = _service_factory(roles)
    alice = mock_identity(agent_id="Alice", role="Owner")

    assert svc.is_mcp_tool_allowed("stripe", "create_refund", alice) is True
    assert svc.is_mcp_tool_allowed("github", "delete_repo", alice) is True
    assert svc.is_mcp_tool_allowed("zapier", "run_action", alice) is False
    assert svc.is_mcp_tool_allowed("zapier", "list_actions", alice) is True


# ── 工具級授權亦經真繼承鏈 ────────────────────────────────────────────────


def test_tool_scoped_grant_survives_real_resolution(_service_factory, mock_identity):
    """`server/tool` 授權寫在中間層，經真解析後仍能掛載＋只開該工具。"""
    roles = _production_shaped_roles([])
    roles["Subagent"]["allowed_mcp_servers"] = ["stripe/list_customers"]
    svc = _service_factory(roles)
    sub_probe = mock_identity(agent_id="SubProbe", role="Subagent")

    assert svc.is_mcp_server_allowed("stripe", sub_probe) is True
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", sub_probe) is True
    assert svc.is_mcp_tool_allowed("stripe", "create_refund", sub_probe) is False
    assert svc.is_mcp_server_allowed("github", sub_probe) is False


# ── 全域地板與角色本地 deny-all ──────────────────────────────────────────


def test_global_deny_floor_binds_owner(_service_factory, mock_identity):
    """跨角色禁令只能由全域地板表達，且 Owner 不能繞過。"""
    svc = _service_factory(
        _production_shaped_roles([]),
        global_denied_mcp_tools=["stripe/create_refund"],
    )
    alice = mock_identity(agent_id="Alice", role="Owner")

    assert svc.is_mcp_tool_allowed("stripe", "create_refund", alice) is False
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", alice) is True


def test_bare_wildcard_on_disk_is_local_deny_all(_service_factory, mock_identity):
    """角色 deny 的裸 `*` 只關閉該角色，不沿繼承鏈殃及 Owner。"""
    roles = _production_shaped_roles(["*"])
    roles["Base"]["allowed_mcp_servers"] = ["stripe"]
    svc = _service_factory(roles)
    base = mock_identity(agent_id="BaseProbe", role="Base")
    alice = mock_identity(agent_id="Alice", role="Owner")

    assert svc.is_mcp_tool_allowed("stripe", "list_customers", base) is False
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", alice) is True
