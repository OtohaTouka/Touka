"""檔案操作的合約與真實作（ADR-0311）。規則都寫在別處，這裡只負責碰磁碟。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

Clock = Callable[[], float]


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


def atomic_write_text(path: Path, text: str) -> None:
    """寫自己的設定與紀錄。中途當機時最多少一個檔，讀不到就當作什麼都沒開放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.new")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        path.unlink()
    temporary.rename(path)
