"""清除連備份：系統快照 zip 只改記憶相關的成員.

處理：主記憶庫與 zip 裡的升級備份只刪目標的列；拿掉目標的 Qdrant collection（含目錄項目）並同步
改 meta.json；拿掉中斷暫存成員。其他成員原樣串流照抄（升級備份資料夾裡作業系統放的雜檔先檢查內容，
見 os_metadata）。重寫後的驗證（_verify_rewrite）只用常數與原始 bytes 獨立重算，不呼叫這裡的分類器
或 meta 轉換函式。任一步失敗原檔不動、逐檔回報。
"""

from __future__ import annotations

import errno
import json
import logging
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from memory_server import factual
from memory_server.utils.collection_names import (
    QDRANT_COLLECTION_DIR,
    QDRANT_DIR_NAME,
    QDRANT_META_FILE,
    collection_name_for,
    names_fold_equal,
)

from src.models.identity import AgentIdentity
from src.models.memory_scrub import ScrubItemResult
from src.services import system_backup
from src.services.memory_scrub import os_metadata, qdrant_meta, swap
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

SNAPSHOT_DATA_DIR = "data"  # manage_backup 把資料根打包在 zip 的 data/ 底下
SNAPSHOT_VAULT_DIR = "vault"
SQLITE_SIDE_SUFFIXES = ("-journal", "-wal", "-shm")
_ENCRYPTED_FLAG = 0x1
_PERSONA_DIRS = ("soul", "custom")  # data/soul/<id>、data/custom/<id> 的目錄名也算已知 ID
# 一份快照裡所有記憶庫加總的上限（逐份處理，記憶體峰值只看單一份；這個上限管的是一份快照要跑多久）
MAX_SNAPSHOT_DB_BYTES = 4 * factual.MEMORY_SCRUB_MAX_IMAGE_BYTES
META_MEMBER = "/".join((SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR, QDRANT_DIR_NAME, QDRANT_META_FILE))


class _Plan(NamedTuple):
    databases: tuple[str, ...]
    target_members: frozenset[str]
    temp_members: frozenset[str]
    collection_dirs: frozenset[str]
    meta: str | None
    dir_ids: frozenset[str]
    legacy_jsonl: int
    os_files: frozenset[str]


def scrub_snapshots(
    identity: AgentIdentity, directory: Path, known_ids: Iterable[str]
) -> list[ScrubItemResult]:
    """處理快照目錄裡清單上的每一份快照（列不出目錄就往外丟，由呼叫端記成類別失敗）."""
    return list(iter_snapshots(identity, directory, known_ids))


def iter_snapshots(
    identity: AgentIdentity, directory: Path, known_ids: Iterable[str]
) -> Iterator[ScrubItemResult]:
    """同上，完成一份交出一份（協調器逐項記錄）."""
    known = set(known_ids)
    try:
        paths = system_backup.list_snapshot_files(directory, strict=True)
    except NotADirectoryError:  # 快照目錄本身是連結：記失敗，不能當成沒有快照
        yield ScrubItemResult(
            kind="snapshot", name=Path(directory).name, status="failed", reason="unknown_file"
        )
        return
    for path in paths:
        yield from scrub_snapshot(path, identity, known)


def scrub_snapshot(
    path: Path, identity: AgentIdentity, known_ids: set[str]
) -> list[ScrubItemResult]:
    """一份快照：回傳主項目（kind=snapshot）與資訊項."""
    return swap.guard_item("snapshot", path.name, lambda: _scrub_zip(path, identity, known_ids))


def _item(path: Path, status: str, reason: str | None = None, **fields: object) -> ScrubItemResult:
    return ScrubItemResult(kind="snapshot", name=path.name, status=status, reason=reason, **fields)


