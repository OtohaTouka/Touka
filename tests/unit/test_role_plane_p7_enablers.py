"""P7 收官 — 角色權限面分離的 SSOT 角色讀寫（ADR-0141）。

- ``display_role``：顯示面有效角色字串。拆分前的扁平 ``role`` 計算屬性已於 P7 移除，
  本屬性為**唯一**有效角色讀法（``human_role or agent_role or "Base"``）。
- ``for_role``：依值路由建構，動態站點（``role=get_role_key(...)`` 值不確定）的唯一入口。
"""

from __future__ import annotations

import pytest
from src.models.identity import AGENT_ROLES, HUMAN_ROLES, AgentIdentity

_SHAPES = {
    "human_admin": AgentIdentity.for_role("H", "admin", is_human=True),
    "human_normal": AgentIdentity.for_role("H", "normal", is_human=True),
    "agent_owner": AgentIdentity.for_role("A", "Owner", is_human=False),
    "agent_base": AgentIdentity.for_role("A", "Base", is_human=False),
    "agent_subagent": AgentIdentity.for_role("A", "Subagent", is_human=False),
    "agent_debugger": AgentIdentity.for_role("A", "Debugger", is_human=False),
    "assumed_admin": AgentIdentity.for_role("A", "admin", is_human=False),
    "agent_system": AgentIdentity.for_role("A", "System", is_human=False),
    "roleless": AgentIdentity(agent_id="X"),
}

# display_role 的有效角色值（human_role 優先、否則 agent_role、皆無則 "Base"）。
_EXPECTED_DISPLAY = {
    "human_admin": "admin",
    "human_normal": "normal",
    "agent_owner": "Owner",
    "agent_base": "Base",
    "agent_subagent": "Subagent",
    "agent_debugger": "Debugger",
    "assumed_admin": "admin",  # admin 代打：human_role 優先
    "agent_system": "System",
    "roleless": "Base",  # 雙欄位皆 None → fallback
}


@pytest.mark.parametrize("key", sorted(_SHAPES))
def test_display_role_returns_effective_role(key: str):
    """display_role＝human_role or agent_role or "Base"（顯示面唯一有效角色讀法）。"""
    assert _SHAPES[key].display_role == _EXPECTED_DISPLAY[key]


def test_display_role_of_roleless_is_base():
    assert AgentIdentity(agent_id="X").display_role == "Base"


@pytest.mark.parametrize("role", sorted(HUMAN_ROLES))
def test_for_role_routes_human_roles_to_human_role(role: str):
    ident = AgentIdentity.for_role("H", role, is_human=True)
    assert ident.human_role == role
    assert ident.agent_role is None


@pytest.mark.parametrize("role", sorted(AGENT_ROLES))
def test_for_role_routes_agent_roles_to_agent_role(role: str):
    ident = AgentIdentity.for_role("A", role, is_human=False)
    assert ident.agent_role == role
    assert ident.human_role is None


def test_for_role_admin_pilot_invariant_routes_by_value_not_is_human():
    """代打不變式：role="admin" 即使 is_human=False 也必落 human_role（絕不進 agent 面）。"""
    pilot = AgentIdentity.for_role("A", "admin", is_human=False)
    assert pilot.human_role == "admin"
    assert pilot.agent_role is None
    assert pilot.is_admin_authority() is True


def test_for_role_agent_role_with_is_human_true_still_routes_by_value():
    """依值路由（非依 is_human）：role="Owner"+is_human=True → agent_role。"""
    ident = AgentIdentity.for_role("A", "Owner", is_human=True)
    assert ident.agent_role == "Owner"
    assert ident.human_role is None


def test_for_role_unknown_value_falls_through_on_is_human_hint():
    assert AgentIdentity.for_role("A", "CustomFoo", is_human=False).agent_role == "CustomFoo"
    assert AgentIdentity.for_role("H", "CustomFoo", is_human=True).human_role == "CustomFoo"


def test_for_role_none_leaves_both_role_fields_unset():
    ident = AgentIdentity.for_role("X", None)
    assert ident.human_role is None
    assert ident.agent_role is None
    assert ident.display_role == "Base"
