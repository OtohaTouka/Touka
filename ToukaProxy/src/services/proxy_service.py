"""代理服務模組，負責處理請求管道並轉發至對應的模型服務。"""

import json
import logging
from contextlib import nullcontext
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from src.core.exceptions import ErrorCode, ToukaException
from src.middleware.layers.hooks import HookLayer
from src.models.identity import AgentIdentity
from src.services.dispatchers.route_family import same_route_family
from src.services.resource_manager import resource_manager
from src.services.stream_buffer_service import stream_buffer_service

from ..middleware.layers.context import ContextLayer
from ..middleware.layers.custom_model_layer import CustomModelLayer
from ..middleware.layers.monitor import MonitorLayer
from ..middleware.layers.native_model_layer import NativeModelLayer
from ..middleware.layers.skill import SkillLayer
from ..middleware.layers.soul import SoulLayer
from ..middleware.layers.tool import ToolLayer
from ..middleware.pipeline import ProxyPipeline
from ..models.context import PipelineContext
from ..services.dispatcher import dispatch_request
from ..utils.brain_persistence import (
    get_last_working_brain,
    record_working_brain,
)
from ..utils.time_utils import get_current_epoch, get_current_taipei_iso

logger = logging.getLogger(__name__)

# Initialize Global Pipeline
pipeline = ProxyPipeline([
    MonitorLayer(),
    HookLayer(),        # ADR-0292：UserPromptSubmit hook（只在 ctx.run_hooks 時）
    ContextLayer(),
    NativeModelLayer(),  # ADR-0111：原生模型預設參數（在 custom/soul 之前→被其覆蓋）
    CustomModelLayer(), # Level 2 自定義模型注入
    SoulLayer(),        # Level 1 靈魂注入
    SkillLayer(),
    ToolLayer()
])

# Note: get_home_messages and get_project_context have been moved to src/utils/async_db.py

def log_payload(request_id: str, identity: AgentIdentity, model: str, payload: Any) -> None:
    """紀錄請求 Payload 至旋轉日誌檔案 (Identity SSOT)."""
    log_path = Path(resource_manager.get_base_path()) / "logs" / "proxy_payload.log"
    resource_manager.make_dir(log_path.parent)

    payload_logger = logging.getLogger("payload_logger")
    if not payload_logger.handlers:
        payload_logger.setLevel(logging.INFO)
        payload_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        payload_logger.addHandler(payload_handler)
        payload_logger.propagate = False

    log_entry = {
        "timestamp": get_current_taipei_iso(),
        "request_id": request_id,
        "agent_id": identity.agent_id,
        "model": model,
        "payload": payload,
    }
    payload_logger.info(json.dumps(log_entry, ensure_ascii=False))


async def process_proxy_request(
    request: Any, ctx: PipelineContext, is_anthropic: bool = True
) -> Any:
    """處理請求管道：採用中介軟體式架構 (SOLID)。"""
    # UI 請求在開始串流前先登記請求者，hook 執行中也停得下來；停止標在這一則請求上，
    # HookLayer 與開始串流前各看一次（ADR-0292）
    requester = ctx.get("ui_requester")
    session_id = getattr(request, "session_id", None)
    waiting = (
        stream_buffer_service.pending_request(session_id, requester)
        if isinstance(requester, AgentIdentity) and isinstance(session_id, str) and session_id
        else nullcontext()
    )
    with waiting as pending:
        ctx["ui_pending"] = pending
        return await _run_request(request, ctx, is_anthropic)


async def _run_request(request: Any, ctx: PipelineContext, is_anthropic: bool) -> Any:
    # 真的請求才跑 UserPromptSubmit hook（上下文預覽直接呼叫 pipeline，不經這裡；ADR-0292）
    ctx["run_hooks"] = True
    # 執行處理管道 (Pipeline執行所有的 Middleware Layers)
    await pipeline.execute(request, ctx)
    
    t0 = ctx.get("t0", get_current_epoch())
    request_id = ctx.get("request_id", "unknown")
    identity: AgentIdentity | None = ctx.get("identity")
    is_toukasoul = ctx.get("is_toukasoul", False)
    
    # 指派處理後的 model_name (可能已被 SoulLayer 更改)
    model_name = getattr(request, "model", "Unknown")
    
    # 紀錄 Payload（ADR-0086：無痕模式不寫 payload 日誌——零殘留）
    if not ctx.get("incognito"):
        payload_data = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else str(request)
        log_payload(request_id, identity or AgentIdentity(agent_id="System"), model_name, payload_data)

    if is_anthropic:
        ctx.update({"is_native": True})

    try:
        response = await dispatch_request(
            model_name, request, identity or AgentIdentity(agent_id="System"), request_id, t0, ctx
        )
        if is_toukasoul and identity and identity.agent_id.lower() != "system":
            record_working_brain(identity, model_name)
        return response
    except ValueError as ve:
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(ve)) from ve
    except Exception as e:
        logger.exception(f"⚠️ Primary brain ({model_name}) failed")
        if is_toukasoul and identity:
            fallback_model = get_last_working_brain(identity)
            if fallback_model and fallback_model.lower() != model_name.lower():
                if not same_route_family(model_name, fallback_model):
                    logger.warning(
                        "⛔ 跨廠商備援已關閉：主要模型 %s 失敗，不改用其他家族的 %s",
                        model_name,
                        fallback_model,
                    )
                    raise
                logger.info(
                    f"🔄 Attempting fallback to reliable brain: {fallback_model}"
                )
                request.model = fallback_model
                try:
                    return await dispatch_request(
                        fallback_model, request, identity or AgentIdentity(agent_id="System"), request_id, t0, ctx
                    )
                except Exception as fe:
                    logger.error(
                        f"❌ Fallback brain ({fallback_model}) also failed: {fe}"
                    )
        raise e