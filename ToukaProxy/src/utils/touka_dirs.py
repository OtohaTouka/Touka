"""Touka 目錄偵測：名稱比對、往上檢查與往下掃描（iso-c2，設計 §3.4）.

純同步的檔案系統讀取（只列資料夾內容，不讀檔、不寫入），由 ``services/isolation`` 在
執行緒裡呼叫。規則：

- 名稱一律 NFC＋casefold 後比對（9p 與 NTFS 不分大小寫）。
- **Touka 目錄**：底下有任一標記（``TOUKA_MARKERS``）、有任一資料根名稱（``DATA_ROOT_NAMES``，
  資料根形狀；兩份名單與 ``listing_kind`` 的 SSOT 在 ``sandbox_kernel/spec.py``，執行器共用），
  或本身是 E、D、INSTALL_ROOT。E、D 與資料根形狀的所有子項目都受保護，沒有例外。
- **往上**：從推導路徑 X 本身往上到最近的鏡像根 M（含 M；不在任何 M 底下就到 ``/``）。X 本身
  是 Touka 目錄就拒絕；落在上層 Touka 目錄 A 裡時，A 底下的第一層必須是使用者自建的資料夾
  （不在 ``TOUKA_TOPLEVEL_PROTECTED``、不是點開頭、不是檔案、本身也不是 Touka 目錄）。
- **往下**：最多 3 層，逐層用 dir fd ``O_DIRECTORY|O_NOFOLLOW`` 開啟，不跟符號連結；沒有
  dir fd 的平台用不跟連結（含 junction）的路徑寫法。超過資料夾數或時間上限就停。
讀不到的資料夾一律不授權（無法確認）。
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING, Literal

from src.sandbox_kernel.spec import fold_name, listing_kind
from src.utils.isolation_policy import TOUKA_TOPLEVEL_PROTECTED
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    from src.sandbox_kernel.spec import DirKind

PROTECTED_LOCATION = "protected_location"
SCAN_LIMIT = "scan_limit"

# 往下掃描不進去的資料夾（依實際名稱完全比對；大小寫不同的照樣掃，寧可慢也不漏）
SCAN_SKIP_NAMES = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build", ".tox"}
    | {".mypy_cache", ".next", "target", "vendor", ".cache", "coverage", "out", ".gradle"}
    | {".terraform"}
)
_CLOCK_CHECK_EVERY = 256  # 大資料夾裡每看這麼多項目就檢查一次時間
_VANISHED = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_FD_WALK = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.scandir in os.supports_fd
    and os.open in os.supports_dir_fd
)
_DIR_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC) if _FD_WALK else 0

ScanOutcome = Literal["clean", "touka", "limit", "error"]


def is_toplevel_protected(name: str) -> bool:
    """Touka 目錄底下不能授權的第一層名稱：清單裡的名稱與所有點開頭的名稱。"""
    folded = fold_name(name)
    return folded.startswith(".") or folded in TOUKA_TOPLEVEL_PROTECTED


class ToukaAnchors:
    """版面推導出的錨點：一定是 Touka 目錄的、整個封住的、往上檢查停在哪些鏡像根。"""

    def __init__(
        self, *, always: Iterable[Path], sealed: Iterable[Path], stop_roots: Iterable[Path]
    ) -> None:
        self.sealed = frozenset(sealed)
        self.always = frozenset(always) | self.sealed
        self.stop_roots = tuple(stop_roots)

    def kind(self, path: Path, names: frozenset[str] | None) -> DirKind | None:
        """這個位置算不算 Touka 目錄；``names`` 是 None 表示它不是資料夾。"""
        by_listing = listing_kind(names) if names is not None else None
        if path in self.sealed or by_listing == "sealed":
            return "sealed"
        return "touka" if path in self.always or by_listing else None

    def stop_for(self, target: Path) -> Path | None:
        """往上檢查的終點：身為 X 上層（或 X 本身）的鏡像根裡最近的那個。"""
        roots = [m for m in self.stop_roots if target == m or target.is_relative_to(m)]
        return max(roots, key=lambda m: len(m.parts), default=None)


def _listing(path: Path) -> frozenset[str] | None:
    """資料夾內容的名稱（fold 後）；不是資料夾回 None，其他讀取錯誤照拋。"""
    try:
        with os.scandir(path) as entries:
            return frozenset(fold_name(entry.name) for entry in entries)
    except NotADirectoryError:
        return None


def upward_verdict(target: Path, anchors: ToukaAnchors) -> str | None:
    """往上檢查（``target`` 已 resolve）；可授權回 None，否則回拒絕原因。"""
    stop = anchors.stop_for(target)
    child: tuple[str, bool, DirKind | None] | None = None  # 名稱、是不是資料夾、類型
    for node in (target, *target.parents):
        try:
            names = _listing(node)
        except OSError:
            return PROTECTED_LOCATION  # 讀不到（含不存在）就無法確認
        kind = anchors.kind(node, names)
        if child is None:
            if kind:
                return PROTECTED_LOCATION  # Touka 目錄本身只能當上層，不能整個授權
        elif kind == "sealed" or (kind == "touka" and not _user_folder(*child)):
            return PROTECTED_LOCATION
        if node == stop:
            break
        child = (node.name, names is not None, kind)
    return None


def _user_folder(name: str, is_dir: bool, kind: DirKind | None) -> bool:
    return is_dir and kind is None and not is_toplevel_protected(name)


class _OverBudget(Exception):
    """資料夾數或時間超過上限。"""


class _Budget:
    def __init__(self, *, max_dirs: int, seconds: float, clock: Callable[[], float]) -> None:
        self._left = max_dirs
        self._clock = clock
        self._deadline = clock() + seconds

    def enter_dir(self) -> None:
        self._left -= 1
        if self._left < 0:
            raise _OverBudget
        self.check_time()

    def check_time(self) -> None:
        if self._clock() > self._deadline:
            raise _OverBudget


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


def top_level_dirs(root: Path) -> dict[str, int] | None:
    """第一層真的資料夾（不含符號連結與 junction）：名稱 → inode；讀不到回 None。"""
    try:
        with os.scandir(root) as entries:
            return {
                entry.name: entry.inode()
                for entry in entries
                if entry.is_dir(follow_symlinks=False) and not entry.is_junction()
            }
    except OSError:
        return None


def scan_below(
    root: Path,
    *,
    max_depth: int,
    max_dirs: int,
    seconds: float,
    clock: Callable[[], float] = _monotonic_seconds,
    only: frozenset[str] | None = None,
) -> ScanOutcome:
    """從 ``root`` 往下最多 ``max_depth`` 層找 Touka 目錄（含 root 本身），不跟連結。

    ``only`` 有值時第一層只進這些資料夾（快取只補掃新增的第一層資料夾）。
    """
    budget = _Budget(max_dirs=max_dirs, seconds=seconds, clock=clock)
    try:
        if _FD_WALK:
            fd = os.open(root, _DIR_FLAGS)
            try:
                found = _walk_fd(fd, max_depth, budget, only)
            finally:
                os.close(fd)
        else:
            found = _walk_path(root, max_depth, budget, only)
    except _OverBudget:
        return "limit"
    except OSError:
        return "error"
    return "touka" if found else "clean"


def _entries(entries: Iterator[os.DirEntry[str]], budget: _Budget) -> list[tuple[str, bool]]:
    """（名稱, 是不是真的資料夾）；符號連結與 junction 都不算資料夾。"""
    result = []
    for index, entry in enumerate(entries):
        if index % _CLOCK_CHECK_EVERY == _CLOCK_CHECK_EVERY - 1:
            budget.check_time()
        real_dir = entry.is_dir(follow_symlinks=False) and not entry.is_junction()
        result.append((entry.name, real_dir))
    return result


def _children(entries: list[tuple[str, bool]], only: frozenset[str] | None) -> Iterator[str]:
    wanted = (name for name, is_dir in entries if is_dir and name not in SCAN_SKIP_NAMES)
    return (name for name in wanted if only is None or name in only)


def _walk_fd(fd: int, depth_left: int, budget: _Budget, only: frozenset[str] | None = None) -> bool:
    budget.enter_dir()
    with os.scandir(fd) as listing:
        entries = _entries(listing, budget)
    if listing_kind(fold_name(name) for name, _ in entries):
        return True
    if depth_left == 0:
        return False
    for name in _children(entries, only):
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
        except OSError as exc:
            if exc.errno in _VANISHED:
                continue  # 掃描途中被刪掉或換成連結：不跟
            raise
        try:
            if _walk_fd(child, depth_left - 1, budget):
                return True
        finally:
            os.close(child)
    return False


def _walk_path(
    path: Path, depth_left: int, budget: _Budget, only: frozenset[str] | None = None
) -> bool:
    """沒有 dir fd 的平台（Windows）：路徑寫法，同樣不跟符號連結與 junction。"""
    budget.enter_dir()
    with os.scandir(path) as listing:
        entries = _entries(listing, budget)
    if listing_kind(fold_name(name) for name, _ in entries):
        return True
    if depth_left == 0:
        return False
    for name in _children(entries, only):
        try:
            if _walk_path(path / name, depth_left - 1, budget):
                return True
        except (FileNotFoundError, NotADirectoryError):
            continue
    return False
