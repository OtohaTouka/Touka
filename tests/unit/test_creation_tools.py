"""ADR-0069 Phase D — 創建 agent 工具單元測試（門檻 + 委派）。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.models.identity import AgentIdentity
from src.services.creation.models import (
    CommitResult,
    CreationResult,
    ScaffoldResult,
    ValidationReport,
)
from src.tools.builtin import creation as creation_tools

_OWNER = AgentIdentity.for_role(agent_id="o", role="Owner")
_MEMBER = AgentIdentity.for_role(agent_id="m", role="Member")


def _committed(cid: str) -> CreationResult:
    return CreationResult(
        validation=ValidationReport(ok=True), committed=True, commit=CommitResult(id=cid)
    )


async def test_create_skill_tool_gates_non_owner():
    with patch.object(creation_tools.creation_api, "create_skill", new=AsyncMock()) as m:
        out = await creation_tools.create_skill(identity=_MEMBER, id="x", description="d")
    assert out.startswith("❌ 權限不足")
    m.assert_not_awaited()


async def test_create_skill_tool_no_identity():
    out = await creation_tools.create_skill(identity=None, id="x", description="d")
    assert out.startswith("❌ 權限不足")


async def test_create_skill_tool_delegates_for_owner():
    with patch.object(
        creation_tools.creation_api,
        "create_skill",
        new=AsyncMock(return_value=_committed("tool-skill-a")),
    ) as m:
        out = await creation_tools.create_skill(
            identity=_OWNER, id="tool-skill-a", description="d"
        )
    assert out.startswith("✅ 已創建 tool-skill-a")
    m.assert_awaited_once()


async def test_create_mcp_tool_missing_params():
    out = await creation_tools.create_mcp(identity=_OWNER, name="x")  # 缺 command
    assert out.startswith("❌ 缺少必要參數")


async def test_scaffold_mcp_tool_delegates():
    sres = ScaffoldResult(ok=True, server_path="/x/server.py")
    with patch.object(
        creation_tools.creation_api,
        "scaffold_and_create_mcp",
        new=AsyncMock(return_value=(sres, _committed("tool-scaf"))),
    ):
        out = await creation_tools.scaffold_mcp(
            identity=_OWNER, name="tool-scaf", tools=[{"name": "ping"}]
        )
    assert "server.py" in out
    assert "✅" in out
