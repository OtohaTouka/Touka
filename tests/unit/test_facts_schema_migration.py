"""Memory store schema upgrade: version, backup, rotation and failure states.

Every database here is synthetic and lives in tmp_path. Old stores are built with the SQL
literals of the version before the upgrade mechanism existed. No embedding model is loaded.
New names are read through the module (``fm.X``) so each test fails on its own before the
feature exists, instead of the whole file failing to import.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import inspect
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest
from memory_server import factual as fm
from memory_server import tools as memory_tools_module
from memory_server.config_utils import get_resource_proxy
from memory_server.services import reembed as reembed_module
from memory_server.services.manager import MemoryManager
from memory_server.tools.recall import random_recall
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.routers import observability
from src.services import embedding_service
from src.services.agent_reset import primitives
from src.tools.builtin import memory as proxy_memory_gateway

if TYPE_CHECKING:
    from pathlib import Path

sqlite = fm.sqlite3

AGENT = "SchemaProbe"
IDENTITY = AgentIdentity.for_role(agent_id=AGENT, role="Owner")
ROWS = (
    ("fact_a", AGENT, "the first fact"),
    ("fact_b", AGENT, "the second fact"),
    ("fact_c", "OtherSoul", "a fact of another soul"),
)
OLD_EPOCH = 1700000000
THREAD_TIMEOUT_S = 15
CHILD_TIMEOUT_S = 120
GARBAGE = b"this is not a sqlite database " * 64
DISK_FULL = "database or disk is full"
INJECTED_SQL_ERROR = "near SECRET_TOKEN: syntax error"
V1_COLUMNS = (
    "importance",
    "emotions",
    "embedding_state",
    "embedding_model",
    "embedding_updated_at",
    "sources",
)

# Schema and writes of the version before schema versioning, copied literally.
OLD_CREATE = """
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    content TEXT,
                    tags TEXT,
                    importance TEXT DEFAULT NULL,
                    emotions TEXT DEFAULT NULL,
                    created_at INTEGER,
                    updated_at INTEGER,
                    archived INTEGER DEFAULT 0
                )
            """
OLD_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_agent_archived ON facts(agent_id, archived)",
    "CREATE INDEX IF NOT EXISTS idx_created ON facts(created_at)",
)
OLD_INSERT = """
                INSERT OR REPLACE INTO facts (
                    id, agent_id, content, tags, importance,
                    emotions, created_at, updated_at, archived
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
OLD_ARCHIVE = "UPDATE facts SET archived = 1, updated_at = ? WHERE id = ? AND agent_id = ?"
# A store from before importance/emotions existed.
ANCIENT_CREATE = (
    "CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT,"
    " tags TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
ANCIENT_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at, archived)"
    " VALUES (?, ?, ?, ?, ?, ?, 0)"
)

# Child process that dies in the middle of a big write and leaves a hot journal behind.
# Standard library only: it never loads project code.
CRASH_CHILD = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA cache_size=2")
conn.execute("BEGIN IMMEDIATE")
for i in range(3000):
    conn.execute(
        "INSERT INTO facts (id, agent_id, content) VALUES (?, ?, ?)",
        ("crash" + str(i), "Crash", "x" * 900),
    )
os._exit(1)
"""


# --- helpers -------------------------------------------------------------------------


def _make_v0_db(path: Path, rows: tuple = ROWS, *, ancient: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(path)
    try:
        conn.execute(ANCIENT_CREATE if ancient else OLD_CREATE)
        for statement in OLD_INDEXES:
            conn.execute(statement)
        for fact_id, agent, content in rows:
            if ancient:
                conn.execute(ANCIENT_INSERT, (fact_id, agent, content, "[]", OLD_EPOCH, OLD_EPOCH))
            else:
                values = (fact_id, agent, content, "[]", None, None, OLD_EPOCH, OLD_EPOCH, 0)
                conn.execute(OLD_INSERT, values)
        conn.commit()
    finally:
        conn.close()
    return path


def _query(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write(path: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _version(path: Path) -> int:
    return _query(path, "PRAGMA user_version")[0][0]


def _columns(path: Path) -> set[str]:
    return {row[1] for row in _query(path, "PRAGMA table_info(facts)")}


def _facts(path: Path) -> list[tuple]:
    return _query(path, "SELECT id, agent_id, content, tags FROM facts ORDER BY id")


def _backup_dir(vault: Path) -> Path:
    return vault / fm.FACTS_BACKUP_DIR_NAME


def _backups(vault: Path) -> list[str]:
    directory = _backup_dir(vault)
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.iterdir() if path.is_file())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _status(name: str) -> Any:
    return fm.FactsSchemaStatus[name]


def _plant_backups(directory: Path, epochs: tuple[int, ...]) -> None:
    for epoch in epochs:
        (directory / f"core_memory.v0-to-v1.{epoch}.db").write_bytes(f"backup {epoch}".encode())


def _is_closed(connection: Any) -> bool:
    try:
        cursor = connection.execute("SELECT 1")
    except sqlite.ProgrammingError:
        return True
    cursor.close()
    return False


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: list | None = None,
    source_factory: type | None = None,
    writer_factory: type | None = None,
    on_writer: Any = None,
    timeout: float | None = None,
    calls: list | None = None,
    reader_factory: type | None = None,
) -> None:
    """Wrap sqlite3.connect: the backup source opens with uri=True, the upgrade lock
    connection with isolation_level=None; a Connection is a C type, so a failure is
    injected through a factory subclass instead of patching the instance."""
    real_connect = sqlite.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        if source_factory is not None and kwargs.get("uri") and "mode=ro" in str(args[0]):
            kwargs["factory"] = source_factory
        is_writer = "isolation_level" in kwargs and kwargs["isolation_level"] is None
        if writer_factory is not None and is_writer:
            kwargs["factory"] = writer_factory
        if reader_factory is not None and not is_writer and "mode=ro" not in str(args[0]):
            kwargs["factory"] = reader_factory
        if timeout is not None:
            kwargs["timeout"] = timeout
        connection = real_connect(*args, **kwargs)
        if record is not None:
            record.append(connection)
        if calls is not None:
            calls.append((str(args[0]), dict(kwargs), connection))
        if on_writer is not None and is_writer:
            on_writer()
        return connection

    monkeypatch.setattr(sqlite, "connect", connect)


class _RollbackFails(sqlite.Connection):
    def execute(self, sql: str, *args: Any) -> Any:
        if sql == "ROLLBACK":
            raise sqlite.OperationalError(DISK_FULL)
        return super().execute(sql, *args)


def _corrupt_after_header(db: Path) -> bytes:
    """Keep page 1 (header and schema) and overwrite every later page; return the healthy bytes."""
    healthy = db.read_bytes()
    page_size = _query(db, "PRAGMA page_size")[0][0]
    assert len(healthy) > page_size, "the store needs more than one page for this test"
    db.write_bytes(healthy[:page_size] + GARBAGE[:1] * (len(healthy) - page_size))
    return healthy


def _failing_backup_source(message: str) -> type:
    class _FailingSource(sqlite.Connection):
        def backup(self, target: Any, **_kwargs: Any) -> None:
            # Write something first so a leftover temp file would really exist.
            target.execute("CREATE TABLE partial_copy (x)")
            target.commit()
            raise sqlite.OperationalError(message)

    return _FailingSource


class _CommitFails(sqlite.Connection):
    def execute(self, sql: str, *args: Any) -> Any:
        if sql == "COMMIT":
            super().execute("ROLLBACK")
            raise sqlite.OperationalError(DISK_FULL)
        return super().execute(sql, *args)


def _build_in_thread(vault: Path) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["store"] = fm.FactualStore(base_dir=vault)
        except BaseException as exc:  # reported by the assertions below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _broken_step() -> Any:
    version = fm._target_version() + 1  # always one past the real steps
    return fm._SchemaStep(
        version=version,
        set_version_sql=f"PRAGMA user_version = {version}",
        add_columns=(("broken", "ALTER TABLE no_such_table ADD COLUMN broken TEXT"),),
    )


def _refuse(*_args: Any, **_kwargs: Any) -> None:
    raise PermissionError(errno.EACCES, "file is in use")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def shared_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the in-process shared store at tmp_path and reset it before and after."""
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
    MemoryManager.reset_singletons()
    yield tmp_path / "vault"
    MemoryManager.reset_singletons()


