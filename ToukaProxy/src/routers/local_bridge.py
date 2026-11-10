"""本機小幫手的端點（ADR-0311）.

- 配對、解除、狀態：只給人類管理員（admin 代打不算）。
- 輪詢、確認、回報結果：只給帶權杖的小幫手。權杖只代表這條通道，不會變成任何身分（禁令 16）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.config import settings
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.local_bridge import (
    MAX_JOB_RESULT_BYTES,
    PROTOCOL_VERSION,
    LocalHelperStatus,
    LocalJobAck,
    LocalJobResult,
    LocalPairingCreated,
    LocalPairingRecord,
    LocalPollRequest,
    LocalPollResponse,
)
from src.services.authorization_gateway import require_admin_human
from src.services.local_bridge import POLL_HOLD_SECONDS, get_local_bridge, host_mount_roots
from src.services.local_bridge_pairing import get_local_pairing_store
from src.utils.csrf_guard import verify_origin

router = APIRouter(prefix="/local-bridge", tags=["local-bridge"])

_require_admin_human = require_admin_human


def _require_local_helper_token(
    x_touka_local_token: Annotated[str, Header()] = "",
) -> LocalPairingRecord:
    """小幫手的權杖閘。"""
    pairing = get_local_pairing_store().authenticate(x_touka_local_token)
    if pairing is None:
        raise ToukaException(ErrorCode.LOCAL_HELPER_INVALID_TOKEN)
    return pairing


@router.post("/pairing", response_model=LocalPairingCreated)
async def create_pairing(
    request: Request,
    admin: Annotated[AgentIdentity, Depends(_require_admin_human)],
) -> LocalPairingCreated:
    """產生配對碼；已經有配對時舊的立刻失效。"""
    verify_origin(request)
    store = get_local_pairing_store()
    code = await store.create(admin.agent_id)
    get_local_bridge().invalidate(store.generation())
    return LocalPairingCreated(code=code)


@router.delete("/pairing", response_model=LocalJobAck)
async def delete_pairing(
    request: Request,
    _admin: Annotated[AgentIdentity, Depends(_require_admin_human)],
) -> LocalJobAck:
    """解除配對，小幫手馬上斷開。"""
    verify_origin(request)
    store = get_local_pairing_store()
    await store.revoke()
    # 等鎖時可能有別的請求改過，這裡讀當下最新的世代號，兩步之間沒有 await
    get_local_bridge().invalidate(store.generation())
    return LocalJobAck()


@router.get("/status", response_model=LocalHelperStatus)
async def helper_status(
    _admin: Annotated[AgentIdentity, Depends(_require_admin_human)],
) -> LocalHelperStatus:
    """設定頁顯示的狀態。"""
    bridge = get_local_bridge()
    return LocalHelperStatus(
        paired=get_local_pairing_store().current() is not None,
        online=bridge.online,
        helper_version=bridge.helper_version,
    )


@router.post("/poll", response_model=LocalPollResponse)
async def poll(
    req: LocalPollRequest,
    pairing: Annotated[LocalPairingRecord, Depends(_require_local_helper_token)],
) -> LocalPollResponse:
    """小幫手來拿下一件工作。"""
    if req.protocol != PROTOCOL_VERSION:
        raise ToukaException(ErrorCode.LOCAL_HELPER_PROTOCOL_MISMATCH)
    response = await get_local_bridge().poll(
        pairing.generation, req.helper_version, hold=POLL_HOLD_SECONDS
    )
    if response is None:
        raise ToukaException(ErrorCode.LOCAL_HELPER_INVALID_TOKEN)
    return response.model_copy(
        update={
            "host_mount_roots": host_mount_roots(
                settings.HOST_WORKSPACE_ROOT, settings.SANCTUARY_DATA_PATH
            )
        }
    )


@router.post("/jobs/{job_id}/confirm", response_model=LocalJobAck)
async def confirm_job(
    job_id: str,
    pairing: Annotated[LocalPairingRecord, Depends(_require_local_helper_token)],
) -> LocalJobAck:
    """人按了核准，問 Touka 還要不要做。不是 200 小幫手就不動手。"""
    if not get_local_bridge().confirm(job_id, pairing.generation):
        raise ToukaException(ErrorCode.LOCAL_HELPER_JOB_GONE)
    return LocalJobAck()


@router.post("/jobs/{job_id}/result", response_model=LocalJobAck)
async def submit_result(
    job_id: str,
    result: LocalJobResult,
    pairing: Annotated[LocalPairingRecord, Depends(_require_local_helper_token)],
) -> LocalJobAck:
    """小幫手回報結果。"""
    if len(result.model_dump_json().encode("utf-8")) > MAX_JOB_RESULT_BYTES:
        raise ToukaException(ErrorCode.LOCAL_HELPER_RESULT_TOO_LARGE)
    if not get_local_bridge().deliver(job_id, pairing.generation, result):
        raise ToukaException(ErrorCode.LOCAL_HELPER_JOB_GONE)
    return LocalJobAck()
