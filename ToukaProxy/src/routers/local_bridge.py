"""本機小幫手的端點（ADR-0312、ADR-0320、ADR-0330）.

- 配對、解除、狀態：只給人類管理員（admin 代打不算）。
- 請小幫手開放資料夾：只給配對的人本人，一定要帶信任的 Origin（ADR-0320）。
- 看開放名單（含完整路徑）：只給配對的人本人（ADR-0330）。
- 輪詢、確認、回報結果、回報開放名單：只給帶權杖的小幫手。
  權杖只代表這條通道，不會變成任何身分（禁令 16）。
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Request, Response

from src.config import settings
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.local_bridge import (
    MAX_JOB_RESULT_BYTES,
    PROTOCOL_VERSION,
    LocalFolderList,
    LocalFolderReport,
    LocalHelperStatus,
    LocalJobAck,
    LocalJobResult,
    LocalOpenRequest,
    LocalOpenResult,
    LocalPairingCreated,
    LocalPairingRecord,
    LocalPollRequest,
    LocalPollResponse,
)
from src.services.authorization_gateway import require_admin_human
from src.services.local_bridge import (
    ACTION_REQUEST_OPEN,
    CODE_NEEDS_NEW_PAIRING,
    POLL_HOLD_SECONDS,
    get_local_bridge,
    host_mount_roots,
    open_request_signer,
    open_result_from,
)
from src.services.local_bridge_pairing import get_local_pairing_store
from src.utils.csrf_guard import require_trusted_origin, verify_origin
from src.utils.local_bridge_signing import decision_key_from_hex

router = APIRouter(prefix="/local-bridge", tags=["local-bridge"])

_require_admin_human = require_admin_human
# 開放名單有完整路徑（含使用者名稱），瀏覽器與中間的快取都不留（ADR-0330）
_NO_STORE: Final = "no-store"


def _pairing_of(admin: AgentIdentity) -> LocalPairingRecord:
    """配對紀錄，而且這個人就是產生配對碼的那位（ADR-0312 決定 5）。

    人和鑰匙只讀這一次，不會一個來自舊配對、一個來自新配對。
    """
    record = get_local_pairing_store().current()
    if record is None:
        raise ToukaException(ErrorCode.LOCAL_HELPER_NOT_PAIRED)
    if record.human_id != admin.agent_id:
        raise ToukaException(ErrorCode.LOCAL_HELPER_NOT_PAIRED_HUMAN)
    return record


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
    created = await store.create(admin.agent_id)
    get_local_bridge().invalidate(store.generation())
    return LocalPairingCreated(code=created.code)


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
        version_problem=bridge.version_problem,
    )


@router.get("/folders", response_model=LocalFolderList)
async def list_folders(
    response: Response,
    admin: Annotated[AgentIdentity, Depends(_require_admin_human)],
) -> LocalFolderList:
    """配對的人本人看小幫手開放了哪些資料夾（ADR-0330）。以小幫手主視窗為準。"""
    _pairing_of(admin)
    response.headers["Cache-Control"] = _NO_STORE
    return get_local_bridge().folder_list()


@router.post("/open-requests", response_model=LocalOpenResult)
async def request_open(
    body: LocalOpenRequest,
    request: Request,
    admin: Annotated[AgentIdentity, Depends(_require_admin_human)],
) -> LocalOpenResult:
    """配對的人本人請小幫手開放資料夾（ADR-0320）。小幫手一律跳視窗，人決定或時間到才有結果。"""
    require_trusted_origin(request)
    record = _pairing_of(admin)
    key = decision_key_from_hex(record.decision_key)
    if key is None:
        return LocalOpenResult(status="refused", code=CODE_NEEDS_NEW_PAIRING)
    result = await get_local_bridge().submit(
        ACTION_REQUEST_OPEN,
        {"path": body.path},
        admin.agent_id,
        signer=open_request_signer(key, body.path),
    )
    return open_result_from(result)


@router.post("/poll", response_model=LocalPollResponse)
async def poll(
    req: LocalPollRequest,
    pairing: Annotated[LocalPairingRecord, Depends(_require_local_helper_token)],
) -> LocalPollResponse:
    """小幫手來拿下一件工作。"""
    if req.protocol != PROTOCOL_VERSION:
        # 權杖已經驗過：設定頁才說得出是哪一邊要更新，不會顯示成「沒有開著」（ADR-0330）
        get_local_bridge().note_mismatch(req.protocol)
        raise ToukaException(ErrorCode.LOCAL_HELPER_PROTOCOL_MISMATCH)
    response = await get_local_bridge().poll(
        pairing.generation, req.helper_version, hold=POLL_HOLD_SECONDS
    )
    if response is None:
        raise ToukaException(ErrorCode.LOCAL_HELPER_INVALID_TOKEN)
    return response.model_copy(
        update={
            "host_mount_roots": host_mount_roots(
                settings.HOST_WORKSPACE_ROOT,
                settings.SANCTUARY_DATA_PATH,
                settings.MEMORY_SYSTEM_SRC_DIR,
                settings.HEARTBEAT_CONFIG_DIR,
            )
        }
    )


@router.post("/folder-report", response_model=LocalJobAck)
async def report_folders(
    report: LocalFolderReport,
    pairing: Annotated[LocalPairingRecord, Depends(_require_local_helper_token)],
) -> LocalJobAck:
    """小幫手回報開放名單（ADR-0330）。只放記憶體，內容不寫日誌；較舊的照樣回成功、不存。"""
    if report.protocol != PROTOCOL_VERSION:
        raise ToukaException(ErrorCode.LOCAL_HELPER_PROTOCOL_MISMATCH)
    get_local_bridge().accept_report(pairing.generation, report)
    return LocalJobAck()


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