# --- T1-T4: upgrade, idempotence, SQLite rollback, new store ----------------------


@pytest.mark.parametrize(
    "folder",
    ["plain", "with space", "中文資料夾", "hash#mark", "percent%20sign"],
    ids=["plain", "space", "cjk", "hash", "percent"],
)
def test_t01_old_store_is_backed_up_then_upgraded(tmp_path: Path, folder: str) -> None:
    vault = tmp_path / folder / "vault"
    db = _make_v0_db(vault / "core_memory.db")
    before = _facts(db)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    names = _backups(vault)
    assert len(names) == 1
    assert re.fullmatch(rf"core_memory\.v0-to-v{fm._target_version()}\.\d+\.db", names[0])
    backup = _backup_dir(vault) / names[0]
    assert _facts(backup) == before
    assert _version(backup) == 0
    assert _query(backup, "PRAGMA integrity_check") == [("ok",)]
    assert _version(db) == fm._target_version()
    assert _facts(db) == before
    assert set(V1_COLUMNS) <= _columns(db)
    assert {row[0] for row in _query(db, "SELECT embedding_state FROM facts")} == {"unverified"}


def test_t02_current_store_is_left_byte_for_byte(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    fm.FactualStore(base_dir=vault)
    digest, names = _digest(db), _backups(vault)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _backups(vault) == names
    assert _digest(db) == digest


def test_t03_sqlite_rolls_back_add_column_and_user_version_together(tmp_path: Path) -> None:
    db = _make_v0_db(tmp_path / "plain.db")
    conn = sqlite.connect(db, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE facts ADD COLUMN probe TEXT DEFAULT NULL")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("ROLLBACK")
    finally:
        conn.close()

    assert "probe" not in _columns(db)
    assert _version(db) == 0


def test_t04_new_store_starts_current_without_backups(vault: Path) -> None:
    store = fm.FactualStore(base_dir=vault)

    db = vault / "core_memory.db"
    assert store.current_schema_status() is _status("CURRENT")
    assert _version(db) == fm._target_version()
    assert set(V1_COLUMNS) <= _columns(db)
    assert not _backup_dir(vault).exists()


def test_t04b_new_store_next_to_upgrade_backups_warns(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _backup_dir(vault)
    directory.mkdir(parents=True)
    _plant_backups(directory, (OLD_EPOCH,))

    with caplog.at_level(logging.WARNING, logger=fm.__name__):
        store = fm.FactualStore(base_dir=vault)

    assert any(
        record.levelno == logging.WARNING and "new empty memory store" in record.getMessage()
        for record in caplog.records
    )
    assert store.new_store_beside_backups is True
    assert observability.memory_schema_notices(
        store.current_schema_status(), beside_backups=store.new_store_beside_backups
    ) == [{"code": "memory_new_store_beside_backups", "level": "warning"}]


def test_t04d_plain_new_store_has_no_backup_notice(vault: Path) -> None:
    store = fm.FactualStore(base_dir=vault)

    assert store.new_store_beside_backups is False


def test_t04c_failed_new_store_leaves_no_table(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = list(fm._BASELINE_SQL)
    baseline[1] = "CREATE INDEX broken_index ON missing_table(nothing)"
    monkeypatch.setattr(fm, "_BASELINE_SQL", tuple(baseline))

    store = fm.FactualStore(base_dir=vault)

    db = vault / "core_memory.db"
    assert store.current_schema_status() is _status("FAILED_OTHER")
    assert _query(db, "SELECT name FROM sqlite_master WHERE type = 'table'") == []
    assert _version(db) == 0


# --- T5-T11: failures before or during the upgrade ---------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [(DISK_FULL, "NO_SPACE"), ("disk I/O error", "FAILED_OTHER")],
    ids=["disk_full", "io_error"],
)
def test_t05_failed_backup_blocks_the_upgrade_and_leaves_no_files(
    vault: Path, monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    _patch_connect(monkeypatch, source_factory=_failing_backup_source(message))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status(expected)
    assert _version(db) == 0
    assert "embedding_state" not in _columns(db)
    assert _backups(vault) == []


def test_t06_backup_while_holding_the_write_lock_does_not_hang(vault: Path) -> None:
    _make_v0_db(vault / "core_memory.db")

    thread, result = _build_in_thread(vault)
    thread.join(THREAD_TIMEOUT_S)

    assert not thread.is_alive(), "building the store hung while backing it up"
    assert "error" not in result
    assert result["store"].current_schema_status() is _status("CURRENT")


def test_t07_partly_upgraded_store_only_gets_the_missing_columns(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    _write(db, "ALTER TABLE facts ADD COLUMN embedding_state TEXT DEFAULT 'unverified'")

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _version(db) == fm._target_version()
    assert set(V1_COLUMNS) <= _columns(db)


def test_t08_ancient_store_gets_importance_and_emotions(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db", ancient=True)
    assert {"importance", "emotions"}.isdisjoint(_columns(db))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _version(db) == fm._target_version()
    assert set(V1_COLUMNS) <= _columns(db)


def test_t09_failed_step_rolls_back_every_step_and_keeps_the_backup(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*fm.FACTS_SCHEMA_STEPS, _broken_step()))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("FAILED_OTHER")
    assert _version(db) == 0
    assert "embedding_state" not in _columns(db)
    names = _backups(vault)
    assert len(names) == 1
    assert names[0].startswith(f"core_memory.v0-to-v{fm._target_version()}.")


def test_t10_locked_store_is_neither_backed_up_nor_changed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.05)
    holder = sqlite.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        store = fm.FactualStore(base_dir=vault)
    finally:
        holder.close()

    assert store.current_schema_status() is _status("LOCKED")
    assert _version(db) == 0
    assert "embedding_state" not in _columns(db)
    assert not _backup_dir(vault).exists()


def _usage(total: int, free: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_t11_too_little_free_space_blocks_backup_and_upgrade(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    needed = db.stat().st_size * 2
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: _usage(10**12, needed - 1))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("NO_SPACE")
    assert _version(db) == 0
    assert not _backup_dir(vault).exists()


@pytest.mark.parametrize("probe", ["error", "zero_total", "just_enough"])
def test_t11b_unknown_or_enough_free_space_does_not_block(
    vault: Path, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    needed = db.stat().st_size * 2

    def disk_usage(_path: Any) -> SimpleNamespace:
        if probe == "error":
            raise OSError(errno.EIO, "cannot query free space")
        return _usage(0, 0) if probe == "zero_total" else _usage(10**12, needed)

    monkeypatch.setattr(fm.shutil, "disk_usage", disk_usage)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert len(_backups(vault)) == 1


# --- T12-T15: unreadable file, backup names, rotation, newer store -----------------


def test_t12_unreadable_file_becomes_a_state_and_queries_fail_loudly(vault: Path) -> None:
    vault.mkdir(parents=True)
    (vault / "core_memory.db").write_bytes(GARBAGE)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("UNREADABLE")
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.query_facts(IDENTITY)
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.query_all_facts()


def test_t13_backup_name_clash_gets_a_suffix(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "get_current_epoch", lambda: float(OLD_EPOCH))
    directory = _backup_dir(vault)
    directory.mkdir(parents=True)
    taken = directory / f"core_memory.v0-to-v1.{OLD_EPOCH}.db"
    taken.write_bytes(b"an older backup made in the same second")

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert taken.read_bytes() == b"an older backup made in the same second"
    assert _version(directory / f"core_memory.v0-to-v1.{OLD_EPOCH}.1.db") == 0


def _rotation_setup(vault: Path) -> Path:
    _make_v0_db(vault / "core_memory.db")
    directory = _backup_dir(vault)
    (directory / "sub").mkdir(parents=True)
    _plant_backups(directory, (100, 200, 300, 400))
    return directory


def test_t14_rotation_keeps_the_newest_and_never_touches_other_files(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _rotation_setup(vault)
    (directory / "notes.txt").write_bytes(b"keep me")
    (directory / "core_memory.v0-to-v1.not-a-number.db").write_bytes(b"not a backup name")
    nested = directory / "sub" / "core_memory.v0-to-v1.50.db"
    nested.write_bytes(b"nested")
    monkeypatch.setattr(fm, "get_current_epoch", lambda: 1000.0)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _backups(vault) == sorted(
        [
            f"core_memory.v0-to-v{fm._target_version()}.1000.db",
            "core_memory.v0-to-v1.300.db",
            "core_memory.v0-to-v1.400.db",
            "core_memory.v0-to-v1.not-a-number.db",
            "notes.txt",
        ]
    )
    assert nested.read_bytes() == b"nested"


def test_t14b_rotation_keeps_the_new_backup_when_the_clock_went_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rotation_setup(vault)
    monkeypatch.setattr(fm, "get_current_epoch", lambda: 10.0)

    fm.FactualStore(base_dir=vault)

    assert _backups(vault) == sorted(
        [
            f"core_memory.v0-to-v{fm._target_version()}.10.db",
            "core_memory.v0-to-v1.300.db",
            "core_memory.v0-to-v1.400.db",
        ]
    )


def test_t14c_rotation_failure_only_warns(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _rotation_setup(vault)
    monkeypatch.setattr(fm, "get_current_epoch", lambda: 1000.0)
    monkeypatch.setattr(get_resource_proxy(), "remove_file", _refuse)

    with caplog.at_level(logging.WARNING, logger=fm.__name__):
        store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert len(_backups(vault)) == 5
    assert any("rotation failed" in record.getMessage() for record in caplog.records)


def test_t15_newer_store_is_used_as_is(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    _write(db, "PRAGMA user_version = 99")
    digest = _digest(db)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("NEWER")
    assert _digest(db) == digest
    assert not _backup_dir(vault).exists()
    assert {fact["id"] for fact in store.query_facts(IDENTITY)} == {"fact_a", "fact_b"}
    assert store.save_fact(IDENTITY, "saved into a newer store")["status"] == "success"
    assert _version(db) == 99


# --- T16-T19: step table rules, frozen v1, plain INSERT, write failure -------------

ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE facts ADD COLUMN (\w+) (?:TEXT|INTEGER|REAL|BLOB)"
    r"(?: DEFAULT (?:NULL|'[^']*'|-?\d+))?"
)
CREATE_RE = re.compile(r"CREATE (?:TABLE|INDEX) IF NOT EXISTS \w+ .+", re.DOTALL)


def test_t16_steps_only_add_and_never_use_executescript() -> None:
    steps = fm.FACTS_SCHEMA_STEPS

    assert [step.version for step in steps] == list(range(1, len(steps) + 1))
    for step in steps:
        assert step.set_version_sql == f"PRAGMA user_version = {step.version}"
        for column, sql in step.add_columns:
            match = ADD_COLUMN_RE.fullmatch(sql)
            assert match is not None, sql
            assert match.group(1) == column, sql
        for sql in step.statements:
            assert CREATE_RE.fullmatch(sql) is not None, sql
            assert "UNIQUE" not in sql.upper(), sql
    assert "executescript" not in inspect.getsource(fm)


def test_t17_v1_is_frozen() -> None:
    step = fm.FACTS_SCHEMA_STEPS[0]

    assert step.version == 1
    assert step.set_version_sql == "PRAGMA user_version = 1"
    assert step.add_columns == (
        ("importance", "ALTER TABLE facts ADD COLUMN importance TEXT DEFAULT NULL"),
        ("emotions", "ALTER TABLE facts ADD COLUMN emotions TEXT DEFAULT NULL"),
        (
            "embedding_state",
            "ALTER TABLE facts ADD COLUMN embedding_state TEXT DEFAULT 'unverified'",
        ),
        ("embedding_model", "ALTER TABLE facts ADD COLUMN embedding_model TEXT DEFAULT NULL"),
        (
            "embedding_updated_at",
            "ALTER TABLE facts ADD COLUMN embedding_updated_at INTEGER DEFAULT NULL",
        ),
        ("sources", "ALTER TABLE facts ADD COLUMN sources TEXT DEFAULT NULL"),
    )
    assert step.statements == ()


def test_t18_inserting_an_existing_id_raises_and_keeps_the_row(vault: Path) -> None:
    store = fm.FactualStore(base_dir=vault)
    record = {"id": "fact_same", "content": "original", "tags": [], "created_at": 1}
    store._insert_record(IDENTITY, record)
    db = vault / "core_memory.db"
    _write(
        db,
        "UPDATE facts SET embedding_state = ?, sources = ? WHERE id = ?",
        ("ready", '["src"]', "fact_same"),
    )
    intruder = AgentIdentity.for_role(agent_id="Intruder", role="Owner")

    with pytest.raises(fm.FactIdConflictError):
        store._insert_record(intruder, {**record, "content": "replacement"})

    assert _query(
        db, "SELECT agent_id, content, embedding_state, sources FROM facts WHERE id = 'fact_same'"
    ) == [(AGENT, "original", "ready", '["src"]')]


def test_t19_write_failure_message_carries_no_database_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=vault)

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite.OperationalError(INJECTED_SQL_ERROR)

    monkeypatch.setattr(store, "_insert_record", broken)

    with pytest.raises(fm.FactWriteError) as caught:
        store.save_fact(IDENTITY, "a fact")

    assert str(caught.value) == fm.FACT_WRITE_FAILED_MESSAGE
    assert "SECRET_TOKEN" not in str(caught.value)


async def test_t19b_write_failure_reaches_the_model_as_a_structured_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=vault)

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite.OperationalError(INJECTED_SQL_ERROR)

    monkeypatch.setattr(store, "_insert_record", broken)
    vector = create_autospec(VectorStore, instance=True)
    vector.query_vectors.return_value = []
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)

    result = await proxy_memory_gateway.memory_tools(
        action="save", identity=IDENTITY, content="a fact"
    )

    assert next(iter(json.loads(result))) == "error_class"
    assert "SECRET_TOKEN" not in result


def test_t19c_id_conflict_is_not_turned_into_a_write_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=vault)

    def conflict(*_args: Any, **_kwargs: Any) -> None:
        raise fm.FactIdConflictError

    monkeypatch.setattr(store, "_insert_record", conflict)

    with pytest.raises(fm.FactIdConflictError):
        store.save_fact(IDENTITY, "a fact")


# --- T20-T23: crash journal, double check under the lock, connections, COMMIT --------


def test_t20_crash_journal_is_rolled_back_on_reopen(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    before = _facts(db)
    journal = db.with_name(f"{db.name}-journal")

    child = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", CRASH_CHILD, str(db)],
        check=False,
        capture_output=True,
        timeout=CHILD_TIMEOUT_S,
    )

    assert child.returncode == 1, child.stderr
    assert journal.exists(), "the child left no journal; this test would prove nothing"
    assert journal.stat().st_size > 0, "the child left an empty journal"

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _facts(db) == before
    assert not journal.exists()
    (backup,) = _backups(vault)
    assert _facts(_backup_dir(vault) / backup) == before


@pytest.mark.parametrize(
    ("version", "expected"), [(None, "CURRENT"), (99, "NEWER")], ids=["upgraded", "newer"]
)
def test_t21_store_rereads_the_version_after_taking_the_lock(
    vault: Path, monkeypatch: pytest.MonkeyPatch, version: int | None, expected: str
) -> None:
    upgraded = version is None
    version = fm._target_version() if upgraded else version
    db = _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", THREAD_TIMEOUT_S)
    holder = sqlite.connect(db, isolation_level=None)
    waiting = threading.Event()
    _patch_connect(monkeypatch, on_writer=waiting.set)
    try:
        holder.execute("BEGIN IMMEDIATE")
        thread, result = _build_in_thread(vault)
        assert waiting.wait(THREAD_TIMEOUT_S), "the store never asked for the write lock"
        # Another upgrader finishes first while the store waits for the lock.
        if upgraded:
            for step in fm.FACTS_SCHEMA_STEPS:
                existing = {row[1] for row in holder.execute("PRAGMA table_info(facts)")}
                for column, sql in step.add_columns:
                    if column not in existing:
                        holder.execute(sql)
                for statement in step.statements:
                    holder.execute(statement)
        holder.execute(f"PRAGMA user_version = {version}")
        holder.execute("COMMIT")
    finally:
        holder.close()
    thread.join(THREAD_TIMEOUT_S)

    assert not thread.is_alive()
    assert "error" not in result
    assert result["store"].current_schema_status() is _status(expected)
    assert _version(db) == version
    assert not _backup_dir(vault).exists()


@pytest.mark.parametrize("path", ["success", "backup_fails", "rename_fails", "step_fails"])
def test_t22_every_connection_is_closed(
    vault: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _make_v0_db(vault / "core_memory.db")
    opened: list = []
    source_factory = _failing_backup_source("disk I/O error") if path == "backup_fails" else None
    _patch_connect(monkeypatch, record=opened, source_factory=source_factory)
    if path == "rename_fails":
        monkeypatch.setattr(get_resource_proxy(), "move_file", _refuse)
    if path == "step_fails":
        monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*fm.FACTS_SCHEMA_STEPS, _broken_step()))

    try:
        store = fm.FactualStore(base_dir=vault)

        expected = "CURRENT" if path == "success" else "FAILED_OTHER"
        assert store.current_schema_status() is _status(expected)
        # fast path, write lock, backup source, backup target
        assert len(opened) >= 4
        assert all(_is_closed(connection) for connection in opened)
    finally:
        for connection in opened:
            connection.close()


def test_t22b_backup_connections_are_closed_before_the_rename(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux can rename an open file, so check the connections at the moment of the rename."""
    _make_v0_db(vault / "core_memory.db")
    calls: list = []
    _patch_connect(monkeypatch, calls=calls)
    proxy = get_resource_proxy()
    real_move = proxy.move_file
    closed_at_rename: list[bool] = []

    def move_file(src: Any, dst: Any) -> None:
        backup_side = [c for path, _, c in calls if "mode=ro" in path or path.endswith(".tmp")]
        closed_at_rename.append(len(backup_side) == 2 and all(map(_is_closed, backup_side)))
        real_move(src, dst)

    monkeypatch.setattr(proxy, "move_file", move_file)
    try:
        store = fm.FactualStore(base_dir=vault)
    finally:
        for _target, _kwargs, connection in calls:
            connection.close()

    assert store.current_schema_status() is _status("CURRENT")
    assert closed_at_rename == [True]


def test_t23_commit_failure_is_a_state_and_the_shared_store_survives(
    shared_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(shared_vault / "core_memory.db")
    _patch_connect(monkeypatch, writer_factory=_CommitFails)

    first = fm.FactualStore.shared()

    assert first.current_schema_status() is _status("NO_SPACE")
    assert _version(db) == 0
    assert "embedding_state" not in _columns(db)
    names = _backups(shared_vault)
    assert len(names) == 1
    assert fm.FactualStore.shared() is first
    assert _backups(shared_vault) == names


# --- T24-T27: rolling back, offline tool, reset screen, failure classes ------------


def test_t24a_old_program_writes_after_the_upgrade_still_work(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    fm.FactualStore(base_dir=vault)
    names = _backups(vault)
    _write(
        db,
        "UPDATE facts SET embedding_state = ?, sources = ? WHERE id = ?",
        ("ready", '["src"]', "fact_a"),
    )
    # The previous version keeps writing with its own SQL after a downgrade.
    old_row = ("fact_old_program", AGENT, "saved by the old program", "[]", None, None)
    _write(db, OLD_INSERT, (*old_row, OLD_EPOCH, OLD_EPOCH, 0))
    _write(db, OLD_ARCHIVE, (OLD_EPOCH + 1, "fact_b", AGENT))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _backups(vault) == names
    assert _query(
        db,
        "SELECT id, embedding_state, sources FROM facts"
        " WHERE id IN ('fact_a', 'fact_old_program') ORDER BY id",
    ) == [("fact_a", "ready", '["src"]'), ("fact_old_program", "unverified", None)]
    assert {fact["id"] for fact in store.query_facts(IDENTITY)} == {"fact_a", "fact_old_program"}


def test_t24b_restoring_the_backup_brings_back_the_old_store(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    before = _facts(db)
    monkeypatch.setattr(fm, "get_current_epoch", lambda: float(OLD_EPOCH))
    fm.FactualStore(base_dir=vault)
    (backup,) = _backups(vault)

    # The restore steps for people: move the live file aside, copy the backup in.
    aside = vault / "before-restore"
    aside.mkdir()
    db.replace(aside / db.name)
    shutil.copyfile(_backup_dir(vault) / backup, db)
    assert _version(db) == 0
    assert _facts(db) == before

    monkeypatch.setattr(fm, "get_current_epoch", lambda: float(OLD_EPOCH + 60))
    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    assert _version(db) == fm._target_version()
    assert _facts(db) == before
    assert len(_backups(vault)) == 2


def test_t25_reembed_refuses_an_outdated_store_without_touching_it(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    digest = _digest(db)

    with pytest.raises(fm.FactsSchemaNotReadyError) as caught:
        reembed_module.open_factual_store_for_reembed(vault)

    assert caught.value.status is _status("OUTDATED")
    assert _version(db) == 0
    assert _digest(db) == digest
    assert not _backup_dir(vault).exists()


def test_t25c_reembed_refuses_a_missing_store_without_creating_it(vault: Path) -> None:
    with pytest.raises(fm.FactsSchemaNotReadyError) as caught:
        reembed_module.open_factual_store_for_reembed(vault)

    assert caught.value.status is _status("UNREADABLE")
    assert not (vault / "core_memory.db").exists()


def test_t25b_reembed_opens_a_current_store(vault: Path) -> None:
    fm.FactualStore(base_dir=vault)

    store = reembed_module.open_factual_store_for_reembed(vault)

    assert store.current_schema_status() is _status("CURRENT")


def test_t26_reset_screen_lists_the_upgrade_backups_as_kept() -> None:
    assert fm.FACTS_BACKUP_DIR_NAME in {name for name, _ in primitives.RETAINED_STORES}


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        (lambda: sqlite.OperationalError("database is locked"), "LOCKED"),
        (lambda: sqlite.OperationalError("database table is locked"), "LOCKED"),
        (lambda: sqlite.OperationalError("database is busy"), "LOCKED"),
        (lambda: sqlite.OperationalError("database or disk is full"), "NO_SPACE"),
        (lambda: OSError(errno.ENOSPC, "No space left on device"), "NO_SPACE"),
        (lambda: fm._NoSpaceForBackupError(), "NO_SPACE"),
        (lambda: sqlite.DatabaseError("file is not a database"), "UNREADABLE"),
        (lambda: sqlite.DatabaseError("database disk image is malformed"), "UNREADABLE"),
        (lambda: sqlite.OperationalError("unable to open database file"), "UNREADABLE"),
        (lambda: sqlite.OperationalError("attempt to write a readonly database"), "FAILED_OTHER"),
        (lambda: sqlite.OperationalError("disk I/O error"), "FAILED_OTHER"),
        (lambda: PermissionError(errno.EACCES, "Permission denied"), "FAILED_OTHER"),
        (lambda: KeyError("a program error"), "FAILED_OTHER"),
        (lambda: LookupError("database is locked"), "FAILED_OTHER"),
    ],
    ids=[
        "locked",
        "table_locked",
        "busy",
        "disk_full",
        "enospc",
        "precheck",
        "not_a_database",
        "malformed",
        "cannot_open",
        "readonly",
        "io_error",
        "permission",
        "program_error",
        "locked_text_but_not_sqlite",
    ],
)
def test_t27_failure_classification(make_error: Any, expected: str) -> None:
    assert fm._classify_failure(make_error()) is _status(expected)


# --- T28-T30: the store breaks after startup ---------------------------------------


def test_t28_store_broken_after_startup_fails_loudly_and_turns_unreadable(vault: Path) -> None:
    store = fm.FactualStore(base_dir=vault)
    store.save_fact(IDENTITY, "saved before the file broke")
    db = vault / "core_memory.db"
    healthy = db.read_bytes()
    db.write_bytes(GARBAGE)

    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.query_facts(IDENTITY)
    assert store.current_schema_status() is _status("UNREADABLE")
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.query_all_facts()

    db.write_bytes(healthy)
    assert store.current_schema_status() is _status("UNREADABLE")  # until a read succeeds
    assert [fact["content"] for fact in store.query_facts(IDENTITY)] == [
        "saved before the file broke"
    ]
    assert store.current_schema_status() is _status("CURRENT")


def test_t28d_good_header_but_broken_pages_stays_unreadable_until_a_read_works(
    vault: Path,
) -> None:
    store = fm.FactualStore(base_dir=vault)
    for index in range(30):
        store.save_fact(IDENTITY, f"fact {index} " + "x" * 200)
    db = vault / "core_memory.db"
    healthy = _corrupt_after_header(db)

    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.query_facts(IDENTITY)
    for _poll in range(3):  # the header recheck reads fine, but must not clear it
        assert store.current_schema_status() is _status("UNREADABLE")
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.save_fact(IDENTITY, "must not be written while reads fail")

    db.write_bytes(healthy)
    assert store.current_schema_status() is _status("UNREADABLE")
    assert len(store.query_facts(IDENTITY)) == 30
    assert store.current_schema_status() is _status("CURRENT")


def test_t28c_wrong_json_types_read_as_empty(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = fm.FactualStore(base_dir=vault)
    fact_id = store.save_fact(IDENTITY, "a fact with odd fields")["id"]
    _write(
        vault / "core_memory.db",
        "UPDATE facts SET tags = ?, emotions = ? WHERE id = ?",
        ('"just a string"', "[1, 2]", fact_id),
    )

    with caplog.at_level(logging.WARNING, logger=fm.__name__):
        (fact,) = store.query_facts(IDENTITY)

    assert fact["tags"] == []
    assert fact["emotions"] is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum(fact_id in message for message in warnings) == 2


def test_t28b_one_malformed_row_does_not_hide_the_others(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = fm.FactualStore(base_dir=vault)
    store.save_fact(IDENTITY, "a healthy fact", tags=["fine"])
    store.save_fact(IDENTITY, "a fact with broken fields")
    _write(
        vault / "core_memory.db",
        "UPDATE facts SET tags = ?, emotions = ? WHERE content = ?",
        ("[not json", "{broken", "a fact with broken fields"),
    )

    with caplog.at_level(logging.WARNING, logger=fm.__name__):
        facts = {fact["content"]: fact for fact in store.query_facts(IDENTITY)}
        everyone = store.query_all_facts()

    assert set(facts) == {"a healthy fact", "a fact with broken fields"}
    assert facts["a healthy fact"]["tags"] == ["fine"]
    assert facts["a fact with broken fields"]["tags"] == []
    assert facts["a fact with broken fields"]["emotions"] is None
    assert len(everyone) == 2
    broken_id = facts["a fact with broken fields"]["id"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(broken_id in message for message in warnings)
    assert not any("a fact with broken fields" in message for message in warnings)


def test_t29_locked_read_raises_without_changing_the_state(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = fm.FactualStore(base_dir=vault)
    holder = sqlite.connect(vault / "core_memory.db", isolation_level=None)
    _patch_connect(monkeypatch, timeout=0.05)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite.OperationalError, match="locked"):
            store.query_facts(IDENTITY)
        with pytest.raises(sqlite.OperationalError, match="locked"):
            store.query_all_facts()
        assert store.current_schema_status() is _status("CURRENT")
    finally:
        holder.close()


def test_t30_unreadable_store_counts_as_having_memories(shared_vault: Path) -> None:
    shared_vault.mkdir(parents=True)
    (shared_vault / "core_memory.db").write_bytes(GARBAGE)

    assert embedding_service.has_existing_memories() is True


def test_t30b_memory_presence_follows_the_shared_store(shared_vault: Path) -> None:
    assert embedding_service.has_existing_memories() is False

    fm.FactualStore.shared().save_fact(IDENTITY, "one fact")

    assert embedding_service.has_existing_memories() is True


# --- T31-T40: state classes, rechecks, rollback, temp files, reads while failed -----


class _HeaderReadFails(sqlite.Connection):
    message = "disk I/O error"

    def execute(self, sql: str, *args: Any) -> Any:
        if sql == "PRAGMA user_version":
            raise sqlite.OperationalError(self.message)
        return super().execute(sql, *args)


@pytest.mark.parametrize(
    ("message", "expected"),
    [("disk I/O error", "UNREADABLE"), ("database is locked", "LOCKED"), (DISK_FULL, "NO_SPACE")],
    ids=["io_error", "locked", "disk_full"],
)
def test_t31_header_read_failure_counts_as_unreadable_unless_locked_or_full(
    vault: Path, monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    _make_v0_db(vault / "core_memory.db")
    reader = type("_Reader", (_HeaderReadFails,), {"message": message})
    _patch_connect(monkeypatch, reader_factory=reader)

    store = fm.FactualStore(base_dir=vault)

    assert store._schema_status is _status(expected)  # set by startup, before any recheck
    assert store.current_schema_status() is _status(expected)


def test_t32_unreadable_store_that_reads_again_but_is_outdated_is_failed_other(
    vault: Path,
) -> None:
    vault.mkdir(parents=True)
    db = vault / "core_memory.db"
    db.write_bytes(GARBAGE)
    store = fm.FactualStore(base_dir=vault)
    assert store.current_schema_status() is _status("UNREADABLE")

    db.unlink()
    _make_v0_db(db)  # readable again, still the old version, nobody upgraded it

    assert store.current_schema_status() is _status("FAILED_OTHER")
    assert observability.memory_schema_notices(store.current_schema_status()) == [
        {"code": "memory_upgrade_failed", "level": "warning"}
    ]
    assert {fact["id"] for fact in store.query_facts(IDENTITY)} == {"fact_a", "fact_b"}


def test_t33_recheck_never_creates_a_missing_store(vault: Path) -> None:
    vault.mkdir(parents=True)
    db = vault / "core_memory.db"
    db.write_bytes(GARBAGE)
    store = fm.FactualStore(base_dir=vault)
    db.unlink()

    assert store.current_schema_status() is _status("UNREADABLE")
    assert not db.exists()


def test_t34_recheck_gives_up_quickly(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault.mkdir(parents=True)
    (vault / "core_memory.db").write_bytes(GARBAGE)
    store = fm.FactualStore(base_dir=vault)
    calls: list = []
    _patch_connect(monkeypatch, calls=calls)

    store.current_schema_status()

    assert [kwargs["timeout"] for _target, kwargs, _c in calls] == [
        fm.FACTS_STATUS_RECHECK_TIMEOUT_S
    ]
    assert fm.FACTS_STATUS_RECHECK_TIMEOUT_S <= 1
    for _target, _kwargs, connection in calls:
        connection.close()


def test_t35_failed_rollback_never_commits_half_an_upgrade(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*fm.FACTS_SCHEMA_STEPS, _broken_step()))
    _patch_connect(monkeypatch, writer_factory=_RollbackFails)

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("FAILED_OTHER")
    assert _version(db) == 0
    assert "embedding_state" not in _columns(db)


def test_t36_interrupted_backup_temp_files_are_cleaned_and_lookalikes_kept(vault: Path) -> None:
    _make_v0_db(vault / "core_memory.db")
    directory = _backup_dir(vault)
    directory.mkdir(parents=True)
    stem = "core_memory.v0-to-v1.100.db"
    leftovers = [f"{stem}.abcdef12.tmp", f"{stem}.abcdef12.tmp-journal"]
    lookalikes = [f"{stem}.abcdef1.tmp", f"{stem}.abcdef12.tmp.bak", f"{stem}.abcdef123.tmp"]
    for name in leftovers + lookalikes:
        (directory / name).write_bytes(b"left behind")

    fm.FactualStore(base_dir=vault)

    names = set(_backups(vault))
    assert names.isdisjoint(leftovers)
    assert set(lookalikes) <= names


def test_t37_failed_upgrade_still_rotates_its_backups(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rotation_setup(vault)
    monkeypatch.setattr(fm, "get_current_epoch", lambda: 1000.0)
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*fm.FACTS_SCHEMA_STEPS, _broken_step()))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("FAILED_OTHER")
    assert _backups(vault) == sorted(
        [
            "core_memory.v0-to-v1.300.db",
            "core_memory.v0-to-v1.400.db",
            f"core_memory.v0-to-v{fm._target_version()}.1000.db",  # the broken step
        ]
    )


def test_t38_every_failure_state_has_exactly_one_notice() -> None:
    assert set(observability._SCHEMA_STATUS_NOTICES) == fm._SCHEMA_FAILURES
    assert len(set(observability._SCHEMA_STATUS_NOTICES.values())) == len(fm._SCHEMA_FAILURES)
    assert observability.memory_schema_notices(None, beside_backups=True) == [
        {"code": "memory_new_store_beside_backups", "level": "warning"}
    ]
    assert observability.memory_schema_notices(_status("CURRENT")) == []


def _failed_store(vault: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> Any:
    db = _make_v0_db(vault / "core_memory.db")
    if state == "NO_SPACE":
        monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: _usage(10**12, 0))
        return fm.FactualStore(base_dir=vault)
    if state == "FAILED_OTHER":
        monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*fm.FACTS_SCHEMA_STEPS, _broken_step()))
        return fm.FactualStore(base_dir=vault)
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.05)
    holder = sqlite.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        return fm.FactualStore(base_dir=vault)
    finally:
        holder.close()


@pytest.mark.parametrize("state", ["NO_SPACE", "LOCKED", "FAILED_OTHER"])
async def test_t39_every_read_path_works_while_the_upgrade_is_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    store = _failed_store(vault, monkeypatch, state)
    assert store.current_schema_status() is _status(state)
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    manager = MemoryManager()

    everyone = store.query_all_facts()
    exported = json.loads(await memory_tools_module.export_facts(IDENTITY, manager=manager))
    recalled = await random_recall(IDENTITY, manager=manager)
    filtered = store.query_facts(
        IDENTITY, date_start=OLD_EPOCH - 1, date_end=OLD_EPOCH + 1, emotions_any=None
    )
    none_tagged = store.query_facts(IDENTITY, tags_any=["absent"], importance="5")

    assert len(everyone) == 3
    assert exported["count"] == 2
    assert "the first fact" in recalled or "the second fact" in recalled
    assert {fact["id"] for fact in filtered} == {"fact_a", "fact_b"}
    assert none_tagged == []


def _is_write_true(keyword: ast.keyword) -> bool:
    return keyword.arg == "write" and getattr(keyword.value, "value", None) is True


def test_t40_writes_to_new_columns_go_through_the_write_gate() -> None:
    new_columns = {
        name
        for step in fm.FACTS_SCHEMA_STEPS
        for name, _ in step.add_columns
        if name not in fm._BASELINE_SQL[0]
    }
    assert new_columns == {
        "embedding_state",
        "embedding_model",
        "embedding_updated_at",
        "sources",
        "status",
        "superseded_by",
        "safety_flags",
    }

    def writes_new(texts: list[str]) -> bool:
        return any(
            re.search(r"\b(?:INSERT|UPDATE)\b", text) and any(c in text for c in new_columns)
            for text in texts
        )

    # A write kept in a module-level constant would hide from the per-function check below.
    module = ast.parse(inspect.getsource(fm))
    module_texts = [
        node.value
        for statement in module.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not writes_new(module_texts), "write SQL for new columns must live in a gated function"

    for func in ast.walk(module):
        if not isinstance(func, ast.FunctionDef):
            continue
        texts = [
            node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        gated = any(
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "_require_schema"
            and any(_is_write_true(keyword) for keyword in node.keywords)
            for node in ast.walk(func)
        )
        assert gated or not writes_new(texts), f"{func.name} writes new columns without the gate"


# --- T17b, T41-T46: v2 (lifecycle and safety flags) -----------------------------------

V2_COLUMNS = ("status", "superseded_by", "safety_flags")
# SQL literals of the v1 program (ce0f8b9a), kept to prove a downgrade still works on v2.
V1_PROGRAM_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, importance, emotions,"
    " created_at, updated_at, archived, sources)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
V1_PROGRAM_ACTIVE = (
    "SELECT id, content, tags, importance, emotions, created_at, updated_at, archived, sources"
    " FROM facts WHERE agent_id = ? AND archived = 0 AND id IN (SELECT value FROM json_each(?))"
)
V1_PROGRAM_QUERY = (
    "SELECT id, content, tags, importance, emotions, created_at, updated_at, archived, sources"
    " FROM facts WHERE agent_id = ? AND archived = 0 ORDER BY created_at DESC"
)
V1_PROGRAM_ARCHIVE = (
    "UPDATE facts SET archived = 1, updated_at = ? WHERE id = ? AND agent_id = ?"
)
V1_PROGRAM_STATE = (
    "UPDATE facts SET embedding_state = ?, embedding_model = ?,"
    " embedding_updated_at = ? WHERE id = ? AND agent_id = ?"
)


def _make_v1_store(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a store with the v1 steps only (what the previous program left behind)."""
    steps = fm.FACTS_SCHEMA_STEPS
    _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", steps[:1])
    fm.FactualStore(base_dir=vault)
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", steps)
    shutil.rmtree(_backup_dir(vault))
    db = vault / "core_memory.db"
    assert _version(db) == 1
    return db


def test_t17b_v2_is_frozen() -> None:
    step = fm.FACTS_SCHEMA_STEPS[1]

    assert step.version == 2
    assert step.set_version_sql == "PRAGMA user_version = 2"
    assert step.add_columns == (
        ("status", "ALTER TABLE facts ADD COLUMN status TEXT DEFAULT NULL"),
        ("superseded_by", "ALTER TABLE facts ADD COLUMN superseded_by TEXT DEFAULT NULL"),
        ("safety_flags", "ALTER TABLE facts ADD COLUMN safety_flags TEXT DEFAULT NULL"),
    )
    assert step.statements == (
        "CREATE INDEX IF NOT EXISTS idx_superseded_by ON facts(superseded_by)",
    )


def test_t41_v1_store_is_backed_up_then_upgraded_to_v2(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v1_store(vault, monkeypatch)
    _write(db, "UPDATE facts SET sources = ?, embedding_state = ? WHERE id = ?",
           ('["src"]', "ready", "fact_a"))
    before = _query(db, "SELECT id, content, sources, embedding_state FROM facts ORDER BY id")

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("CURRENT")
    (backup,) = _backups(vault)
    assert re.fullmatch(r"core_memory\.v1-to-v2\.\d+\.db", backup)
    assert _version(_backup_dir(vault) / backup) == 1
    assert _version(db) == 2
    assert set(V2_COLUMNS) <= _columns(db)
    assert _query(db, "SELECT id, content, sources, embedding_state FROM facts ORDER BY id") == (
        before
    )
    assert _query(db, "SELECT DISTINCT status, superseded_by, safety_flags FROM facts") == [
        (None, None, None)
    ]
    indexes = {row[1] for row in _query(db, "PRAGMA index_list(facts)")}
    assert "idx_superseded_by" in indexes


def test_t42_failed_v2_upgrade_leaves_the_v1_store_readable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v1_store(vault, monkeypatch)
    digest = _digest(db)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: _usage(10**12, 0))

    store = fm.FactualStore(base_dir=vault)

    assert store.current_schema_status() is _status("NO_SPACE")
    assert _digest(db) == digest
    facts = store.query_facts(IDENTITY)
    assert {fact["id"] for fact in facts} == {"fact_a", "fact_b"}
    assert all("sources" not in fact and "safety_flags" not in fact for fact in facts)
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.save_fact(IDENTITY, "cannot be written while the upgrade is failed")


def test_t43_v1_program_sql_still_works_on_a_v2_store(vault: Path) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    store = fm.FactualStore(base_dir=vault)
    assert _version(db) == 2
    replaced = store.save_fact(IDENTITY, "an old version")["id"]
    _write(
        db,
        "UPDATE facts SET archived = 1, status = 'superseded', superseded_by = 'fact_a'"
        " WHERE id = ?",
        (replaced,),
    )

    # The v1 program keeps working with its own SQL after a downgrade.
    _write(db, V1_PROGRAM_INSERT,
           ("fact_v1", AGENT, "saved by v1", "[]", None, None, OLD_EPOCH, OLD_EPOCH, 0, None))
    _write(db, V1_PROGRAM_STATE, ("ready", "model", OLD_EPOCH, "fact_v1", AGENT))
    _write(db, V1_PROGRAM_ARCHIVE, (OLD_EPOCH, "fact_b", AGENT))
    active = _query(db, V1_PROGRAM_ACTIVE, (AGENT, json.dumps(["fact_a", replaced, "fact_v1"])))
    listed = _query(db, V1_PROGRAM_QUERY, (AGENT,))

    assert {row[0] for row in active} == {"fact_a", "fact_v1"}  # the replaced row stays hidden
    assert {row[0] for row in listed} == {"fact_a", "fact_v1"}
    [v1_row] = [fact for fact in store.query_facts(IDENTITY) if fact["id"] == "fact_v1"]
    assert (v1_row["status"], v1_row["superseded_by"], v1_row["safety_flags"]) == (None, None, [])


def test_t44_v1_program_treats_a_v2_store_as_newer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(vault / "core_memory.db")
    fm.FactualStore(base_dir=vault)
    digest = _digest(db)
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", fm.FACTS_SCHEMA_STEPS[:1])

    v1_view = fm.FactualStore(base_dir=vault)

    assert v1_view.current_schema_status() is _status("NEWER")
    assert _digest(db) == digest
    assert {fact["id"] for fact in v1_view.query_facts(IDENTITY)} == {"fact_a", "fact_b"}
    assert v1_view.save_fact(IDENTITY, "saved while downgraded")["status"] == "success"
    assert _version(db) == 2


@pytest.mark.parametrize("state", ["NO_SPACE", "LOCKED", "FAILED_OTHER"])
async def test_t45_new_read_paths_work_while_the_upgrade_is_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    store = _failed_store(vault, monkeypatch, state)
    assert store.current_schema_status() is _status(state)
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    vector = create_autospec(VectorStore, instance=True)
    vector.query_vectors.return_value = []
    vector.agent_point_count.return_value = 0
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)
    manager = MemoryManager()
    monkeypatch.setattr(memory_tools_module, "_global_manager", manager)

    unflagged = store.query_facts(IDENTITY, exclude_flagged=True)
    found = await proxy_memory_gateway.memory_tools(
        action="query", identity=IDENTITY, query="", include_history=True
    )
    report = await proxy_memory_gateway.memory_tools(action="diagnostics", identity=IDENTITY)

    assert {fact["id"] for fact in unflagged} == {"fact_a", "fact_b"}
    assert "the first fact" in found and "暫時無法讀取" in found
    assert re.search(r"\[Safety\][^\n]*unavailable", report), report
    with pytest.raises(fm.FactsSchemaNotReadyError):
        store.superseded_history(IDENTITY, "fact_a", 3)