def _read_member(path: Path, name: str) -> bytes:
    """從 zip 讀一個成員；呼叫端一次只拿一份，處理完就放掉."""
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def _scrub_zip(path: Path, identity: AgentIdentity, known_ids: set[str]) -> list[ScrubItemResult]:
    if swap.is_read_only(path):
        return [_item(path, "failed", "read_only")]
    before = swap.stamp(path)
    if before.size == 0:
        return [_item(path, "not_needed", "empty_file")]
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        plan = _classify(infos, identity)
        sizes = {name: zf.getinfo(name).file_size for name in plan.databases}
        os_sizes = {name: zf.getinfo(name).file_size for name in plan.os_files}
        meta = zf.read(plan.meta) if plan.meta else None
    _check_os_members(path, os_sizes, identity)
    if (
        any(size > factual.MEMORY_SCRUB_MAX_IMAGE_BYTES for size in sizes.values())
        or sum(sizes.values()) > MAX_SNAPSHOT_DB_BYTES
    ):
        return [_item(path, "failed", "too_large")]
    target = collection_name_for(identity)
    needle = identity.agent_id.encode("utf-8", "surrogatepass")
    inspections: dict[str, factual.StoreInspection] = {}
    needed: list[str] = []
    for name in plan.databases:  # 一次一份：讀、檢查、放掉
        image = _read_member(path, name)
        inspections[name] = factual.inspect_store_image(image, identity)
        if inspections[name].needs_scrub(contains_id_bytes=needle in image):
            needed.append(name)
        del image
    ids = set(known_ids) | set(plan.dir_ids)
    for found in inspections.values():
        ids |= found.agent_ids
    meta_names = qdrant_meta.collection_names(meta) if meta is not None else set()
    if _shared(identity, target, ids, plan.collection_dirs | meta_names):
        return [_item(path, "failed", "collection_shared")]
    meta_lists_target = target in meta_names
    info = _info(path, plan, inspections)
    drop = plan.target_members | plan.temp_members
    if not needed and not drop and not meta_lists_target:
        return [_item(path, "not_needed"), *info]
    if not swap.has_space(path.parent, before.size + sum(sizes[name] for name in needed)):
        return [_item(path, "failed", "no_space")]
    temps: list[Path] = []
    try:
        replace: dict[str, Path | bytes] = {}
        removed = 0
        for index, name in enumerate(sorted(needed)):  # 一次清一份
            temp = swap.new_temp(path.parent, f"{path.name}.{index}")
            temps.append(temp)
            removed += factual.scrub_store_image(_read_member(path, name), identity, temp)
            replace[name] = temp
        if meta_lists_target and plan.meta is not None and meta is not None:
            replace[plan.meta] = qdrant_meta.without_collection(meta, target)
        output = swap.new_temp(path.parent, path.name)
        temps.append(output)
        _rewrite(path, output, drop, replace)
        _verify_rewrite(infos, path, output, target, identity)
        swap.replace_verified(path, output, before)
    finally:
        for temp in temps:
            swap.discard(temp)
    item = _item(
        path,
        "scrubbed",
        facts_removed=removed,
        collection_removed=bool(plan.target_members) or meta_lists_target,
        members_dropped=len(drop),
    )
    return [item, *info]


def _check_os_members(path: Path, sizes: dict[str, int], identity: AgentIdentity) -> None:
    """名字像雜檔的成員：太大、是 SQLite 或含目標 ID 就當不認得的成員.

    先看宣告的大小，太大的不讀；zipfile 讀出的不會超過宣告的大小（超過就是 CRC 錯）。一次讀一份。
    """
    for name in sorted(sizes):
        if sizes[name] > os_metadata.MAX_OS_FILE_BYTES:
            raise swap.ScrubItemFailure("unknown_member")
        if not os_metadata.is_os_metadata_content(_read_member(path, name), identity):
            raise swap.ScrubItemFailure("unknown_member")


def _rewrite(
    path: Path, output: Path, drop: frozenset[str], replace: dict[str, Path | bytes]
) -> None:
    try:
        resource_manager.rewrite_archive(path, output, drop=set(drop), replace=replace)
    except OSError as exc:  # 寫不出新 zip：原檔沒動
        reason = "no_space" if exc.errno == errno.ENOSPC else "write_failed"
        raise swap.ScrubItemFailure(reason) from exc


