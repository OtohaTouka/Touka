"""快照目錄裡中斷留下的暫存：自家的清除暫存，以及快照腳本（manage_backup）的中斷暫存.

只刪完全符合形狀的；不符合就不碰、回報 unknown_file。快照腳本的暫存還要夠舊（命令列做的快照
不經 Proxy 的鎖，可能正在寫），內容也要像快照腳本寫的。常數和腳本裡的同值，由測試核對。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from memory_server.factual import SCRUB_TMP_RE

from src.models.memory_scrub import ScrubItemResult
from src.services.memory_scrub import swap
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

P1_PARTIAL_SUFFIX = ".partial"  # 同 manage_backup.PARTIAL_SUFFIX
P1_WORKDIR_PREFIX = ".snapshot-"  # 同 manage_backup.SNAPSHOT_WORKDIR_PREFIX
LEFTOVER_MIN_AGE_S = 600  # 快照腳本的暫存要比清除開始早這麼多秒才算中斷殘留
_ZIP_HEADER = b"PK\x03\x04"
_PARTIAL_RE = re.compile(r"^\..+\.[A-Za-z0-9_]{8}" + re.escape(P1_PARTIAL_SUFFIX) + r"$")
_WORKDIR_RE = re.compile("^" + re.escape(P1_WORKDIR_PREFIX) + r"[A-Za-z0-9_]{8}$")
_WORK_FILE_RE = re.compile(r"^tmp[a-z0-9_]{8}\.sqlite(?:-journal)?$")


def sweep_snapshot_leftovers(directory: Path, started_at: float) -> list[ScrubItemResult]:
    """同 ``iter_snapshot_leftovers``，一次回傳全部（測試與單獨呼叫用）."""
    return list(iter_snapshot_leftovers(directory, started_at))


def iter_snapshot_leftovers(directory: Path, started_at: float) -> Iterator[ScrubItemResult]:
    """清快照目錄頂層的中斷暫存，完成一項交出一項（名稱不符任何樣式的檔不列）.

    快照腳本的暫存一組判斷：只要其中任何一個（含工作資料夾裡的檔）比門檻新，就可能有命令列快照
    正在跑，這一輪全部不碰。目錄本身是連結時記失敗、不跟進去。
    """
    problem = swap.folder_problem("leftover", directory)
    if problem is not None:
        yield problem
        return
    if not swap.regular_dir(directory):
        return
    deadline = started_at - LEFTOVER_MIN_AGE_S
    entries = sorted(directory.iterdir(), key=lambda p: p.name)
    snapshot_temps = [e for e in entries if _PARTIAL_RE.match(e.name) or _WORKDIR_RE.match(e.name)]
    busy = any(_too_new(entry, deadline) for entry in snapshot_temps)
    for entry in entries:
        # 列出之後才消失（快照剛發布、暫存改名成正式快照）就跳過：新的正式快照會在同一個清除
        # 工作稍後的快照類別被清到（舊審查報告丙-3）
        if swap.missing(entry):
            continue
        try:
            result = _sweep_one(entry, busy=busy)
        except FileNotFoundError:
            if swap.missing(entry):
                continue
            # 這一項還在（例如工作資料夾刪到一半、裡面的檔被別人先刪掉）：裡面可能還有記憶庫副本，
            # 不能當成沒事，記成刪不掉
            result = ScrubItemResult(
                kind="leftover", name=entry.name, status="failed", reason="in_use"
            )
        if result is not None:
            yield result


def _sweep_one(entry: Path, *, busy: bool) -> ScrubItemResult | None:
    """清一項；名稱不符任何樣式回 None。處理到一半消失時 FileNotFoundError 往上丟."""
    if SCRUB_TMP_RE.match(entry.name):
        ok = swap.regular_file(entry)
        return _remove(entry, directory=False) if ok else _unknown(entry)
    if _PARTIAL_RE.match(entry.name):
        ok = not busy and _partial_ok(entry)
        return _remove(entry, directory=False) if ok else _unknown(entry)
    if _WORKDIR_RE.match(entry.name):
        ok = not busy and _workdir_ok(entry)
        return _remove(entry, directory=True) if ok else _unknown(entry)
    return None


def _too_new(path: Path, deadline: float) -> bool:
    """這個暫存（工作資料夾連同裡面的檔）有沒有比門檻新的；中途消失的部分不算."""
    try:
        if path.lstat().st_mtime > deadline:
            return True
        if not swap.regular_dir(path):
            return False
        children = list(path.iterdir())
    except FileNotFoundError:
        return False
    return any(_mtime(entry) > deadline for entry in children)


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except FileNotFoundError:
        return float("-inf")


def _partial_ok(path: Path) -> bool:
    if not swap.regular_file(path):
        return False
    head = resource_manager.read_file_prefix(path, len(_ZIP_HEADER))
    return head in (b"", _ZIP_HEADER)


def _workdir_ok(path: Path) -> bool:
    if not swap.regular_dir(path):
        return False
    return all(
        _WORK_FILE_RE.match(entry.name) and swap.regular_file(entry) for entry in path.iterdir()
    )


def _remove(path: Path, *, directory: bool) -> ScrubItemResult:
    try:
        if directory:
            resource_manager.remove_dir(path)
        else:
            resource_manager.remove_file(path)
    except FileNotFoundError:
        raise  # 已經不見：交給呼叫端跳過，不算刪不掉
    except OSError:
        logger.warning("[memory-scrub] could not remove a leftover: %s", path.name, exc_info=True)
        return ScrubItemResult(kind="leftover", name=path.name, status="failed", reason="in_use")
    return ScrubItemResult(kind="leftover", name=path.name, status="removed")


def _unknown(path: Path) -> ScrubItemResult:
    return ScrubItemResult(kind="leftover", name=path.name, status="failed", reason="unknown_file")
