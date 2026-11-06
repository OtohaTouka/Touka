"""清除連備份的換檔原語：戳記、暫存、空間與唯讀檢查、驗證後的原子取代.

順序固定：驗證 → fsync 暫存 → 複製原檔時間與權限 → 再量一次戳記 → 改名取代 → fsync 資料夾。
任何一步失敗都丟 ScrubItemFailure（只帶原因代碼），呼叫端負責刪暫存、原檔不動。
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import stat
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.factual import MemoryScrubError

from src.models.memory_scrub import ScrubItemResult
from src.services import system_backup
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.memory_scrub import ScrubKind

logger = logging.getLogger(__name__)

SPACE_MARGIN = 1.1  # 預檢空間時多留 10%
RENAME_ATTEMPTS = 3  # 改名遇到暫時占用（防毒、索引）時最多試幾次
RENAME_RETRY_DELAY_S = 0.5
_UNREADABLE_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError)


class ScrubItemFailure(Exception):
    """一個檔的清除失敗；只帶原因代碼."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"memory scrub item failed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class FileStamp:
    """原檔的戳記：處理期間有人動過就不取代."""

    size: int
    mtime_ns: int
    ino: int
    dev: int
    ctime_ns: int | None


def stamp(path: Path) -> FileStamp:
    """量戳記；POSIX 另含 ctime（Windows 的 st_ctime 是建立時間，不算變動）."""
    info = path.stat()
    return FileStamp(
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ino=info.st_ino,
        dev=info.st_dev,
        ctime_ns=None if os.name == "nt" else info.st_ctime_ns,
    )


def regular_file(path: Path) -> bool:
    """不跟連結：只有一般檔才算（符號連結、junction 與其他 reparse point 都不算）."""
    return system_backup.path_kind(path) == "file"


def regular_dir(path: Path) -> bool:
    """不跟連結：只有真的資料夾才算."""
    return system_backup.path_kind(path) == "dir"


def missing(path: Path) -> bool:
    """不跟連結：這個名字已經不存在."""
    return system_backup.path_kind(path) == "missing"


def folder_problem(kind: ScrubKind, directory: Path) -> ScrubItemResult | None:
    """要處理的資料夾本身存在、卻不是真的資料夾（連結、junction）：記失敗，不能當成沒東西."""
    if system_backup.path_kind(directory) in ("missing", "dir"):
        return None
    return ScrubItemResult(kind=kind, name=directory.name, status="failed", reason="unknown_file")


def is_read_only(path: Path) -> bool:
    """原檔沒有擁有者寫入權限（Windows 的唯讀屬性也一樣反映在這裡）."""
    return not path.stat().st_mode & stat.S_IWUSR


def has_space(directory: Path, needed: float) -> bool:
    """可用空間夠不夠；查不到當未知、照常處理（真的寫滿時寫入會失敗並回報）."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return True
    return usage.total <= 0 or usage.free >= needed * SPACE_MARGIN


def new_temp(directory: Path, base_name: str) -> Path:
    """在 directory 建一個空的暫存檔：``.<base_name>.memscrub-<8 個隨機字元>.tmp``（不是 *.zip）."""
    try:
        fd, name = tempfile.mkstemp(prefix=f".{base_name}.memscrub-", suffix=".tmp", dir=directory)
    except OSError as exc:
        reason = "no_space" if exc.errno == errno.ENOSPC else "write_failed"
        raise ScrubItemFailure(reason) from exc
    os.close(fd)
    return Path(name)


def replace_verified(original: Path, temp: Path, before: FileStamp) -> None:
    """驗證通過後才呼叫：fsync → 複製時間與權限 → 戳記比對 → 原子改名 → fsync 資料夾."""
    resource_manager.fsync_file(temp)
    resource_manager.copy_file_metadata(original, temp)
    if stamp(original) != before:
        raise ScrubItemFailure("changed_during_scrub")
    _rename(temp, original)
    try:
        resource_manager.fsync_dir(original.parent)
    except OSError:  # 已經換上了：檔案是清過的，只是資料夾的落盤沒確認
        logger.warning("[memory-scrub] fsync of the folder failed after %s", original.name)


def _rename(temp: Path, original: Path) -> None:
    """原子改名；Windows 上防毒或索引短暫占住時稍等重試，仍占用才算 in_use."""
    for attempt in range(1, RENAME_ATTEMPTS + 1):
        try:
            resource_manager.move_file(temp, original)
        except PermissionError as exc:  # 有人開著原檔（下載、加密、還原、防毒）
            if attempt == RENAME_ATTEMPTS:
                raise ScrubItemFailure("in_use") from exc
            time.sleep(RENAME_RETRY_DELAY_S)
        else:
            return


def discard(path: Path) -> None:
    """刪掉暫存（不存在就算了）；刪不掉先清唯讀位元再刪，仍失敗只記警告."""
    if not path.exists():
        return
    try:
        resource_manager.remove_file(path)
    except PermissionError:
        with contextlib.suppress(OSError):
            resource_manager.make_writable(path)
            resource_manager.remove_file(path)
    if path.exists():
        logger.warning("[memory-scrub] could not remove a temp file: %s", path.name)


def guard_item(
    kind: ScrubKind, name: str, work: Callable[[], list[ScrubItemResult]]
) -> list[ScrubItemResult]:
    """一檔一個邊界：任何失敗都變成這一檔的 failed 項目（只帶代碼），不中斷其他檔."""
    try:
        return work()
    except MemoryScrubError as exc:
        reason = exc.reason.value
    except ScrubItemFailure as exc:
        reason = exc.reason
    except _UNREADABLE_ERRORS:
        reason = "unreadable"
    except FileNotFoundError:  # 處理到一半原檔不見了
        reason = "changed_during_scrub"
    except OSError as exc:
        logger.warning("[memory-scrub] %s failed", name, exc_info=True)
        reason = "no_space" if exc.errno == errno.ENOSPC else "unreadable"
    except Exception:  # noqa: BLE001 - 意外只記日誌、回報代碼，不中斷其他檔
        logger.exception("[memory-scrub] unexpected failure on %s", name)
        reason = "internal_error"
    return [ScrubItemResult(kind=kind, name=name, status="failed", reason=reason)]
