"""檔案操作的合約與真實作（ADR-0311）。規則都寫在別處，這裡只負責碰磁碟。"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

Clock = Callable[[], float]

TEMPORARY_SUFFIX: Final = ".new"
_PREVIOUS_SUFFIX: Final = ".old"


@dataclass(frozen=True)
class EntryInfo:
    """一個檔案或資料夾的狀態，不跟隨連結。"""

    is_dir: bool
    size: int
    mtime_ns: int
    is_reparse: bool
    is_hidden: bool


class Fs(Protocol):
    """小幫手會用到的檔案操作。``rename`` 和 ``create_new`` 都不能覆蓋既有的檔案。"""

    def lstat(self, path: str) -> EntryInfo | None:
        """目標不存在時回傳 None，不跟隨連結。"""

    def children(self, path: str) -> list[str]:
        """資料夾裡的名稱。"""

    def mkdir(self, path: str) -> None:
        """建立資料夾，已存在就丟 FileExistsError。"""

    def create_new(self, path: str, data: bytes) -> None:
        """建立新檔，已存在就丟 FileExistsError。"""

    def read(self, path: str, limit: int) -> bytes:
        """讀檔案開頭。"""

    def rename(self, source: str, destination: str) -> None:
        """改名或搬移，目的地已存在就丟 FileExistsError。"""


class Trash(Protocol):
    """回收桶。回傳 ``ok``、``aborted`` 或 ``failed``。"""

    def send(self, path: str) -> str:
        """丟一項到回收桶。"""


class RealFs:
    """Windows 上的實作。"""

    def lstat(self, path: str) -> EntryInfo | None:
        """目標不存在時回傳 None。"""
        try:
            result = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        attributes = result.st_file_attributes
        return EntryInfo(
            is_dir=stat.S_ISDIR(result.st_mode),
            size=result.st_size,
            mtime_ns=result.st_mtime_ns,
            is_reparse=bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            or stat.S_ISLNK(result.st_mode),
            is_hidden=bool(
                attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)
            ),
        )

    def children(self, path: str) -> list[str]:
        """資料夾裡的名稱，排序過。"""
        return sorted(child.name for child in Path(path).iterdir())

    def mkdir(self, path: str) -> None:
        """建立資料夾，已存在就失敗。"""
        Path(path).mkdir()

    def create_new(self, path: str, data: bytes) -> None:
        """建立新檔，已存在就失敗。"""
        with Path(path).open("xb") as handle:
            handle.write(data)

    def read(self, path: str, limit: int) -> bytes:
        """讀檔案開頭。"""
        with Path(path).open("rb") as handle:
            return handle.read(limit)

    def rename(self, source: str, destination: str) -> None:
        """Windows 上目的地已存在時會失敗，不會覆蓋（``Path.replace`` 才會）。"""
        Path(source).rename(destination)


def temporary_path(path: Path) -> Path:
    """寫到一半的暫存檔。"""
    return path.with_name(f"{path.name}{TEMPORARY_SUFFIX}")


def previous_path(path: Path) -> Path:
    """換檔時暫時留著的舊內容。"""
    return path.with_name(f"{path.name}{_PREVIOUS_SUFFIX}")


def durable_write_text(path: Path, text: str) -> None:
    """寫自己的設定與紀錄：暫存檔落盤，舊檔改名留著，新檔改名就位，再刪舊檔。

    能覆蓋的改名被能力邊界禁掉了，所以要分這幾步。當機在中間時正式檔不在，
    ``read_durable_text`` 會改讀已經落盤的暫存檔；改名失敗時把舊檔放回去，內容不變。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_path(path)
    previous = previous_path(path)
    # 上次當機留下的暫存檔是唯一的一份，先收回來才不會被這次截斷
    if temporary.exists() and not path.exists():
        temporary.rename(path)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    previous.unlink(missing_ok=True)
    had_previous = path.exists()
    if had_previous:
        path.rename(previous)
    try:
        temporary.rename(path)
    except OSError:
        if had_previous:
            previous.rename(path)
        raise
    # 新內容已經就位，舊檔刪不掉只是下次再清
    with contextlib.suppress(OSError):
        previous.unlink(missing_ok=True)


def read_durable_text(path: Path) -> str:
    """讀 ``durable_write_text`` 寫的檔；正式檔不在就讀暫存檔。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return temporary_path(path).read_text(encoding="utf-8")
