"""SkillLayer auto-trigger 測試 (ADR-0066 Phase 3，Pull 模式；ADR-0148 廣告 RBAC 過濾).

驗證 SkillLayer：
  - skill_ids 來源 = header ∪ session.active_skills（activate_skill 寫入 / pulse 來源）
  - 使用者/Agent 指定的 skill 過 RBAC（can_use_skill）
  - **ADR-0148**：技能調度員（skill_registry＋activate_skill 廣告）依 can_use_skill 過濾
    ——唯有「用得到 ≥1 技能」的身分才注入 dispatcher，且 registry 只列 usable 技能；
    無可用技能的角色（Base/Subagent）看不到 registry、拿不到 activate_skill（廣告面＝授權面）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.middleware.layers.skill import SkillLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity

_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Owner")
_DISPATCHER = "system-skill-dispatcher"


def _ctx(header="", session_id=None):
    ctx = PipelineContext(identity=_IDENTITY)
    ctx["headers"] = {"x-touka-skills": header}
    if session_id:
        ctx["session_id"] = session_id
    return ctx


def _skill(sid="x"):
    return {"id": sid, "system_prompt": "body", "required_tools": [], "mcp_servers": []}


def _catalog(*ids):
    """list_skills() 回傳形狀（含 is_system 旗標）。"""
    return [{"id": sid, "is_system": False} for sid in ids]


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_session_active_skills_injected(mock_perm, mock_skill, mock_session):
    """session.active_skills 有 skill（header 空）→ 從 session 讀並注入。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _skill("biz")
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = []
    mock_session.get_session_by_id = AsyncMock(return_value={"active_skills": ["biz"]})

    ctx = _ctx(header="", session_id="s1")
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert "biz" in injected
    assert "biz" in ctx["active_skills"]


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_header_session_union_dedup(mock_perm, mock_skill, mock_session):
    """header ∪ session 聯集去重（兩邊同一 skill → 注入一次）。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.side_effect = lambda sid: _skill(sid)
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = []
    mock_session.get_session_by_id = AsyncMock(
        return_value={"active_skills": ["shared", "from-session"]}
    )

    ctx = _ctx(header="shared,from-header", session_id="s1")
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert injected.count("shared") == 1
    assert "from-header" in injected
    assert "from-session" in injected


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_unauthorized_session_skill_filtered(mock_perm, mock_skill, mock_session):
    """未授權 skill（session 內）被 RBAC 過濾，不注入。"""
    mock_perm.can_use_skill.side_effect = lambda identity, sid: sid == "good"
    mock_skill.get_skill.side_effect = lambda sid: _skill(sid)
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = []
    mock_session.get_session_by_id = AsyncMock(
        return_value={"active_skills": ["good", "evil"]}
    )

    ctx = _ctx(header="", session_id="s1")
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert "good" in injected
    assert "evil" not in injected


# ── ADR-0148：技能調度員廣告 RBAC 過濾 ─────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_dispatcher_injected_when_has_usable_skills(mock_perm, mock_skill, mock_session):
    """有可用技能（can_use_skill=True）→ 注入 dispatcher＋registry 只列 usable。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = _catalog("alpha", "beta")
    mock_skill.render_skill_registry.return_value = "<skill_registry>...</skill_registry>"

    ctx = _ctx(header="")
    await SkillLayer().process(MagicMock(), ctx)

    assert _DISPATCHER in ctx["active_skills"]
    # registry 依 usable 集合渲染（只含 usable ids）並以 dispatcher_body 併入同一 apply 呼叫
    mock_skill.render_skill_registry.assert_called_once_with(["alpha", "beta"])
    assert (
        mock_skill.apply_skills_to_request.call_args.kwargs.get("dispatcher_body")
        == "<skill_registry>...</skill_registry>"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_dispatcher_omitted_when_no_usable_skills(mock_perm, mock_skill, mock_session):
    """無可用技能（can_use_skill=False，如 Base/Subagent）→ 不注入 dispatcher、
    不廣告 activate_skill、不渲染 registry（廣告面＝授權面，杜絕「看得到用不了」）。"""
    mock_perm.can_use_skill.return_value = False
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = _catalog("alpha", "beta")

    ctx = _ctx(header="")
    await SkillLayer().process(MagicMock(), ctx)

    assert _DISPATCHER not in ctx["active_skills"]
    mock_skill.render_skill_registry.assert_not_called()
    # 無可用技能 → dispatcher_body 為 None（不注入 registry）
    assert mock_skill.apply_skills_to_request.call_args.kwargs.get("dispatcher_body") is None


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_registry_lists_only_usable_skills(mock_perm, mock_skill, mock_session):
    """部分可用：registry 只列 can_use_skill=True 的技能（未授權的不廣告）。"""
    mock_perm.can_use_skill.side_effect = lambda identity, sid: sid == "usable"
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = _catalog("usable", "forbidden")
    mock_skill.render_skill_registry.return_value = "<skill_registry>usable</skill_registry>"

    ctx = _ctx(header="")
    await SkillLayer().process(MagicMock(), ctx)

    assert _DISPATCHER in ctx["active_skills"]
    mock_skill.render_skill_registry.assert_called_once_with(["usable"])


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_system_skills_excluded_from_usable(mock_perm, mock_skill, mock_session):
    """系統技能（is_system）不入 usable 集合（不自我列舉）。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.apply_skills_to_request = MagicMock()
    mock_skill.list_skills.return_value = [
        {"id": "biz", "is_system": False},
        {"id": _DISPATCHER, "is_system": True},
    ]
    mock_skill.render_skill_registry.return_value = "<skill_registry>biz</skill_registry>"

    ctx = _ctx(header="")
    await SkillLayer().process(MagicMock(), ctx)

    mock_skill.render_skill_registry.assert_called_once_with(["biz"])
