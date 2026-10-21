"""內部 Dispatch 回呼端點（ToukaDispatch → ToukaProxy，ADR-0051）.

架構位置：ToukaProxy 內部服務端點，僅接受 localhost 來源的 ToukaDispatch 呼叫。
認證方案（Option A）：X-Callback-Token 本身即為授權憑證，由 ToukaProxy 在
initialize_mission(notify_on_complete=True) 時生成，透過 MissionState.metadata 傳遞。

安全邊界：
  - localhost 來源驗證（防止外部直接呼叫）
  - callback_token 存在驗證（防止 replay 與 token 猜測）
  - Guard #22 合規：使用 Depends(verify_dispatch_callback)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request  # noqa: TC002
from memory_server.utils.content_guard import sanitize_memory_content
from pydantic import BaseModel

from src.core.exceptions import ErrorCode, ToukaException
from src.services.dispatch_callback_store import get_callback_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class DispatchCallbackPayload(BaseModel):
    """ToukaDispatch 任務完成通知的 payload 結構。"""

    task_id: str
    status: str  # "DONE" | "FAILED"
    result_summary: str = ""


async def verify_dispatch_callback(
    x_callback_token: str = Header(alias="X-Callback-Token"),  # noqa: B008
    request: Request = ...,  # type: ignore[assignment]
) -> str:
    """驗證 localhost 來源 + callback_token（Guard #22 合規）.

    雙層防護：
    1. IP 來源限制：僅接受 127.0.0.1 / ::1 / localhost（防止外部觸發）
    2. Token 存在驗證：token 必須在 callback store 中已注冊（防止偽造）

    Args:
        x_callback_token: 請求標頭 X-Callback-Token 的值。
        request: FastAPI 請求物件（取得 client host）。

    Returns:
        已驗證的 callback_token 字串。

    Raises:
        ToukaException AUTH_PERMISSION_DENIED (403)：非 localhost 來源。
        ToukaException AUTH_INVALID_TOKEN (401)：token 未注冊或已失效。
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "Dispatch callback rejected: non-localhost origin host=%s", client_host
        )
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    if not get_callback_store().is_registered(x_callback_token):
        logger.warning(
            "Dispatch callback rejected: unregistered token host=%s token=%s…",
            client_host,
            x_callback_token[:8] if x_callback_token else "empty",
        )
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)

    return x_callback_token


@router.post("/dispatch/callback")
async def receive_dispatch_callback(
    payload: DispatchCallbackPayload,
    callback_token: str = Depends(verify_dispatch_callback),  # noqa: B008
) -> dict[str, str]:
    """接收 ToukaDispatch 的任務完成通知，解除 Alice 的等待.

    Args:
        payload: 任務狀態與結果摘要。
        callback_token: 已由 verify_dispatch_callback 驗證的 token。

    Returns:
        {"status": "delivered"} 或 HTTP 404（token 在驗證後消失，屬競態，不正常）。
    """
    # #11 fix: external-AI result text must pass the H3 content guard before it
    # is delivered into the waiting agent's LLM context (defense-in-depth — the
    # callback is a localhost+token boundary that could be POSTed directly).
    safe_summary, flagged = sanitize_memory_content(payload.result_summary)
    if flagged:
        logger.warning(
            "⚠️ [DispatchCallback] result_summary sanitized by ContentGuard (task=%s).",
            payload.task_id,
        )
    result = {
        "task_id": payload.task_id,
        "status": payload.status,
        "result_summary": safe_summary,
    }
    delivered = get_callback_store().deliver_result(callback_token, result)
    if not delivered:
        logger.error(
            "Dispatch callback delivery failed after auth: token=%s…",
            callback_token[:8],
        )
        raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND)

    logger.info(
        "Dispatch callback delivered: task=%s status=%s", payload.task_id, payload.status
    )
    return {"status": "delivered"}
