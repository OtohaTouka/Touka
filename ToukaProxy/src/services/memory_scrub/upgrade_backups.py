"""清除連備份：升級備份（vault 的 core_memory_backups，所有靈魂共用）只刪目標的列.

全程持有升級備份鎖（和升級的備份＋輪替互斥）。每份都在記憶體裡處理，輸出到同資料夾的暫存，
驗證後原子取代、保留原檔的時間與權限；任一步失敗原檔不動、逐檔回報。作業系統放的雜檔
（os_metadata）原樣留著、記成資訊。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memory_server import factual

from src.models.memory_scrub import ScrubItemResult
from src.services.memory_scrub import os_metadata, swap
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def scrub_upgrade_backups(identity: AgentIdentity, directory: Path) -> list[ScrubItemResult]:
    """處理升級備份資料夾頂層的每個檔；等不到升級備份鎖丟 MemoryScrubError(lock_timeout)."""
    return list(iter_upgrade_backups(identity, directory))


def iter_upgrade_backups(identity: AgentIdentity, directory: Path) -> Iterator[ScrubItemResult]:
    """同上，完成一項交出一項（協調器逐項記錄；中途出錯時已完成的不會遺失）."""
    with factual.upgrade_backup_lock(timeout=factual.FACTS_SCHEMA_LOCK_TIMEOUT_S):
        problem = swap.folder_problem("upgrade_backup", directory)
        if problem is not None:  # 資料夾本身是連結：記失敗，不能當成沒有升級備份
            yield problem
            return
        if not swap.regular_dir(directory):
            return
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            kind = factual.classify_upgrade_backup_name(entry.name)
            if kind in ("upgrade_tmp", "scrub_tmp") and swap.regular_file(entry):
                yield _remove_leftover(entry)
            elif kind == "backup" and swap.regular_file(entry):
                yield from _scrub_one(entry, identity)
            elif os_metadata.is_os_metadata_name(entry.name) and swap.regular_file(entry):
                yield from _note_os_file(entry, identity)
            else:
                yield ScrubItemResult(
                    kind="upgrade_backup", name=entry.name, status="failed", reason="unknown_file"
                )


def _remove_leftover(path: Path) -> ScrubItemResult:
    try:
        resource_manager.remove_file(path)
    except OSError:
        logger.warning("[memory-scrub] could not remove a leftover: %s", path.name, exc_info=True)
        return ScrubItemResult(kind="leftover", name=path.name, status="failed", reason="in_use")
    return ScrubItemResult(kind="leftover", name=path.name, status="removed")


def _failed(path: Path, reason: str) -> list[ScrubItemResult]:
    return [ScrubItemResult(kind="upgrade_backup", name=path.name, status="failed", reason=reason)]


def _note_os_file(path: Path, identity: AgentIdentity) -> list[ScrubItemResult]:
    return swap.guard_item("upgrade_backup", path.name, lambda: _check_os_file(path, identity))


def _check_os_file(path: Path, identity: AgentIdentity) -> list[ScrubItemResult]:
    """名字像雜檔：太大、是 SQLite 或含目標 ID 就照舊當不認得的檔；其餘不動、記成資訊."""
    if swap.stamp(path).size > os_metadata.MAX_OS_FILE_BYTES:
        return _failed(path, "unknown_file")
    if not os_metadata.is_os_metadata_content(resource_manager.read_file_binary(path), identity):
        return _failed(path, "unknown_file")
    return [ScrubItemResult(kind="info", name=path.name, status="info", reason="os_metadata_file")]


def _scrub_one(path: Path, identity: AgentIdentity) -> list[ScrubItemResult]:
    return swap.guard_item("upgrade_backup", path.name, lambda: _scrub_file(path, identity))


def _scrub_file(path: Path, identity: AgentIdentity) -> list[ScrubItemResult]:
    if swap.is_read_only(path):
        return _failed(path, "read_only")
    before = swap.stamp(path)
    if before.size > factual.MEMORY_SCRUB_MAX_IMAGE_BYTES:
        return _failed(path, "too_large")
    image = resource_manager.read_file_binary(path)
    found = factual.inspect_store_image(image, identity)
    info = _out_of_scope(path, found.legacy_rows)
    contains_id = identity.agent_id.encode("utf-8", "surrogatepass") in image
    if not found.needs_scrub(contains_id_bytes=contains_id):
        return [ScrubItemResult(kind="upgrade_backup", name=path.name, status="not_needed"), *info]
    if not swap.has_space(path.parent, before.size * 2):
        return _failed(path, "no_space")
    temp = swap.new_temp(path.parent, path.name)
    try:
        removed = factual.scrub_store_image(image, identity, temp)
        del image
        swap.replace_verified(path, temp, before)
    finally:
        swap.discard(temp)
    item = ScrubItemResult(
        kind="upgrade_backup", name=path.name, status="scrubbed", facts_removed=removed
    )
    return [item, *info]


def _out_of_scope(path: Path, legacy_rows: int) -> list[ScrubItemResult]:
    if not legacy_rows:
        return []
    return [
        ScrubItemResult(
            kind="info", name=path.name, status="info", reason="out_of_scope", count=legacy_rows
        )
    ]
