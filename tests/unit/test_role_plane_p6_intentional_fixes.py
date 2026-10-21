"""P6 — 四條刻意行為修正（ADR-0141 §8-4）的顯式行為 diff。

**與 P2–P5 不同：這批刻意改變行為**（拆分前的舊/漏洞行為 → 拆分後的正確行為）。
fix a（identity_map 拒人類/內部角色）於 test_identity_admin_security.py；本檔涵蓋 b/c/d。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from src.models.identity import AGENT_ROLES, HUMAN_ROLES, AgentIdentity
from src.services.resource_manager import resource_manager

_TARGET = AgentIdentity.for_role(agent_id="OtherTarget", role="Owner", is_human=False)


# ── fix b：is_owner 的 Debugger 旁路補 `not is_human`（對齊姊妹函式）─────────────


def test_fix_b_debugger_bypass_requires_not_is_human():
    """不一致組合「is_human=True＋agent_role=Debugger」不再經 Debugger 旁路取得主權。

    拆分前 is_owner 的 Debugger 旁路無 is_human 守衛（與 get_vault_root/list_all_agent_stats
    的特權 agent 判定不一致）；補上後，人類帶 Debugger agent 角色落到 human-pilot 分支
    （非該 target 擁有者 → False）。"""
    inconsistent = AgentIdentity.for_role(agent_id="CallerAgent", role="Debugger", is_human=True)
    assert inconsistent.agent_role == "Debugger"
    assert inconsistent.is_human is True
    with patch(
        "src.services.auth_service.AuthService.get_user_by_id", return_value=None
    ):
        assert resource_manager.is_owner(_TARGET, inconsistent) is False


def test_fix_b_consistent_agent_debugger_still_bypasses():
    """回歸鎖：一致的 agent Debugger（is_human=False）仍經旁路取得主權（行為不變）。"""
    agent_dbg = AgentIdentity.for_role(agent_id="CallerAgent", role="Debugger", is_human=False)
    assert resource_manager.is_owner(_TARGET, agent_dbg) is True


# ── fix c：_resolve_project_path 的工作區旁路拆顯式雙面（admin 代打不再旁路）──────


@pytest.mark.asyncio
async def test_fix_c_admin_pilot_no_longer_bypasses_workspace_jail():
    """admin 代打（human_role="admin"／is_human=False）失去 :503 工作區 jail 旁路。

    拆分後（P4）此處走 `is_admin_or_debugger()`＝human_role=="admin" 放行（含代打）；
    P6-c 拆顯式雙面後代打（is_human=False）不再放行——與其在 get_vault_root/is_owner
    已無旁路一致（P5 Option A：代打＝管理閘權、非資源監獄旁路）。"""
    pilot = AgentIdentity.for_role(agent_id="X", role="admin", is_human=False)  # admin 代打
    with patch(
        "src.utils.async_db.get_project_root_path",
        new=AsyncMock(return_value="/outside/workspace"),
    ), patch.object(
        resource_manager, "is_within_workspace", new=AsyncMock(return_value=False)
    ):
        result = await resource_manager.get_project_path("proj1", identity=pilot)
    assert result is None  # 越界根目錄 → 代打被拒


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "is_human"),
    [("admin", True), ("Debugger", False)],  # 人類 admin／agent Debugger
    ids=["human_admin", "agent_debugger"],
)
async def test_fix_c_legit_privileged_still_bypasses(role: str, is_human: bool):
    """回歸鎖：人類 admin 與 agent Debugger 仍可越工作區 jail（顯式雙面各一支）。"""
    ident = AgentIdentity.for_role(agent_id="X", role=role, is_human=is_human)
    with patch(
        "src.utils.async_db.get_project_root_path",
        new=AsyncMock(return_value="/outside/workspace"),
    ), patch.object(
        resource_manager, "is_within_workspace", new=AsyncMock(return_value=False)
    ):
        result = await resource_manager.get_project_path("proj1", identity=ident)
    assert result == "/outside/workspace"  # 特權放行、不受 jail 限制


# ── fix d：跨面純度 validator（human_role ∉ AGENT_ROLES、agent_role ∉ HUMAN_ROLES）─


@pytest.mark.parametrize("human_role", sorted(HUMAN_ROLES))
def test_fix_d_agent_role_cannot_be_a_human_role(human_role: str):
    """agent_role 結構性不可能為人類角色（admin/normal）→ ValidationError。"""
    with pytest.raises(ValidationError):
        AgentIdentity(agent_id="X", agent_role=human_role)


@pytest.mark.parametrize("agent_role", sorted(AGENT_ROLES))
def test_fix_d_human_role_cannot_be_an_agent_role(agent_role: str):
    """human_role 結構性不可能為 agent 角色（Owner/Base/Subagent/Debugger/System）→ 422。"""
    with pytest.raises(ValidationError):
        AgentIdentity(agent_id="X", human_role=agent_role)


def test_fix_d_admin_pilot_shape_still_constructible():
    """合法的 admin 代打形態（human_role="admin"／is_human=False）不受純度 validator 影響
    （admin ∉ AGENT_ROLES）；`is_admin_authority()` 仍放行（收斂為純 human_role 判定）。"""
    pilot = AgentIdentity.for_role(agent_id="X", role="admin", is_human=False)
    assert pilot.human_role == "admin"
    assert pilot.agent_role is None
    assert pilot.is_admin_authority() is True


def test_fix_d_unknown_agent_role_still_allowed():
    """未知/自訂 agent 角色（identity_map 舊資料）不被純度 validator 誤擋（只擋跨面污染）。"""
    ident = AgentIdentity(agent_id="X", agent_role="CustomLegacyRole")
    assert ident.agent_role == "CustomLegacyRole"
