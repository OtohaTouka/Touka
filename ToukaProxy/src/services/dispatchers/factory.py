"""Dispatcher factory：依模型名選擇對應的串流處理器（Claude／Gemini／OpenAI 相容／Human）。

供應商金鑰一律經 Provider Registry 即時解析（SSOT），不做進程啟動時的金鑰快照（root-cause-B）。
"""

import logging
from collections.abc import AsyncGenerator
from typing import Any

from src.adapters.schemas import OpenAIChatRequest
from src.config import strip_anthropic_sdk_env_overrides
from src.handlers.claude_handler import handle_claude_request
from src.handlers.gemini_handler import handle_gemini_request
from src.handlers.openai_handler import handle_openai_request, handle_responses_request
from src.models.identity import AgentIdentity
from src.services.dispatchers.route_family import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    HUMAN_ROUTE,
    RESPONSES_ROUTE,
    resolve_model_route,
)
from src.services.dispatchers.utils import convert_to_openai, get_provider_info
from src.services.model_manager import model_manager
from src.services.models.human import HumanModelHandler

logger = logging.getLogger(__name__)


async def get_stream_generator(
    model_name: str,
    request: Any,
    identity: AgentIdentity,
    request_id: str,
    start_time: float,
    context: dict,
    session_id: str | None = None,
) -> AsyncGenerator[str]:
    """Factory to select the appropriate stream handler based on the model name."""
    context["identity"] = identity
    # 抓取 Context 資訊
    system_degraded = context.get("system_degraded", False)
    degraded_reason = context.get("degraded_reason", "")

    # 路由（Human → 有效自訂連線 → claude／gemini → registry）與 complete_once 共用；
    # 傳本模組的 model_manager／get_provider_info（既有 patch 點不變）。
    route = resolve_model_route(
        model_name, manager=model_manager, provider_info=get_provider_info
    )
    if route.kind == HUMAN_ROUTE:
        handler = HumanModelHandler()
        return handler.generate_response(request, context)

    if route.kind == ANTHROPIC_FAMILY:
        # root-cause-B（免重啟）：啟動時的 anthropic_client 以當時金鑰快照建立；此處改以
        # registry 即時金鑰（SSOT）套用之。with_options 回傳共用連線池的副本，不污染原 client；
        # 僅在有即時金鑰且與現值不同時替換（無金鑰＝維持現狀，不以空金鑰覆寫）。
        anthropic_client = context.get("anthropic_client")
        live_key = route.api_key
        if (
            anthropic_client is not None
            and live_key
            and getattr(anthropic_client, "api_key", None) != live_key
        ):
            strip_anthropic_sdk_env_overrides()  # 副本建構時 SDK 會重讀環境（ADR-0247）
            context["anthropic_client"] = anthropic_client.with_options(api_key=live_key)
        context["model"] = model_name  # 與另外兩家一致：子代理可沿用這一輪實際的模型（附註 35）
        return handle_claude_request(request, context)

    active_req = request
    if hasattr(request, "messages") and not isinstance(request, OpenAIChatRequest):
        active_req = convert_to_openai(request, system_degraded, degraded_reason)
    run_info = {"request_id": request_id, "t0": start_time, "session_id": session_id}

    if route.kind == GEMINI_FAMILY:
        context.update({**run_info, "gemini_api_key": route.api_key, "model": model_name})
        return handle_gemini_request(active_req, context)

    if route.kind == RESPONSES_ROUTE:  # 官方 OpenAI、規則表標 Responses（base URL 是常數）
        context.update({**run_info, "api_key": route.api_key, "base_url": route.url,
                        "provider": route.provider, "model": model_name})
        return handle_responses_request(active_req, context)

    # 有效自訂連線與一般 OpenAI 相容廠商（連線資訊皆在 route）
    context.update(
        {
            **run_info,
            "api_key": route.api_key,
            "url": route.url,
            "provider": route.provider,
            "custom_connection": route.custom,
            "model": model_name,
        }
    )
    return handle_openai_request(active_req, context)
