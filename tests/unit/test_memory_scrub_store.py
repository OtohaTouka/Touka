"""清除連備份：事實庫層（記憶體副本檢查與清除、線上庫整理、secure_delete、升級備份鎖）.

只用合成 ID 與 tmp_path；資料庫都是測試自己建的。F 組對照設計 §12-2。
"""

from __future__ import annotations

import dataclasses
import re
import tempfile
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from memory_server import factual as fm
from memory_server import vector as vector_module
from memory_server.utils import collection_names as cn
from src.models.identity import AgentIdentity

from tests.helpers.memory_scrub import (
    OTHER,
    TARGET,
    V2_MARKERS,
    delete_rows,
    file_has,
    make_db,
    make_v2_lifecycle_db,
    other_rows,
    other_rows_digest,
    pragma,
    residue_db,
    rows,
)

if TYPE_CHECKING:
    from pathlib import Path

sqlite = fm.sqlite3
IDENT = AgentIdentity(agent_id=TARGET)


def _db(tmp_path: Path, *, schema: str = "v1", extra_sql: tuple[str, ...] = ()) -> Path:
    return make_db(
        tmp_path / "src" / "core_memory.db",
        [
            ("fact_t1", TARGET, "target one"),
            ("fact_t2", TARGET, "target two " + "y" * 5000),
            ("fact_o1", OTHER, "other one"),
            ("fact_o2", None, "no owner"),
            ("fact_o3", "ToukaSoul" + TARGET, "legacy prefixed"),
        ],
        schema=schema,
        extra_sql=extra_sql,
    )


def _out(tmp_path: Path, name: str = "out.db") -> Path:
    out = tmp_path / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"")
    return out


def _reason(exc_info: pytest.ExceptionInfo) -> str:
    return str(exc_info.value.reason)


# --- F01 檢查 ---------------------------------------------------------------


@pytest.mark.parametrize("schema", ["ancient", "v0", "v1"])
def test_f01_inspect_counts_rows_and_ids(tmp_path: Path, schema: str) -> None:
    db = _db(tmp_path, schema=schema)
    found = fm.inspect_store_image(db.read_bytes(), IDENT)
    assert found.target_rows == 2
    assert found.legacy_rows == 1
    assert found.user_version == (1 if schema == "v1" else 0)
    assert found.agent_ids == frozenset({OTHER, "ToukaSoul" + TARGET})
    assert found.needs_scrub(contains_id_bytes=False)


@pytest.mark.parametrize(
    "extra",
    [
        "CREATE TABLE other_table (agent_id TEXT, x TEXT)",
        "CREATE TABLE notes (x TEXT)",
        "CREATE VIEW v AS SELECT content FROM facts",
        "CREATE TRIGGER trg AFTER INSERT ON facts BEGIN SELECT 1; END",
        "CREATE VIRTUAL TABLE facts_fts USING fts5(content)",
    ],
    ids=["agent-table", "plain-table", "view", "trigger", "virtual"],
)
def test_f01_unknown_schema_objects_fail_closed(tmp_path: Path, extra: str) -> None:
    db = _db(tmp_path, extra_sql=(extra,))
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.inspect_store_image(db.read_bytes(), IDENT)
    assert _reason(exc) == "unknown_schema"
    with pytest.raises(fm.MemoryScrubError):
        fm.scrub_store_image(db.read_bytes(), IDENT, _out(tmp_path))


def test_f01_clean_store_needs_nothing(tmp_path: Path) -> None:
    db = make_db(tmp_path / "c.db", [("fact_o1", OTHER, "other")])
    found = fm.inspect_store_image(db.read_bytes(), IDENT)
    assert found.target_rows == 0
    assert found.freelist_pages == 0
    assert not found.needs_scrub(contains_id_bytes=False)
    assert found.needs_scrub(contains_id_bytes=True)


# --- F02 清除副本 -------------------------------------------------------------


