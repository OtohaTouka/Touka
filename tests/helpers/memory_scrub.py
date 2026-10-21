"""清除連備份測試共用的建構工具（不是測試）.

只用合成 ID（``zz-…``），所有檔案都建在呼叫端給的 ``tmp_path`` 底下。資料庫一律用標準函式庫的
sqlite3 直接寫（刻意不開 secure_delete），用來模擬舊版留下的殘片。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import stat
import sys
import time
import types
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server import factual as fm
from memory_server.utils.collection_names import fact_point_id
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from src import config as config_module
from src.services import memory_scrub, system_backup
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Iterable

sqlite = fm.sqlite3

TARGET = "zz-scrub-a"
OTHER = "zz-scrub-b"

ANCIENT_CREATE = (
    "CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT,"
    " tags TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
NOCASE_CREATE = (
    "CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, agent_id TEXT COLLATE NOCASE,"
    " content TEXT, tags TEXT, importance TEXT DEFAULT NULL, emotions TEXT DEFAULT NULL,"
    " created_at INTEGER, updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at)"
    " VALUES (?, ?, ?, '[]', 1, 1)"
)
_SCHEMA_VERSIONS = {"v1": 1, "v2": 2}
_INSERT_V2 = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at, archived,"
    " sources, status, superseded_by, safety_flags) VALUES (?, ?, ?, '[]', 1, 2, ?, ?, ?, ?, ?)"
)
FLAGS = '["instruction_like"]'

# v2 的生命週期列：每個 ID 都有「被取代的舊版（連到新版）」「有安全標記的新版」「單純 forget 的」
V2_MARKERS = ("V2OLDMARK", "V2NEWMARK", "V2FORGOTMARK")


def lifecycle_rows(agent_id: str, tag: str) -> list[tuple]:
    """(id, agent_id, content, archived, sources, status, superseded_by, safety_flags)."""
    old, new, gone = (f"fact_{tag}_old", f"fact_{tag}_new", f"fact_{tag}_gone")
    mark = tag.upper()
    return [
        (old, agent_id, f"{mark} {V2_MARKERS[0]} " + "o" * 4500, 1, None, "superseded", new, None),
        (new, agent_id, f"{mark} {V2_MARKERS[1]}", 0, '[{"kind": "chat"}]', None, None, FLAGS),
        (gone, agent_id, f"{mark} {V2_MARKERS[2]}", 1, None, None, None, None),
    ]


def make_v2_lifecycle_db(path: Path) -> Path:
    """v2 事實庫：目標與另一個 ID 各有一條取代鏈、一筆有安全標記、一筆 forget 的."""
    make_db(path, schema="v2")
    conn = sqlite.connect(path)
    try:
        conn.executemany(_INSERT_V2, lifecycle_rows(TARGET, "t") + lifecycle_rows(OTHER, "o"))
        conn.commit()
    finally:
        conn.close()
    return path


def other_rows(path: Path, target: str = TARGET) -> list[tuple]:
    """不屬於 target 的列，逐列完整內容（含 v2 的 status／superseded_by／safety_flags）."""
    return [row for row in rows(path) if row[1] != target]


def make_db(
    path: Path,
    rows: Iterable[tuple[str, str | None, str]] = (),
    *,
    schema: str = "v1",
    extra_sql: Iterable[str] = (),
) -> Path:
    """建一個事實庫檔.

    ``schema`` 是 ancient／v0／v1／v2／nocase；rows 是 (id, agent_id, content)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(path)
    try:
        if schema == "ancient":
            conn.execute(ANCIENT_CREATE)
        elif schema == "nocase":
            conn.execute(NOCASE_CREATE)
        else:
            for statement in fm._BASELINE_SQL:  # 測試直接用正本的建表語句
                conn.execute(statement)
        if schema in _SCHEMA_VERSIONS:  # 照正本的升級步驟走到指定版本（v1 停在 v1）
            for step in fm.FACTS_SCHEMA_STEPS:
                if step.version > _SCHEMA_VERSIONS[schema]:
                    break
                columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
                for column, sql in step.add_columns:
                    if column not in columns:
                        conn.execute(sql)
                for statement in step.statements:
                    conn.execute(statement)
                conn.execute(step.set_version_sql)
        for statement in extra_sql:
            conn.execute(statement)
        conn.executemany(_INSERT, list(rows))
        conn.commit()
    finally:
        conn.close()
    return path


