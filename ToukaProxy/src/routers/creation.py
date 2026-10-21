"""ADR-0069 Phase D — 創建路由（Owner/admin 限定）。委派 services.creation.api（DRY）。

prefix 不含 ``/v1``（由 main.py 集中加，ApiPrefixSovereignty）。驗證/SMOKE 失敗回
200 + ``errors``（引擎不丟例外）；無 token → 401、角色不符 → 403 由依賴丟。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_owner_or_admin
from src.services.creation import api as creation_api
from src.services.creation.kinds import launch_context
from src.services.creation.models import (
    McpScaffoldSpec,
    McpSpec,
    ParamSpec,
    SkillSpec,
    ToolSpec,
)

if TYPE_CHECKING:
    from src.services.creation.models import CreationResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/creation", tags=["creation"])

# 限 Owner/admin 使用創建引擎。
_require_creation_role = require_owner_or_admin


class SkillCreateRequest(BaseModel):
    """建立 skill 的請求。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    description: str = ""
    category: str = "General"
    author: str = ""
    is_system: bool = False
    system_prompt: str = ""
    required_tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    run_smoke: bool = True
    run_eval: bool = True
    eval_scoring: bool = False


class McpCreateRequest(BaseModel):
    """建立 MCP server 的請求（既有指令）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    run_smoke: bool = True
    run_eval: bool = True
    eval_scoring: bool = False


class ToolParamReq(BaseModel):
    """scaffold 工具參數。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "str"


class ToolReq(BaseModel):
    """scaffold 工具宣告。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    params: list[ToolParamReq] = Field(default_factory=list)


class ScaffoldCreateRequest(BaseModel):
    """從零 scaffold 一個 MCP server 的請求。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    tools: list[ToolReq] = Field(default_factory=list)
    activate: bool = True  # True：scaffold 後接著走 create('mcp') 完整流水線
    run_smoke: bool = True
    run_eval: bool = True
    eval_scoring: bool = False


class CreationResponse(BaseModel):
    """創建結果（統一回應）。"""

    model_config = ConfigDict(extra="forbid")

    status: str  # "created" | "rejected"
    committed: bool
    result: dict | None = None
    scaffold: dict | None = None
    errors: list[str] = Field(default_factory=list)


def _to_response(res: CreationResult) -> CreationResponse:
    """CreationResult → 統一回應（驗證/ SMOKE 失敗 → status=rejected + errors）。"""
    errors = list(res.validation.errors)
    if res.smoke and not res.smoke.ok and res.smoke.detail:
        errors.append(res.smoke.detail)
    return CreationResponse(
        status="created" if res.committed else "rejected",
        committed=res.committed,
        result=res.model_dump(exclude_none=True),
        errors=[] if res.committed else errors,
    )


@router.post("/skill", response_model=CreationResponse)
async def create_skill_endpoint(
    request: SkillCreateRequest,
    identity: AgentIdentity = Depends(_require_creation_role),  # noqa: B008
) -> CreationResponse:
    """建立 skill（Owner/admin）。"""
    spec = SkillSpec(
        id=request.id,
        name=request.name,
        description=request.description,
        category=request.category,
        author=request.author,
        is_system=request.is_system,
        system_prompt=request.system_prompt,
        required_tools=request.required_tools,
        mcp_servers=request.mcp_servers,
    )
    try:
        res = await creation_api.create_skill(
            spec,
            run_smoke=request.run_smoke,
            run_eval=request.run_eval,
            eval_scoring=request.eval_scoring,
            identity=identity,
        )
    except Exception:
        logger.exception("create_skill endpoint failed")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None
    return _to_response(res)


@router.post("/mcp", response_model=CreationResponse)
async def create_mcp_endpoint(
    request: McpCreateRequest,
    identity: AgentIdentity = Depends(_require_creation_role),  # noqa: B008
) -> CreationResponse:
    """建立 MCP server（既有指令；Owner/admin）。"""
    spec = McpSpec(
        name=request.name,
        command=request.command,
        args=request.args,
        env=request.env,
        description=request.description,
    )
    try:
        res = await creation_api.create_mcp(
            spec,
            run_smoke=request.run_smoke,
            run_eval=request.run_eval,
            eval_scoring=request.eval_scoring,
            identity=identity,
            context=await launch_context("creation"),
        )
    except Exception:
        logger.exception("create_mcp endpoint failed")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None
    return _to_response(res)


@router.post("/scaffold", response_model=CreationResponse)
async def scaffold_mcp_endpoint(
    request: ScaffoldCreateRequest,
    identity: AgentIdentity = Depends(_require_creation_role),  # noqa: B008
) -> CreationResponse:
    """從零 scaffold 一個 MCP server（Owner/admin）；activate=True 則接著走完整流水線。"""
    spec = McpScaffoldSpec(
        name=request.name,
        description=request.description,
        tools=[
            ToolSpec(
                name=t.name,
                description=t.description,
                params=[ParamSpec(name=p.name, type=p.type) for p in t.params],
            )
            for t in request.tools
        ],
    )
    try:
        if request.activate:
            sres, cres = await creation_api.scaffold_and_create_mcp(
                spec,
                run_smoke=request.run_smoke,
                run_eval=request.run_eval,
                eval_scoring=request.eval_scoring,
                identity=identity,
                context=await launch_context("scaffold"),
            )
        else:
            sres, cres = creation_api.scaffold_mcp(spec), None
    except Exception:
        logger.exception("scaffold_mcp endpoint failed")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None

    committed = bool(cres and cres.committed)
    errors = list(sres.errors)
    if cres and not cres.committed:
        errors.extend(cres.validation.errors)
        if cres.smoke and not cres.smoke.ok and cres.smoke.detail:
            errors.append(cres.smoke.detail)
    return CreationResponse(
        status="created" if committed else "rejected",
        committed=committed,
        result=cres.model_dump(exclude_none=True) if cres else None,
        scaffold=sres.model_dump(exclude_none=True),
        errors=[] if committed else errors,
    )