@pytest.mark.parametrize("schema", ["ancient", "v0", "v1"])
def test_f02_scrub_removes_target_and_keeps_everything_else(tmp_path: Path, schema: str) -> None:
    db = _db(tmp_path, schema=schema)
    before_others = other_rows_digest(db)
    before_schema = rows(db, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")
    out = _out(tmp_path)
    removed = fm.scrub_store_image(db.read_bytes(), IDENT, out)
    assert removed == 2
    assert rows(out, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
    assert other_rows_digest(out) == before_others
    assert (
        rows(out, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")
        == before_schema
    )
    assert pragma(out, "user_version") == pragma(db, "user_version")
    assert pragma(out, "freelist_count") == 0
    assert pragma(out, "integrity_check") == "ok"
    assert not file_has(out, "target two")


def test_f02b_nothing_but_the_output_touches_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    image = db.read_bytes()
    empty_tmp = tmp_path / "empty-tmp"
    empty_cwd = tmp_path / "empty-cwd"
    empty_tmp.mkdir()
    empty_cwd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(empty_tmp))
    for name in ("TMP", "TEMP", "TMPDIR", "SQLITE_TMPDIR"):
        monkeypatch.setenv(name, str(empty_tmp))
    monkeypatch.chdir(empty_cwd)
    with fm._memory_db(image) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    out = _out(tmp_path)
    fm.scrub_store_image(image, IDENT, out)
    assert list(empty_tmp.iterdir()) == []
    assert list(empty_cwd.iterdir()) == []
    assert sorted(p.name for p in out.parent.iterdir()) == ["out.db"]


# --- F03 精確比對 -------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["zz-50%", "zz_x", "zz'q\"", "zz-中文", "zz-🙂", "zz-scrub-a"],
)
def test_f03_only_exact_id_is_removed(tmp_path: Path, target: str) -> None:
    db = make_db(
        tmp_path / "e.db",
        [
            ("fact_1", target, "mine"),
            ("fact_2", target + "2", "sibling"),
            ("fact_3", target.upper() if target.upper() != target else target + "X", "case"),
            ("fact_4", "zz-5%%", "pattern"),
            ("fact_5", None, "null"),
        ],
    )
    out = _out(tmp_path)
    fm.scrub_store_image(db.read_bytes(), AgentIdentity(agent_id=target), out)
    kept = {row[0] for row in rows(out, "SELECT id FROM facts")}
    assert kept == {"fact_2", "fact_3", "fact_4", "fact_5"}


def test_f03b_nocase_column_does_not_widen_the_delete(tmp_path: Path) -> None:
    db = make_db(
        tmp_path / "n.db",
        [("fact_1", "zz-A", "upper"), ("fact_2", "zz-a", "lower")],
        schema="nocase",
    )
    out = _out(tmp_path)
    removed = fm.scrub_store_image(db.read_bytes(), AgentIdentity(agent_id="zz-A"), out)
    assert removed == 1
    assert rows(out, "SELECT id, agent_id FROM facts") == [("fact_2", "zz-a")]


# --- F04 殘片 ---------------------------------------------------------------


def test_f04_residue_is_gone_from_the_scrubbed_copy(tmp_path: Path) -> None:
    db = residue_db(tmp_path / "r.db", "MARKER-F04")
    found = fm.inspect_store_image(db.read_bytes(), IDENT)
    assert found.target_rows == 0
    assert found.needs_scrub(contains_id_bytes=TARGET.encode() in db.read_bytes())
    out = _out(tmp_path)
    assert fm.scrub_store_image(db.read_bytes(), IDENT, out) == 0
    assert not file_has(out, "MARKER-F04")


def test_f04_live_compaction_removes_old_residue(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    conn = sqlite.connect(store.db_path)
    try:
        conn.execute("PRAGMA secure_delete = OFF")
        conn.execute(
            "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at)"
            " VALUES ('fact_t', ?, 'live MARKER-LIVE', '[]', 1, 1)",
            (TARGET,),
        )
        conn.execute(
            "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at)"
            " VALUES ('fact_o', ?, 'keep me', '[]', 1, 1)",
            (OTHER,),
        )
        conn.commit()
    finally:
        conn.close()
    delete_rows(store.db_path, TARGET)
    assert file_has(store.db_path, "MARKER-LIVE")
    assert store.compact_after_reset(IDENT) is True
    assert not file_has(store.db_path, "MARKER-LIVE")
    assert rows(store.db_path, "SELECT id FROM facts") == [("fact_o",)]


# --- F05／F06 驗證擋得住 -----------------------------------------------------


def _patch_delete(monkeypatch: pytest.MonkeyPatch, sql: str) -> None:
    table = fm.MEMORY_SCRUB_TABLES["facts"]
    monkeypatch.setitem(
        fm.MEMORY_SCRUB_TABLES, "facts", dataclasses.replace(table, delete_target=sql)
    )


def test_f05_verification_catches_a_delete_that_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_delete(monkeypatch, "DELETE FROM facts WHERE 0 AND agent_id = ? COLLATE BINARY")
    db = _db(tmp_path)
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.scrub_store_image(db.read_bytes(), IDENT, _out(tmp_path))
    assert _reason(exc) == "verify_failed"


def test_f06_verification_catches_a_delete_that_takes_too_much(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_delete(monkeypatch, "DELETE FROM facts WHERE agent_id >= ? COLLATE BINARY")
    db = _db(tmp_path)
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.scrub_store_image(db.read_bytes(), IDENT, _out(tmp_path))
    assert _reason(exc) == "verify_failed"


def test_f06_failure_messages_carry_no_row_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_delete(monkeypatch, "DELETE FROM facts WHERE agent_id >= ? COLLATE BINARY")
    db = make_db(
        tmp_path / "m.db", [("fact_1", TARGET, "SECRET-F06"), ("fact_2", OTHER, "SECRET-F06b")]
    )
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.scrub_store_image(db.read_bytes(), IDENT, _out(tmp_path))
    assert "SECRET" not in str(exc.value)
    assert "SECRET" not in caplog.text


# --- F07 secure_delete --------------------------------------------------------


def test_f07_every_store_connection_turns_on_secure_delete(tmp_path: Path) -> None:
    db = make_db(tmp_path / "s.db")
    with fm.FactualStore._connection(db) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    with fm.FactualStore._connection(f"{db.absolute().as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_f07b_deleted_and_moved_content_is_zeroed_without_vacuum(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    other = AgentIdentity(agent_id=OTHER)
    store.save_fact(IDENT, "short MARKER-SD1")
    store.save_fact(IDENT, "long MARKER-SD2 " + "z" * 6000)
    store.save_fact(other, "other MARKER-SD3 " + "w" * 300)
    store.delete_agent_facts(IDENT)
    with store._connection(store.db_path) as conn:
        conn.execute("UPDATE facts SET content = 'tiny' WHERE agent_id = ?", (OTHER,))
        conn.commit()
    data = store.db_path.read_bytes()
    assert b"MARKER-SD1" not in data
    assert b"MARKER-SD2" not in data
    assert b"MARKER-SD3" not in data
    assert pragma(store.db_path, "freelist_count") >= 0


# --- F08 線上庫整理 -----------------------------------------------------------


def test_f08_missing_live_store_is_not_created(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    store.db_path.unlink()
    assert store.compact_after_reset(IDENT) is False
    assert not store.db_path.exists()


def test_f08_busy_live_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.1)
    holder = sqlite.connect(store.db_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(fm.MemoryScrubError) as exc:
            store.compact_after_reset(IDENT)
        assert _reason(exc) == "busy"
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_f08_rewritten_after_reset_skips_vacuum(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    conn = sqlite.connect(store.db_path)
    try:
        conn.execute("PRAGMA secure_delete = OFF")
        for i in range(30):
            conn.execute(
                "INSERT INTO facts (id, agent_id, content, tags) VALUES (?, ?, ?, '[]')",
                (f"fact_x{i}", OTHER, "pad " * 300),
            )
        conn.commit()
        conn.execute("DELETE FROM facts WHERE agent_id = ?", (OTHER,))
        conn.execute(
            "INSERT INTO facts (id, agent_id, content, tags) VALUES ('fact_t', ?, 'new', '[]')",
            (TARGET,),
        )
        conn.commit()
    finally:
        conn.close()
    free_before = pragma(store.db_path, "freelist_count")
    assert free_before > 0
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "rewritten_after_reset"
    assert pragma(store.db_path, "freelist_count") == free_before


def test_f08c_a_write_after_the_check_is_reported_as_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查過沒有這個身分的列、VACUUM 之前別的連線寫進一筆：報重置後又有新寫入，不是驗證失敗（丙-3）.

    SQLite 不能在交易裡 VACUUM，這段鎖不住；VACUUM 不會多出資料列，多出來的只可能是新寫入。
    """
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    real_check = fm._require_no_target

    def check_then_write(*args: object) -> None:
        real_check(*args)
        other = sqlite.connect(store.db_path)
        try:
            other.execute(
                "INSERT INTO facts (id, agent_id, content, tags)"
                " VALUES ('fact_late', ?, 'late', '[]')",
                (TARGET,),
            )
            other.commit()
        finally:
            other.close()

    monkeypatch.setattr(fm, "_require_no_target", check_then_write)
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "rewritten_after_reset"


def test_f08d_a_broken_store_wins_over_a_late_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """integrity 不過、同時又有新寫入：報比較嚴重的 live_verify_failed（先查結構與 integrity）。"""
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    real_check = fm._require_no_target

    def check_then_write(*args: object) -> None:
        real_check(*args)
        other = sqlite.connect(store.db_path)
        try:
            other.execute(
                "INSERT INTO facts (id, agent_id, content, tags)"
                " VALUES ('fact_late', ?, 'late', '[]')",
                (TARGET,),
            )
            other.commit()
        finally:
            other.close()

    monkeypatch.setattr(fm, "_require_no_target", check_then_write)
    monkeypatch.setattr(fm, "_integrity_ok", lambda _conn: False)
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "live_verify_failed"


def test_f08_unknown_schema_on_live_store(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    with store._connection(store.db_path) as conn:
        conn.execute("CREATE TABLE extra (agent_id TEXT)")
        conn.commit()
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "unknown_schema"


def test_f08b_live_verification_failure_has_its_own_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    real = fm._schema_rows
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        result = real(conn)
        return result if calls["n"] == 1 else (*result, ("table", "ghost", "ghost", None))

    monkeypatch.setattr(fm, "_schema_rows", flaky)
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "live_verify_failed"


# --- F09 升級備份鎖 -----------------------------------------------------------


def test_f09_upgrade_holds_the_backup_lock_through_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    make_db(vault / "core_memory.db", [("fact_o1", OTHER, "x")], schema="v0")
    seen: list[bool] = []
    real_rotate = fm.FactualStore._rotate_backups

    def spy(self, new_backup):
        seen.append(fm._UPGRADE_BACKUP_LOCK.locked())
        return real_rotate(self, new_backup)

    monkeypatch.setattr(fm.FactualStore, "_rotate_backups", spy)
    store = fm.FactualStore(base_dir=vault)
    assert store.current_schema_status() is fm.FactsSchemaStatus.CURRENT
    assert seen == [True]
    assert not fm._UPGRADE_BACKUP_LOCK.locked()


def test_f09_upgrade_reports_locked_when_the_backup_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    db = make_db(vault / "core_memory.db", [("fact_o1", OTHER, "x")], schema="v0")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.1)
    assert fm._UPGRADE_BACKUP_LOCK.acquire(timeout=1)
    try:
        store = fm.FactualStore(base_dir=vault)
    finally:
        fm._UPGRADE_BACKUP_LOCK.release()
    assert store.current_schema_status() is fm.FactsSchemaStatus.LOCKED
    assert pragma(db, "user_version") == 0
    assert not (vault / fm.FACTS_BACKUP_DIR_NAME).exists()


def test_f09_scrub_side_lock_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fm._UPGRADE_BACKUP_LOCK.acquire(timeout=1)
    try:
        result: dict[str, object] = {}

        def run() -> None:
            try:
                with fm.upgrade_backup_lock(timeout=0.1):
                    result["entered"] = True
            except fm.MemoryScrubError as exc:
                result["reason"] = str(exc.reason)

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        fm._UPGRADE_BACKUP_LOCK.release()
    assert result == {"reason": "lock_timeout"}


# --- F10 登記表涵蓋所有結構步驟 ----------------------------------------------


def test_f10_registry_covers_every_created_table_and_no_collation() -> None:
    statements = list(fm._BASELINE_SQL)
    for step in fm.FACTS_SCHEMA_STEPS:
        statements.extend(step.statements)
        statements.extend(sql for _column, sql in step.add_columns)
    created = {
        match.group(1)
        for statement in statements
        for match in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", statement, re.I
        )
    }
    assert created
    assert created <= set(fm.MEMORY_SCRUB_TABLES)
    assert not any("COLLATE" in statement.upper() for statement in statements)
    for table in fm.MEMORY_SCRUB_TABLES.values():
        assert "COLLATE BINARY" in table.delete_target
        assert "COLLATE BINARY" in table.count_target


# --- F11 特殊字元路徑 ---------------------------------------------------------


@pytest.mark.parametrize("folder", ["with space", "中文資料夾", "hash#mark", "percent%20sign"])
def test_f11_vacuum_into_handles_special_paths(tmp_path: Path, folder: str) -> None:
    db = _db(tmp_path)
    out = tmp_path / folder / "out.db"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"")
    fm.scrub_store_image(db.read_bytes(), IDENT, out)
    assert pragma(out, "user_version") == 1
    assert rows(out, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]


# --- F12／F13 ----------------------------------------------------------------


def test_f12_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fm, "MEMORY_SCRUB_MAX_IMAGE_BYTES", 10)
    image = _db(tmp_path).read_bytes()
    for call in (
        lambda: fm.inspect_store_image(image, IDENT),
        lambda: fm.scrub_store_image(image, IDENT, _out(tmp_path)),
    ):
        with pytest.raises(fm.MemoryScrubError) as exc:
            call()
        assert _reason(exc) == "too_large"


def test_f13_sqlite_supports_deserialize() -> None:
    assert hasattr(sqlite.Connection, "deserialize")
    assert fm.memory_scrub_supported()


def test_unreadable_image(tmp_path: Path) -> None:
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.inspect_store_image(b"not a database at all" * 200, IDENT)
    assert _reason(exc) == "unreadable"


# --- 事實庫的小函式與名稱 ------------------------------------------------------


def test_store_ids_and_fact_ids(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    store.save_fact(IDENT, "one")
    store.save_fact(AgentIdentity(agent_id=OTHER), "two")
    store.archive_fact(IDENT, store.agent_fact_ids(IDENT)[0])
    assert store.list_agent_ids() == {TARGET, OTHER}
    assert len(store.agent_fact_ids(IDENT)) == 1
    assert store.upgrade_backup_dir == store.db_path.parent / fm.FACTS_BACKUP_DIR_NAME
    assert store.db_path.name == fm.FACTS_DB_FILE_NAME


def test_store_ids_when_missing(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    store.db_path.unlink()
    assert store.list_agent_ids() == set()
    assert not store.db_path.exists()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("core_memory.v0-to-v1.1700000000.db", "backup"),
        ("core_memory.v0-to-v1.1700000000.2.db", "backup"),
        ("core_memory.v0-to-v1.1700000000.db.0123abcd.tmp", "upgrade_tmp"),
        ("core_memory.v0-to-v1.1700000000.db.0123abcd.tmp-journal", "upgrade_tmp"),
        (".core_memory.v0-to-v1.1700000000.db.memscrub-ab_12XYZ.tmp", "scrub_tmp"),
        ("core_memory.db", "unknown"),
        ("notes.txt", "unknown"),
        (".x.memscrub-short.tmp", "unknown"),
    ],
)
def test_classify_upgrade_backup_names(name: str, kind: str) -> None:
    assert fm.classify_upgrade_backup_name(name) == kind


def test_collection_names_are_shared_with_the_vector_store() -> None:
    for agent_id in ("zz-scrub-a", "zz.a b", "zz-中文", "zzı"):
        identity = AgentIdentity(agent_id=agent_id)
        assert vector_module.VectorStore.get_collection_name(
            None, identity
        ) == cn.collection_name_for(identity)
    assert vector_module.fact_point_id is cn.fact_point_id
    assert cn.collection_name_for(AgentIdentity(agent_id="zz-a-b")) == cn.collection_name_for(
        AgentIdentity(agent_id="zz_a_b")
    )


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("memory_zz_Case", "memory_zz_case", True),
        ("memory_zzς", "memory_zzσ", True),
        ("memory_zzı", "memory_zzi", True),
        ("memory_zzK", "memory_zzk", True),
        ("memory_zz_a", "memory_zz_a2", False),
        ("memory_zz_a", "memory_zz_a", True),
    ],
)
def test_names_fold_equal(first: str, second: str, same: bool) -> None:
    assert cn.names_fold_equal(first, second) is same


# --- F14 記憶庫 v2（取代鏈、安全標記）---------------------------------------------


def test_f14_v2_lifecycle_rows_are_removed_and_others_kept_row_by_row(tmp_path: Path) -> None:
    db = make_v2_lifecycle_db(tmp_path / "src" / "core_memory.db")
    before = other_rows(db)
    out = _out(tmp_path)
    assert fm.scrub_store_image(db.read_bytes(), IDENT, out) == 3
    assert pragma(out, "user_version") == 2
    assert rows(out, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]
    assert other_rows(out) == before  # 另一個 ID 的取代鏈、安全標記、forget 的列逐列不變
    assert ("idx_superseded_by",) in rows(
        out, "SELECT name FROM sqlite_master WHERE type = 'index'"
    )
    for marker in V2_MARKERS:
        assert not file_has(out, f"T {marker}")
        assert file_has(out, f"O {marker}")


def test_f14_live_v2_store_after_supersede_is_compacted(tmp_path: Path) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    other = AgentIdentity(agent_id=OTHER)
    for identity, mark in ((IDENT, "LIVETARGET"), (other, "LIVEOTHER")):
        old = store.save_fact(identity, f"{mark} old " + "x" * 4500)["id"]
        store.supersede_fact(identity, old, f"{mark} new", safety_flags=["instruction_like"])
    assert pragma(store.db_path, "user_version") == 2
    before = other_rows(store.db_path)
    store.delete_agent_facts(IDENT)
    assert store.compact_after_reset(IDENT) is True
    assert other_rows(store.db_path) == before
    assert not file_has(store.db_path, "LIVETARGET")
    assert file_has(store.db_path, "LIVEOTHER")


# --- 成果審修正：寫入失敗、線上庫讀不到與空間不夠（R10、R16）、BINARY 比對（R19） ------------


def test_f15_vacuum_into_that_cannot_write_is_write_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(fm.MemoryScrubError) as exc:
        fm.scrub_store_image(db.read_bytes(), IDENT, tmp_path / "missing-dir" / "out.db")
    assert _reason(exc) == "write_failed"


def test_f16_live_store_stat_failure_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    store.save_fact(IDENT, "x")
    store.db_path = tmp_path / "vault" / "gone.db"  # 存在檢查之後檔案就不見了
    monkeypatch.setattr(fm, "get_resource_proxy", lambda: SimpleNamespace(exists=lambda _p: True))
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "unreadable"


def test_f17_live_store_without_space_is_not_compacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    store.save_fact(AgentIdentity(agent_id=OTHER), "x")
    monkeypatch.setattr(
        fm.shutil, "disk_usage", lambda _p: SimpleNamespace(total=10**9, used=10**9, free=1)
    )
    with pytest.raises(fm.MemoryScrubError) as exc:
        store.compact_after_reset(IDENT)
    assert _reason(exc) == "no_space"


def test_f18_delete_and_count_compare_ids_exactly_on_a_nocase_column(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    make_db(
        vault / "core_memory.db",
        [("fact_up", "zz-CASE", "upper"), ("fact_lo", "zz-case", "lower")],
        schema="nocase",
    )
    store = fm.FactualStore(base_dir=vault)
    upper = AgentIdentity(agent_id="zz-CASE")
    assert store.count_agent_facts(upper) == 1
    assert store.delete_agent_facts(upper) == 1
    assert rows(store.db_path, "SELECT agent_id FROM facts ORDER BY id") == [("zz-case",)]
