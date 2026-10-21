"""清除連備份：系統快照（S 組，對照設計 §12-2）.

快照都是測試在 tmp_path 建的合成 zip；S01／S20 用第 6 段的 BackupEngine 實際打包。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pyzipper
from memory_server import factual as fm
from memory_server.utils import collection_names as cn
from qdrant_client import QdrantClient
from qdrant_client.local import qdrant_local
from src.models.identity import AgentIdentity
from src.services import backup_scheduler_service as scheduler
from src.services import system_backup
from src.services.memory_scrub import leftovers, qdrant_meta, snapshots, swap
from src.services.memory_scrub import orchestrator as SO
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

from tests.helpers.memory_scrub import (
    OTHER,
    TARGET,
    V2_MARKERS,
    assert_no_breach,
    delete_rows,
    fake_links,
    install_scrub_sandbox,
    load_manage_backup,
    make_db,
    make_qdrant,
    make_v2_lifecycle_db,
    other_rows,
    other_rows_digest,
    pragma,
    rows,
    sha256,
    wait_scrub_idle,
    zip_members,
    zip_read,
    zip_tree,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

IDENT = AgentIdentity(agent_id=TARGET)
TARGET_COLLECTION = cn.collection_name_for(IDENT)
OTHER_COLLECTION = cn.collection_name_for(AgentIdentity(agent_id=OTHER))
MAIN = "data/vault/core_memory.db"
META = "data/vault/_qdrant_db/meta.json"


@pytest.fixture(autouse=True)
def backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    assert wait_scrub_idle(0) == [], "a previous test left the scrub flag or upgrade lock held"
    yield install_scrub_sandbox(monkeypatch, tmp_path)
    assert wait_scrub_idle() == [], "a scrub thread was still holding the upgrade lock"
    assert_no_breach()


def _data_root(
    tmp_path: Path, *, collections: dict[str, list[str]] | None = None, schema: str = "v1"
) -> Path:
    data = tmp_path / "data-src"
    vault = data / "vault"
    make_db(
        vault / "core_memory.db",
        [
            ("fact_t1", TARGET, "target one"),
            ("fact_t2", TARGET, "target two"),
            ("fact_o1", OTHER, "other one"),
        ],
        schema=schema,
    )
    backups_dir = vault / fm.FACTS_BACKUP_DIR_NAME
    for epoch in (1_700_000_001, 1_700_000_002):
        make_db(
            backups_dir / f"core_memory.v0-to-v1.{epoch}.db",
            [(f"fact_t{epoch}", TARGET, "old target"), (f"fact_o{epoch}", OTHER, "old other")],
            schema="v0",
        )
    make_qdrant(
        vault / cn.QDRANT_DIR_NAME,
        collections
        if collections is not None
        else {TARGET_COLLECTION: ["fact_t1", "fact_t2"], OTHER_COLLECTION: ["fact_o1"]},
    )
    (data / "soul" / TARGET).mkdir(parents=True)
    (data / "soul" / TARGET / "soul.md").write_text("persona", encoding="utf-8")
    (data / "ui_sessions.db").write_bytes(b"sessions-bytes")
    return data


def _snapshot(
    backups: Path, tmp_path: Path, name: str = "Touka_Backup_s.zip", **kwargs: object
) -> Path:
    path = zip_tree(_data_root(tmp_path, **kwargs), backups / name)
    os.utime(path, ns=(1_700_000_000 * 10**9, 1_700_000_000 * 10**9 + 7))
    return path


def _items(path: Path, known: set[str] | None = None) -> list:
    return snapshots.scrub_snapshot(path, IDENT, known or set())


def _main(items: list) -> object:
    return next(i for i in items if i.kind == "snapshot")


def _temps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if ".memscrub-" in p.name]


def _extract(path: Path, prefix: str, dest: Path) -> Path:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.filename.startswith(prefix) and not info.is_dir():
                target = dest / info.filename[len(prefix) :]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
    return dest


def _db_from_zip(path: Path, member: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(zip_read(path, member))
    return dest


def _rewrite_zip(path: Path, transform) -> None:
    """測試用：重建 zip，把成員清單交給 transform 改（注入故障）."""
    with zipfile.ZipFile(path) as zf:
        members = [(info, zf.read(info)) for info in zf.infolist()]
    members = transform(members)
    with zipfile.ZipFile(path, "w") as zf:
        for info, data in members:
            zf.writestr(info, data)


# --- S01 用 P1 的 BackupEngine 實際打包 ---------------------------------------


def _engine_snapshot(tmp_path: Path, backups: Path) -> Path:
    mb = load_manage_backup()
    data = _data_root(tmp_path)
    env = tmp_path / "env" / ".env"
    env.parent.mkdir()
    env.write_text("NORMAL=1\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    resolver = mb.PathResolver(str(tmp_path), env_path=env, data_root=data, config_dir=config)
    return mb.BackupEngine(resolver).run_backup(backups, "Touka_Backup")


def test_s01_engine_snapshot_is_scrubbed_and_everything_else_is_kept(
    tmp_path: Path, backups: Path
) -> None:
    path = _engine_snapshot(tmp_path, backups)
    os.utime(path, ns=(1_700_000_000 * 10**9, 1_700_000_000 * 10**9 + 7))
    before = {m[0]: m for m in zip_members(path)}
    mtime = path.stat().st_mtime_ns
    old_meta = json.loads(zip_read(path, META))
    main_others = other_rows_digest(_db_from_zip(path, MAIN, tmp_path / "before" / "main.db"))
    item = _main(_items(path))
    assert (item.status, item.reason, item.collection_removed) == ("scrubbed", None, True)
    assert item.facts_removed == 4
    after = {m[0]: m for m in zip_members(path)}
    dropped = set(before) - set(after)
    assert dropped and all(
        f"/collection/{TARGET_COLLECTION}/" in n or n.endswith(f"/{TARGET_COLLECTION}")
        for n in dropped
    )
    changed = {n for n in after if after[n] != before[n]}
    assert changed <= {MAIN, META} | {n for n in after if "/core_memory_backups/" in n}
    assert after["data/ui_sessions.db"] == before["data/ui_sessions.db"]
    new_meta = json.loads(zip_read(path, META))
    assert set(new_meta["collections"]) == set(old_meta["collections"]) - {TARGET_COLLECTION}
    for member in [MAIN] + [n for n in after if n.endswith(".db") and "/core_memory_backups/" in n]:
        db = _db_from_zip(path, member, tmp_path / "after" / Path(member).name)
        assert rows(db, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
    assert (
        other_rows_digest(_db_from_zip(path, MAIN, tmp_path / "after2" / "main.db")) == main_others
    )
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
    assert path.stat().st_mtime_ns == mtime
    assert _temps(backups) == []


def test_s02_qdrant_local_opens_the_scrubbed_snapshot(tmp_path: Path, backups: Path) -> None:
    path = _snapshot(backups, tmp_path)
    _items(path)
    qdrant_dir = _extract(path, "data/vault/_qdrant_db/", tmp_path / "restored-qdrant")
    client = QdrantClient(path=str(qdrant_dir))
    try:
        assert not client.collection_exists(TARGET_COLLECTION)
        assert client.count(OTHER_COLLECTION).count == 1
        assert [
            str(p.id) for p in client.retrieve(OTHER_COLLECTION, ids=[cn.fact_point_id("fact_o1")])
        ] == [cn.fact_point_id("fact_o1")]
    finally:
        client.close()


def test_s03_snapshot_without_target_is_left_alone(tmp_path: Path, backups: Path) -> None:
    data = tmp_path / "clean"
    make_db(data / "vault" / "core_memory.db", [("fact_o", OTHER, "other")])
    path = zip_tree(data, backups / "Touka_Backup_clean.zip")
    digest = sha256(path)
    assert _main(_items(path)).status == "not_needed"
    assert sha256(path) == digest


def test_s04_only_the_exact_collection_is_dropped(tmp_path: Path, backups: Path) -> None:
    siblings = {
        TARGET_COLLECTION: ["fact_t1"],
        TARGET_COLLECTION + "2": ["fact_x"],
        TARGET_COLLECTION + "_x": ["fact_y"],
    }
    path = _snapshot(backups, tmp_path, collections=siblings)
    _items(path)
    names = [m[0] for m in zip_members(path)]
    assert not any(f"/{TARGET_COLLECTION}/" in n for n in names)
    assert any(f"/{TARGET_COLLECTION}2/" in n for n in names)
    assert any(f"/{TARGET_COLLECTION}_x/" in n for n in names)
    assert set(json.loads(zip_read(path, META))["collections"]) == {
        TARGET_COLLECTION + "2",
        TARGET_COLLECTION + "_x",
    }


@pytest.mark.parametrize(
    ("target", "known", "extra_dir"),
    [
        ("zz-a-b", {"zz_a_b"}, None),
        ("zz-case", set(), "memory_zz_Case"),
        ("zzı", {"zzi"}, None),
        ("zzς", {"zzσ"}, None),
        ("zzK", {"zzk"}, None),
    ],
)
def test_s05_shared_collection_is_refused(
    tmp_path: Path, backups: Path, target: str, known: set[str], extra_dir: str | None
) -> None:
    identity = AgentIdentity(agent_id=target)
    data = tmp_path / "shared"
    make_db(data / "vault" / "core_memory.db", [("fact_1", target, "mine")])
    collections = {cn.collection_name_for(identity): ["fact_1"]}
    if extra_dir:
        collections[extra_dir] = []
    make_qdrant(data / "vault" / cn.QDRANT_DIR_NAME, collections)
    path = zip_tree(data, backups / "Touka_Backup_shared.zip")
    digest = sha256(path)
    item = _main(snapshots.scrub_snapshot(path, identity, known))
    assert (item.status, item.reason) == ("failed", "collection_shared")
    assert sha256(path) == digest


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"data/vault/core_memory.db-journal": b"j"}, "has_journal"),
        ({"data/vault/core_memory.db-wal": b"w"}, "has_journal"),
        ({"data/vault/core_memory_backups/manual-copy.db": b"x"}, "unknown_member"),
        ({"data/vault/core_memory_backups/deeper/x.db": b"x"}, "unknown_member"),
        ({"data/VAULT/core_memory.db": b"x"}, "unknown_member"),
    ],
)
def test_s06_s07_unsafe_members_fail_closed(
    tmp_path: Path, backups: Path, extra: dict[str, bytes], reason: str
) -> None:
    path = zip_tree(_data_root(tmp_path), backups / "Touka_Backup_u.zip", extra=extra)
    digest = sha256(path)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", reason)
    assert sha256(path) == digest


def test_s08_duplicate_members(tmp_path: Path, backups: Path) -> None:
    path = zip_tree(_data_root(tmp_path), backups / "Touka_Backup_dup.zip")
    with pytest.warns(UserWarning), zipfile.ZipFile(path, "a") as zf:
        zf.writestr(MAIN, b"second copy")
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "ambiguous_member")


def test_s09_broken_zips_do_not_stop_the_others(tmp_path: Path, backups: Path) -> None:
    (backups / "Touka_Backup_broken.zip").write_bytes(b"PK\x03\x04 not really")
    (backups / "Touka_Backup_text.zip").write_text("hello", encoding="utf-8")
    good = _snapshot(backups, tmp_path, name="Touka_Backup_good.zip")
    items = {
        i.name: i for i in snapshots.scrub_snapshots(IDENT, backups, set()) if i.kind == "snapshot"
    }
    assert (items["Touka_Backup_broken.zip"].status, items["Touka_Backup_broken.zip"].reason) == (
        "failed",
        "unreadable",
    )
    assert (items["Touka_Backup_text.zip"].status, items["Touka_Backup_text.zip"].reason) == (
        "failed",
        "unreadable",
    )
    assert items[good.name].status == "scrubbed"


@pytest.mark.parametrize(
    "meta",
    [
        {"collections": {TARGET_COLLECTION: {}}, "aliases": {}, "extra": 1},
        {"collections": {TARGET_COLLECTION: {}}, "aliases": {"alias": TARGET_COLLECTION}},
    ],
    ids=["extra-key", "aliases"],
)
def test_s10_unknown_meta_fails_closed(tmp_path: Path, backups: Path, meta: dict) -> None:
    data = _data_root(tmp_path)
    (data / "vault" / cn.QDRANT_DIR_NAME / cn.QDRANT_META_FILE).write_text(
        json.dumps(meta), encoding="utf-8"
    )
    path = zip_tree(data, backups / "Touka_Backup_meta.zip")
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "unknown_qdrant_meta")


def test_s11_no_space(tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)
    monkeypatch.setattr(
        swap.shutil, "disk_usage", lambda _p: SimpleNamespace(total=10**9, free=10, used=0)
    )
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "no_space")
    assert sha256(path) == digest
    assert _temps(backups) == []


def test_s12_in_use(tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)

    def locked(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(resource_manager, "move_file", locked)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "in_use")
    assert sha256(path) == digest
    assert _temps(backups) == []


def test_s13_changed_during_scrub(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    real = resource_manager.rewrite_archive

    def touch_after(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        os.utime(path, ns=(10**9, 2 * 10**9))

    monkeypatch.setattr(resource_manager, "rewrite_archive", touch_after)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "changed_during_scrub")
    assert path.stat().st_mtime_ns == 2 * 10**9
    assert _temps(backups) == []


@pytest.mark.parametrize("schema", ["ancient", "v0"])
def test_s14_old_store_versions(tmp_path: Path, backups: Path, schema: str) -> None:
    path = _snapshot(backups, tmp_path, schema=schema)
    assert _main(_items(path)).status == "scrubbed"
    db = _db_from_zip(path, MAIN, tmp_path / "s14" / "main.db")
    assert rows(db, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]


def test_s15_temp_members_are_dropped(tmp_path: Path, backups: Path) -> None:
    folder = "data/vault/core_memory_backups"
    extra = {
        f"{folder}/core_memory.v0-to-v1.1700000003.db.0123abcd.tmp": b"partial",
        f"{folder}/.core_memory.v0-to-v1.1700000003.db.memscrub-ab12cd34.tmp": b"ours",
    }
    path = zip_tree(_data_root(tmp_path), backups / "Touka_Backup_tmp.zip", extra=extra)
    item = _main(_items(path))
    names = {m[0] for m in zip_members(path)}
    assert not names & set(extra)
    assert item.members_dropped >= 2


def test_s16_residue_only_snapshot(tmp_path: Path, backups: Path) -> None:
    data = tmp_path / "residue"
    make_db(
        data / "vault" / "core_memory.db",
        [("fact_t", TARGET, "MARKER-S16 " + "q" * 5000), ("fact_o", OTHER, "other")],
    )
    delete_rows(data / "vault" / "core_memory.db", TARGET)
    path = zip_tree(data, backups / "Touka_Backup_res.zip")
    assert b"MARKER-S16" in zip_read(path, MAIN)
    assert _main(_items(path)).status == "scrubbed"
    with zipfile.ZipFile(path) as zf:
        assert not any(b"MARKER-S16" in zf.read(i) for i in zf.infolist() if not i.is_dir())


def test_s17_only_listed_snapshots_are_processed(tmp_path: Path, backups: Path) -> None:
    good = _snapshot(backups, tmp_path, name="Touka_Backup_ok.ZIP")
    nested = backups / "sub" / "nested.zip"
    nested.parent.mkdir()
    shutil.copyfile(good, nested)
    hidden = backups / ".hidden.zip"
    shutil.copyfile(good, hidden)
    nested_digest, hidden_digest = sha256(nested), sha256(hidden)
    items = [i for i in snapshots.scrub_snapshots(IDENT, backups, set()) if i.kind == "snapshot"]
    assert [(i.name, i.status) for i in items] == [("Touka_Backup_ok.ZIP", "scrubbed")]
    assert sha256(nested) == nested_digest
    assert sha256(hidden) == hidden_digest


# --- S18 中斷暫存 -------------------------------------------------------------


def _old(path: Path) -> Path:
    os.utime(path, (get_current_epoch() - 3600, get_current_epoch() - 3600))
    return path


def test_s18_leftovers_only_exact_old_shapes_are_removed(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = backups / ".Touka_Backup_x.zip.memscrub-ab12cd34.tmp"
    own.write_bytes(b"x")
    partial_empty = backups / f".Touka_Backup_1.ab12cd34{leftovers.P1_PARTIAL_SUFFIX}"
    partial_empty.write_bytes(b"")
    _old(partial_empty)
    partial_zip = backups / f".Touka_Backup_2.ef56gh78{leftovers.P1_PARTIAL_SUFFIX}"
    partial_zip.write_bytes(b"PK\x03\x04rest")
    _old(partial_zip)
    workdir = backups / f"{leftovers.P1_WORKDIR_PREFIX}ab12cd34"
    workdir.mkdir()
    (workdir / "tmpabcd1234.sqlite").write_bytes(b"db")
    _old(workdir / "tmpabcd1234.sqlite")
    _old(workdir)
    keep = {
        "dated": backups / f"{leftovers.P1_WORKDIR_PREFIX}20261015",
        "busy_dir": backups / f"{leftovers.P1_WORKDIR_PREFIX}zz99yy88",
        "not_zip": backups / f".Touka_Backup_4.mn34op56{leftovers.P1_PARTIAL_SUFFIX}",
        "short": backups / ".Touka_Backup_x.zip.memscrub-abc.tmp",
        "suffix": backups / ".Touka_Backup_x.zip.memscrub-ab12cd34.tmpx",
        "nodot": backups / "Touka_Backup_x.zip.memscrub-ab12cd34.tmp",
        "linkdir": backups / f"{leftovers.P1_WORKDIR_PREFIX}qr78st90",
    }
    keep["dated"].mkdir()
    (keep["dated"] / "notes.txt").write_text("human backup", encoding="utf-8")
    _old(keep["dated"] / "notes.txt")
    _old(keep["dated"])
    keep["busy_dir"].mkdir()
    (keep["busy_dir"] / "tmpabcd1234.sqlite").write_bytes(b"db")
    (keep["busy_dir"] / "other.txt").write_bytes(b"x")
    _old(keep["busy_dir"] / "tmpabcd1234.sqlite")
    _old(keep["busy_dir"] / "other.txt")
    _old(keep["busy_dir"])
    keep["not_zip"].write_bytes(b"hello")
    _old(keep["not_zip"])
    for name in ("short", "suffix", "nodot"):
        keep[name].write_bytes(b"x")
    keep["linkdir"].mkdir()
    _old(keep["linkdir"])
    real_lstat = Path.lstat

    def fake_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == keep["linkdir"]:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0, st_mtime=0)
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    items = {i.name: i for i in leftovers.sweep_snapshot_leftovers(backups, get_current_epoch())}
    for removed in (own, partial_empty, partial_zip, workdir):
        assert (items[removed.name].kind, items[removed.name].status) == ("leftover", "removed")
        assert not removed.exists()
    # 太新的暫存會讓整組都不碰，另見 S18c
    for name in ("dated", "busy_dir", "not_zip", "linkdir"):
        assert (items[keep[name].name].status, items[keep[name].name].reason) == (
            "failed",
            "unknown_file",
        ), name
    for path in keep.values():
        assert path.exists()
    for name in ("short", "suffix", "nodot"):
        assert keep[name].name not in items


def test_s18b_leftover_removal_failure_is_reported(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = backups / f".Touka_Backup_9.ab12cd34{leftovers.P1_PARTIAL_SUFFIX}"
    partial.write_bytes(b"")
    _old(partial)

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(resource_manager, "remove_file", refuse)
    items = leftovers.sweep_snapshot_leftovers(backups, get_current_epoch())
    assert [(i.kind, i.status) for i in items] == [("leftover", "failed")]
    assert partial.exists()


def test_s19_prune_order_is_unchanged(tmp_path: Path, backups: Path) -> None:
    for i in range(3):
        path = _snapshot(
            backups, tmp_path / f"p{i}", name=f"{system_backup.AUTO_BACKUP_PREFIX}_{i}.zip"
        )
        # 時間和檔名順序相反：清除依檔名逐份處理，沒保留時間的話，修剪順序會跟著變
        stamp = (1_700_000_000 + 2 - i) * 10**9
        os.utime(path, ns=(stamp, stamp))
    snapshots.scrub_snapshots(IDENT, backups, set())
    removed = scheduler.prune_old_auto_backups(backups, 1)
    assert sorted(p.name for p in removed) == [
        f"{system_backup.AUTO_BACKUP_PREFIX}_1.zip",
        f"{system_backup.AUTO_BACKUP_PREFIX}_2.zip",
    ]


def test_s20_layout_is_tied_to_the_snapshot_script_and_qdrant(
    tmp_path: Path, backups: Path
) -> None:
    mb = load_manage_backup()
    assert str(mb.MEMORY_DB_ARC) == MAIN
    assert str(mb.QDRANT_ARC) == f"data/vault/{cn.QDRANT_DIR_NAME}"
    assert mb.QDRANT_META == cn.QDRANT_META_FILE
    assert mb.PARTIAL_SUFFIX == leftovers.P1_PARTIAL_SUFFIX
    assert mb.SNAPSHOT_WORKDIR_PREFIX == leftovers.P1_WORKDIR_PREFIX
    assert qdrant_local.META_INFO_FILENAME == cn.QDRANT_META_FILE
    path = _engine_snapshot(tmp_path, backups)
    assert path.parent == system_backup.backup_dir()
    names = {m[0] for m in zip_members(path)}
    assert MAIN in names
    assert META in names
    assert (
        f"data/vault/{cn.QDRANT_DIR_NAME}/{cn.QDRANT_COLLECTION_DIR}/{TARGET_COLLECTION}/storage.sqlite"
        in names
    )
    assert any(f"/{fm.FACTS_BACKUP_DIR_NAME}/" in n for n in names)
    qdrant = make_qdrant(tmp_path / "q", {"memory_zz_probe": []})
    assert (qdrant / cn.QDRANT_META_FILE).is_file()
    assert (qdrant / cn.QDRANT_COLLECTION_DIR / "memory_zz_probe").is_dir()


# --- S21／S22 獨立驗證擋得住 ---------------------------------------------------


def test_s21a_classifier_bug_is_caught(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)
    real = snapshots._classify

    def greedy(infos, identity):
        plan = real(infos, identity)
        extra = {i.filename for i in infos if f"/{OTHER_COLLECTION}/" in i.filename}
        return plan._replace(target_members=plan.target_members | frozenset(extra))

    monkeypatch.setattr(snapshots, "_classify", greedy)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "verify_failed")
    assert sha256(path) == digest
    assert _temps(backups) == []


def _inject(monkeypatch: pytest.MonkeyPatch, transform) -> None:
    real = resource_manager.rewrite_archive

    def faulty(source: object, output: object, **kwargs: object) -> None:
        real(source, output, **kwargs)
        _rewrite_zip(Path(output), transform)

    monkeypatch.setattr(resource_manager, "rewrite_archive", faulty)


@pytest.mark.parametrize(
    "case",
    ["alter-untouched", "stale-db", "reorder"],
)
def test_s21bcd_rewrite_faults_are_caught(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)
    original_main = zip_read(path, MAIN)

    def transform(members):
        if case == "alter-untouched":
            return [
                (i, b"tampered" if i.filename == "data/ui_sessions.db" else d) for i, d in members
            ]
        if case == "stale-db":
            return [(i, original_main if i.filename == MAIN else d) for i, d in members]
        return list(reversed(members))

    _inject(monkeypatch, transform)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "verify_failed")
    assert sha256(path) == digest
    assert _temps(backups) == []


def test_s22_meta_transform_bug_is_caught(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)

    def too_much(meta: bytes, name: str) -> bytes:
        raw = json.loads(meta)
        raw["collections"] = {}
        return json.dumps(raw).encode()

    monkeypatch.setattr(qdrant_meta, "without_collection", too_much)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "verify_failed")
    assert sha256(path) == digest


def test_s23_swap_order(tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _snapshot(backups, tmp_path)
    calls: list[str] = []

    def spy(name: str, real):
        def wrapper(*args: object, **kwargs: object):
            calls.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(snapshots, "_verify_rewrite", spy("verify", snapshots._verify_rewrite))
    for name in ("fsync_file", "copy_file_metadata", "move_file", "fsync_dir"):
        monkeypatch.setattr(resource_manager, name, spy(name, getattr(resource_manager, name)))
    monkeypatch.setattr(swap, "stamp", spy("stamp", swap.stamp))
    assert _main(_items(path)).status == "scrubbed"
    assert calls[calls.index("verify") :] == [
        "verify",
        "fsync_file",
        "copy_file_metadata",
        "stamp",
        "move_file",
        "fsync_dir",
    ]


def test_s24_disk_full_while_writing(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)

    def full(source: object, output: object, **_kwargs: object) -> None:
        Path(output).write_bytes(b"PK\x03\x04half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(resource_manager, "rewrite_archive", full)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "no_space")
    assert sha256(path) == digest
    assert _temps(backups) == []


def test_s25_encrypted_member(tmp_path: Path, backups: Path) -> None:
    plain = zip_tree(_data_root(tmp_path), tmp_path / "plain.zip")
    path = backups / "Touka_Backup_enc.zip"
    with (
        zipfile.ZipFile(plain) as src,
        pyzipper.AESZipFile(
            path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as enc,
    ):
        enc.setpassword(b"zz-password-123")
        for info in src.infolist():
            enc.writestr(info.filename, src.read(info))
    digest = sha256(path)
    item = _main(_items(path))
    assert sha256(path) == digest
    assert (item.status, item.reason) == ("failed", "unreadable")


def test_s25_collection_dir_without_meta(tmp_path: Path, backups: Path) -> None:
    data = _data_root(tmp_path)
    (data / "vault" / cn.QDRANT_DIR_NAME / cn.QDRANT_META_FILE).unlink()
    path = zip_tree(data, backups / "Touka_Backup_nometa.zip")
    item = _main(_items(path))
    assert item.status == "scrubbed"
    assert not any(f"/{TARGET_COLLECTION}/" in m[0] for m in zip_members(path))


def test_s25_collision_seen_only_through_soul_dirs(tmp_path: Path, backups: Path) -> None:
    identity = AgentIdentity(agent_id="zz-a-b")
    data = tmp_path / "souldir"
    make_db(data / "vault" / "core_memory.db", [("fact_1", "zz-a-b", "mine")])
    (data / "soul" / "zz_a_b").mkdir(parents=True)
    (data / "soul" / "zz_a_b" / "soul.md").write_text("x", encoding="utf-8")
    path = zip_tree(data, backups / "Touka_Backup_soul.zip")
    item = _main(snapshots.scrub_snapshot(path, identity, set()))
    assert (item.status, item.reason) == ("failed", "collection_shared")


def test_s26_empty_zip_needs_nothing(backups: Path) -> None:
    path = backups / "Touka_Backup_empty.zip"
    path.write_bytes(b"")
    item = _main(_items(path))
    assert (item.status, item.reason) == ("not_needed", "empty_file")


def test_s27_out_of_scope_is_reported_but_does_not_fail(tmp_path: Path, backups: Path) -> None:
    data = _data_root(tmp_path)
    jsonl = data / "vault" / TARGET / "fact" / "facts.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}", encoding="utf-8")
    conn = fm.sqlite3.connect(data / "vault" / "core_memory.db")
    conn.execute(
        "INSERT INTO facts (id, agent_id, content, tags) VALUES ('fact_l', ?, 'legacy', '[]')",
        ("ToukaSoul" + TARGET,),
    )
    conn.commit()
    conn.close()
    path = zip_tree(data, backups / "Touka_Backup_oos.zip")
    items = _items(path)
    assert _main(items).status == "scrubbed"
    info = [i for i in items if i.kind == "info"]
    assert {i.reason for i in info} == {"out_of_scope"}
    assert sum(i.count or 0 for i in info) >= 2


def test_s28_every_store_version_in_a_snapshot_is_scrubbed(tmp_path: Path, backups: Path) -> None:
    data = _data_root(tmp_path)
    vault = data / "vault"
    (vault / "core_memory.db").unlink()
    make_v2_lifecycle_db(vault / "core_memory.db")
    folder = f"data/vault/{fm.FACTS_BACKUP_DIR_NAME}"
    make_db(
        vault / fm.FACTS_BACKUP_DIR_NAME / "core_memory.v1-to-v2.1700000003.db",
        [("fact_t13", TARGET, "T V1MARK"), ("fact_o13", OTHER, "O V1MARK")],
        schema="v1",
    )
    path = zip_tree(data, backups / "Touka_Backup_v2.zip")
    members = {
        MAIN: 2,
        f"{folder}/core_memory.v0-to-v1.1700000001.db": 0,
        f"{folder}/core_memory.v0-to-v1.1700000002.db": 0,
        f"{folder}/core_memory.v1-to-v2.1700000003.db": 1,
    }
    before = {
        name: other_rows(_db_from_zip(path, name, tmp_path / "before" / f"{i}.db"))
        for i, name in enumerate(members)
    }
    item = _main(_items(path))
    assert (item.status, item.facts_removed) == ("scrubbed", 3 + 1 + 1 + 1)
    for i, (name, version) in enumerate(members.items()):
        db = _db_from_zip(path, name, tmp_path / "after" / f"{i}.db")
        assert pragma(db, "user_version") == version
        assert rows(db, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
        assert other_rows(db) == before[name]
    with zipfile.ZipFile(path) as zf:
        content = b"".join(zf.read(info) for info in zf.infolist())
    for marker in (*V2_MARKERS, "V1MARK"):
        assert f"T {marker}".encode() not in content
        assert f"O {marker}".encode() in content


# --- 成果審修正：逐份讀取、總量上限、寫入失敗、連結、暫存成組、逐項交出（R4、R8、R10、R11、R18）


def test_s29_databases_are_read_one_at_a_time(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    log: list[str] = []
    real_read, real_inspect, real_scrub = (
        snapshots._read_member,
        fm.inspect_store_image,
        fm.scrub_store_image,
    )

    def read(zip_path: Path, name: str) -> bytes:
        if name.endswith(".db"):  # 只看記憶庫成員（meta.json 很小）
            log.append("read")
        return real_read(zip_path, name)

    def inspect(image: bytes, identity: AgentIdentity) -> object:
        log.append("inspect")
        return real_inspect(image, identity)

    def scrub(image: bytes, identity: AgentIdentity, out: Path) -> int:
        log.append("scrub")
        return real_scrub(image, identity, out)

    monkeypatch.setattr(snapshots, "_read_member", read)
    monkeypatch.setattr(fm, "inspect_store_image", inspect)
    monkeypatch.setattr(fm, "scrub_store_image", scrub)
    assert _main(_items(path)).status == "scrubbed"
    assert log.count("read") >= 3
    assert "read,read" not in ",".join(log)  # 讀一份、處理完才讀下一份


def test_s30_total_size_cap_per_snapshot(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_DB_BYTES", 10)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "too_large")
    assert sha256(path) == digest


def test_s31_rewrite_that_cannot_write_is_write_failed(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _snapshot(backups, tmp_path)
    digest = sha256(path)

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(resource_manager, "rewrite_archive", refuse)
    item = _main(_items(path))
    assert (item.status, item.reason) == ("failed", "write_failed")
    assert sha256(path) == digest
    assert _temps(backups) == []


def test_s32_linked_snapshot_folder_is_a_failure_not_done(
    tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot(backups, tmp_path)
    fake_links(monkeypatch, backups)
    assert [
        (i.kind, i.name, i.status, i.reason)
        for i in snapshots.scrub_snapshots(IDENT, backups, set())
    ] == [("snapshot", backups.name, "failed", "unknown_file")]
    assert [
        (i.kind, i.name, i.status, i.reason)
        for i in leftovers.sweep_snapshot_leftovers(backups, get_current_epoch())
    ] == [("leftover", backups.name, "failed", "unknown_file")]


def test_s18c_a_young_snapshot_leftover_keeps_every_snapshot_leftover(backups: Path) -> None:
    partial = backups / f".Touka_Backup_1.ab12cd34{leftovers.P1_PARTIAL_SUFFIX}"
    partial.write_bytes(b"PK\x03\x04rest")
    _old(partial)
    workdir = backups / f"{leftovers.P1_WORKDIR_PREFIX}ab12cd34"
    workdir.mkdir()
    (workdir / "tmpabcd1234.sqlite").write_bytes(b"db")  # 剛寫：命令列快照可能還在跑
    own = backups / ".Touka_Backup_x.zip.memscrub-ab12cd34.tmp"
    own.write_bytes(b"x")
    items = {i.name: i for i in leftovers.sweep_snapshot_leftovers(backups, get_current_epoch())}
    assert (items[partial.name].status, items[partial.name].reason) == ("failed", "unknown_file")
    assert (items[workdir.name].status, items[workdir.name].reason) == ("failed", "unknown_file")
    assert items[own.name].status == "removed"
    assert partial.exists() and workdir.exists()


def test_s18d_leftovers_already_removed_stay_in_the_report_when_the_sweep_fails(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = backups / ".Touka_Backup_a.zip.memscrub-aaaaaaaa.tmp"
    second = backups / ".Touka_Backup_b.zip.memscrub-bbbbbbbb.tmp"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    real = swap.regular_file

    def flaky(path: Path) -> bool:
        if path.name == second.name:
            raise OSError(5, "vanished")
        return real(path)

    monkeypatch.setattr(swap, "regular_file", flaky)
    progress = SO.ScrubProgress()
    progress.category(
        "leftovers", lambda: leftovers.iter_snapshot_leftovers(backups, get_current_epoch())
    )
    assert [(i.kind, i.name, i.status) for i in progress.items] == [
        ("leftover", first.name, "removed"),
        ("category", "leftovers", "failed"),
    ]
    assert progress.build(None).leftovers_removed == 1