def delete_rows(path: Path, agent_id: str) -> None:
    """用沒開 secure_delete 的連線刪掉某個 ID 的列（模擬舊版重置留下的殘片）."""
    conn = sqlite.connect(path)
    try:
        conn.execute("PRAGMA secure_delete = OFF")
        conn.execute("DELETE FROM facts WHERE agent_id = ?", (agent_id,))
        conn.commit()
    finally:
        conn.close()


def residue_db(path: Path, marker: str, *, schema: str = "v1") -> Path:
    """目標的列已被舊式刪除、但檔案裡還留著含記號的殘片；另一個 ID 的列完好."""
    make_db(
        path,
        [
            ("fact_t1", TARGET, f"secret {marker} one"),
            ("fact_t2", TARGET, f"secret {marker} " + "x" * 5000),
            ("fact_o1", OTHER, "other memory"),
        ],
        schema=schema,
    )
    delete_rows(path, TARGET)
    assert file_has(path, marker), "fixture must leave residue"
    return path


def file_has(path: Path, marker: str) -> bool:
    """檔案的位元組裡找不找得到記號."""
    return marker.encode() in path.read_bytes()


def rows(path: Path, sql: str = "SELECT * FROM facts ORDER BY id") -> list[tuple]:
    conn = sqlite.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def other_rows_digest(path: Path, target: str = TARGET) -> list[str]:
    """不屬於 target 的列逐列雜湊（測試自己算一份，不用產品函式）."""
    conn = sqlite.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        cursor = conn.execute("SELECT * FROM facts ORDER BY id")
        index = [d[0] for d in cursor.description].index("agent_id")
        return sorted(
            hashlib.sha256(repr(row).encode()).hexdigest() for row in cursor if row[index] != target
        )
    finally:
        conn.close()


def pragma(path: Path, name: str) -> object:
    conn = sqlite.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


# --- 快照、向量庫與沙盒守衛 ------------------------------------------------------

DATE_TIME = (2026, 10, 15, 12, 0, 0)


def make_qdrant(directory: Path, collections: dict[str, list[str]]) -> Path:
    """用真的 QdrantLocal 建本機模式資料夾；collections 是 {名稱: [fact_id...]}，向量 4 維."""
    client = QdrantClient(path=str(directory))
    try:
        for name, fact_ids in collections.items():
            client.create_collection(
                name, vectors_config=VectorParams(size=4, distance=Distance.COSINE)
            )
            if fact_ids:
                client.upsert(
                    name,
                    points=[
                        PointStruct(
                            id=fact_point_id(fid),
                            vector=[0.1, 0.2, 0.3, float(i + 1)],
                            payload={"fact_id": fid},
                        )
                        for i, fid in enumerate(fact_ids)
                    ],
                )
    finally:
        client.close()
    return directory


def zip_tree(root: Path, out: Path, *, extra: dict[str, bytes] | None = None) -> Path:
    """把 root 底下的檔包成 data/<相對路徑>（固定時間、deflate），另外加 extra 成員."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f"data/{path.relative_to(root).as_posix()}", date_time=DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
        for name, data in (extra or {}).items():
            info = zipfile.ZipInfo(name, date_time=DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return out


def zip_members(path: Path) -> list[tuple]:
    with zipfile.ZipFile(path) as zf:
        return [
            (i.filename, i.CRC, i.file_size, i.date_time, i.compress_type, i.external_attr)
            for i in zf.infolist()
        ]


def zip_read(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manage_backup():
    """用 importlib 載入快照腳本（不經 src；P1 的打包介面）."""
    script = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
    spec = importlib.util.spec_from_file_location("manage_backup_scrub_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SandboxBreach(BaseException):  # 不是 Exception：產品的 except Exception 吞不掉
    """清除程式碰了沙盒外的路徑或路徑來源."""


_BREACHES: list[str] = []


def _breach(message: str) -> SandboxBreach:
    """記下違規（就算被吞掉，fixture 收尾也會讓測試紅）並回傳例外."""
    _BREACHES.append(message)
    return SandboxBreach(f"sandbox breach: {message}")


SCRUB_IDLE_TIMEOUT_S = 15.0  # 看門狗：測試結束後等背景清除收尾的上限


def wait_scrub_idle(timeout: float = SCRUB_IDLE_TIMEOUT_S) -> list[str]:
    """看門狗：等清除旗標清掉、升級備份鎖放開；逾時就強制歸零並回報卡住的項目."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not memory_scrub.in_progress() and not fm._UPGRADE_BACKUP_LOCK.locked():
            return []
        time.sleep(0.05)
    stuck = []
    if memory_scrub.in_progress():
        memory_scrub._IN_PROGRESS.clear()
        stuck.append("scrub flag")
    if fm._UPGRADE_BACKUP_LOCK.locked():
        fm._UPGRADE_BACKUP_LOCK.release()
        stuck.append("upgrade backup lock")
    return stuck


