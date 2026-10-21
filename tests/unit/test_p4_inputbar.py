"""輸入列修復批 P4（四功能區調查 §inputbar，ADR-0161）後端對釘。

- is_system 過濾：get_skills(include_system=false) 排除系統技能（三個 dispatcher／
  coordinator 不外洩成使用者可勾選項）；預設 True＝設定頁等既有消費者零破壞。
- MCP 動態掛載旗標：ctx.mcp_dispatcher（輸入列 toggle 逐則帶入）等效於舊
  active_skills 含 system-mcp-dispatcher，觸發 dispatcher 模式（漸進揭露）。
- 雙重注入守衛：手動勾 system-skill-dispatcher 被剔除授權路徑（其 body 由
  dispatcher_body 統一注入，不再於 apply_skills_to_request 迴圈重注一次）。
- schema：mcp_dispatcher 預設 False。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.middleware.layers.skill import SkillLayer
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers.skills import get_skills
from src.utils.permission_grants import NameGrant

_IDENTITY = AgentIdentity.for_role(agent_id="P4Agent", role="Owner")
_SKILL_DISPATCHER = "system-skill-dispatcher"
_MCP_DISPATCHER = "system-mcp-dispatcher"


# ── is_system 過濾（get_skills 端點）──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_skills_excludes_system_when_opted_out():
    """include_system=false → 系統技能全排除；預設 True → 保留（零破壞既有消費者）。"""
    mixed = [
        {"id": "biz-skill", "is_system": False},
        {"id": _MCP_DISPATCHER, "is_system": True},
        {"id": _SKILL_DISPATCHER, "is_system": True},
        {"id": "deep-research-coordinator", "is_system": True},
    ]
    with patch("src.routers.skills.skill_service") as mock_svc:
        mock_svc.list_skills.return_value = mixed
        excluded = await get_skills(include_system=False, _identity=_IDENTITY)
        included = await get_skills(include_system=True, _identity=_IDENTITY)

    assert [s["id"] for s in excluded["skills"]] == ["biz-skill"]  # 三系統技能全退出
    assert len(included["skills"]) == 4  # 預設保留全部


# ── MCP 動態掛載旗標（ctx.mcp_dispatcher 觸發 dispatcher 模式）──────────────────


def _setup_perm_deny_builtins(mock_perm: MagicMock) -> None:
    mock_perm.get_allowed_tools.return_value = NameGrant()
    mock_perm.evaluate_policy.return_value = type("D", (), {"effect": "DENY", "reason": "x"})()


def _make_pm(tools: list[dict]) -> AsyncMock:
    pm = AsyncMock()
    pm.get_all_tools = AsyncMock(return_value=tools)
    pm.ensure_connected = AsyncMock()
    reg = MagicMock()
    reg.get_server.return_value = MagicMock()
    pm.registry = reg
    return pm


@pytest.mark.asyncio
@patch("src.middleware.layers.tool.permission_service")
async def test_mcp_dispatcher_flag_activates_dispatcher_mode(mock_perm: MagicMock):
    """ctx.mcp_dispatcher=True（無 active_skills dispatcher）＝觸發 dispatcher 模式：
    skill_required_mcp 的 server 工具可見（等效舊 active_skills 含 system-mcp-dispatcher）。"""
    _setup_perm_deny_builtins(mock_perm)
    skill_tool: dict[str, Any] = {
        "server": "srv-skill", "type": "function",
        "function": {"name": "skill_tool", "description": "x", "parameters": {}},
    }
    pm = _make_pm([skill_tool])
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="P4Agent", role="Base"))
    ctx["agent_id"] = "P4Agent"
    ctx["is_custom"] = False
    ctx["is_toukasoul"] = False
    ctx["headers"] = {}
    ctx["skill_required_mcp"] = ["srv-skill"]
    ctx["mcp_dispatcher"] = True   # ADR-0161：新旗標路徑（無 active_skills dispatcher）
    ctx["protocol_manager"] = pm

    request = MagicMock()
    request.tools = []
    await ToolLayer().process(request, ctx)

    names = [t.get("function", {}).get("name") for t in request.tools if isinstance(t, dict)]
    assert "skill_tool" in names, "ctx.mcp_dispatcher 應等效觸發 dispatcher 模式"


# ── 雙重注入守衛（system-skill-dispatcher 手動勾被剔除授權路徑）─────────────────


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_manual_skill_dispatcher_deduped(mock_perm, mock_skill, mock_session):
    """手動勾 system-skill-dispatcher → 不進 authorized_skill_ids（避免與 dispatcher_body
    雙重注入）；但仍由 dispatcher 路徑進 active_skills（body 注一次）。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = {"id": "x", "system_prompt": "b"}
    mock_skill.list_skills.return_value = [{"id": "biz", "is_system": False}]
    mock_skill.render_skill_registry.return_value = "registry-body"
    mock_skill.apply_skills_to_request = MagicMock()

    ctx = PipelineContext(identity=_IDENTITY)
    ctx["headers"] = {"x-touka-skills": _SKILL_DISPATCHER}  # 手動勾調度員本身
    await SkillLayer().process(MagicMock(), ctx)

    authorized = mock_skill.apply_skills_to_request.call_args[0][1]
    assert _SKILL_DISPATCHER not in authorized, "手動勾的 skill-dispatcher 不得進授權注入路徑"
    # 但 dispatcher 路徑仍把它放進 active_skills（body 由 dispatcher_body 統一注一次）
    assert _SKILL_DISPATCHER in ctx["active_skills"]


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_manual_mcp_dispatcher_not_deduped(mock_perm, mock_skill, mock_session):
    """守衛只剔 skill-dispatcher，不得誤傷 system-mcp-dispatcher（啟用旗標須保留進
    active_skills 供 tool.py 向後相容讀取）。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = {"id": _MCP_DISPATCHER, "system_prompt": "b"}
    mock_skill.list_skills.return_value = [{"id": "biz", "is_system": False}]
    mock_skill.render_skill_registry.return_value = "r"
    mock_skill.apply_skills_to_request = MagicMock()

    ctx = PipelineContext(identity=_IDENTITY)
    ctx["headers"] = {"x-touka-skills": _MCP_DISPATCHER}
    await SkillLayer().process(MagicMock(), ctx)

    assert _MCP_DISPATCHER in ctx["active_skills"], "mcp-dispatcher 啟用旗標須保留（向後相容）"


# ── schema 預設 ──────────────────────────────────────────────────────────────


def test_mcp_dispatcher_schema_default_false():
    """OpenAIChatRequest / AnthropicRequest 的 mcp_dispatcher 預設 False、可設 True。"""
    assert OpenAIChatRequest(model="m", messages=[]).mcp_dispatcher is False
    assert OpenAIChatRequest(model="m", messages=[], mcp_dispatcher=True).mcp_dispatcher is True
    assert AnthropicRequest(model="m", messages=[]).mcp_dispatcher is False
    assert AnthropicRequest(model="m", messages=[], mcp_dispatcher=True).mcp_dispatcher is True
