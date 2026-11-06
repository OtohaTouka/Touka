"""清除連備份：升級備份（U 組，對照設計 §12-2）.

全部在 tmp_path；寫入出口由沙盒守衛把關，只用合成 ID。
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from memory_server import factual as fm
from src.models.identity import AgentIdentity
from src.services.memory_scrub import os_metadata, swap
from src.services.memory_scrub import upgrade_backups as ub
from src.services.resource_manager import resource_manager

from tests.helpers.memory_scrub import (
    OTHER,
    TARGET,
    V2_MARKERS,
    assert_no_breach,
    fake_links,
    file_has,
    install_scrub_sandbox,
    make_db,
    make_v2_lifecycle_db,
    other_rows,
    other_rows_digest,
    pragma,
    residue_db,
    rows,
    sha256,
    wait_scrub_idle,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

IDENT = AgentIdentity(agent_id=TARGET)


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    assert wait_scrub_idle(0) == [], "a previous test left the scrub flag or upgrade lock held"
    yield install_scrub_sandbox(monkeypatch, tmp_path)
    assert wait_scrub_idle() == [], "a scrub thread was still holding the upgrade lock"
    assert_no_breach()


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "vault" / fm.FACTS_BACKUP_DIR_NAME
    directory.mkdir(parents=True)
    return directory


def _backup(directory: Path, epoch: int, rows_: list[tuple] | None = None) -> Path:
    path = make_db(
        directory / f"core_memory.v0-to-v1.{epoch}.db",
        rows_
        if rows_ is not None
        else [
            (f"fact_t{epoch}", TARGET, f"target {epoch}"),
            (f"fact_o{epoch}", OTHER, f"other {epoch}"),
        ],
        schema="v0",
    )
    os.utime(path, ns=(epoch * 10**9, epoch * 10**9 + 123))
    return path


def _by_name(items) -> dict[str, object]:
    return {item.name: item for item in items}


def _temps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if ".memscrub-" in p.name]


def test_u01_every_backup_is_scrubbed_and_keeps_its_name_time_and_mode(backup_dir: Path) -> None:
    paths = [_backup(backup_dir, 1_700_000_000 + i) for i in range(3)]
    others = {p.name: other_rows_digest(p) for p in paths}
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in paths}
    mtimes = {p.name: p.stat().st_mtime_ns for p in paths}
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert {name: (i.status, i.facts_removed) for name, i in items.items()} == {
        p.name: ("scrubbed", 1) for p in paths
    }
    assert sorted(p.name for p in backup_dir.iterdir()) == sorted(p.name for p in paths)
    for p in paths:
        assert rows(p, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
        assert other_rows_digest(p) == others[p.name]
        assert p.stat().st_mtime_ns == mtimes[p.name]
        assert stat.S_IMODE(p.stat().st_mode) == modes[p.name]


def test_u02_backup_without_target_is_left_alone(backup_dir: Path) -> None:
    path = _backup(backup_dir, 1_700_000_100, [("fact_o", OTHER, "other only")])
    digest, mtime = sha256(path), path.stat().st_mtime_ns
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.name, i.status) for i in items] == [(path.name, "not_needed")]
    assert sha256(path) == digest
    assert path.stat().st_mtime_ns == mtime


def test_u03_residue_only_backup_is_scrubbed(backup_dir: Path) -> None:
    path = residue_db(backup_dir / "core_memory.v0-to-v1.1700000200.db", "MARKER-U03", schema="v0")
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.status, i.facts_removed) for i in items] == [("scrubbed", 0)]
    assert not file_has(path, "MARKER-U03")


def test_u04_one_broken_backup_does_not_stop_the_others(backup_dir: Path) -> None:
    broken = backup_dir / "core_memory.v0-to-v1.1700000300.db"
    broken.write_bytes(b"not a database" * 500)
    good = _backup(backup_dir, 1_700_000_301)
    digest = sha256(broken)
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert (items[broken.name].status, items[broken.name].reason) == ("failed", "unreadable")
    assert items[good.name].status == "scrubbed"
    assert sha256(broken) == digest


def test_u05_in_use_original_is_untouched(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_400)
    digest = sha256(path)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(resource_manager, "move_file", locked)
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.status, i.reason) for i in items] == [("failed", "in_use")]
    assert sha256(path) == digest
    assert _temps(backup_dir) == []


def test_u06_original_changed_during_scrub_is_not_overwritten(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_500)
    real = fm.scrub_store_image

    def change_after(image: bytes, identity: AgentIdentity, out: Path) -> int:
        removed = real(image, identity, out)
        replacement = backup_dir / "replacement.bin"
        replacement.write_bytes(b"someone else wrote this")
        os.replace(replacement, path)  # 換掉 inode（模擬別人改了原檔）
        return removed

    monkeypatch.setattr(fm, "scrub_store_image", change_after)
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.status, i.reason) for i in items] == [("failed", "changed_during_scrub")]
    assert path.read_bytes() == b"someone else wrote this"
    assert _temps(backup_dir) == []


def test_u07_leftovers_and_unknown_files(backup_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upgrade_tmp = backup_dir / "core_memory.v0-to-v1.1700000600.db.0123abcd.tmp"
    own_tmp = backup_dir / ".core_memory.v0-to-v1.1700000600.db.memscrub-ab12cd34.tmp"
    fake_link = backup_dir / ".core_memory.v0-to-v1.1700000600.db.memscrub-zz99yy88.tmp"
    unknown = backup_dir / "manual-copy.db"
    for p in (upgrade_tmp, own_tmp, fake_link, unknown):
        p.write_bytes(b"x")
    real_lstat = Path.lstat

    def fake_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == fake_link:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0, st_mtime=0)
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert (items[upgrade_tmp.name].kind, items[upgrade_tmp.name].status) == ("leftover", "removed")
    assert (items[own_tmp.name].kind, items[own_tmp.name].status) == ("leftover", "removed")
    assert (items[unknown.name].status, items[unknown.name].reason) == ("failed", "unknown_file")
    assert (items[fake_link.name].status, items[fake_link.name].reason) == (
        "failed",
        "unknown_file",
    )
    assert not upgrade_tmp.exists()
    assert not own_tmp.exists()
    assert unknown.exists()
    assert fake_link.exists()


def test_u08_upgrade_waits_for_the_scrub_and_then_finishes(
    backup_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backup(backup_dir, 1_700_000_700)
    other_vault = tmp_path / "other-vault"
    make_db(other_vault / "core_memory.db", [("fact_x", OTHER, "x")], schema="v0")
    entered, release = threading.Event(), threading.Event()
    real = fm.scrub_store_image

    def slow(image: bytes, identity: AgentIdentity, out: Path) -> int:
        entered.set()
        release.wait(5)
        return real(image, identity, out)

    monkeypatch.setattr(fm, "scrub_store_image", slow)
    scrubber = threading.Thread(target=ub.scrub_upgrade_backups, args=(IDENT, backup_dir))
    scrubber.start()
    assert entered.wait(5)
    built: dict[str, object] = {}
    upgrader = threading.Thread(
        target=lambda: built.setdefault("store", fm.FactualStore(base_dir=other_vault))
    )
    upgrader.start()
    upgrader.join(timeout=0.5)
    assert upgrader.is_alive()  # 清除持有鎖時升級在等
    release.set()
    scrubber.join(timeout=10)
    upgrader.join(timeout=10)
    assert not scrubber.is_alive() and not upgrader.is_alive()
    assert built["store"].current_schema_status() is fm.FactsSchemaStatus.CURRENT


def test_u08b_scrub_gives_up_when_the_upgrade_holds_the_lock(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backup(backup_dir, 1_700_000_800)
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.2)
    assert fm._UPGRADE_BACKUP_LOCK.acquire(timeout=1)
    try:
        with pytest.raises(fm.MemoryScrubError) as exc:
            ub.scrub_upgrade_backups(IDENT, backup_dir)
    finally:
        fm._UPGRADE_BACKUP_LOCK.release()
    assert str(exc.value.reason) == "lock_timeout"


def test_u09_second_run_finds_nothing_to_do(backup_dir: Path) -> None:
    for i in range(2):
        _backup(backup_dir, 1_700_000_900 + i)
    ub.scrub_upgrade_backups(IDENT, backup_dir)
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert {i.status for i in items} == {"not_needed"}


def test_u10_swap_order(backup_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _backup(backup_dir, 1_700_001_000)
    calls: list[str] = []

    def spy(name: str, real):
        def wrapper(*args: object, **kwargs: object):
            calls.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(fm, "scrub_store_image", spy("verify", fm.scrub_store_image))
    for name in ("fsync_file", "copy_file_metadata", "move_file", "fsync_dir"):
        monkeypatch.setattr(resource_manager, name, spy(name, getattr(resource_manager, name)))
    monkeypatch.setattr(swap, "stamp", spy("stamp", swap.stamp))
    ub.scrub_upgrade_backups(IDENT, backup_dir)
    tail = calls[calls.index("verify") :]
    assert tail == ["verify", "fsync_file", "copy_file_metadata", "stamp", "move_file", "fsync_dir"]


def test_u11_read_only_original_is_skipped(backup_dir: Path) -> None:
    path = _backup(backup_dir, 1_700_001_100)
    digest = sha256(path)
    path.chmod(stat.S_IREAD)
    try:
        items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert [(i.status, i.reason) for i in items] == [("failed", "read_only")]
    assert sha256(path) == digest


def test_u11_read_only_temp_is_still_removed(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backup(backup_dir, 1_700_001_200)

    def lock_temp_then_fail(src: object, dst: object) -> None:
        Path(src).chmod(stat.S_IREAD)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(resource_manager, "move_file", lock_temp_then_fail)
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.status, i.reason) for i in items] == [("failed", "in_use")]
    assert _temps(backup_dir) == []


def test_legacy_prefixed_rows_are_reported_out_of_scope(backup_dir: Path) -> None:
    _backup(
        backup_dir,
        1_700_001_300,
        [("fact_t", TARGET, "t"), ("fact_l", "ToukaSoul" + TARGET, "legacy")],
    )
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    info = [i for i in items if i.kind == "info"]
    assert [(i.reason, i.count) for i in info] == [("out_of_scope", 1)]


def test_u12_backups_of_every_store_version_are_scrubbed(backup_dir: Path) -> None:
    v0 = make_db(
        backup_dir / "core_memory.v0-to-v1.1700000001.db",
        [("fact_t0", TARGET, "T V0MARK"), ("fact_o0", OTHER, "O V0MARK")],
        schema="v0",
    )
    v1 = make_db(
        backup_dir / "core_memory.v1-to-v2.1700000002.db",
        [("fact_t1", TARGET, "T V1MARK"), ("fact_o1", OTHER, "O V1MARK")],
        schema="v1",
    )
    v2 = make_v2_lifecycle_db(backup_dir / "core_memory.v2-to-v3.1700000003.db")
    expected = {v0.name: (0, 1, ["V0MARK"]), v1.name: (1, 1, ["V1MARK"])}
    expected[v2.name] = (2, 3, list(V2_MARKERS))
    before = {p.name: other_rows(p) for p in (v0, v1, v2)}
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert {name: (i.status, i.facts_removed) for name, i in items.items()} == {
        name: ("scrubbed", removed) for name, (_v, removed, _m) in expected.items()
    }
    for path in (v0, v1, v2):
        version, _removed, markers = expected[path.name]
        assert pragma(path, "user_version") == version
        assert pragma(path, "integrity_check") == "ok"
        assert rows(path, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
        assert other_rows(path) == before[path.name]
        for marker in markers:
            assert not file_has(path, f"T {marker}")
            assert file_has(path, f"O {marker}")
    assert _temps(backup_dir) == []


# --- 成果審修正：連結、原因代碼、改名後的 fsync、改名重試、空間（R4、R10、R16、R17、R27） ---


def test_u13_linked_backup_folder_is_a_failure_not_done(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backup(backup_dir, 1_700_000_000)
    fake_links(monkeypatch, backup_dir)
    items = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert [(i.kind, i.name, i.status, i.reason) for i in items] == [
        ("upgrade_backup", backup_dir.name, "failed", "unknown_file")
    ]


def test_u13_missing_backup_folder_has_nothing_to_do(tmp_path: Path) -> None:
    assert ub.scrub_upgrade_backups(IDENT, tmp_path / "vault" / "missing") == []


def test_u14_temp_that_cannot_be_written_is_write_failed(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_000)
    digest = sha256(path)

    def refuse(*_a: object, **_k: object) -> tuple[int, str]:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(swap.tempfile, "mkstemp", refuse)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert (item.status, item.reason) == ("failed", "write_failed")
    assert sha256(path) == digest


def test_u15_original_that_vanishes_is_changed_during_scrub(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_000)
    real = fm.scrub_store_image

    def vanish(image: bytes, identity: AgentIdentity, out: Path) -> int:
        removed = real(image, identity, out)
        path.unlink()
        return removed

    monkeypatch.setattr(fm, "scrub_store_image", vanish)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert (item.status, item.reason) == ("failed", "changed_during_scrub")
    assert _temps(backup_dir) == []


def test_u16_directory_fsync_failure_after_the_rename_still_counts_as_scrubbed(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _backup(backup_dir, 1_700_000_000)

    def broken(_path: object) -> None:
        raise OSError(22, "invalid argument")

    monkeypatch.setattr(resource_manager, "fsync_dir", broken)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert item.status == "scrubbed"
    assert rows(path, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
    assert "fsync" in caplog.text


@pytest.mark.parametrize(("failures", "status"), [(2, "scrubbed"), (3, "failed")])
def test_u17_rename_retries_a_briefly_held_file(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch, failures: int, status: str
) -> None:
    _backup(backup_dir, 1_700_000_000)
    real = resource_manager.move_file
    attempts: list[int] = []

    def held(*args: object, **kwargs: object) -> None:
        attempts.append(1)
        if len(attempts) <= failures:
            raise PermissionError(32, "in use")
        real(*args, **kwargs)

    monkeypatch.setattr(resource_manager, "move_file", held)
    monkeypatch.setattr(swap, "RENAME_RETRY_DELAY_S", 0)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert item.status == status
    assert len(attempts) == min(failures + 1, swap.RENAME_ATTEMPTS)
    if status == "failed":
        assert item.reason == "in_use"


def test_u18_no_space_for_an_upgrade_backup(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_000)
    digest = sha256(path)
    monkeypatch.setattr(swap, "has_space", lambda *_a: False)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert (item.status, item.reason) == ("failed", "no_space")
    assert sha256(path) == digest


# --- 丙-3b：作業系統自己放的雜檔（ADR-0270 補記 2026-10-28）---------------------

OS_FILES = ("Thumbs.db", "desktop.ini", ".DS_Store")
HARMLESS = b"\xd0\xcf\x11\xe0 thumbnail cache"  # 縮圖快取的 OLE 檔頭，不是 SQLite


def test_u19_os_metadata_files_are_left_as_they_are(backup_dir: Path) -> None:
    path = _backup(backup_dir, 1_700_000_000)
    for name in OS_FILES:
        (backup_dir / name).write_bytes(HARMLESS)
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert items[path.name].status == "scrubbed"
    for name in OS_FILES:
        item = items[name]
        assert (item.kind, item.status, item.reason, item.count) == (
            "info",
            "info",
            "os_metadata_file",
            None,
        )
        assert (backup_dir / name).read_bytes() == HARMLESS


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("Thumbs.db", b"SQLite format 3\x00" + bytes(84)),  # 名字像雜檔，內容是資料庫格式
        ("THUMBS.DB", b"cache " + TARGET.encode() + b" cache"),  # 含這個靈魂的 ID
        ("desktop.ini", b"[x]" + TARGET.encode("utf-16-le")),  # UTF-16LE 的 ID
        (".DS_Store", b"Bud1" + TARGET.encode("utf-16-be")),  # UTF-16BE 的 ID
        ("thumb\u017f.db", HARMLESS),  # 非 ASCII：不分大小寫比對後才像
        ("des\u212atop.ini", HARMLESS),  # 克耳文符號轉小寫後就是 ASCII 的 k
        ("Thumbs.db.old", HARMLESS),
    ],
)
def test_u20_lookalikes_still_fail_closed(backup_dir: Path, name: str, content: bytes) -> None:
    (backup_dir / name).write_bytes(content)
    (item,) = ub.scrub_upgrade_backups(IDENT, backup_dir)
    assert (item.kind, item.status, item.reason) == ("upgrade_backup", "failed", "unknown_file")
    assert (backup_dir / name).read_bytes() == content


def test_u21_os_metadata_name_that_is_not_a_small_file_fails_closed(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (backup_dir / "Thumbs.db").mkdir()
    (backup_dir / "desktop.ini").write_bytes(HARMLESS)
    monkeypatch.setattr(os_metadata, "MAX_OS_FILE_BYTES", len(HARMLESS) - 1)
    read = []
    real_read = resource_manager.read_file_binary
    monkeypatch.setattr(
        resource_manager,
        "read_file_binary",
        lambda p, *a, **k: read.append(p.name) or real_read(p, *a, **k),
    )
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert read == []  # 先看大小，太大的不讀進記憶體
    assert (items["Thumbs.db"].status, items["Thumbs.db"].reason) == ("failed", "unknown_file")
    assert (items["desktop.ini"].status, items["desktop.ini"].reason) == ("failed", "unknown_file")
    assert (backup_dir / "desktop.ini").read_bytes() == HARMLESS


def test_u22_content_check_on_its_own() -> None:
    limit = os_metadata.MAX_OS_FILE_BYTES
    assert os_metadata.is_os_metadata_content(b"x" * limit, IDENT) is True
    assert os_metadata.is_os_metadata_content(b"x" * (limit + 1), IDENT) is False  # 讀的時候變大


def test_u23_os_metadata_read_error_fails_only_that_file(
    backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _backup(backup_dir, 1_700_000_000)
    (backup_dir / "Thumbs.db").write_bytes(HARMLESS)
    real_read = resource_manager.read_file_binary

    def flaky(p: Path, *a: object, **k: object) -> bytes:
        if p.name == "Thumbs.db":
            raise PermissionError(13, "locked")
        return real_read(p, *a, **k)

    monkeypatch.setattr(resource_manager, "read_file_binary", flaky)
    items = _by_name(ub.scrub_upgrade_backups(IDENT, backup_dir))
    assert (items["Thumbs.db"].kind, items["Thumbs.db"].status) == ("upgrade_backup", "failed")
    assert items["Thumbs.db"].reason == "unreadable"
    assert items[path.name].status == "scrubbed"  # 一檔出錯不擋其他檔
