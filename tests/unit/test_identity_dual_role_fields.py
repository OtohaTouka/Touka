"""P7 收官 — AgentIdentity 雙欄位（human_role／agent_role）＋相容層拆除後的不變式（ADR-0141）。

角色權限面分離絞殺者收官：角色由**互斥雙欄位**承載，扁平 ``role`` 計算屬性與舊式 ``role=``
建構相容映射已移除。有效角色一律讀 ``display_role``／走謂詞；動態建構走 ``for_role``。
本檔鎖住拆除後的模型不變式——含「``.role`` 已不存在」與「舊式 ``role=`` 不再被路由」的
回歸鎖，確保相容層永不悄悄長回。
"""

from __future__ import annotations

import pytest
from src.models.identity import (
    AGENT_ROLES,
    ASSIGNABLE_AGENT_ROLES,
    HUMAN_ROLES,
    AgentIdentity,
)

# ── 雙欄位建構：for_role 值路由與顯式 human_role/agent_role ──────────────────────


def test_agent_construction_populates_agent_role():
    """agent 角色（for_role，is_human 預設 False）：進 agent_role，human_role 為 None。"""
    ident = AgentIdentity.for_role("A", "Owner")
    assert ident.agent_role == "Owner"
    assert ident.human_role is None
    assert ident.display_role == "Owner"


def test_human_construction_populates_human_role():
    """人類角色（for_role，is_human=True）：進 human_role，agent_role 為 None。"""
    ident = AgentIdentity.for_role("H", "admin", is_human=True)
    assert ident.human_role == "admin"
    assert ident.agent_role is None
    assert ident.display_role == "admin"


def test_explicit_dual_fields_construction():
    """顯式雙欄位建構（不經 for_role）：直接指定 agent_role。"""
    ident = AgentIdentity(agent_id="A", agent_role="Subagent")
    assert ident.agent_role == "Subagent"
    assert ident.human_role is None
    assert ident.display_role == "Subagent"


def test_admin_pilot_pair_human_role_wins():
    """admin 代打（human_role="admin"、agent_role=None、is_human=False）：display_role="admin"。"""
    pilot = AgentIdentity.for_role("A", "admin", is_human=False)
    assert pilot.human_role == "admin"
    assert pilot.agent_role is None
    assert pilot.display_role == "admin"
    assert pilot.is_admin_authority()


def test_default_display_role_is_base():
    """無角色（雙欄位皆 None）：display_role fallback "Base"。"""
    ident = AgentIdentity(agent_id="A")
    assert ident.display_role == "Base"
    assert ident.agent_role is None
    assert ident.human_role is None


def test_str_coercion_still_works():
    """字串轉型（Identity Objectification）不變。"""
    ident = AgentIdentity.model_validate("SomeAgent")
    assert ident.agent_id == "SomeAgent"
    assert ident.display_role == "Base"


# ── 相容層拆除的回歸鎖：`.role` 不存在、舊式 role= 不再被路由 ─────────────────────


def test_flat_role_attribute_removed():
    """扁平 ``role`` 計算屬性已於 P7 移除——存取須 AttributeError（相容層不可復活）。"""
    ident = AgentIdentity.for_role("A", "Owner")
    with pytest.raises(AttributeError):
        _ = ident.role  # type: ignore[attr-defined]


def test_model_dump_has_no_role_key_and_keeps_dual_fields():
    """``model_dump()`` 不再含 ``"role"`` 鍵（計算屬性已移除），雙欄位照常序列化。"""
    ident = AgentIdentity.for_role("A", "Owner")
    d = ident.model_dump()
    assert "role" not in d
    assert d["agent_role"] == "Owner"
    assert d["human_role"] is None


def test_legacy_role_kwarg_no_longer_routed():
    """舊式扁平 ``role=`` 不再被路由進雙欄位（相容映射已移除）——須改走 for_role。

    以 dict 展開模擬殘留鍵；pydantic 依 extra 規則不落入任一角色欄位（fail-safe → Base）。
    """
    ident = AgentIdentity(**{"agent_id": "A", "role": "Owner"})
    assert ident.agent_role is None
    assert ident.human_role is None
    assert ident.display_role == "Base"


def test_model_dump_round_trip_reconstructs():
    """dump（雙欄位）回建等價，忽略無 role 鍵。"""
    ident = AgentIdentity.for_role("H", "admin", is_human=True)
    rebuilt = AgentIdentity(**ident.model_dump())
    assert rebuilt.human_role == "admin"
    assert rebuilt.agent_role is None
    assert rebuilt.display_role == "admin"


def test_frozen_still_holds():
    """模型仍不可變（frozen=True 未變）。"""
    ident = AgentIdentity.for_role("A", "Owner")
    with pytest.raises((AttributeError, TypeError, ValueError)):
        ident.agent_role = "Subagent"  # type: ignore[misc]


# ── 兩集合常數 ──────────────────────────────────────────────────────────────


def test_role_sets_defined():
    assert frozenset({"admin", "normal"}) == HUMAN_ROLES
    assert frozenset({"Owner", "Base", "Subagent", "Debugger", "System"}) == AGENT_ROLES
    # ADR-0245：Subagent 只給子代理，和 System 一樣不能經 identity_map 指派
    assert frozenset({"Owner", "Base", "Debugger"}) == ASSIGNABLE_AGENT_ROLES
    # 兩面不相交（拆分不變式的基礎）
    assert HUMAN_ROLES.isdisjoint(AGENT_ROLES)
