"""系統備份服務（SSOT）。

包裝 `scripts/manage_backup.py`，供 `/system/backup/run` 端點與破壞性操作（如 DebugAgent
重置前的「備份優先」）及自動排程（ADR-0109）共用，避免重複實作。
"""
from __future__ import annotations

import asyncio
import logging
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.config import PROXY_ROOT, get_config_dir, resolve_env_file_path
from src.core.exceptions import ErrorCode, ToukaException
from src.services.execution_broker import run_sync
from src.services.resource_manager import resource_manager
from src.utils.child_env import child_python_env
from src.utils.secrets import redact_secrets_from_content

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# 序列化備份「執行」——手動（/system/backup/run）與排程（auto）共用，避免同時跑兩個備份
# 程序造成 I/O 競爭與暫存暴衝（ADR-0109 複審 #1/#8）。只由 run_backup_job 取得。
backup_execution_lock = asyncio.Lock()

# run_backup_job 進行中的 task：保留參考直到完成，呼叫端被取消後也不會被回收。
_backup_jobs: set[asyncio.Task[Any]] = set()

# ADR-0109：自動排程備份的檔名前綴，與人工觸發的 ``Touka_Backup`` 區分。保留策略只修剪
# 此前綴的檔（人工備份永不被自動刪、且不計入保留份數）。SSOT —— 排程器修剪亦引用此常數。
AUTO_BACKUP_PREFIX = "Touka_AutoBackup"


def _forget_backup_job(task: asyncio.Task[Any]) -> None:
    """完成回呼：釋放參考，並取回例外（呼叫端已取消時沒人取，避免 asyncio 未取回警告）。"""
    _backup_jobs.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # 呼叫端已取消時沒人接手結果，這是唯一紀錄（例如排程修剪失敗）。
        logger.warning("Backup job failed: %r", task.exception())


async def run_backup_job[T](job: Callable[[], T]) -> T:
    """在獨立 task 裡持 ``backup_execution_lock`` 並以執行緒跑 ``job``；呼叫端以 shield 等待。

    呼叫端被取消（重置中途取消、HTTP 斷線等）時，鎖會一直持有到執行緒裡的備份真的結束，
    另一個備份不會同時開始。手動備份、設定匯入前備份、重置前備份、排程備份共用此入口。
    """

    async def _locked() -> T:
        async with backup_execution_lock:
            return await asyncio.to_thread(job)

    task = asyncio.create_task(_locked(), name="system-backup-job")
    _backup_jobs.add(task)
    task.add_done_callback(_forget_backup_job)
    return await asyncio.shield(task)


def backup_script() -> Path:
    """快照／還原腳本（SSOT）：在 ToukaProxy/scripts，不在 repo 根的 scripts。"""
    return PROXY_ROOT / "scripts" / "manage_backup.py"


def backup_dir() -> Path:
    """系統快照目錄（SSOT）：.env 所在資料夾底下的 backups。

    快照、清單、下載、刪除、還原、排程清理都用這裡。容器裡 .env 在掛載的 compose 目錄，
    快照跟著留在容器外；本機整合模式就是 repo 根的 backups。
    """
    return resolve_env_file_path().parent / "backups"


def script_path_args() -> list[str]:
    """把執行中 Proxy 的路徑交給腳本，不讓它自己往上猜 .env（容器裡猜不到）；快照與還原共用。"""
    return [
        "--data-root", str(resource_manager.get_base_path()),
        "--env-file", str(resolve_env_file_path()),
        "--config-dir", str(get_config_dir()),
        "--output-dir", str(backup_dir()),
    ]  # fmt: skip


def is_snapshot_name(name: str) -> bool:
    """清單上的快照檔名：單層、不是點開頭、副檔名不分大小寫是 .zip（還原、下載、刪除共用）.

    含 ``:`` 一律不算（Windows 上 ``x:.zip`` 是檔案 x 的替代資料流，不在清單上）。
    """
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and ":" not in name
        and Path(name).name == name
        and not name.startswith(".")
        and name.casefold().endswith(".zip")
    )


