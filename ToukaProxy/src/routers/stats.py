"""統計與指標路由 (Stats Router).

提供系統使用率與舊對話資料庫 (Legacy Vault) 的統計與同步介面。
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request  # noqa: TC002
from memory_server.utils.memory_worker import run_memory_job

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.activity_registry import activity_registry
from src.services.authorization_gateway import can_access_session
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("")
async def get_touka_stats(
    request: Request,
    identity: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, Any]:
    """讀取 usage_audit.csv 與其他統計數據 (Identity SSOT).

    Grandfather Batch-3 fix: added None identity check to prevent anonymous access.
    Previously had Depends but did not check if identity is None.
    """
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 依身分過濾（2026-10-28）：背景紀錄只列 can_access_session 放行的靈魂，
    # 全域用量紀錄與派工伺服器的 pid 只給 admin（含代打）。
    # 這次請求內每個靈魂只判斷一次；對不到靈魂的紀錄只給 admin／Debugger
    visible: dict[str, bool] = {}

    def _can_see(agent: object) -> bool:
        if not isinstance(agent, str) or not agent:
            return can_access_session(identity, None)
        if agent not in visible:
            visible[agent] = can_access_session(identity, agent)
        return visible[agent]

    is_admin = identity.is_admin_authority()
    stats_data = {"total_usage": 0, "usage_history": []}
    stats_path = Path(resource_manager.get_base_path()) / "stats" / "usage_audit.csv"
    if is_admin and stats_path.exists():
        try:
            content = resource_manager.read_file_text(stats_path)
            if content:
                reader = csv.DictReader(io.StringIO(content))
                rows = list(reader)
                total_rows = len(rows)
                stats_data["total_usage"] = total_rows
                # Ensure we only take the last 50 if they exist
                stats_data["usage_history"] = rows[-50:] if total_rows >= 50 else rows
        except Exception:
            logger.exception("Error reading stats")

    # 讀取各 Agent 的 Token 消耗 (動態掃描)
    token_stats = {}
    agent_stats_list = resource_manager.list_all_agent_stats(identity)
    for agent, stats_dir in agent_stats_list:
        tpath = Path(stats_dir) / "token_usage.json"
        try:
            data = resource_manager.read_json(tpath)
            if data is not None:
                token_stats[agent] = data
        except Exception:
            logger.debug("Failed to load token usage for %s", agent)
    stats_data["token_usage"] = token_stats

    # P3：本人今日自主配額狀態（daily_count / daily_max / remaining）
    stats_data["daily_quota"] = autonomy_service.get_daily_quota(identity)

    # P4：背景活動（背景心跳脈衝進度等），供 UI「背景跑中」顯示
    stats_data["background_activity"] = {
        session_id: record
        for session_id, record in activity_registry.snapshot().items()
        if _can_see(record.get("agent") if isinstance(record, dict) else None)
    }

    # P5c：上次未完成的背景任務（當機復原可見化），供 UI「⚠️ 上次有 N 個未完成」通知
    stats_data["interrupted_runs"] = [
        run
        for run in autonomy_service.get_interrupted_runs()
        if _can_see(run.get("agent") if isinstance(run, dict) else None)
    ]

    # ADR-0082：dispatch supervisor 觀測（status/pid/restarts）——dict-fold 免動 openapi
    supervisor = getattr(request.app.state, "dispatch_supervisor", None)
    supervisor_status = (
        supervisor.status()
        if supervisor is not None
        else {"status": "unavailable", "pid": None, "restarts": 0}
    )
    if not is_admin:
        supervisor_status = {**supervisor_status, "pid": None}
    stats_data["dispatch_supervisor"] = supervisor_status

    return stats_data


@router.get("/legacy/{agent_id}")
async def get_legacy_sync_stats(
    agent_id: str,  # noqa: IDENTITY_SSOT — FastAPI URL path param; AgentIdentity.from_str() conversion in body
    identity: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, Any]:
    """獲取指定代理人的舊對話資料庫統計 (Identity SSOT: URL agent_id 為唯一事實來源)."""
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    target = AgentIdentity.from_str(agent_id)
    if not resource_manager.is_owner(target, identity):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    try:
        from memory_server.services.manager import MemoryManager
        manager = MemoryManager()

        sync_service = manager.get_legacy_sync_service()
        # 讀資料庫與檔案雜湊都在記憶工作者上做，不卡住主迴圈（ADR-0288）
        store = await run_memory_job(sync_service.get_store, target)
        stats = await run_memory_job(store.get_stats)
        # 已處理的同步來源數、目錄裡還沒處理的 JSON 數（同步不搬檔，不再數 backup/）
        files = await run_memory_job(sync_service.file_status, target)

        return {
            "status": "success",
            "agent_id": agent_id,
            "message_count": stats["message_count"],
            "session_count": stats["session_count"],
            "archived_files": files["processed"],
            "pending_files": files["pending"],
            "db_path": str(store.db_path) if hasattr(store, "db_path") else "unknown",
        }
    except ToukaException:
        raise
    except Exception:
        logger.exception("Error getting legacy stats for %s", agent_id)
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None


@router.post("/legacy/{agent_id}/sync")
async def trigger_legacy_sync(
    agent_id: str,  # noqa: IDENTITY_SSOT — FastAPI URL path param; AgentIdentity.from_str() conversion in body
    identity: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, Any]:
    """手動觸發舊對話同步 (JSON -> DB) (Identity SSOT: URL agent_id 為唯一事實來源)."""
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    target = AgentIdentity.from_str(agent_id)
    if not resource_manager.is_owner(target, identity):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    try:
        from memory_server.services.manager import MemoryManager
        manager = MemoryManager()

        sync_service = manager.get_legacy_sync_service()
        result = await run_memory_job(sync_service.sync_all_json_to_db, target)
    except ToukaException:
        raise
    except Exception:
        logger.exception("Error syncing legacy for %s", agent_id)
        raise ToukaException(ErrorCode.SYNC_FAILED) from None
    if result.get("errors"):
        # 有檔案寫入失敗：那些 JSON 原地保留，不算同步成功。
        raise ToukaException(
            ErrorCode.SYNC_FAILED, synced=result.get("synced", 0), errors=result["errors"]
        )
    return {"status": "success", "result": result}
