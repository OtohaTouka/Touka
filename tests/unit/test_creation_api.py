"""ADR-0069 Phase D — 創建門面（services.creation.api）單元測試。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.services.creation import api as creation_api
from src.services.creation.models import (
    McpScaffoldSpec,
    McpSpec,
    SkillSpec,
    SmokeReport,
    ToolSpec,
)
from src.services.creation.smoke import ToolInfo

_PATCH_SMOKE = "src.services.creation.mcp_kind.smoke_mcp_server"
_PATCH_LIST = "src.services.creation.mcp_kind.list_mcp_tools"


async def test_create_skill_delegates_and_commits():
    res = await creation_api.create_skill(
        SkillSpec(id="api-skill-a", description="d", system_prompt="b")
    )
    assert res.committed is True


async def test_create_mcp_validation_failure_returns_report():
    res = await creation_api.create_mcp(
        McpSpec(name="api-evil", command="sh;rm -rf /", args=[])
    )
    assert res.committed is False
    assert res.validation.ok is False


async def test_scaffold_and_create_skips_engine_on_scaffold_fail():
    sres, cres = await creation_api.scaffold_and_create_mcp(
        McpScaffoldSpec(name="api-empty", tools=[])  # 空工具 → scaffold 失敗
    )
    assert sres.ok is False
    assert cres is None


async def test_scaffold_and_create_full_pipeline():
    with (
        patch(_PATCH_SMOKE, new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=1))),
        patch(_PATCH_LIST, new=AsyncMock(return_value=[ToolInfo(name="ping")])),
    ):
        sres, cres = await creation_api.scaffold_and_create_mcp(
            McpScaffoldSpec(
                name="api-scaffold-full",
                tools=[ToolSpec(name="ping", description="p")],
            )
        )
    assert sres.ok is True
    assert cres is not None and cres.committed is True
    assert cres.evaluation is not None and len(cres.evaluation.tasks) == 1
