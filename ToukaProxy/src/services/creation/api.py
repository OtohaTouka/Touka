"""ADR-0069 Phase D — 創建門面（DRY/SSOT）：route 與 agent 工具共用的薄編排層。

無角色邏輯（由呼叫端 gate）、零 IO、**不依賴 fastapi**（保持傳輸無關、雙介面共用）。
僅將已驗證的 Spec 1:1 委派給 ``creation_engine`` / ``mcp_scaffolder``，``eval_scoring``
預設 False 一路傳遞（不啟用即零成本）；``identity``（建立者）一路傳給計分記帳。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.services.creation.engine import creation_engine
from src.services.creation.kinds import launch_context
from src.services.creation.scaffold import mcp_scaffolder

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.services.creation.kinds import CreationLaunchContext
    from src.services.creation.models import (
        CreationResult,
        McpScaffoldSpec,
        McpSpec,
        ScaffoldResult,
        SkillSpec,
    )


async def create_skill(
    spec: SkillSpec,
    *,
    run_smoke: bool = True,
    run_eval: bool = True,
    eval_scoring: bool = False,
    identity: AgentIdentity | None = None,
) -> CreationResult:
    """委派建立 skill（VALIDATE→SMOKE→COMMIT→EVAL）。"""
    return await creation_engine.create(
        "skill", spec, run_smoke=run_smoke, run_eval=run_eval,
        eval_scoring=eval_scoring, identity=identity,
    )


async def create_mcp(
    spec: McpSpec,
    *,
    run_smoke: bool = True,
    run_eval: bool = True,
    eval_scoring: bool = False,
    identity: AgentIdentity | None = None,
    context: CreationLaunchContext | None = None,
) -> CreationResult:
    """委派建立 MCP server（VALIDATE→SMOKE→COMMIT→EVAL）。"""
    return await creation_engine.create(
        "mcp", spec, run_smoke=run_smoke, run_eval=run_eval,
        eval_scoring=eval_scoring, identity=identity,
        context=context or await launch_context("creation"),
    )


def scaffold_mcp(spec: McpScaffoldSpec) -> ScaffoldResult:
    """委派從零生成 MCPServer v2 stub（產出可餵引擎的 McpSpec）。"""
    return mcp_scaffolder.scaffold(spec)


async def scaffold_and_create_mcp(
    spec: McpScaffoldSpec,
    *,
    run_smoke: bool = True,
    run_eval: bool = True,
    eval_scoring: bool = False,
    identity: AgentIdentity | None = None,
    context: CreationLaunchContext | None = None,
) -> tuple[ScaffoldResult, CreationResult | None]:
    """先 scaffold 生成 server stub，成功則接著走 create('mcp')；回 (scaffold, create|None)。"""
    sres = mcp_scaffolder.scaffold(spec)
    if not sres.ok or sres.spec is None:
        return sres, None
    cres = await creation_engine.create(
        "mcp",
        sres.spec,
        run_smoke=run_smoke,
        run_eval=run_eval,
        eval_scoring=eval_scoring,
        identity=identity,
        context=await launch_context("scaffold", context.grant if context else None),
    )
    return sres, cres
