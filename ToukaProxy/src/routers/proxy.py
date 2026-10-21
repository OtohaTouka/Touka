"""Proxy router for handling model requests."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request  # noqa: TC002

from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest  # noqa: TC001
from src.core.exceptions import ErrorCode, ToukaException
from src.handlers.adapters.base import THINKING_PROFILE_KEY, ThinkingProfile
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.session_persistence import FailedResult
from src.routers.models import list_all_models
from src.routers.ui_sessions import _can_access_session
from src.services.authorization_gateway import require_authenticated
from src.services.completion import PURPOSE_INLINE_COMPLETION, thinking_profile_for
from src.services.model_manager import model_manager
from src.services.proxy_service import process_proxy_request
from src.utils.async_db import SessionPersistenceError, session_service
from src.utils.auth_mode import request_is_incognito

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["proxy"])

# ADR-0245：聊天端點必須有已驗證身分，沒登入回 401。首裝前的本機領卡（LDH）
# 仍在 get_verified_identity 內處理，照舊發 DebugAgent 卡。
_require_authenticated = require_authenticated

# r55：客戶端用途標頭只能讓 Claude 5.5 少想（比照禁令 16：標頭不是身分，也不碰路由、工具、
# 權限、花費上限）。只認這張表的值（ASCII、不分大小寫、去頭尾空白），其餘一律忽略。
PURPOSE_HEADER = "x-touka-purpose"
_CLIENT_PURPOSES = {"inline-completion": PURPOSE_INLINE_COMPLETION}


def _client_thinking_profile(headers: dict[str, str]) -> ThinkingProfile | None:
    """標頭 → 思考強度；不在白名單（含偽造的用途名、變形、非 ASCII）＝None，不報錯。"""
    raw = headers.get(PURPOSE_HEADER, "")
    purpose = _CLIENT_PURPOSES.get(raw.strip(" \t").lower()) if raw.isascii() else None
    return thinking_profile_for(purpose) if purpose else None


def _apply_identity_to_context(
    ctx: dict[str, Any],
    identity: AgentIdentity,
    *,
    human_principal_id: str | None = None,
) -> None:
    """Apply a verified AgentIdentity to the pipeline context (SSOT helper)."""
    ctx["agent_id"] = identity.agent_id
    ctx["role"] = identity.display_role
    ctx["capabilities"] = identity.capabilities
    ctx["identity"] = identity
    ctx["human_principal_id"] = human_principal_id


def _apply_chat_request_context(
    ctx: PipelineContext,
    request: OpenAIChatRequest | AnthropicRequest,
    fast_request: Request,
    identity: AgentIdentity,
) -> None:
    """兩個聊天端點共用：把已驗證身分、標頭與 UI 旗標寫進 pipeline context。"""
    _apply_identity_to_context(
        ctx,
        identity,
        human_principal_id=getattr(
            fast_request.state,
            "human_principal_id",
            identity.agent_id if identity.is_human else None,
        ),
    )
    ctx["headers"] = {k.lower(): v for k, v in fast_request.headers.items()}
    ctx[THINKING_PROFILE_KEY] = _client_thinking_profile(ctx["headers"])  # 只有 ClaudeAdapter 讀
    # ADR-0081：UI 對話端點顯式啟用可恢復串流（heartbeat／內部呼叫不設此旗標）
    ctx["resumable_stream"] = True
    # ADR-0086：無痕模式旗標（由 request body 帶入，貫穿管線觸發零殘留閘門）；兩欄任一即無痕
    ctx["incognito"] = request_is_incognito(request)
    # ADR-0087：深入研究旗標（由 request body 帶入，SkillLayer 據此注入研究協調 Skill）
    ctx["deep_research"] = request.deep_research
    # ADR-0161：MCP 動態掛載旗標（輸入列 toggle 逐則帶入，tool.py 據此啟用漸進揭露）
    ctx["mcp_dispatcher"] = request.mcp_dispatcher


async def _authorize_ui_persistence(
    request: OpenAIChatRequest | AnthropicRequest,
    ctx: PipelineContext,
    identity: AgentIdentity,
) -> None:
    """在 pipeline 換身分之前驗證 owner 並捕捉 requester；版本由 dispatcher 再驗。"""
    admission = request.ui_persistence
    if admission is None:
        return
    if admission.mode == "persistent":
        if not identity.is_human:
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        try:
            snapshot = await session_service.read_persistence_snapshot(request.session_id, identity)
        except SessionPersistenceError:
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None
        session = snapshot.session
        if session is not None and not _can_access_session(
            identity,
            session.get("agent_id"),
            session.get("owner_user_id"),
        ):
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        if session is None or snapshot.revision != admission.revision:
            result = FailedResult(
                status="failed", error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id
            )
            raise HTTPException(  # noqa: ERROR_SSOT_VIOLATION — 固定 typed precommit 衝突結果，不反射輸入
                status_code=409, detail={"persistence": result.model_dump()}
            )
        # 記憶來源：這輪已保存的 user 節點（dispatcher 開始生成前會再驗它已保存）；無痕不記
        ctx["source_message_id"] = admission.user_parent_id
    ctx["ui_requester"] = identity
    ctx["ui_authorized"] = True


@router.post("/messages")
async def proxy_anthropic_messages(
    request: AnthropicRequest,
    fast_request: Request,
    identity: Annotated[AgentIdentity, Depends(_require_authenticated)],
) -> Any:
    """Proxy Anthropic messages request."""
    model_manager.mark_model_seen(AgentIdentity(agent_id=request.model))

    ctx_dict = fast_request.app.state.context.copy()
    ctx = PipelineContext(**ctx_dict)
    _apply_chat_request_context(ctx, request, fast_request, identity)

    await _authorize_ui_persistence(request, ctx, identity)
    response = await process_proxy_request(request, ctx, is_anthropic=True)

    # 自動補發 Token 的攔截：如果是從私有網路降級而來，我們在 Response Header 回傳新 Token
    new_token = getattr(fast_request.state, "new_auth_token", None)
    if new_token and hasattr(response, "headers"):
        response.headers["X-New-Auth-Token"] = new_token

    return response


@router.post("/chat/completions")
async def proxy_chat_completions(
    request: OpenAIChatRequest,
    fast_request: Request,
    identity: Annotated[AgentIdentity, Depends(_require_authenticated)],
) -> Any:
    """Proxy OpenAI chat completions request."""
    model_manager.mark_model_seen(AgentIdentity(agent_id=request.model))

    ctx_dict = fast_request.app.state.context.copy()
    ctx = PipelineContext(**ctx_dict)
    _apply_chat_request_context(ctx, request, fast_request, identity)
    await _authorize_ui_persistence(request, ctx, identity)
    return await process_proxy_request(request, ctx, is_anthropic=False)


@router.get("/models")
async def proxy_models_list(_fast_request: Request) -> Any:
    """OpenAI compatibility endpoint for listing models.

    Mounted at /v1/models.
    """
    return await list_all_models()