def _shared(
    identity: AgentIdentity, target: str, ids: set[str], collection_names: set[str]
) -> bool:
    """目標的 collection 名稱和別的 ID、快照裡別的目錄或 meta.json 的鍵撞名（含不分大小寫）."""
    for other in ids:
        if other != identity.agent_id and names_fold_equal(
            collection_name_for(AgentIdentity(agent_id=other)), target
        ):
            return True
    return any(name != target and names_fold_equal(name, target) for name in collection_names)


def _info(
    path: Path, plan: _Plan, inspections: dict[str, factual.StoreInspection]
) -> list[ScrubItemResult]:
    count = plan.legacy_jsonl + sum(found.legacy_rows for found in inspections.values())
    if not count:
        return []
    return [
        ScrubItemResult(
            kind="info", name=path.name, status="info", reason="out_of_scope", count=count
        )
    ]


def _exact(actual: str, expected: str) -> bool:
    """名稱完全相同才算；只差大小寫（Windows 還原時會落到同一個位置）就 fail closed."""
    if actual == expected:
        return True
    if names_fold_equal(actual, expected):
        raise swap.ScrubItemFailure("unknown_member")
    return False


def _classify(infos: list[zipfile.ZipInfo], identity: AgentIdentity) -> _Plan:
    """把成員分成：要處理的資料庫、目標 collection、可拿掉的暫存、meta.json 與其他."""
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise swap.ScrubItemFailure("ambiguous_member")
    target = collection_name_for(identity)
    databases: list[str] = []
    target_members: set[str] = set()
    temp_members: set[str] = set()
    collection_dirs: set[str] = set()
    dir_ids: set[str] = set()
    os_files: set[str] = set()
    meta: str | None = None
    legacy_jsonl = 0
    for info in infos:
        if info.flag_bits & _ENCRYPTED_FLAG:
            raise swap.ScrubItemFailure("unreadable")
        if "\\" in info.filename:
            raise swap.ScrubItemFailure("unknown_member")
        parts = PurePosixPath(info.filename).parts
        if len(parts) < 2 or not _exact(parts[0], SNAPSHOT_DATA_DIR):
            continue
        if parts[1] in _PERSONA_DIRS and len(parts) >= 3:
            dir_ids.add(parts[2])
            continue
        if not _exact(parts[1], SNAPSHOT_VAULT_DIR):
            continue
        rest = parts[2:]
        if not rest:
            continue
        if _exact(rest[0], factual.FACTS_DB_FILE_NAME):
            if len(rest) == 1:
                databases.append(info.filename)
            continue
        if any(
            _exact(rest[0], factual.FACTS_DB_FILE_NAME + suffix) for suffix in SQLITE_SIDE_SUFFIXES
        ):
            raise swap.ScrubItemFailure("has_journal")
        if _exact(rest[0], factual.FACTS_BACKUP_DIR_NAME):
            _classify_backup_member(info, rest, databases, temp_members, os_files)
            continue
        if _exact(rest[0], QDRANT_DIR_NAME):
            meta = (
                _classify_qdrant_member(info, rest, target, target_members, collection_dirs) or meta
            )
            continue
        if (
            len(rest) >= 3
            and rest[0] == identity.agent_id
            and rest[1] == "fact"
            and rest[2].startswith("facts.jsonl")
        ):
            legacy_jsonl += 1
    return _Plan(
        databases=tuple(databases),
        target_members=frozenset(target_members),
        temp_members=frozenset(temp_members),
        collection_dirs=frozenset(collection_dirs),
        meta=meta,
        dir_ids=frozenset(dir_ids),
        legacy_jsonl=legacy_jsonl,
        os_files=frozenset(os_files),
    )


def _classify_backup_member(
    info: zipfile.ZipInfo,
    rest: tuple[str, ...],
    databases: list[str],
    temp_members: set[str],
    os_files: set[str],
) -> None:
    if len(rest) == 1:  # 資料夾項目本身；和資料夾同名的檔案不認得
        if not info.is_dir():
            raise swap.ScrubItemFailure("unknown_member")
        return
    if len(rest) != 2:
        raise swap.ScrubItemFailure("unknown_member")
    kind = factual.classify_upgrade_backup_name(rest[1])
    if kind == "backup" and not info.is_dir():
        databases.append(info.filename)
    elif kind in ("upgrade_tmp", "scrub_tmp"):
        temp_members.add(info.filename)
    elif os_metadata.is_os_metadata_name(rest[1]) and not info.is_dir():
        os_files.add(info.filename)  # 內容由 _check_os_members 檢查
    else:
        raise swap.ScrubItemFailure("unknown_member")