def take_breaches() -> list[str]:
    """取出並清空目前記下的違規（故意觸發違規的測試用）."""
    found = list(_BREACHES)
    _BREACHES.clear()
    return found


def assert_no_breach() -> None:
    """fixture 收尾：這個測試期間不得有任何違規."""
    found = take_breaches()
    assert not found, f"sandbox breach during the test: {found}"


def _under(root: Path, value: object) -> None:
    if isinstance(value, (str, Path)) and str(value) not in ("", ":memory:"):
        resolved = Path(value).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise _breach(str(resolved))


def _checked(real, root: Path):
    def wrapper(*args, **kwargs):
        for value in (*args, *kwargs.values()):
            if isinstance(value, dict):
                for inner in value.values():
                    if isinstance(inner, Path):
                        _under(root, inner)
            elif not isinstance(value, (bytes, set, frozenset)):
                _under(root, value)
        return real(*args, **kwargs)

    return wrapper


def _replace_bindings(monkeypatch, original, replacement) -> int:
    count = 0
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if not name.startswith(("src", "memory_server")):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attr, replacement)
                count += 1
    return count


def install_scrub_sandbox(monkeypatch, tmp_path: Path) -> Path:
    """正面白名單：快照目錄換成 tmp_path；路徑來源被呼叫就丟例外；寫入與讀取出口只准 tmp_path.

    違規丟 ``SandboxBreach``（BaseException，產品的 except Exception 吞不掉），同時記進清單；
    呼叫端的 fixture 收尾要呼叫 ``assert_no_breach()``。
    """
    snapshots = importlib.import_module("src.services.memory_scrub.snapshots")
    swap = importlib.import_module("src.services.memory_scrub.swap")
    _BREACHES.clear()
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    _replace_bindings(monkeypatch, system_backup.backup_dir, lambda: backups)

    def breach(*_args, **_kwargs):
        raise _breach("path source called")

    _replace_bindings(monkeypatch, config_module.resolve_env_file_path, breach)
    _replace_bindings(monkeypatch, config_module.get_workspace_root, breach)
    for name in (
        "move_file",
        "remove_file",
        "remove_dir",
        "copy_file",
        "copy_file_metadata",
        "fsync_file",
        "fsync_dir",
        "make_writable",
        "rewrite_archive",
        "write_file_binary",
        "read_file_binary",
        "read_file_prefix",
    ):
        monkeypatch.setattr(
            resource_manager, name, _checked(getattr(resource_manager, name), tmp_path)
        )
    monkeypatch.setattr(fm, "scrub_store_image", _checked(fm.scrub_store_image, tmp_path))
    real_new_temp = swap.new_temp

    def new_temp(directory, base_name):  # 只檢查資料夾；base_name 是檔名、不是路徑
        _under(tmp_path, directory)
        return real_new_temp(directory, base_name)

    monkeypatch.setattr(swap, "new_temp", new_temp)
    real_compact = fm.FactualStore.compact_after_reset

    def compact(self, identity):
        _under(tmp_path, self.db_path)
        return real_compact(self, identity)

    monkeypatch.setattr(fm.FactualStore, "compact_after_reset", compact)
    real_list = system_backup.list_snapshot_files

    def listing(directory=None, **kwargs):
        _under(tmp_path, directory if directory is not None else system_backup.backup_dir())
        return real_list(directory, **kwargs)

    _replace_bindings(monkeypatch, real_list, listing)

    class _CheckedZip(zipfile.ZipFile):
        def __init__(self, file, *args, **kwargs):
            _under(tmp_path, file)
            super().__init__(file, *args, **kwargs)

    proxy = types.SimpleNamespace(
        **{k: getattr(zipfile, k) for k in dir(zipfile) if not k.startswith("__")}
    )
    proxy.ZipFile = _CheckedZip
    monkeypatch.setattr(snapshots, "zipfile", proxy)
    return backups


def fake_links(monkeypatch, *paths: Path) -> None:
    """讓這些路徑的 lstat 看起來是符號連結（不必真的建連結；Windows 建連結要權限）."""
    targets = {Path(p) for p in paths}
    real_lstat = Path.lstat

    def fake_lstat(self: Path, *args, **kwargs):
        if self in targets:
            return types.SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0, st_mtime=0
            )
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
