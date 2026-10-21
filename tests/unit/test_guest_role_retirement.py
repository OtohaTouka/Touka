"""ADR-0245（U2a-2）：Guest 角色退役，子代理改用專屬的 Subagent 角色。

- Subagent 的工具集合與改名前的 Guest **逐項相同**（字面值清單；決策 P4）。
- Owner 不再繼承 Guest；Owner 本來就是 ``*``，三軸答案不變。
- 使用者自己的舊檔仍可能有 Guest：permissions／identity_map 讀到舊的 Guest 值時略過並記
  警告、不改檔。舊 Guest 不再當 Subagent 用：沒有 Subagent 的檔＝Subagent 沒有工具。
  本檔只用 ``.example`` 與 tmp 合成檔，不讀 live 設定。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.models.identity import AgentIdentity
from src.services.permission_service import PermissionService
from src.tools.builtin import tool_registry
from src.utils.permission_grants import McpGrant, NameGrant, RoleGrants

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"
_EXAMPLE = _CONFIG_DIR / "permissions.json.example"
_LOGGER = "src.services.permission_service"
ALL_TOOLS = frozenset(tool_registry)

# 改名前 Guest 的工具清單（P4：Subagent 逐項照抄）。刻意硬編，不從設定檔推導。
PRE_RENAME_GUEST_TOOLS = frozenset({
    "delete", "edit", "find", "grep", "ide_open_diff", "insert", "list",
    "project_tools", "read", "research", "undo", "vault_tools", "write",
})
PRE_RENAME_GUEST_DENIED = frozenset({"update_my_soul", "edit_soul"})

# 改名前的出貨版面（Owner 繼承 Guest），代表尚未手動更新的 live 設定。
_LEGACY_ROLES = {
    "Owner": {
        "inherits": ["Guest"],
        "allowed_tools": ["*"],
        "allowed_mcp_servers": ["*"],
        "allowed_skills": ["*"],
    },
    "Guest": {
        "inherits": ["Base"],
        "allowed_tools": sorted(PRE_RENAME_GUEST_TOOLS),
        "denied_tools": sorted(PRE_RENAME_GUEST_DENIED),
        "allowed_mcp_servers": [],
    },
    "Base": {"allowed_tools": [], "denied_tools": ["*"], "allowed_mcp_servers": []},
    "Debugger": {"inherits": ["Owner"]},
}
_EMPTY_MAP = {"tag_rules": {}, "agent_overrides": {}, "default_role": "Base"}


@pytest.fixture
def build(tmp_path):
    """以 tmp 合成檔建立真載入的 PermissionService（自動重載關閉）。"""

    def _build(permissions: dict | str, identity: dict | None = None) -> PermissionService:
        PermissionService.reset_singleton()
        cfg = tmp_path / "permissions.json"
        text = permissions if isinstance(permissions, str) else json.dumps(permissions)
        cfg.write_text(text, encoding="utf-8")
        ident = tmp_path / "identity_map.json"
        ident.write_text(json.dumps(identity or _EMPTY_MAP), encoding="utf-8")
        svc = PermissionService(config_path=str(cfg), identity_path=str(ident))
        svc.disable_auto_reload()
        return svc

    yield _build
    PermissionService.reset_singleton()


def _layout(name: str) -> dict | str:
    return _EXAMPLE.read_text(encoding="utf-8") if name == "example" else {"roles": _LEGACY_ROLES}


def _retirement_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "ADR-0245" in r.getMessage()]


# ── P4：子代理權限與改名前逐項相同 ──────────────────────────────────────────


def test_subagent_tools_equal_pre_rename_guest_list(build):
    svc = build(_layout("example"))
    grants = svc.resolve_grants("Subagent")
    assert grants.tools.open_ended is False
    assert grants.tools.explicit == PRE_RENAME_GUEST_TOOLS
    assert grants.tools.denied == PRE_RENAME_GUEST_DENIED
    assert grants.tools.deny_all is False
    assert grants.mcp == McpGrant()
    assert grants.skills == NameGrant()

    sub = AgentIdentity.for_role("__subagent_probe__", "Subagent")
    advertised = svc.get_allowed_tools(sub)
    assert frozenset(t for t in ALL_TOOLS if svc.is_tool_allowed(t, advertised)) == (
        PRE_RENAME_GUEST_TOOLS
    )
    assert frozenset(t for t in ALL_TOOLS if svc.is_authorized(sub, t, {}, {})) == (
        PRE_RENAME_GUEST_TOOLS
    )


@pytest.mark.parametrize("layout", ["example", "legacy"])
def test_owner_answers_unchanged_without_guest_inheritance(build, layout):
    svc = build(_layout(layout))
    owner = AgentIdentity.for_role("owner_probe", "Owner")
    advertised = svc.get_allowed_tools(owner)
    assert frozenset(t for t in ALL_TOOLS if svc.is_tool_allowed(t, advertised)) == ALL_TOOLS
    assert frozenset(t for t in ALL_TOOLS if svc.is_authorized(owner, t, {}, {})) == ALL_TOOLS
    assert svc.resolve_grants("Owner").tools.denied == frozenset()
    assert svc.can_use_skill(owner, "brand_new_skill") is True
    assert svc.is_mcp_tool_allowed("stripe", "create_refund", owner) is True
    assert svc.roles_config.roles["Owner"].inherits == []


# ── 舊 permissions：略過 Guest、不改檔 ──────────────────────────────────────


def test_legacy_permissions_guest_is_ignored_and_file_unchanged(build, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        svc = build({"roles": _LEGACY_ROLES})
    on_disk = json.loads((tmp_path / "permissions.json").read_text(encoding="utf-8"))
    assert on_disk == {"roles": _LEGACY_ROLES}
    assert set(svc.roles_config.roles) == {"Owner", "Base", "Debugger"}
    assert svc.roles_config.roles["Debugger"].inherits == ["Owner"]
    assert svc.resolve_grants("Guest") == RoleGrants()
    assert len(_retirement_warnings(caplog)) == 1


def test_legacy_guest_is_not_used_as_subagent(build, caplog):
    """沒有 Subagent 的舊檔：舊 Guest 只略過，Subagent 沒有工具並記警告（人類 10/12 同意）。"""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        svc = build({"roles": _LEGACY_ROLES})
    assert svc.resolve_grants("Subagent") == RoleGrants()
    sub = AgentIdentity.for_role("__subagent_probe__", "Subagent")
    advertised = svc.get_allowed_tools(sub)
    assert frozenset(t for t in ALL_TOOLS if svc.is_tool_allowed(t, advertised)) == frozenset()
    assert frozenset(t for t in ALL_TOOLS if svc.is_authorized(sub, t, {}, {})) == frozenset()
    assert len(_retirement_warnings(caplog)) == 1


def test_existing_subagent_wins_over_legacy_guest(build):
    svc = build({"roles": {**_LEGACY_ROLES, "Subagent": {"allowed_tools": ["read"]}}})
    assert svc.resolve_grants("Subagent").tools.explicit == frozenset({"read"})
    assert svc.resolve_grants("Guest") == RoleGrants()


def test_config_without_guest_keeps_subagent_closed_and_quiet(build, caplog):
    roles = {
        "Owner": {"allowed_tools": ["*"]},
        "Base": {"allowed_tools": [], "denied_tools": ["*"]},
    }
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        svc = build({"roles": roles})
    assert svc.resolve_grants("Subagent") == RoleGrants()
    assert _retirement_warnings(caplog) == []


# ── 舊 identity_map：Guest 指派一律當 Base、不改檔 ──────────────────────────


def test_identity_map_guest_values_resolve_to_base(build, tmp_path, caplog, monkeypatch):
    # R-2a：靈魂旗標要經原始 ID 確認；合成的兩個靈魂 ID 宣告為靈魂。
    souls = SimpleNamespace(is_toukasoul=lambda agent_id: agent_id in {"SoulDemoted", "Soul"})
    monkeypatch.setattr("src.services.permission_service.soul_service", souls)
    legacy_map = {
        "tag_rules": {
            "is_toukasoul": {"role": "Owner", "capabilities": ["heartbeat_awakening"]},
            "is_custom": {"role": "Guest", "capabilities": []},
        },
        "agent_overrides": {
            "SoulDemoted": {"role": "Guest", "capabilities": ["heartbeat_awakening"]},
            "DebugAgent": {"role": "Debugger", "capabilities": []},
        },
        "default_role": "Guest",
    }
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        svc = build(_layout("example"), legacy_map)
    on_disk = json.loads((tmp_path / "identity_map.json").read_text(encoding="utf-8"))
    assert on_disk == legacy_map

    assert svc.get_role_key("AnyCustom", is_custom=True) == "Base"
    # 被覆寫成 Guest 的靈魂改當 Base，不落到 is_toukasoul→Owner（不升級）。
    assert svc.get_role_key("SoulDemoted", is_toukasoul=True) == "Base"
    assert svc.get_capabilities_by_id("SoulDemoted", is_toukasoul=True) == [
        "heartbeat_awakening"
    ]
    assert svc.get_role_key("Stranger") == "Base"
    assert svc.get_role_key("DebugAgent") == "Debugger"
    assert svc.get_role_key("Soul", is_toukasoul=True) == "Owner"

    (warning,) = _retirement_warnings(caplog)
    for location in ("tag_rules.is_custom", "agent_overrides.SoulDemoted", "default_role"):
        assert location in warning
