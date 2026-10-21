"""系統快照（manage_backup）：不覆蓋既有快照、執行中的 SQLite 用 backup API 取一致複本、
Qdrant 的 meta.json 與 collection 資料對得上；路徑由參數給時不猜 .env，資料根不存在就失敗。

全部在 tmp_path 裡建資料（含 Docker 式配置：腳本往上找不到 .env、.env 放在別處），不碰實體 data/。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from memory_server import factual as fm
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

_SCRIPT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
FIXED_NOW = datetime(2026, 10, 15, 13, 15, 42)
STEM = "Touka_Backup_20261015_131542"
COMMITTED_ROWS = 40
PENDING_ROWS = 400
ROW_BYTES = 2048


def _load():
    spec = importlib.util.spec_from_file_location("manage_backup_snapshot_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FrozenClock:
    @staticmethod
    def now() -> datetime:
        return FIXED_NOW


@pytest.fixture
def mb(monkeypatch: pytest.MonkeyPatch):
    module = _load()
    monkeypatch.setattr(module, "datetime", _FrozenClock)
    return module


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """Docker 式配置：.env 在 host_root、資料根與設定目錄各在別處、腳本目錄往上沒有 .env。"""
    data = tmp_path / "data"
    (data / "soul" / "Alice").mkdir(parents=True)
    (data / "soul" / "Alice" / "soul.md").write_text("persona", encoding="utf-8")
    (data / "vault").mkdir()
    env = tmp_path / "host_root" / ".env"
    env.parent.mkdir()
    env.write_text("NORMAL_SETTING=kept\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    return {
        "app": tmp_path / "app",
        "data": data,
        "env": env,
        "config": config,
        "out": env.parent / "backups",
    }


def _engine(mb, layout: dict[str, Path], data_root: Path | None = None):
    resolver = mb.PathResolver(
        layout["app"],
        env_path=layout["env"],
        data_root=data_root or layout["data"],
        config_dir=layout["config"],
    )
    return mb.BackupEngine(resolver)


def _names(archive_path: Path) -> set[str]:
    with zipfile.ZipFile(archive_path) as archive:
        return set(archive.namelist())


def _make_facts_db(path: Path, rows: int) -> None:
    with fm.FactualStore._connection(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, body BLOB)")
        conn.executemany(
            "INSERT INTO facts (body) VALUES (?)", [(b"x" * ROW_BYTES,) for _ in range(rows)]
        )


def _rows_and_integrity(path: Path) -> tuple[int, str]:
    with fm.FactualStore._connection(path) as conn:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    return count, verdict


# ── 檔名：同一秒連做也不互蓋，絕不覆蓋既有快照 ──────────────────────────────────


def test_same_second_backups_get_distinct_names(mb, layout):
    engine = _engine(mb, layout)

    first = engine.run_backup(layout["out"], "Touka_Backup")
    first_bytes = first.read_bytes()
    second = engine.run_backup(layout["out"], "Touka_Backup")

    assert first.name == f"{STEM}.zip"
    assert second.name == f"{STEM}_2.zip"
    assert first.read_bytes() == first_bytes
    assert sorted(p.name for p in layout["out"].iterdir()) == [f"{STEM}.zip", f"{STEM}_2.zip"]


def test_existing_snapshot_is_never_overwritten(mb, layout):
    layout["out"].mkdir()
    taken = layout["out"] / f"{STEM}.zip"
    taken.write_bytes(b"older snapshot")

    created = _engine(mb, layout).run_backup(layout["out"], "Touka_Backup")

    assert taken.read_bytes() == b"older snapshot"
    assert created.name == f"{STEM}_2.zip"
    assert "data/soul/Alice/soul.md" in _names(created)


def test_missing_data_root_fails_without_leaving_files(mb, layout, tmp_path):
    engine = _engine(mb, layout, data_root=tmp_path / "not-mounted")

    with pytest.raises(RuntimeError):
        engine.run_backup(layout["out"], "Touka_Backup")

    assert not layout["out"].exists() or list(layout["out"].iterdir()) == []


# ── SQLite：寫入中途做快照，拿到的是某個提交點 ─────────────────────────────────────


def test_memory_db_snapshot_is_a_commit_point_while_a_write_is_in_flight(mb, layout):
    db = layout["data"] / "vault" / "core_memory.db"
    _make_facts_db(db, COMMITTED_ROWS)
    spilled = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        conn = fm.sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA cache_size=2")  # 逼頁面在提交前就寫進檔案（拿到 EXCLUSIVE 鎖）
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO facts (body) VALUES (?)",
                [(b"y" * ROW_BYTES,) for _ in range(PENDING_ROWS)],
            )
            spilled.set()
            release.wait(10)
            conn.execute("COMMIT")
        finally:
            conn.close()

    writer = threading.Thread(target=_writer)
    writer.start()
    assert spilled.wait(10)
    # 前提：資料庫檔已有未提交頁面，旁邊有熱 journal——直接複製檔案會拿到一半的狀態。
    assert (db.parent / "core_memory.db-journal").exists()

    result: dict[str, Path] = {}
    engine = _engine(mb, layout)
    snapshot = threading.Thread(
        target=lambda: result.update(zip=engine.run_backup(layout["out"], "Touka_Backup"))
    )
    snapshot.start()
    time.sleep(0.5)
    release.set()
    writer.join(10)
    snapshot.join(30)

    names = _names(result["zip"])
    assert "data/vault/core_memory.db" in names
    assert not any(name.endswith(("-journal", "-wal", "-shm")) for name in names)
    extracted = layout["out"] / "check"
    with zipfile.ZipFile(result["zip"]) as archive:
        archive.extract("data/vault/core_memory.db", extracted)
    count, verdict = _rows_and_integrity(extracted / "data" / "vault" / "core_memory.db")
    assert verdict == "ok"
    assert count in {COMMITTED_ROWS, COMMITTED_ROWS + PENDING_ROWS}


def test_copy_sqlite_snapshot_reads_source_only(tmp_path):
    source = tmp_path / "live.db"
    _make_facts_db(source, 3)
    before = source.read_bytes()

    fm.FactualStore.copy_sqlite_snapshot(source, tmp_path / "copy.db")

    assert source.read_bytes() == before
    assert _rows_and_integrity(tmp_path / "copy.db") == (3, "ok")
    with pytest.raises(FileNotFoundError):
        fm.FactualStore.copy_sqlite_snapshot(tmp_path / "absent.db", tmp_path / "other.db")


# ── Qdrant 本機模式：快照解開後打得開，collection 與點數對得上 ───────────────────────


def _seed_qdrant(path: Path) -> QdrantClient:
    client = QdrantClient(path=str(path))
    for name, points in (("memory_Alice", 3), ("memory_Bob", 2)):
        client.create_collection(
            collection_name=name, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
        )
        client.upsert(
            collection_name=name,
            points=[PointStruct(id=i, vector=[1.0, 0.5, i], payload={}) for i in range(points)],
        )
    return client


def test_qdrant_snapshot_restores_and_reopens_with_matching_collections(mb, layout, tmp_path):
    qdrant = layout["data"] / "vault" / "_qdrant_db"
    live = _seed_qdrant(qdrant)  # 和執行中的 Proxy 一樣，client 開著、握著 .lock
    (qdrant / "collection" / "memory_Ghost").mkdir()  # 沒列在 meta.json 的殘留資料夾
    try:
        archive_path = _engine(mb, layout).run_backup(layout["out"], "Touka_Backup")
    finally:
        live.close()

    names = _names(archive_path)
    qdrant_members = {n for n in names if n.startswith("data/vault/_qdrant_db/")}
    assert qdrant_members == {
        "data/vault/_qdrant_db/meta.json",
        "data/vault/_qdrant_db/collection/memory_Alice/storage.sqlite",
        "data/vault/_qdrant_db/collection/memory_Bob/storage.sqlite",
    }

    # 用同一組參數還原到新的資料根（.env 寫回參數給的位置）
    restored = tmp_path / "restored"
    restored.mkdir()
    env_after = tmp_path / "restored_env" / ".env"
    env_after.parent.mkdir()
    restore_resolver = mb.PathResolver(
        layout["app"], env_path=env_after, data_root=restored, config_dir=layout["config"]
    )
    mb.RestoreEngine(restore_resolver).run_restore(str(archive_path))

    assert env_after.read_text(encoding="utf-8") == "NORMAL_SETTING=kept\n"
    assert (restored / "soul" / "Alice" / "soul.md").read_text(encoding="utf-8") == "persona"
    reopened = QdrantClient(path=str(restored / "vault" / "_qdrant_db"))
    try:
        assert {c.name for c in reopened.get_collections().collections} == {
            "memory_Alice",
            "memory_Bob",
        }
        assert reopened.count("memory_Alice").count == 3
        assert reopened.count("memory_Bob").count == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("storage_missing", [False, True], ids=["empty", "listed-no-storage"])
def test_qdrant_collection_without_points_survives_snapshot_and_restore(
    mb, layout, tmp_path, storage_missing
):
    """collection 沒有任何點（或 meta 列了、storage 不在）：快照照樣成功，還原後打得開。"""
    qdrant = layout["data"] / "vault" / "_qdrant_db"
    client = QdrantClient(path=str(qdrant))
    vectors = VectorParams(size=3, distance=Distance.COSINE)
    client.create_collection(collection_name="memory_Empty", vectors_config=vectors)
    client.close()
    storage = qdrant / "collection" / "memory_Empty" / "storage.sqlite"
    assert storage.is_file()  # QdrantLocal 建 collection 時就建出空的 storage.sqlite
    if storage_missing:
        storage.unlink()

    archive_path = _engine(mb, layout).run_backup(layout["out"], "Touka_Backup")

    member = "data/vault/_qdrant_db/collection/memory_Empty/storage.sqlite"
    assert (member in _names(archive_path)) is not storage_missing
    restored = tmp_path / "restored"
    restored.mkdir()
    resolver = mb.PathResolver(
        layout["app"], env_path=tmp_path / "env", data_root=restored, config_dir=layout["config"]
    )
    mb.RestoreEngine(resolver).run_restore(str(archive_path))
    reopened = QdrantClient(path=str(restored / "vault" / "_qdrant_db"))
    try:
        assert [c.name for c in reopened.get_collections().collections] == ["memory_Empty"]
        assert reopened.count("memory_Empty").count == 0
    finally:
        reopened.close()


def _plain_qdrant_dir(layout: dict[str, Path]) -> Path:
    qdrant = layout["data"] / "vault" / "_qdrant_db"
    _seed_qdrant(qdrant).close()
    return qdrant


def test_qdrant_meta_changing_mid_copy_is_retried(mb, layout, monkeypatch):
    qdrant = _plain_qdrant_dir(layout)
    meta = qdrant / "meta.json"
    real_copy = fm.FactualStore.copy_sqlite_snapshot
    calls = {"n": 0}

    def _copy_then_touch_meta(source: Path, target: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次複製途中，有人新增／刪除了 collection
            meta.write_text(meta.read_text(encoding="utf-8") + " ", encoding="utf-8")
        real_copy(source, target)

    monkeypatch.setattr(fm.FactualStore, "copy_sqlite_snapshot", _copy_then_touch_meta)

    archive_path = _engine(mb, layout).run_backup(layout["out"], "Touka_Backup")

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("data/vault/_qdrant_db/meta.json") == meta.read_bytes()
    assert calls["n"] > 2


def test_qdrant_meta_that_never_settles_fails_the_snapshot(mb, layout, monkeypatch):
    qdrant = _plain_qdrant_dir(layout)
    meta = qdrant / "meta.json"
    real_copy = fm.FactualStore.copy_sqlite_snapshot

    def _always_touch(source: Path, target: Path) -> None:
        meta.write_text(meta.read_text(encoding="utf-8") + " ", encoding="utf-8")
        real_copy(source, target)

    monkeypatch.setattr(fm.FactualStore, "copy_sqlite_snapshot", _always_touch)

    with pytest.raises(RuntimeError):
        _engine(mb, layout).run_backup(layout["out"], "Touka_Backup")
    assert not any(p.suffix == ".zip" for p in layout["out"].iterdir())


# ── 由 Proxy 呼叫：四個路徑參數一起給，不猜 .env ──────────────────────────────────


def test_main_with_explicit_paths_never_guesses_env(mb, layout, monkeypatch):
    guessed: list[object] = []

    monkeypatch.setattr(mb, "find_project_root", guessed.append)
    monkeypatch.setattr(mb.PathResolver, "get_heartbeat_config_path", lambda _self: layout["app"])
    argv = [
        "manage_backup.py", "backup", "--no-docker",
        "--data-root", str(layout["data"]),
        "--env-file", str(layout["env"]),
        "--config-dir", str(layout["config"]),
        "--output-dir", str(layout["out"]),
    ]  # fmt: skip
    monkeypatch.setattr(sys, "argv", argv)

    mb.main()

    created = layout["out"] / f"{STEM}.zip"
    assert guessed == []  # 沒有往上找 .env
    assert {"data/soul/Alice/soul.md", ".env"} <= _names(created)


def test_main_rejects_partial_path_arguments(mb, layout, monkeypatch):
    argv = ["manage_backup.py", "backup", "--no-docker", "--data-root", str(layout["data"])]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit):
        mb.main()