def path_kind(path: Path) -> Literal["missing", "file", "dir", "other"]:
    """不跟連結判斷路徑種類（快照與清除共用的連結標準）.

    符號連結、junction 與其他 reparse point 都是 other；只有 lstat 找不到才是 missing。
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "other"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "other"


def _is_regular_file(path: Path) -> bool:
    try:
        return path_kind(path) == "file"
    except OSError:
        return False


def list_snapshot_files(directory: Path | None = None, *, strict: bool = False) -> list[Path]:
    """快照目錄頂層、符合清單規則的一般檔，依檔名排序（清單、統計、修剪、清除共用）.

    目錄本身不存在回空清單；存在但不是真的資料夾（連結、junction）時不跟進去：清單看到的是空的，
    ``strict=True``（清除用）則丟 ``NotADirectoryError``，讓清除記失敗而不是當成沒東西要處理。
    """
    root = backup_dir() if directory is None else Path(directory)
    kind = path_kind(root)
    if kind != "dir":
        if kind != "missing":
            logger.warning("Snapshot folder is not a real directory; not following it")
            if strict:
                raise NotADirectoryError(root.name)
        return []
    entries = (entry for entry in root.iterdir() if is_snapshot_name(entry.name))
    return sorted((entry for entry in entries if _is_regular_file(entry)), key=lambda e: e.name)


def is_listed_snapshot(name: str, directory: Path | None = None) -> Path | None:
    """清單上的快照回傳路徑，否則 None（還原、下載、刪除只接受清單上的檔名）."""
    if not is_snapshot_name(name):
        return None
    candidate = (backup_dir() if directory is None else Path(directory)) / name
    return candidate if _is_regular_file(candidate) else None


def create_backup(*, auto: bool = False) -> dict[str, str | None]:
    """執行即時全系統備份（`manage_backup.py backup --no-docker` ＋ Proxy 的路徑參數）。

    成功回傳 ``{"status","message","filename"}``：filename 是這次新增的快照檔名（在備份鎖內比對
    前後清單；不是剛好多一份就填 None 並記警告），不回傳腳本輸出（有絕對路徑）；
    任何失敗 raise ``SYSTEM_BACKUP_FAILED``，讓呼叫端（破壞性操作）可在無備份時安全中止。
    資料根不存在時腳本直接失敗，不會產出不含資料的快照。
    以 ``sys.executable`` 確保用當前執行中的直譯器（與伺服器同一個 venv）。

    ``auto=True``（排程器）使用 ``AUTO_BACKUP_PREFIX`` 檔名前綴，以與人工備份區隔。
    """
    script_path = backup_script()
    if not script_path.exists():
        logger.error("Backup script missing at %s", script_path)
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED, error_output="Backup script missing")
    cmd = [sys.executable, str(script_path), "backup", "--no-docker"]
    if auto:
        cmd += ["--prefix", AUTO_BACKUP_PREFIX]
    cmd += script_path_args()
    try:
        before = {entry.name for entry in list_snapshot_files()}
        run_sync(cmd, capture_output=True, text=True, check=True, env=child_python_env())
        after = {entry.name for entry in list_snapshot_files()}
    except subprocess.CalledProcessError as e:
        # 腳本輸出可能有路徑與像金鑰的字串：只進伺服器 log（先消毒），不放進回應（禁令 23）。
        stderr, _ = redact_secrets_from_content(e.stderr or "")
        logger.exception("Backup process failed (exit %s): %s", e.returncode, stderr)
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED) from e
    except Exception:
        logger.exception("Unexpected error during backup")
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED) from None
    added = sorted(after - before)
    if len(added) != 1:
        logger.warning("Backup finished but the new snapshot is unclear (%d new files)", len(added))
    return {
        "status": "success",
        "message": "Backup created successfully",
        "filename": added[0] if len(added) == 1 else None,
    }
