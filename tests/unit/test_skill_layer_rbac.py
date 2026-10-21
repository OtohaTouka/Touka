"""ADR-0060：SkillLayer RBAC 工具授權隔離測試 (TDD).

驗證 SkillLayer 的工具收集步驟（step 3）只使用 authorized_skill_ids，
而非 skill_ids（原始未過濾清單），防止 RBAC 授權繞過。

繞過情境（Red phase 必須失敗）：
  攻擊者在 x-touka-skills 標頭中包含未授權技能的 ID。
  舊代碼：for sid in skill_ids   → 未授權技能的 required_tools 仍被收集 (Bug)
  新代碼：for sid in authorized_skill_ids → RBAC 過濾後才收集 (Fix)
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

_SKILL_A = {
    "id": "skill-a",
    "name": "Skill A",
    "system_prompt": "你是技能A",
    "required_tools": ["tool_a1", "tool_a2"],
}

_SKILL_B = {
    "id": "skill-b",
    "name": "Skill B",
    "system_prompt": "你是技能B",
    "required_tools": ["tool_b1"],
}

_BASE_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Base")


def _make_ctx(skill_header: str, identity: AgentIdentity = _BASE_IDENTITY) -> PipelineContext:
    """建立含有技能標頭的 PipelineContext 測試輔助。"""
    ctx = PipelineContext(identity=identity)
    ctx["headers"] = {"x-touka-skills": skill_header}
    return ctx


# ---------------------------------------------------------------------------
# 測試案例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_empty_header_no_usable_no_dispatcher(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """空 header ＋ 無可用技能（ADR-0148）：不注入 dispatcher、不收集工具。

    ADR-0148：dispatcher（skill_registry＋activate_skill 廣告）依 can_use_skill 過濾——
    此身分（Base）對 catalog 內技能皆無權 → usable 為空 → 不注入 dispatcher（廣告面
    ＝授權面）。無業務 header 技能故不收 required_tools。
    """
    mock_perm.can_use_skill.return_value = False
    mock_skill.list_skills.return_value = [{"id": "skill-a", "is_system": False}]

    ctx = _make_ctx("")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_tools" not in ctx
    assert ctx["active_skills"] == []  # 無 dispatcher
    mock_skill.render_skill_registry.assert_not_called()


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_authorized_skill_tools_are_collected(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """已授權技能的 required_tools 必須被收集到 ctx['skill_required_tools']。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _SKILL_A
    mock_skill.apply_skills_to_request.return_value = None
    mock_skill.list_skills.return_value = []

    ctx = _make_ctx("skill-a")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_tools" in ctx
    tools = ctx["skill_required_tools"]
    assert "tool_a1" in tools
    assert "tool_a2" in tools


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_rbac_bypass_blocked_unauthorized_tools_not_collected(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """RBAC 繞過防護：未授權技能的 required_tools 絕不能出現在工具白名單中。

    情境：skill-a 授權通過，skill-b 被 RBAC 拒絕。
    修復前（Bug）：for sid in skill_ids → tool_b1 被收集（安全漏洞）
    修復後（Fix）：for sid in authorized_skill_ids → tool_b1 被隔離
    """
    def _can_use(identity: AgentIdentity, skill_id: str) -> bool:  # noqa: ARG001
        return skill_id == "skill-a"  # 只授權 skill-a

    def _get_skill(skill_id: str) -> dict | None:
        return _SKILL_A if skill_id == "skill-a" else _SKILL_B

    mock_perm.can_use_skill.side_effect = _can_use
    mock_skill.get_skill.side_effect = _get_skill
    mock_skill.apply_skills_to_request.return_value = None
    mock_skill.list_skills.return_value = []

    ctx = _make_ctx("skill-a,skill-b")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_tools" in ctx
    tools = ctx["skill_required_tools"]

    # skill-a 的工具必須存在（已授權）
    assert "tool_a1" in tools, "已授權技能 skill-a 的工具應被收集"
    assert "tool_a2" in tools, "已授權技能 skill-a 的工具應被收集"

    # skill-b 的工具必須不存在（RBAC 攔截）
    assert "tool_b1" not in tools, (
        "RBAC 繞過漏洞：未授權技能 skill-b 的 tool_b1 不應出現在工具白名單"
    )


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_all_skills_unauthorized_no_tools_collected(
    mock_perm: MagicMock, mock_skill: MagicMock
) -> None:
    """所有技能均未授權時，不收集工具、且不注入 dispatcher（ADR-0148 廣告面＝授權面）。"""
    mock_perm.can_use_skill.return_value = False
    mock_skill.get_skill.return_value = _SKILL_A
    mock_skill.list_skills.return_value = [{"id": "skill-a", "is_system": False}]

    ctx = _make_ctx("skill-a")
    await SkillLayer().process(MagicMock(), ctx)

    assert "skill_required_tools" not in ctx
    # 無可用技能 → dispatcher 不注入（不再「常駐」）
    assert ctx["active_skills"] == []
