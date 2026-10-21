"""ADR-0061：SkillLayer mcp_servers 收集與 RBAC 隔離測試 (TDD).

驗證 SkillLayer 在收集工具名單的同時，也正確收集技能所需的 MCP 伺服器清單，
並存入 ctx["skill_required_mcp"] 供 ToolLayer 使用。

安全約束：
  ctx["skill_required_mcp"] 只能包含來自「已授權」技能的 server ID。
  未授權技能的 mcp_servers 必須被 RBAC 隔離，不得進入 context。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.middleware.layers.skill import SkillLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity

# ---------------------------------------------------------------------------
# 測試資料
# ---------------------------------------------------------------------------

_SKILL_MCP = {
    "id": "skill-mcp",
    "name": "MCP Skill",
    "system_prompt": "uses mcp",
    "required_tools": [],
    "mcp_servers": ["srv-alpha", "srv-beta"],
}

_SKILL_BOTH = {
    "id": "skill-both",
    "name": "Both Skill",
    "system_prompt": "has both",
    "required_tools": ["tool-x"],
    "mcp_servers": ["srv-gamma"],
}

_SKILL_NO_MCP = {
    "id": "skill-no-mcp",
    "name": "No MCP Skill",
    "system_prompt": "no mcp",
    "required_tools": ["tool-y"],
    "mcp_servers": [],
}

_SKILL_SHARE_A = {
    "id": "skill-share-a",
    "name": "Share A",
    "system_prompt": "share",
    "required_tools": [],
    "mcp_servers": ["shared-srv"],
}

_SKILL_SHARE_B = {
    "id": "skill-share-b",
    "name": "Share B",
    "system_prompt": "share",
    "required_tools": [],
    "mcp_servers": ["shared-srv"],
}

_SKILL_EVIL = {
    "id": "skill-evil",
    "name": "Evil Skill",
    "system_prompt": "evil",
    "required_tools": [],
    "mcp_servers": ["srv-secret"],
}

_BASE_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Base")


def _make_ctx(skill_header: str, identity: AgentIdentity = _BASE_IDENTITY) -> PipelineContext:
    ctx = PipelineContext(identity=identity)
    ctx["headers"] = {"x-touka-skills": skill_header}
    return ctx


# ---------------------------------------------------------------------------
# 測試案例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_mcp_servers_from_authorized_skill_stored_in_ctx(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """已授權技能的 mcp_servers 必須存入 ctx['skill_required_mcp']。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _SKILL_MCP
    mock_skill.apply_skills_to_request.return_value = None

    ctx = _make_ctx("skill-mcp")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_mcp" in ctx, "已授權技能的 mcp_servers 應存入 ctx"
    mcp = ctx["skill_required_mcp"]
    assert "srv-alpha" in mcp
    assert "srv-beta" in mcp


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_mcp_servers_deduplicated_across_multiple_skills(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """兩個授權技能都宣告同一個 server，ctx 中只應出現一次（dict.fromkeys 去重）。"""
    _lookup = {"skill-share-a": _SKILL_SHARE_A, "skill-share-b": _SKILL_SHARE_B}

    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.side_effect = lambda sid: _lookup.get(sid)
    mock_skill.apply_skills_to_request.return_value = None

    ctx = _make_ctx("skill-share-a,skill-share-b")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_mcp" in ctx
    assert ctx["skill_required_mcp"].count("shared-srv") == 1, (
        "重複的 server ID 應被去重，只保留一份"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_unauthorized_skill_mcp_servers_not_collected(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """RBAC 隔離：未授權技能的 mcp_servers 絕不能進入 ctx['skill_required_mcp']。"""
    _lookup = {"skill-mcp": _SKILL_MCP, "skill-evil": _SKILL_EVIL}

    def _can_use(identity: AgentIdentity, skill_id: str) -> bool:  # noqa: ARG001
        return skill_id == "skill-mcp"

    mock_perm.can_use_skill.side_effect = _can_use
    mock_skill.get_skill.side_effect = lambda sid: _lookup.get(sid)
    mock_skill.apply_skills_to_request.return_value = None

    ctx = _make_ctx("skill-mcp,skill-evil")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_mcp" in ctx
    mcp = ctx["skill_required_mcp"]

    assert "srv-alpha" in mcp, "已授權技能的 server 應被收集"
    assert "srv-secret" not in mcp, (
        "RBAC 隔離漏洞：未授權技能的 srv-secret 不應出現在 ctx"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_skill_with_no_mcp_servers_does_not_set_ctx_key(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """mcp_servers 為空的技能不應讓 ctx['skill_required_mcp'] 出現。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _SKILL_NO_MCP
    mock_skill.apply_skills_to_request.return_value = None

    ctx = _make_ctx("skill-no-mcp")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_mcp" not in ctx, (
        "mcp_servers 為空的技能不應設定 ctx['skill_required_mcp']"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_skill_with_both_fields_sets_both_ctx_keys(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """同時具有 required_tools 與 mcp_servers 的技能，兩個 ctx key 都應被設定。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _SKILL_BOTH
    mock_skill.apply_skills_to_request.return_value = None

    ctx = _make_ctx("skill-both")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_tools" in ctx
    assert "tool-x" in ctx["skill_required_tools"]

    assert "skill_required_mcp" in ctx
    assert "srv-gamma" in ctx["skill_required_mcp"]


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_empty_header_no_mcp_ctx_key(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """空的 x-touka-skills 標頭不應設定 ctx['skill_required_mcp']。"""
    ctx = _make_ctx("")
    await SkillLayer().process(MagicMock(), ctx)

    mock_perm.can_use_skill.assert_not_called()
    assert "skill_required_mcp" not in ctx
