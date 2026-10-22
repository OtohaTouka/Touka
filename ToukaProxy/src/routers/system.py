"""系統管理路由 — 負責備份、還原與進程生命週期管理 (SSOT)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request  # noqa: TC002
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from src import config as app_config
from src.config import get_workspace_root
from src.core.exceptions import ErrorCode, ToukaException
from src.models.backup_schedule import BackupScheduleConfig
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.sliding_window import SlidingWindowConfig  # noqa: TC001
from src.models.system_restore import RestoreStatus
from src.models.user import User  # noqa: TC001
from src.routers.auth import get_auth_service, get_current_user
from src.services import system_backup
from src.services.agent_reset.orchestrator import reset_in_progress
from src.services.auth_service import AuthService  # noqa: TC001
from src.services.authorization_gateway import require_admin_authority, require_authenticated
from src.services.backup_crypto import encrypt_backup_zip
from src.services.backup_scheduler_service import load_schedule_config, persist_policy
from src.services.execution_broker import run_sync
from src.services.memory_scrub import in_progress as memory_scrub_in_progress
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.services.settings_snapshot_service import (
    apply_snapshot,
    build_snapshot,
    package_encrypted_snapshot,
)
from src.services.sliding_window_service import sliding_window_service
from src.services.system_backup import create_backup, run_backup_job
from src.utils.async_db import session_service
from src.utils.child_env import child_python_env
from src.utils.csrf_guard import verify_origin
from src.utils.path_security import validate_no_symlink_escape
from src.utils.time_utils import format_epoch_to_taipei

router = APIRouter(prefix="/system", tags=["system"])
logger = logging.getLogger(__name__)

_SW_CONFIG_FILE = "sliding_window.json"
# ADR-0112：序列化設定匯入（備份→套用全程），防併發 TOCTOU 競態互相覆寫設定。
_settings_import_lock = asyncio.Lock()
_OAUTH_RUNTIME_FILENAMES = (
    "mcp_oauth.sqlite3",
    "mcp_oauth.sqlite3-wal",
    "mcp_oauth.sqlite3-shm",
    "mcp_oauth_workload.sqlite3",
    "mcp_oauth_workload.sqlite3-wal",
    "mcp_oauth_workload.sqlite3-shm",
)
# H1：最後一次還原的結果只放記憶體——成功會 os._exit 重啟、紀錄跟著消失；
# 失敗時進程還活著、紀錄留著，前端重整前先查 GET /system/restore/status。
_last_restore = RestoreStatus(state="idle")


# Grandfather Batch-1 fix: system backup/restore requires admin
# Require admin role for system backup/restore operations.
_require_admin = require_admin_authority


# Require any authenticated identity.
_require_authenticated = require_authenticated

# ── SSOT: 路徑定址 ─────────────────────────────────────────────────────────────


# 備份目錄與腳本路徑的 SSOT 在 system_backup（快照、清單、下載、還原、排程清理共用）。
_get_backup_dir = system_backup.backup_dir


def _oauth_restore_destination_populated() -> bool:
    """Return whether restore would collide with live OAuth credentials."""
    has_runtime_store = any(
        (app_config.CONFIG_DIR / filename).exists()
        for filename in _OAUTH_RUNTIME_FILENAMES
    )
    has_key_ring = bool(app_config.TOUKA_MCP_OAUTH_KEY.strip()) or (
        app_config.TOUKA_MCP_OAUTH_RETIRED_KEYS.strip() not in {"", "[]"}
    )
    has_workload_key_ring = bool(
        app_config.TOUKA_MCP_OAUTH_WORKLOAD_KEY.strip()
    ) or app_config.TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS.strip() not in {
        "",
        "[]",
    }
    return has_runtime_store or has_key_ring or has_workload_key_ring


# ── 模型定義 ──────────────────────────────────────────────────────────────────


class BackupInfo(BaseModel):
    """備份檔案資訊模型."""

    filename: str = Field(..., description="檔案名稱")
    size_mb: float = Field(..., description="檔案大小 (MB)")
    created_at: str = Field(..., description="建立時間 (台北時間)")


class BackupStats(BaseModel):
    """備份目錄統計資訊模型."""

    count: int = Field(..., description="備份檔數量")
    total_size_mb: float = Field(..., description="備份總大小 (MB)")
    oldest: str | None = Field(None, description="最舊備份時間 (台北時間)")
    newest: str | None = Field(None, description="最新備份時間 (台北時間)")
    free_disk_mb: float = Field(..., description="備份磁碟可用空間 (MB)")


class RestoreRequest(BaseModel):
    """系統還原請求模型."""

    filename: str = Field(..., description="目標備份檔名")
    current_password: str = Field(..., description="操作者帳號密碼（破壞性操作再確認）")


class SettingsExportRequest(BaseModel):
    """設定快照匯出請求（ADR-0112）。"""

    encrypt: bool = Field(default=False, description="是否以密語加密為 AES-256 zip")
    passphrase: str | None = Field(default=None, min_length=12, description="加密密語（≥12）")


class SettingsImportRequest(BaseModel):
    """設定快照匯入請求（ADR-0112）。"""

    snapshot: dict[str, Any] = Field(..., description="設定快照 envelope")
    mode: Literal["merge", "replace"] = Field(default="merge", description="合併或整區覆蓋")
    sections: list[str] | None = Field(default=None, description="限定只套用的區（None＝全部）")
    current_password: str = Field(default="", description="操作者密碼（套用時必填；dry_run 免）")
    dry_run: bool = Field(default=False, description="只驗證與預覽、不寫入")


class BackupDownloadRequest(BaseModel):
    """加密下載請求模型（ADR-0108）."""

    # 此加密檔含全系統機密（.env／所有金鑰）→ 密碼短語下限拉高到 12（強過一般 8）。
    passphrase: str = Field(..., min_length=12, description="加密下載的密碼短語（≥12 字）")


# 即時加密為 CPU＋暫存磁碟密集；限制同時加密數，避免（admin 誤觸/併發）執行緒與暫存磁碟暴衝。
_DOWNLOAD_ENCRYPT_SEMAPHORE = asyncio.Semaphore(2)


# ── API 端點 ──────────────────────────────────────────────────────────────────


@router.get("/backups", response_model=list[BackupInfo])
async def list_backups(
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> list[BackupInfo]:
    """列出所有已存在的備份檔案。"""
    backup_dir = _get_backup_dir()
    if not backup_dir.exists():
        return []

    backups = []
    try:
        for file_path in system_backup.list_snapshot_files(backup_dir):
            stats = file_path.stat()
            time_str, _ = format_epoch_to_taipei(stats.st_mtime)
            backups.append(
                BackupInfo(
                    filename=file_path.name,
                    size_mb=round(stats.st_size / (1024 * 1024), 2),
                    created_at=time_str,
                )
            )
    except Exception:
        logger.exception("Failed to list backups")
        return []

    # 按日期排序（最新在前）
    return sorted(backups, key=lambda x: x.created_at, reverse=True)


@router.post("/backup/run")
async def run_backup(
    http_request: Request,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> dict[str, Any]:
    """執行即時系統備份（委派 SSOT 服務 `system_backup.create_backup`）。"""
    # S3: CSRF Origin guard on destructive system endpoint (對齊 terminal.py)。
    verify_origin(http_request)
    # 與排程自動備份共用執行鎖（防併發備份）＋移出事件迴圈（create_backup 為阻塞子程序）；
    # 請求被取消時鎖仍持有到備份真的結束。
    return await run_backup_job(create_backup)


def restore_in_progress() -> bool:
    """還原是否正在進行（重置端點據此擋下；還原成功會重啟程序，所以只看 running）."""
    return _last_restore.state == "running"


def _record_restore(state: Literal["running", "failed"]) -> None:
    """整筆替換最後一次還原的結果；只記狀態與錯誤碼，不記路徑或例外原文（禁令 23）。"""
    global _last_restore  # 背景執行緒寫、事件迴圈讀；整筆替換參照，不需鎖
    failed = state == "failed"
    _last_restore = RestoreStatus(
        state=state, error_code=ErrorCode.SYSTEM_BACKUP_FAILED.value if failed else None
    )


def _execute_restore(script_path: Path, zip_path: Path) -> None:
    """背景執行系統還原，並於完成後結束進程以利 Docker 重啟服務。"""
    # 給前端一點時間接收 Response
    time.sleep(2)

    try:
        logger.info(f"💣 Initiating system RESTORE from: {zip_path}")
        session_service.mark_restore_pending()
        # 以目前的直譯器執行還原腳本（不依賴 PATH 上的 python）。不設硬逾時：還原是逐檔
        # 就地覆寫，中途被殺會讓資料一半新一半舊；卡住的還原只能重啟服務處理。
        try:
            run_sync(
                [
                    sys.executable, str(script_path), "restore", "--file", str(zip_path),
                    "--no-docker", *system_backup.script_path_args(),
                ],  # fmt: skip
                check=True,
                env=child_python_env(),
            )
        finally:
            # 子程序失敗也可能已覆蓋對話庫；在 Proxy 的背景執行緒完成降標記。
            asyncio.run(session_service.invalidate_restored_sessions())
        # connections.json 可能已換掉：隔離封鎖埠立刻跟上（失敗、行程沒重啟時也一樣）。
        model_manager.reload_connections()
        logger.info("✅ Restore complete. Restarting process...")

        # 在某些環境下，直接結束進程讓 Docker 重新啟動它是最安全的
        os._exit(0)
    except Exception:
        logger.exception("Restore CRITICAL failure")
        _record_restore("failed")
        model_manager.reload_connections()


@router.post("/restore/run")
async def run_restore(
    request: RestoreRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict[str, Any]:
    """執行系統還原 (警告：這會導致服務重啟)。

    破壞性程度高於「清空我的資料」（覆蓋 .env／連線／Soul／記憶＋強制重啟），故同樣採
    **密碼重輸**閘（DA25 模式）：被竊的 access token 單獨無法觸發。需 admin。
    """
    # S3: CSRF Origin guard — full-system restore is destructive (對齊 terminal.py)。
    verify_origin(http_request)
    if current_user.role != "admin":
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)
    if not auth_service.verify_password(request.current_password, current_user.hashed_password):
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)
    if memory_scrub_in_progress():  # 清除進行中還原，會把清掉的記憶寫回並中途殺掉清除
        raise ToukaException(ErrorCode.MEMORY_SCRUB_IN_PROGRESS)
    if reset_in_progress():  # 重置進行中還原，會被重置中途改掉一半（反過來也一樣擋）
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)
    script_path = system_backup.backup_script()
    backup_dir = _get_backup_dir()
    zip_path = system_backup.is_listed_snapshot(request.filename, backup_dir)

    # 只接受清單上的檔名（頂層一般檔）；S2：再確認沒有逃出 backups/。
    if zip_path is None or not validate_no_symlink_escape(zip_path, backup_dir):
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_NOT_FOUND, filename=request.filename)
    if _oauth_restore_destination_populated():
        raise ToukaException(ErrorCode.MCP_OAUTH_RESTORE_CONFLICT)

    _record_restore("running")  # 蓋掉上一次的失敗，前端不會誤判這一次
    background_tasks.add_task(_execute_restore, script_path, zip_path)
    return {
        "status": "accepted",
        "message": "Restore process initiated. The server will restart shortly.",
    }


@router.get("/restore/status", response_model=RestoreStatus)
async def get_restore_status(
    _identity: Annotated[AgentIdentity, Depends(_require_admin)],
) -> RestoreStatus:
    """最後一次還原的結果（admin、唯讀）：idle／running／failed＋錯誤碼。"""
    return _last_restore


@router.get("/backups/stats", response_model=BackupStats)
async def backup_stats(
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> BackupStats:
    """回傳備份目錄統計（數量／總大小／最舊最新／磁碟可用空間）。"""
    backup_dir = _get_backup_dir()
    files = system_backup.list_snapshot_files(backup_dir)
    total_bytes = sum(f.stat().st_size for f in files)
    mtimes = sorted(f.stat().st_mtime for f in files)
    oldest = format_epoch_to_taipei(mtimes[0])[0] if mtimes else None
    newest = format_epoch_to_taipei(mtimes[-1])[0] if mtimes else None
    # 磁碟可用空間：備份目錄存在則量它，否則量 workspace 根（尚未建立任何備份時）。
    usage_target = backup_dir if backup_dir.exists() else Path(get_workspace_root())
    try:
        free_bytes = shutil.disk_usage(str(usage_target)).free
    except OSError:
        # 不可量測（權限/未掛載）時優雅降級為 0，而非讓統計端點 500。
        free_bytes = 0
    return BackupStats(
        count=len(files),
        total_size_mb=round(total_bytes / (1024 * 1024), 2),
        oldest=oldest,
        newest=newest,
        free_disk_mb=round(free_bytes / (1024 * 1024), 2),
    )


@router.delete("/backups/{filename}")
async def delete_backup(
    filename: str,
    http_request: Request,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> dict[str, Any]:
    """刪除指定備份檔（admin）。只接受清單上的檔名；拿備份鎖再刪（和清除、備份互斥）。"""
    verify_origin(http_request)
    backup_dir = _get_backup_dir()
    zip_path = system_backup.is_listed_snapshot(filename, backup_dir)
    # S2: filename 不得逃出 backups/（`.exists()` 擋不住 `../`）。
    if zip_path is None or not validate_no_symlink_escape(zip_path, backup_dir):
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_NOT_FOUND, filename=filename)
    def _delete() -> None:
        # 刪除可能要等清除或備份跑完；稽核寫在工作裡，等待中斷線也不會遺失
        resource_manager.remove_file(zip_path)
        logger.warning("AUDIT: admin %s deleted backup %r", _identity.agent_id, filename)

    await run_backup_job(_delete)
    return {"status": "deleted", "filename": filename}


@router.post("/backups/{filename}/download")
async def download_backup(
    filename: str,
    request: BackupDownloadRequest,
    http_request: Request,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> FileResponse:
    """以密碼短語把備份即時加密成 WinZip-AES-256 zip 後串流下載（ADR-0108）。

    伺服器端明文 zip 永不以明文出網；下載產物可被 7-Zip/WinRAR 以密碼開啟。admin only。
    """
    verify_origin(http_request)
    backup_dir = _get_backup_dir()
    zip_path = system_backup.is_listed_snapshot(filename, backup_dir)
    # 同 delete：只接受清單上的檔名、不得逃出 backups/。
    if zip_path is None or not validate_no_symlink_escape(zip_path, backup_dir):
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_NOT_FOUND, filename=filename)
    # CPU 密集（重壓縮＋加密）→ 移出事件迴圈，避免阻塞其他請求；並以號誌限制同時加密數。
    async with _DOWNLOAD_ENCRYPT_SEMAPHORE:
        enc_path = await run_in_threadpool(encrypt_backup_zip, zip_path, request.passphrase)
    logger.warning("AUDIT: admin %s downloaded encrypted backup %s", _identity.agent_id, filename)
    return FileResponse(
        enc_path,
        media_type="application/zip",
        filename=f"{filename[:-4]}_encrypted.zip",
        background=BackgroundTask(resource_manager.remove_file, enc_path),
    )


# ── Backup Schedule（ADR-0109 自動備份排程）───────────────────────────────


@router.get("/backup/schedule", response_model=BackupScheduleConfig)
async def get_backup_schedule(
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> BackupScheduleConfig:
    """讀取自動備份排程設定（admin）。"""
    return load_schedule_config()


@router.post("/backup/schedule", response_model=BackupScheduleConfig)
async def update_backup_schedule(
    payload: BackupScheduleConfig,
    http_request: Request,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> BackupScheduleConfig:
    """更新自動備份排程策略（admin）。``last_run_epoch`` 為排程器狀態，不被前端重設。"""
    verify_origin(http_request)
    return await persist_policy(payload)


# ── 設定快照（ADR-0112 設定匯入／匯出）─────────────────────────────────────────


@router.post("/settings/export")
async def export_settings(
    request: SettingsExportRequest,
    http_request: Request,
    identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> Any:
    """匯出可攜設定快照（admin）。零機密、零使用者資料（ADR-0112）。

    預設回傳明文 JSON（可 git diff／審閱）；``encrypt=true`` 時以密語封成 AES-256 zip 串流下載。
    唯讀、不洩漏機密（MCP env／連線金鑰已遮罩），故無密碼閘；POST 仍經 CSRF Origin 守衛。
    """
    verify_origin(http_request)
    snapshot = build_snapshot(exported_by_user_id=identity.agent_id)
    if not request.encrypt:
        return snapshot
    if not request.passphrase:
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override="加密需提供密語（≥12 字）")
    async with _DOWNLOAD_ENCRYPT_SEMAPHORE:
        enc_path = await run_in_threadpool(
            package_encrypted_snapshot, snapshot, request.passphrase,
        )
    logger.warning("AUDIT: admin %s exported settings snapshot (encrypted)", identity.agent_id)
    return FileResponse(
        enc_path,
        media_type="application/zip",
        filename="touka-settings_encrypted.zip",
        background=BackgroundTask(resource_manager.remove_file, enc_path),
    )


@router.post("/settings/import")
async def import_settings(
    request: SettingsImportRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict[str, Any]:
    """匯入設定快照（admin）。破壞性 → 套用時採密碼重輸（DA25）＋匯入前自動備份。

    ``dry_run=true``：只驗證與預覽（免密碼，唯讀、不寫入），供前端確認前預覽。
    實際套用：admin＋CSRF＋密碼重輸；先建還原點（自動備份），再合併／覆蓋寫入並熱載入。
    無自動回滾——不滿意時以回傳的 ``pre_import_backup`` 經 /system/restore/run 手動還原。
    """
    verify_origin(http_request)
    if current_user.role != "admin":
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)

    # 乾跑預覽：唯讀（免密碼、免備份、免鎖；不寫入任何狀態）。
    if request.dry_run:
        result = await run_in_threadpool(
            apply_snapshot, request.snapshot,
            mode=request.mode, sections=request.sections, dry_run=True,
        )
        result["pre_import_backup"] = None
        return result

    # 套用：密碼重輸（DA25）→ 序列化（防併發競態）→ 還原點備份 → 套用。
    if not auth_service.verify_password(request.current_password, current_user.hashed_password):
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)
    async with _settings_import_lock:
        pre_import_backup = await run_backup_job(create_backup)  # 與排程備份共用執行鎖
        # 先記錄還原點——即使後續套用中途失敗，admin 仍可由 log 找到備份手動還原。
        logger.warning(
            "AUDIT: admin %s settings-import pre-backup: %s",
            current_user.id, pre_import_backup.get("message"),
        )
        result = await run_in_threadpool(
            apply_snapshot, request.snapshot,
            mode=request.mode, sections=request.sections, dry_run=False,
        )
    result["pre_import_backup"] = pre_import_backup
    logger.warning(
        "AUDIT: admin %s imported settings snapshot (mode=%s, applied=%s)",
        current_user.id, request.mode, result.get("applied"),
    )
    return result


# ── Sliding Window Config ─────────────────────────────────────────────────


@router.get("/sliding_window")
async def get_sliding_window_config(
    _identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """取得滑動視窗設定（任何已認證身份可讀）。"""
    config = sliding_window_service.load_config()
    return config.model_dump(exclude_none=True)


@router.post("/sliding_window")
async def update_sliding_window_config(
    payload: SlidingWindowConfig,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> dict[str, Any]:
    """更新滑動視窗設定（僅限 admin）。"""
    try:
        base = resource_manager.get_base_path()
        config_path = Path(base) / "config" / _SW_CONFIG_FILE
        resource_manager.write_json(config_path, payload.model_dump(exclude_none=True), indent=2)
        logger.info("SlidingWindow config updated by admin.")
        return {"status": "success", "config": payload.model_dump(exclude_none=True)}
    except Exception:
        logger.exception("Failed to update sliding_window config")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from None