def _classify_qdrant_member(
    info: zipfile.ZipInfo,
    rest: tuple[str, ...],
    target: str,
    target_members: set[str],
    collection_dirs: set[str],
) -> str | None:
    if len(rest) == 2 and _exact(rest[1], QDRANT_META_FILE):
        return info.filename
    if len(rest) >= 3 and _exact(rest[1], QDRANT_COLLECTION_DIR):
        collection_dirs.add(rest[2])
        if rest[2] == target:
            target_members.add(info.filename)
    return None


# --- 獨立驗證（不呼叫上面的分類器與 meta 轉換） --------------------------------------


def _may_drop(name: str, target: str) -> bool:
    parts = PurePosixPath(name).parts
    if parts[:5] == (
        SNAPSHOT_DATA_DIR,
        SNAPSHOT_VAULT_DIR,
        QDRANT_DIR_NAME,
        QDRANT_COLLECTION_DIR,
        target,
    ):
        return True
    return (
        len(parts) == 4
        and parts[:3] == (SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR, factual.FACTS_BACKUP_DIR_NAME)
        and factual.classify_upgrade_backup_name(parts[3]) in ("upgrade_tmp", "scrub_tmp")
    )


def _collection_dir_names(names: Iterable[str]) -> set[str]:
    prefix = (SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR, QDRANT_DIR_NAME, QDRANT_COLLECTION_DIR)
    found = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) >= 5 and parts[:4] == prefix:
            found.add(parts[4])
    return found


def _expected_meta(original: bytes, target: str) -> dict:
    raw = json.loads(original)
    if raw.get("aliases"):
        raise swap.ScrubItemFailure("verify_failed")
    collections = {k: v for k, v in raw["collections"].items() if k != target}
    return {**raw, "collections": collections}


def _member_key(info: zipfile.ZipInfo) -> tuple:
    return (info.CRC, info.file_size, info.date_time, info.compress_type, info.external_attr)


def _verify_rewrite(
    original_infos: list[zipfile.ZipInfo],
    original_zip: Path,
    new_zip: Path,
    target: str,
    identity: AgentIdentity,
) -> None:
    """重寫後逐項核對；不符就 verify_failed（原檔還沒動）.

    依常數版面自己找出記憶庫成員與 meta.json，不看分類器與清除時的對照表：
    - 每一份記憶庫成員在新 zip 裡目標都是 0 筆；內容有變的，非目標列的多重集合要和原本同名成員相同；
    - 升級備份資料夾裡名字像雜檔的成員不能有變，內容自己重驗（不是 SQLite、不含目標 ID）；
      這個資料夾裡留下來的成員只能是記憶庫或雜檔；
    - 其他內容有變的成員只能是 meta.json，而且要等於「原本拿掉目標」。
    """
    with zipfile.ZipFile(new_zip) as zf:
        if zf.testzip() is not None:
            raise swap.ScrubItemFailure("verify_failed")
        new_infos = zf.infolist()
    original_names = [info.filename for info in original_infos]
    new_names = [info.filename for info in new_infos]
    kept = set(new_names)
    if [name for name in original_names if name in kept] != new_names:
        raise swap.ScrubItemFailure("verify_failed")
    if any(not _may_drop(name, target) for name in original_names if name not in kept):
        raise swap.ScrubItemFailure("verify_failed")
    if any(
        PurePosixPath(name).parts[:5]
        == (SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR, QDRANT_DIR_NAME, QDRANT_COLLECTION_DIR, target)
        for name in new_names
    ):
        raise swap.ScrubItemFailure("verify_failed")
    if _collection_dir_names(new_names) != _collection_dir_names(original_names) - {target}:
        raise swap.ScrubItemFailure("verify_failed")
    by_name = {info.filename: info for info in original_infos}
    for info in new_infos:
        changed = _member_key(info) != _member_key(by_name[info.filename])
        if _is_os_member(info.filename):
            if changed:
                raise swap.ScrubItemFailure("verify_failed")
            _verify_os_member(new_zip, info, identity)
        elif _is_store_member(info.filename):
            _verify_store_member(original_zip, new_zip, info.filename, identity, changed=changed)
        elif info.filename == META_MEMBER:
            if changed:
                _verify_meta(original_zip, new_zip, target)
        elif _in_backup_folder(info.filename) or changed:  # 資料夾裡的其他名字、或有變的其他成員
            raise swap.ScrubItemFailure("verify_failed")
    if META_MEMBER in by_name and META_MEMBER not in kept:
        raise swap.ScrubItemFailure("verify_failed")


