"""Terminal Router — bash 執行審核與 SSE 串流。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request  # noqa: TC002
from fastapi.responses import StreamingResponse

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.terminal import TerminalApprovalRequest  # noqa: TC001
from src.services.identity_router import get_verified_identity
from src.utils.csrf_guard import verify_origin
from src.utils.time_utils import get_current_taipei_iso

if TYPE_CHECKING:
    from asyncio import Queue
    from collections.abc import AsyncGenerator, Awaitable, Callable

router = APIRouter(prefix="/terminal", tags=["terminal"])
logger = logging.getLogger(__name__)

# ── 全局狀態 ──────────────────────────────────────────────────────────────────

# 待審核的危險指令 {id: asyncio.Event}
pending_approvals: dict[str, asyncio.Event] = {}
# 審核結果 {id: "approved" | "rejected"}
approval_results: dict[str, str] = {}
# 審核附帶數據 {id: payload_dict}
approval_payloads: dict[str, Any] = {}
# SSE 訂閱者佇列列表
sse_subscribers: list[Queue] = []
# 執行歷史（最近 200 筆）
terminal_history: list[dict[str, Any]] = []

MAX_HISTORY = 200
APPROVAL_TIMEOUT = 60  # 每次等待秒數
APPROVAL_RETRIES = 3  # 最多等待次數
# M2 fix: cap the number of concurrent pending approvals to prevent memory exhaustion
MAX_PENDING_APPROVALS: int = 50
# N5 fix: asyncio.Lock to prevent TOCTOU race between approve/reject/request_approval
# Python's GIL makes simple dict reads/writes atomic, but check-then-set patterns
# (e.g., `if exec_id not in pending_approvals: ... pending_approvals[exec_id] = ...`)
# can interleave across await points and cause exec_id confusion.
_approval_lock: asyncio.Lock = asyncio.Lock()


def _push_event(event: dict[str, Any]) -> None:
    """廣播事件給所有 SSE 訂閱者，並記錄歷史。"""
    terminal_history.append(event)
    if len(terminal_history) > MAX_HISTORY:
        terminal_history.pop(0)
    for q in sse_subscribers:
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)


async def request_approval(
    exec_id: str,
    identity: AgentIdentity,
    command: str,
    type: str = "bash_approval_request",
    extra_data: dict[str, Any] | None = None,
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """廣播審核請求，等待使用者批准或拒絕。

    M2 fix: rejects immediately when pending_approvals is at MAX_PENDING_APPROVALS
    to prevent unbounded memory growth via approval-request flooding.

    H2（hitl-unification）：可選 ``emit``＝當前聊天 SSE 佇列的 emitter（工具的
    ``_emit_event`` kwarg）——審批請求**同時**推進對話串流，ToukaChat 的審批卡
    因此收得到（其從未訂閱 /terminal/stream）。無任何通道（無訂閱者且無 emit）
    時**快速拒絕**，不再空等 3×60s 逾時（headless fail-closed）。

    回傳 (是否核准, 附帶數據)。
    """
    if not sse_subscribers and emit is None:
        logger.warning(
            "🚫 [Terminal] No approval channel (no ToukaUI subscriber, no chat emitter) "
            "for %s — fast-rejecting (fail-closed).", exec_id,
        )
        return False, {}

    # N5 fix: guard queue check-and-insert under the lock to prevent TOCTOU
    async with _approval_lock:
        if len(pending_approvals) >= MAX_PENDING_APPROVALS:
            logger.warning(
                "🚫 [Terminal] Approval queue full (%d/%d). Rejecting exec_id=%s.",
                len(pending_approvals), MAX_PENDING_APPROVALS, exec_id,
            )
            return False, {}

        event = asyncio.Event()
        pending_approvals[exec_id] = event

    for attempt in range(1, APPROVAL_RETRIES + 1):
        payload = {
            "type": type,
            "id": exec_id,
            "agent_id": identity.agent_id,
            "command": command,
            "attempt": attempt,
            "max_attempts": APPROVAL_RETRIES,
            "timeout": APPROVAL_TIMEOUT,
            "timestamp": get_current_taipei_iso(),
        }
        if extra_data:
            payload.update(extra_data)

        _push_event(payload)
        if emit is not None:
            try:
                await emit(payload)
            except Exception:
                logger.warning("[Terminal] chat-stream emit failed for %s", exec_id)

        try:
            await asyncio.wait_for(asyncio.shield(event.wait()), timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            if attempt < APPROVAL_RETRIES:
                logger.warning(
                    "[Terminal] Approval timeout (attempt %s/%s) for %s",
                    attempt,
                    APPROVAL_RETRIES,
                    exec_id,
                )
                continue
            logger.warning(
                "[Terminal] All approval attempts exhausted for %s, auto-rejecting.", exec_id
            )
            pending_approvals.pop(exec_id, None)
            approval_results.pop(exec_id, None)
            approval_payloads.pop(exec_id, None)
            _push_event(
                {
                    "type": "bash_approval_timeout",
                    "id": exec_id,
                    "agent_id": identity.agent_id,
                    "command": command,
                    "timestamp": get_current_taipei_iso(),
                }
            )
            return False, {}
        else:
            # 事件被設定，檢查結果
            result = approval_results.pop(exec_id, "rejected")
            payload = approval_payloads.pop(exec_id, {})
            pending_approvals.pop(exec_id, None)
            return result == "approved", payload
    return False, {}


def push_bash_start(
    exec_id: str, identity: AgentIdentity, command: str, is_dangerous: bool
) -> None:
    """推送 Bash 執行開始事件。"""
    _push_event(
        {
            "type": "bash_start",
            "id": exec_id,
            "agent_id": identity.agent_id,
            "command": command,
            "is_dangerous": is_dangerous,
            "timestamp": get_current_taipei_iso(),
        }
    )


def push_bash_result(
    exec_id: str,
    identity: AgentIdentity,
    command: str,
    exit_code: int,
    output: str,
    duration_ms: int,
) -> None:
    """推送 Bash 執行結果事件。"""
    _push_event(
        {
            "type": "bash_result",
            "id": exec_id,
            "agent_id": identity.agent_id,
            "command": command,
            "exit_code": exit_code,
            "output": output,
            "duration_ms": duration_ms,
            "timestamp": get_current_taipei_iso(),
        }
    )


# ── API Endpoints ─────────────────────────────────────────────────────────────


@router.get("/stream")
async def terminal_stream(
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: ARG001, B008
) -> StreamingResponse:
    """SSE 串流：推送所有 bash 事件給前端。"""
    queue: Queue = asyncio.Queue(maxsize=100)
    sse_subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[str]:
        # 先推送最近歷史（最多 50 筆）
        for event in terminal_history[-50:]:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    # 心跳
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/approve/{exec_id}")
async def approve_command(
    exec_id: str,
    http_request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
    request: TerminalApprovalRequest | None = None,
) -> dict[str, Any]:
    """批准審核請求，可附帶 Payload (如環境變數)。

    C8 fix: requires a verified identity with Owner/Debugger/admin role.
    M1 fix: validates Origin header to prevent CSRF from untrusted origins.
    """
    verify_origin(http_request)
    if not identity or not identity.is_privileged():
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # N5 fix: serialize state mutation under the lock
    async with _approval_lock:
        if exec_id not in pending_approvals:
            raise ToukaException(ErrorCode.TERMINAL_APPROVAL_NOT_FOUND)

        approval_results[exec_id] = "approved"
        has_payload = False
        if request and request.payload:
            approval_payloads[exec_id] = request.payload
            has_payload = True

        pending_approvals[exec_id].set()
    return {"status": "approved", "id": exec_id, "has_payload": has_payload}


@router.post("/reject/{exec_id}")
async def reject_command(
    exec_id: str,
    http_request: Request,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """拒絕危險指令執行。

    C8 fix: requires a verified identity with Owner/Debugger/admin role.
    M1 fix: validates Origin header to prevent CSRF from untrusted origins.
    N5 fix: serialized under _approval_lock.
    """
    verify_origin(http_request)
    if not identity or not identity.is_privileged():
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    async with _approval_lock:
        if exec_id not in pending_approvals:
            raise ToukaException(ErrorCode.TERMINAL_APPROVAL_NOT_FOUND)
        approval_results[exec_id] = "rejected"
        pending_approvals[exec_id].set()
    return {"status": "rejected", "id": exec_id}


@router.get("/history")
async def get_history(
    limit: int = 100,
    _identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """取得最近執行歷史（需認證）."""
    if not _identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return {"history": terminal_history[-limit:]}