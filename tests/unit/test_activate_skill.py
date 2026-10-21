"""activate_skill 工具測試 (ADR-0066 Phase 3，Pull 模式).

驗證 Agent 呼叫 activate_skill 後，技能寫入 session.active_skills（下一輪生效），
含技能存在性檢查、RBAC、去重。仿 connect_mcp_server 範式。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult
from src.tools.builtin.system import activate_skill
from src.utils.async_db import SessionSnapshot

_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Owner")


def _session(active_skills=None):
    # ADR-0245 修補：session 綁定須等於執行中的 agent（executor 注入的 agent_id）。
    return {
        "title": "t",
        "messages": [],
        "active_skills": active_skills or [],
        "agent_id": "TestAgent",
    }


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_success(mock_skill, mock_perm, mock_session):
    """合法 skill_id + 有權限 → 寫入 session.active_skills、回 Success。"""
    mock_skill.get_skill.return_value = {"id": "biz", "system_prompt": "x"}
    mock_perm.can_use_skill.return_value = True
    mock_session.read_persistence_snapshot = AsyncMock(
        return_value=SessionSnapshot(_session(), "rev")
    )
    mock_session.conditional_save_session = AsyncMock(
        return_value=SavedResult(status="saved", representation="full", revision="next")
    )

    result = await activate_skill("biz", session_id="s1", identity=_IDENTITY, agent_id="TestAgent")
    assert "Success" in result
    mock_session.conditional_save_session.assert_awaited_once()
    assert mock_session.conditional_save_session.await_args.args[:2] == ("s1", "rev")


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_not_found(mock_skill, mock_perm, mock_session):
    """skill 不存在 → Error，不寫 session。"""
    mock_skill.get_skill.return_value = None
    mock_session.conditional_save_session = AsyncMock()

    result = await activate_skill("ghost", session_id="s1", identity=_IDENTITY)
    assert "Error" in result
    mock_session.conditional_save_session.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_rbac_denied(mock_skill, mock_perm, mock_session):
    """RBAC 拒絕（can_use_skill False）→ Error，不寫 session。"""
    mock_skill.get_skill.return_value = {"id": "biz", "system_prompt": "x"}
    mock_perm.can_use_skill.return_value = False
    mock_session.conditional_save_session = AsyncMock()

    result = await activate_skill("biz", session_id="s1", identity=_IDENTITY)
    assert "Error" in result or "not authorized" in result
    mock_session.conditional_save_session.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_already_active(mock_skill, mock_perm, mock_session):
    """已 active → 不重複 append、不再次保存。"""
    mock_skill.get_skill.return_value = {"id": "biz", "system_prompt": "x"}
    mock_perm.can_use_skill.return_value = True
    mock_session.read_persistence_snapshot = AsyncMock(
        return_value=SessionSnapshot(_session(["biz"]), "rev")
    )
    mock_session.conditional_save_session = AsyncMock()

    result = await activate_skill("biz", session_id="s1", identity=_IDENTITY, agent_id="TestAgent")
    assert "already active" in result.lower()
    mock_session.conditional_save_session.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_no_session_id(mock_skill, mock_perm, mock_session):
    """無 session_id → Error。"""
    result = await activate_skill("biz", identity=_IDENTITY)
    assert "Error" in result


@pytest.mark.asyncio
@patch("src.tools.builtin.system.session_service")
@patch("src.tools.builtin.system.permission_service")
@patch("src.tools.builtin.system.skill_service")
async def test_activate_skill_save_contains_skill(mock_skill, mock_perm, mock_session):
    """條件保存的 active_skills 同時包含既有與新啟用的 skill。"""
    mock_skill.get_skill.return_value = {"id": "biz", "system_prompt": "x"}
    mock_perm.can_use_skill.return_value = True
    mock_session.read_persistence_snapshot = AsyncMock(
        return_value=SessionSnapshot(_session(["existing"]), "rev")
    )
    mock_session.conditional_save_session = AsyncMock(
        return_value=SavedResult(status="saved", representation="full", revision="next")
    )

    await activate_skill("biz", session_id="s1", identity=_IDENTITY, agent_id="TestAgent")
    args = mock_session.conditional_save_session.await_args.args
    assert args[:2] == ("s1", "rev")
    assert "biz" in args[2].active_skills
    assert "existing" in args[2].active_skills
    assert args[3] == _IDENTITY