def _verify_meta(original_zip: Path, new_zip: Path, target: str) -> None:
    expected = _expected_meta(_read_member(original_zip, META_MEMBER), target)
    if json.loads(_read_member(new_zip, META_MEMBER)) != expected:
        raise swap.ScrubItemFailure("verify_failed")


def _in_backup_folder(name: str) -> bool:
    """升級備份資料夾底下的成員（不含資料夾項目本身）."""
    parts = PurePosixPath(name).parts
    return len(parts) > 3 and parts[:3] == (
        SNAPSHOT_DATA_DIR,
        SNAPSHOT_VAULT_DIR,
        factual.FACTS_BACKUP_DIR_NAME,
    )


def _is_os_member(name: str) -> bool:
    """依常數版面認升級備份資料夾裡名字像雜檔的成員（不經分類器）."""
    parts = PurePosixPath(name).parts
    return (
        len(parts) == 4
        and parts[:3] == (SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR, factual.FACTS_BACKUP_DIR_NAME)
        and parts[3].isascii()
        and parts[3].lower() in os_metadata.OS_METADATA_NAMES
    )


def _verify_os_member(new_zip: Path, info: zipfile.ZipInfo, identity: AgentIdentity) -> None:
    """只用 os_metadata 的常數重算，不呼叫它的內容判斷；先看新 zip 宣告的大小，太大的不讀."""
    if info.file_size > os_metadata.MAX_OS_FILE_BYTES:
        raise swap.ScrubItemFailure("verify_failed")
    content = _read_member(new_zip, info.filename)
    needles = [identity.agent_id.encode(enc, "surrogatepass") for enc in os_metadata.ID_ENCODINGS]
    if content.startswith(os_metadata.SQLITE_HEADER) or any(
        needle in content for needle in needles
    ):
        raise swap.ScrubItemFailure("verify_failed")


def _is_store_member(name: str) -> bool:
    """依常數版面認記憶庫成員：主庫，或升級備份資料夾裡的 *.db（不經分類器）."""
    parts = PurePosixPath(name).parts
    root = (SNAPSHOT_DATA_DIR, SNAPSHOT_VAULT_DIR)
    if parts == (*root, factual.FACTS_DB_FILE_NAME):
        return True
    return (
        len(parts) == 4
        and parts[:3] == (*root, factual.FACTS_BACKUP_DIR_NAME)
        and parts[3].endswith(".db")
    )


def _verify_store_member(
    original_zip: Path, new_zip: Path, name: str, identity: AgentIdentity, *, changed: bool
) -> None:
    """新 zip 裡的記憶庫成員目標必須 0 筆；有改的，新舊各重算一次指紋比對（一次只讀一份）."""
    try:
        after = factual.inspect_store_image(_read_member(new_zip, name), identity)
        before = (
            factual.inspect_store_image(_read_member(original_zip, name), identity)
            if changed
            else after
        )
    except factual.MemoryScrubError as exc:
        raise swap.ScrubItemFailure("verify_failed") from exc
    if (
        after.target_rows != 0
        or after.others_digest != before.others_digest
        or after.user_version != before.user_version
    ):
        raise swap.ScrubItemFailure("verify_failed")
