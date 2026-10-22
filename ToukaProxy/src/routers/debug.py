"""Debug endpoints for tool inspection and execution."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request  # noqa: TC002
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: TC002
from pydantic import BaseModel, Field  # noqa: TC002

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.routers.system import restore_in_progress
from src.services import context_snapshot_service
from src.services.agent_reset.orchestrator import reset_debug_agent_data
from src.services.agent_service import agent_service
from src.services.authorization_gateway import (
    require_owner_or_admin,
    require_privileged,
    require_soul_access,
)
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.resource_manager import resource_manager
from src.services.scorecard import formatter as scorecard_formatter
from src.services.scorecard import scenarios as scorecard_scenarios
from src.services.scorecard.engine import run_scorecard
from src.services.scorecard.runner import make_real_runner
from src.services.tool_executor import ToolExecutor
from src.utils.auth_mode import AuthMode  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/tools")
async def get_debug_tools(
    request: Request,
    identity: AgentIdentity = Depends(get_verified_identity)  # noqa: B008
) -> Any:
    """獲取全系統可用的工具 Schema 清單 (SSOT)。"""
    # 僅限 Owner／Debugger／人類 admin（與 _require_debug_role 三元組對齊）
    if not identity or not identity.is_privileged():
        agent_id = identity.agent_id if identity else "Anonymous"
        role = identity.display_role if identity else "None"
        logger.warning(f"🚫 Unauthorized tools access attempt by {agent_id} (Role: {role})")
        return JSONResponse(content={"tools": []}, status_code=200)

    context = request.app.state.context
    system_degraded = context.get("system_degraded", False)
    all_tools = []

    # 1. 彙整原生內建工具
    builtin_schemas = context.get("builtin_tools_schema", [])
    all_tools.extend(builtin_schemas)

    # 2. 彙整記憶模組工具
    memory_schemas = context.get("memory_tools_schema", [])
    if memory_schemas:
        all_tools.extend(memory_schemas)

    # 3. 彙整插件工具
    plugin_schemas = context.get("plugin_schemas", [])
    if plugin_schemas:
        all_tools.extend(plugin_schemas)

    # 4. 彙整 MCP 工具
    protocol_manager = context.get("protocol_manager")
    if protocol_manager:
        try:
            mcp_tools = await protocol_manager.get_all_tools()
            all_tools.extend(mcp_tools)
        except Exception:
            logger.exception("Error fetching MCP tools")

    # 去重 (依據名稱) 並注入降級標籤
    seen = set()
    unique_tools = []
    for tool in all_tools:
        name = tool.get("function", {}).get("name")
        if name and name not in seen:
            if system_degraded:
                desc = tool.get("function", {}).get("description", "")
                if "[⚠️ DEGRADED MODE]" not in desc:
                    tool["function"]["description"] = desc + " [⚠️ DEGRADED MODE]"
            unique_tools.append(tool)
            seen.add(name)

    content = {"tools": unique_tools}
    response = JSONResponse(content=content)

    # 支援自動領卡（Handshake）
    new_token = getattr(request.state, "new_auth_token", None)
    if new_token:
        response.headers["X-New-Auth-Token"] = new_token

    return response

def _safe_json_line(line: str) -> dict[str, Any] | None:
    """把一行日誌解析為 dict；壞行回 None（避免 try-except-continue）。"""
    try:
        data = json.loads(line.strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_last_payload(agent_id: str) -> dict[str, Any] | None:  # noqa: IDENTITY_SSOT
    """讀 proxy_payload.log 最近一筆屬於該 agent 的請求 payload（static vs 實際送出對照）。"""
    log_path = Path(resource_manager.get_base_path()) / "logs" / "proxy_payload.log"
    if not log_path.exists():
        return None
    try:
        content = resource_manager.read_file_text(log_path)
    except Exception:
        logger.debug("read last payload failed", exc_info=True)
        return None
    for line in reversed((content or "").splitlines()):
        data = _safe_json_line(line)
        if data is not None and data.get("agent_id") == agent_id:
            return data
    return None


@router.get("/context-snapshot")
async def get_context_snapshot(
    request: Request,
    agent_id: str,  # noqa: IDENTITY_SSOT
    include_last_payload: bool = False,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> Any:
    """預先檢視指定 agent 「組裝完成的系統提示」（ADR-0133：跑真實 pipeline、停在模型前）。

    僅限 Owner／Debugger／人類 admin 存取（與 ``_require_debug_role`` 三元組對齊；admin
    曾在 IDOR 修復時涵蓋、重寫後遺失）。拒絕一律 403 fail-loud——早年回 200＋空 blocks
    會把權限問題偽裝成「組裝壞掉」（前端只見空白、無從報錯）。``include_last_payload``
    → 併回最近一筆實際送出的 payload（static 預覽 vs 實際送出對照）。
    """
    if not identity or not identity.is_privileged():
        agent_label = identity.agent_id if identity else "Anonymous"
        role_label = identity.display_role if identity else "None"
        logger.warning(
            f"🚫 Unauthorized context-snapshot access by {agent_label} (Role: {role_label})"
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    target = AgentIdentity(agent_id=agent_id, agent_role="Owner")
    # IDOR fix: the role gate alone let any Owner read ANY agent's snapshot. Scope to
    # ownership (admin/Debugger bypass inside is_owner); cross-owner → 403, no leak.
    # Self-access (caller IS the agent) is always allowed (token-derived agent_id).
    if identity.agent_id != agent_id and not resource_manager.is_owner(target, identity):
        logger.warning(
            "🚫 Cross-owner context-snapshot denied: %s → %s", identity.agent_id, agent_id
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 以目標 agent 的**真實身分**（真實角色→真實工具面）組裝；base_ctx 取自 app.state
    # （memory/MCP 工具、降級旗標皆在）；harness 內不打模型、純讀。
    snapshot_identity = identity_service.get_identity(agent_id) or target
    base_ctx = dict(getattr(request.app.state, "context", {}) or {})
    last_payload = _read_last_payload(agent_id) if include_last_payload else None
    snapshot = await context_snapshot_service.get_context_snapshot(
        snapshot_identity, base_ctx, last_payload=last_payload
    )
    response = JSONResponse(content=snapshot)

    new_token = getattr(request.state, "new_auth_token", None)
    if new_token:
        response.headers["X-New-Auth-Token"] = new_token

    return response


@router.get("/context-snapshot/export")
async def export_context_snapshot(
    request: Request,
    agent_id: str,  # noqa: IDENTITY_SSOT
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> PlainTextResponse:
    """輸出指定 agent 的 context 快照為 Markdown 檔案，並回傳其內容。

    匯出路徑：data/snapshots/{agent_id}.md。
    僅限 Owner／Debugger／人類 admin 存取（與讀取端點同閘）。
    """
    if not identity or not identity.is_privileged():
        agent_label = identity.agent_id if identity else "Anonymous"
        role_label = identity.display_role if identity else "None"
        logger.warning(
            f"🚫 Unauthorized context-snapshot/export access by "
            f"{agent_label} (Role: {role_label})"
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    target = AgentIdentity(agent_id=agent_id, agent_role="Owner")
    # IDOR fix: same ownership scoping as the read endpoint — an Owner must not be
    # able to export (write to disk) another user's agent context. 403 on cross-owner.
    # Self-access (caller IS the agent) is always allowed (token-derived agent_id).
    if identity.agent_id != agent_id and not resource_manager.is_owner(target, identity):
        logger.warning(
            "🚫 Cross-owner context-snapshot export denied: %s → %s",
            identity.agent_id,
            agent_id,
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    snapshot_identity = identity_service.get_identity(agent_id) or target
    base_ctx = dict(getattr(request.app.state, "context", {}) or {})
    snapshot = await context_snapshot_service.get_context_snapshot(snapshot_identity, base_ctx)
    md_content = context_snapshot_service.render_snapshot_markdown(snapshot)

    export_path = Path(resource_manager.get_base_path()) / "snapshots" / f"{agent_id}.md"
    resource_manager.write_file_text(export_path, md_content)
    logger.info(f"📄 Context snapshot exported: {export_path}")

    response = PlainTextResponse(content=md_content, media_type="text/markdown")

    new_token = getattr(request.state, "new_auth_token", None)
    if new_token:
        response.headers["X-New-Auth-Token"] = new_token

    return response


class ToolExecuteRequest(BaseModel):
    """除錯工具執行請求模型。"""

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    # Identity SSOT。validate_default=True 確保字串預設值也轉為 AgentIdentity 物件。
    agent_id: AgentIdentity = Field(default="DebugAgent", validate_default=True)
    # H1（hitl-unification）：監督者可顯式指定本次執行的 HITL 等級（端點已
    # owner/admin/Debugger 閘後）；未帶＝fail-safe Manual（唯讀豁免、其餘 fail-closed）。
    auth_level: AuthMode | None = None

@router.post("/execute")
async def debug_execute_tool(
    payload: ToolExecuteRequest,
    identity: AgentIdentity = Depends(get_verified_identity)  # noqa: B008
) -> Any:
    """真實執行工具指令 (僅限 Debugger/Owner)。"""
    if not identity or not identity.is_privileged():
        agent_label = identity.agent_id if identity else "Anonymous"
        logger.warning(f"🚫 Unauthorized tool execution attempt by {agent_label}")
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # P14 fix: Debugger role may only execute tools in its Gate-1 allowed_tools list.
    if identity.agent_role == "Debugger":
        from src.services.permission_service import permission_service  # noqa: LOCAL_IMPORT_BAN

        # DA20 fix: use the SSOT Gate-1 API. The old `get_capabilities_by_id(...)`
        # used to return a list[str], so `.get("allowed_tools")` raised AttributeError →
        # every Debugger tool execution 500'd. get_allowed_tools/is_tool_allowed now share
        # the same NameGrant as request schema filtering. An empty grant means no extra
        # Debugger restriction here (preserves the original endpoint-specific intent).
        allowed_tools = permission_service.get_allowed_tools(identity)
        if not allowed_tools.is_empty and not permission_service.is_tool_allowed(
            payload.tool_name, allowed_tools
        ):
            logger.warning(
                "🚫 P14: Debugger %s attempted forbidden tool %s",
                identity.agent_id,
                payload.tool_name,
            )
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 從全域 app 獲取 context (SSOT)
    from src.main import app  # noqa: LOCAL_IMPORT_BAN
    context = app.state.context

    # 初始化執行器 (SSOT: 使用核心註冊表)
    executor = ToolExecutor(
        protocol_manager=context.get("protocol_manager"),
        tool_registry=context.get("tool_registry", {}),
        memory_tool_registry=context.get("memory_tool_registry", {}),
        plugin_registry=context.get("plugin_registry", {}),
        # 人按的除錯執行（已過特權閘）：不核對這一輪列給模型的工具
        enforce_advertised=False,
    )

    # 構建執行上下文
    execution_ctx = {
        "agent_id": payload.agent_id,
        "session_id": payload.session_id,
        "identity": identity,
        # H1（hitl-unification）：尊重 payload.auth_level（欄位本就存在、原被硬編
        # "Manual" 無視——舊註解「手動模式以獲取最大權限」只在 CONFIRM=照做的
        # fail-open 世界成立）。本端點已 owner/admin/Debugger 閘後，監督者自行指定；
        # 未帶則 fail-safe Manual（唯讀豁免、其餘 fail-closed）。
        "auth_level": payload.auth_level or "Manual",
        "model": "DebugAgent"
    }

    request_id = f"debug_{uuid.uuid4().hex[:8]}"

    result_text = await executor.execute_to_queue(
        name=payload.tool_name,
        args=payload.args,
        request_id=request_id,
        ctx=execution_ctx,
        queue=asyncio.Queue() # 臨時 Queue 用於接收日誌，這裡僅取最終結果
    )

    return {
        "status": "success",
        "tool": payload.tool_name,
        "result": result_text,
        "request_id": request_id
    }


# ── 模型計分卡（軌B；手動觸發、會花費真實 API 費用、不進 CI）──
# job 暫存於記憶體（單機、單輪手動觸發足矣）。
# _scorecard_runner=None＝正式環境用 make_real_runner（生產同源）；
# 測試以 monkeypatch 注入假 runner。
_scorecard_jobs: dict[str, dict[str, Any]] = {}
_scorecard_runner: Any = None


class ScorecardRunRequest(BaseModel):
    """計分卡執行請求。budget_usd 為單輪累計預算上限（回合間檢查）。"""

    model: str
    budget_usd: float = Field(default=0.50, gt=0)
    scenario_ids: list[str] | None = None
    # 乾淨模式（無痕／ADR-0086）：白紙狀態跑、不讀記憶不殘留——供「原始 vs 累積」對照。
    clean_slate: bool = False


_require_debug_role = require_privileged


# 破壞性重置專用閘：Owner/admin（**排除 Debugger**），比 _require_debug_role 嚴。
#
# reset 不僅清資料、還會觸發全系統備份（捕捉所有 agent 的 soul/vault/.env），故對齊
# admin-gated 的 /system/backup/run。Debugger 是 LDH 對「無 token 同網段客戶端」自動發放
# 的角色（DebugAgent→Debugger），排除它即擋掉無認證 LAN 觸發此破壞性端點的路徑
# （ADR-0104 對抗式複審 R2/R8）。計分卡 run/stream 仍用 _require_debug_role、不受影響。
_require_reset_role = require_owner_or_admin


class DebugResetRequest(BaseModel):
    """DebugAgent 破壞性重置請求（雙重確認，防誤觸）。

    `confirm_agent_id` 為字面字串確認（須恰為 "DebugAgent"），非身分傳遞——故不適用
    IDENTITY_SSOT（其僅禁恰名 agent_id；此處 confirm_ 前綴且僅作確認比對）。
    """

    confirm: bool = False
    confirm_agent_id: str = ""


@router.post("/reset")
async def debug_reset(
    payload: DebugResetRequest,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> Any:
    """破壞性清除 DebugAgent 全部累積資料（保留身分/註冊），**備份優先**。

    僅限 **Owner/admin**（排除 Debugger，見 ``_require_reset_role``）；需 ``confirm=true`` 且
    ``confirm_agent_id`` 字面為 "DebugAgent"（雙重確認）——任一不符即 ``VALIDATION_ERROR``
    （一筆都不刪）。實際刪除前會先跑全系統備份，備份失敗即中止。安全閘 ``assert_resettable``
    硬鎖 DebugAgent，真實 agent（Jin/Yan/Code…）永遠拒絕（縱有人改前端送入別的 id 也擋下）。
    """
    _require_reset_role(identity)
    if not payload.confirm or payload.confirm_agent_id != "DebugAgent":
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override="重置需 confirm=true 且 confirm_agent_id 字面為 'DebugAgent'。",
        )
    if restore_in_progress():  # 還原會整個蓋掉資料，和一般重置一樣不能同時做
        raise ToukaException(ErrorCode.SYSTEM_RESTORE_IN_PROGRESS)
    logger.warning("⚠️ DebugAgent 破壞性重置由 %s 觸發（備份優先）", identity.agent_id)
    report = await reset_debug_agent_data()
    return report.to_dict()


@router.post("/scorecard/run")
async def scorecard_run(
    payload: ScorecardRunRequest,
    request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> Any:
    """啟動一輪模型計分（背景執行，回 job_id）。⚠️ 真實模型會花費 API 費用。"""
    _require_debug_role(identity)
    # 計分卡在 pipeline 裡以 DebugAgent 跑；model 是靈魂時先驗真正的呼叫者（ADR-0245 修補）
    target = agent_service.identify_model_target(payload.model)
    if target and target["is_toukasoul"]:
        require_soul_access(identity, AgentIdentity(agent_id=target["agent_id"]))
    # 計分卡與生產同源：base context 取自 app.state.context（記憶／MCP 工具、降級旗標皆在）；
    # 身分用 identity_service SSOT 解析（DebugAgent→Debugger，資料仍歸 DebugAgent）。
    # 測試以 monkeypatch 注入假 runner → 短路、跳過真實組裝（零 API）。
    runner = _scorecard_runner or make_real_runner(
        request.app.state.context.copy(),
        identity_service.get_identity("DebugAgent"),
        clean_slate=payload.clean_slate,
    )
    selected = scorecard_scenarios.select_scenarios(payload.scenario_ids)
    job_id = f"sc_{uuid.uuid4().hex[:8]}"
    queue: asyncio.Queue = asyncio.Queue()
    _scorecard_jobs[job_id] = {"status": "running", "queue": queue, "scorecard": None}

    async def _run_job() -> None:
        try:
            card = await run_scorecard(
                payload.model,
                payload.budget_usd,
                selected,
                runner,
                on_progress=lambda ev: queue.put_nowait(ev),
            )
            _scorecard_jobs[job_id]["scorecard"] = card
            _scorecard_jobs[job_id]["status"] = "completed"
            queue.put_nowait({"type": "result", "scorecard": scorecard_formatter.to_json(card)})
        except Exception as exc:  # 背景任務失敗回報前端，不外洩堆疊
            logger.exception("scorecard job %s failed", job_id)
            _scorecard_jobs[job_id]["status"] = "failed"
            queue.put_nowait({"type": "error", "message": str(exc)})
        finally:
            queue.put_nowait(None)

    asyncio.create_task(_run_job())
    return {"job_id": job_id, "status": "running", "scenarios": [s.id for s in selected]}


@router.get("/scorecard/stream/{job_id}")
async def scorecard_stream(
    job_id: str,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> Any:
    """SSE 串流：即時進度＋最終成績單（沿用 terminal.py 樣式）。"""
    _require_debug_role(identity)
    job = _scorecard_jobs.get(job_id)
    if not job:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR, detail_override=f"未知的計分卡 job：{job_id}"
        )
    queue: asyncio.Queue = job["queue"]

    async def _gen() -> AsyncIterator[str]:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
