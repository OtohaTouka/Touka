# ruff: noqa: T201, S602, S603, S607
"""Touka 系統備份與還原工具（CLI）。

將 .env、Proxy/Heartbeat 設定與 Sanctuary 資料（soul/custom/vault/personal_drive/
stats/legacy）打包成單一 zip；還原時可重映射 SANCTUARY_DATA_PATH。根目錄偵測為
env-aware 多-.env（ADR-0052）：選真正含 SANCTUARY_DATA_PATH 的那份 .env，而非第一個
遇到的子專案 .env。Proxy 呼叫時改由參數給路徑（資料根、.env、設定目錄、輸出目錄），不猜。

執行中可能被寫入的 SQLite（記憶庫、Qdrant 各 collection）用 SQLite backup API 取一致複本；
快照先寫暫存檔再以新檔名發布，絕不覆蓋既有快照。代理人資料夾（個人空間與自訂模型）備份與
還原都不跟隨連結（ADR-0293），略過的連結與名稱印出計數。
"""
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from memory_server.factual import FactualStore
from memory_server.utils.no_follow import (
    LinkRefusedError,
    WalkReport,
    open_read_no_follow,
    walk_open_no_follow,
    walk_pruned,
    write_stream_no_follow,
    zip_open_file,
)
from memory_server.utils.zone_paths import (
    Anchor,
    ZoneRoots,
    anchor_dirname,
    contains_zone,
    ensure_anchor,
    is_zone_segment,
    zone_of,
)

# 代理人資料夾（ADR-0293）：個人空間與自訂模型，另外不跟隨連結地備份與還原
PERSONAL_DIR = anchor_dirname(Anchor.PERSONAL_DRIVE)
CUSTOM_DIR = anchor_dirname(Anchor.CUSTOM_DRIVE)
_AGENT_FOLDERS = {
    os.path.normcase(PERSONAL_DIR): Anchor.PERSONAL_DRIVE,
    os.path.normcase(CUSTOM_DIR): Anchor.CUSTOM_DRIVE,
}
# 備份涵蓋的 Sanctuary 資料子目錄（SSOT）。legacy 為舊版 agent 資料，亦需備份。
# avatars（ADR-0106）：使用者頭像上傳檔，須納入備份否則還原時遺失。
SANCTUARY_FOLDERS = ["soul", CUSTOM_DIR, "vault", PERSONAL_DIR, "stats", "legacy", "avatars"]
OAUTH_RUNTIME_FILENAMES = (
    "mcp_oauth.sqlite3",
    "mcp_oauth.sqlite3-wal",
    "mcp_oauth.sqlite3-shm",
    "mcp_oauth_workload.sqlite3",
    "mcp_oauth_workload.sqlite3-wal",
    "mcp_oauth_workload.sqlite3-shm",
)
OAUTH_SECRET_ENV_KEYS = frozenset(
    {
        "TOUKA_MCP_OAUTH_KEY",
        "TOUKA_MCP_OAUTH_RETIRED_KEYS",
        "TOUKA_MCP_OAUTH_WORKLOAD_KEY",
        "TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS",
    }
)
OAUTH_ROTATION_ENV_KEYS = frozenset(
    {
        "TOUKA_MCP_OAUTH_KEY_EPOCH",
        "TOUKA_MCP_OAUTH_ROTATION_PHASE",
        "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH",
        "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE",
    }
)


# 執行中可能被寫入的 SQLite（zip 內路徑）：改用 backup API 取一致複本，旁邊的 journal 不收。
MEMORY_DB_ARC = PurePosixPath("data/vault/core_memory.db")
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
# Qdrant 本機模式：meta.json 列出 collection，資料在 collection/<名稱>/storage.sqlite。
QDRANT_ARC = PurePosixPath("data/vault/_qdrant_db")
QDRANT_META = "meta.json"
QDRANT_STORAGE = "storage.sqlite"
QDRANT_SNAPSHOT_ATTEMPTS = 5  # 複製期間 meta.json 被改（新增／刪除 collection）時最多重來幾次
BACKUP_NAME_MAX_SUFFIX = 99  # 同一秒撞名時最多試幾個序號
PARTIAL_SUFFIX = ".partial"  # 寫到一半的暫存檔（不符 *.zip，清單與排程都看不到）
SNAPSHOT_WORKDIR_PREFIX = ".snapshot-"  # SQLite 複本的工作資料夾（中斷殘留由清除依形狀清掉）
# 發布改名遇到暫時占用時的重試；同 memory_scrub.swap 的 RENAME_ATTEMPTS／RENAME_RETRY_DELAY_S
# （腳本不載入 Proxy，數值由測試核對兩邊一致）
PUBLISH_RENAME_ATTEMPTS = 3
PUBLISH_RETRY_DELAY_S = 0.5
_SUPPORTS_DIR_FSYNC = os.name != "nt"  # Windows 不支援對資料夾 fsync


class OAuthRestoreConflictError(RuntimeError):
    """Restore must not overwrite or orphan an existing OAuth credential store."""


def _env_key(line: str) -> str | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def _is_oauth_secret_or_rotation_key(key: str) -> bool:
    return key in OAUTH_SECRET_ENV_KEYS or key in OAUTH_ROTATION_ENV_KEYS


def _filtered_env_text(env_path: Path) -> str:
    """Build the archived .env copy in memory without touching the live file."""
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(
        line
        for line in lines
        if (key := _env_key(line)) is None
        or not _is_oauth_secret_or_rotation_key(key)
    )


def _env_has_oauth_key(env_path: Path) -> bool:
    if not env_path.exists():
        return False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key = _env_key(line)
        if key in OAUTH_SECRET_ENV_KEYS and line.split("=", 1)[1].strip():
            return True
    return False


class PathResolver:
    """SSOT Path Resolver for Touka System."""

    def __init__(self, project_root: str, *, env_path=None, data_root=None, config_dir=None):
        """初始化路徑解析器.

        Args:
            project_root (str): Touka 系統的專案根目錄物理路徑。
            env_path: Proxy 傳入的 .env 路徑；有給就用。
            data_root: Proxy 傳入的資料根；有給就用（不存在直接失敗），不從 .env 猜。
            config_dir: Proxy 傳入的設定目錄；有給就用。
        """
        self.project_root = project_root
        self._env_path = env_path
        self._data_root = data_root
        self._config_dir = config_dir

    def get_env_path(self) -> str:
        """回傳 .env 路徑（Proxy 有給就用，否則專案根目錄的 .env）。"""
        return Path(self._env_path) if self._env_path else Path(self.project_root) / ".env"

    def get_sanctuary_path(self) -> str:
        """回傳資料根：Proxy 給的就用（不存在直接失敗）；否則從根 .env 讀 SANCTUARY_DATA_PATH。"""
        if self._data_root is not None:
            if not Path(self._data_root).is_dir():
                msg = f"CRITICAL: data root not found: {self._data_root}"
                raise RuntimeError(msg)
            return str(self._data_root)
        env_path = self.get_env_path()
        if not Path(env_path).exists():
            msg = "CRITICAL: .env file not found. Cannot resolve SANCTUARY_DATA_PATH."
            raise RuntimeError(msg)

        with Path(env_path).open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("SANCTUARY_DATA_PATH="):
                    return line.split("=")[1].strip().strip('"').strip("'")
        msg = "CRITICAL: SANCTUARY_DATA_PATH not found in .env."
        raise RuntimeError(msg)

    def get_proxy_config_dir(self) -> str:
        """回傳 Proxy 設定目錄（Proxy 有給就用，否則 ToukaProxy/config）。"""
        if self._config_dir:
            return Path(self._config_dir)
        return Path(self.project_root) / "ToukaProxy" / "config"

    def get_heartbeat_config_path(self) -> str:
        """回傳 ToukaHeartbeat/config.json 路徑。"""
        return Path(self.project_root) / "ToukaHeartbeat" / "config.json"


class DockerController:
    """Manages Docker container states."""

    def __init__(self, project_root: str):
        """初始化 Docker 控制器.

        Args:
            project_root (str): 含有 docker-compose.yml 檔案的專案根目錄路徑。
        """
        self.project_root = project_root

    def stop_containers(self):
        """停止 Touka 容器以確保備份資料一致性。"""
        print("Stopping Touka containers to ensure data integrity...")
        try:
            subprocess.run(["docker-compose", "down"], cwd=self.project_root, check=True)
        except Exception as e:
            print(f"Warning: Failed to stop containers: {e}")

    def start_containers(self):
        """重新啟動 Touka 容器。"""
        print("Starting Touka containers...")
        try:
            subprocess.run(["docker-compose", "up", "-d"], cwd=self.project_root, check=True)
        except Exception as e:
            print(f"Error: Failed to start containers: {e}")


class BackupEngine:
    """Handles the backup process."""

    def __init__(self, resolver: PathResolver):
        """初始化備份引擎.

        Args:
            resolver (PathResolver): 用於查找系統物理路徑的解析器實例。
        """
        self.resolver = resolver

    def collect_items(self) -> list[tuple]:
        """Returns a list of tuples (source_path, arcname)."""
        items = []

        # 1. .env
        env_path = self.resolver.get_env_path()
        if Path(env_path).exists():
            items.append((env_path, ".env"))

        # 2. Proxy Configs
        proxy_config = self.resolver.get_proxy_config_dir()
        if Path(proxy_config).exists():
            for file in Path(proxy_config).iterdir():
                if file.name.endswith(".json") and not file.name.endswith(".example"):
                    items.append((file, f"config/proxy/{file.name}"))

        # 3. Heartbeat Config
        hb_config = self.resolver.get_heartbeat_config_path()
        if Path(hb_config).exists():
            items.append((hb_config, "config/heartbeat/config.json"))

        # 4. Sanctuary Data
        data_path = self.resolver.get_sanctuary_path()
        if Path(data_path).exists():
            for folder in SANCTUARY_FOLDERS:
                if os.path.normcase(folder) in _AGENT_FOLDERS:
                    continue  # 寫 zip 時才走訪（_write_agent_folders）
                folder_path = Path(data_path) / folder
                if not folder_path.exists():
                    continue
                for full_path in folder_path.rglob("*"):
                    if full_path.is_file():
                        rel_path = full_path.relative_to(data_path)
                        # zipfile 會將 Path 統一成正斜線（跨平台）
                        arcname = Path("data") / rel_path
                        items.append((full_path, arcname))

            # 5. Root-level DBs（ADR-0104）：ui_sessions.db / feedback.db 等坐落於 data 根目錄，
            # 不在任何 SANCTUARY_FOLDERS 子夾內。過去 collect_items 只 rglob 子夾，從未納入這些
            # 根層 DB——使 DebugAgent 重置宣稱的「備份優先」對 sessions/feedback 形同虛設（清了卻
            # 無從還原）。明確納入，讓全系統備份名副其實（restore 的 data/ 分支已能處理根層成員）。
            for db_file in sorted(Path(data_path).glob("*.db")):
                if db_file.is_file():
                    items.append((db_file, Path("data") / db_file.name))

        return items

    def run_backup(self, backup_dir: Path, prefix: str) -> Path:
        """收集所有項目寫成新的 zip（先寫暫存檔、fsync，再以沒人用過的檔名發布）；回傳路徑。"""
        items = self.collect_items()
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=PARTIAL_SUFFIX, dir=backup_dir)
        tmp = Path(tmp_name)
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                tempfile.TemporaryDirectory(prefix=SNAPSHOT_WORKDIR_PREFIX, dir=backup_dir) as work,
            ):
                with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zipf:
                    _write_items(zipf, items, Path(work))
                    report = _write_agent_folders(zipf, self.resolver.get_sanctuary_path())
                raw.flush()
                os.fsync(raw.fileno())
            output = _publish_new(tmp, backup_dir, stem)
        except BaseException:
            _discard_partial(tmp)
            raise
        _print_skipped(report)
        print(f"Backup completed: {output}")
        return output


def _write_items(zipf: zipfile.ZipFile, items: list[tuple], work: Path) -> None:
    """一般檔照原樣；記憶庫與 Qdrant 改寫一致複本；記憶庫旁的 journal 類檔不收。"""
    sidecars = {MEMORY_DB_ARC.with_name(MEMORY_DB_ARC.name + s) for s in SQLITE_SIDECAR_SUFFIXES}
    qdrant_dir = None
    for src, arc in items:
        name = PurePosixPath(str(arc).replace("\\", "/"))
        if str(name) == ".env":
            zipf.writestr(".env", _filtered_env_text(Path(src)))
        elif name == MEMORY_DB_ARC:
            zipf.write(_sqlite_copy(Path(src), work), arc)
        elif QDRANT_ARC in name.parents:
            if name == QDRANT_ARC / QDRANT_META:
                qdrant_dir = Path(src).parent
        elif name not in sidecars:
            zipf.write(src, arc)
    if qdrant_dir is not None:
        meta, copies = _qdrant_snapshot(qdrant_dir, work)
        zipf.writestr(str(QDRANT_ARC / QDRANT_META), meta)
        for collection, copy in copies.items():
            zipf.write(copy, str(QDRANT_ARC / "collection" / collection / QDRANT_STORAGE))


def _write_agent_folders(zipf: zipfile.ZipFile, data_path: str) -> WalkReport:
    """個人空間與自訂模型：走訪時不跟隨連結、一次開一個檔案寫進 zip（ADR-0293）.

    個人空間與每個 ``custom/<id>/drive`` 用區內走訪；``custom`` 其他地方（設定、人格）用
    ``walk_pruned``，每個檔案不跟隨地開。略過的連結、名稱與讀不到的記在回傳的統計。
    """
    report = WalkReport()
    roots = ZoneRoots(Path(data_path).absolute(), None)
    arc = PurePosixPath("data")
    _zip_zone(zipf, Path(roots.resolved(Anchor.PERSONAL_DRIVE)), arc / PERSONAL_DIR, report)
    custom = Path(roots.resolved(Anchor.CUSTOM_DRIVE))
    try:
        with contextlib.closing(walk_pruned(custom, roots, report)) as walk:
            for entry in walk:
                if entry.kind == "file":
                    name = str(arc.joinpath(CUSTOM_DIR, *entry.parts))
                    _zip_one(zipf, custom.joinpath(*entry.parts), name, report)
    except LinkRefusedError:
        report.links += 1  # custom 本身路上被換成連結：走訪開始時就拒絕
    for anchor, parts in list(report.pruned):
        if anchor is Anchor.CUSTOM_DRIVE and parts:
            _zip_zone(zipf, custom.joinpath(*parts), arc.joinpath(CUSTOM_DIR, *parts), report)
    return report


def _zip_zone(zipf: zipfile.ZipFile, root: Path, arc: PurePosixPath, report: WalkReport) -> None:
    """一個區（個人空間或 ``custom/<id>/drive``）：區內走訪開好的檔案寫進 zip。"""
    try:
        with contextlib.closing(walk_open_no_follow(root, report)) as walk:
            for parts, handle, info in walk:
                zip_open_file(zipf, str(arc.joinpath(*parts)), handle, info)
    except LinkRefusedError:
        report.links += 1  # 區根路上被換成連結：走訪開始時就拒絕，還沒寫任何東西


def _zip_one(zipf: zipfile.ZipFile, path: Path, arcname: str, report: WalkReport) -> None:
    """``custom/<id>`` 裡區外的一個檔案：不跟隨連結地開，寫進 zip。"""
    try:
        handle, info = open_read_no_follow(path)
    except LinkRefusedError:
        report.links += 1
        return
    except OSError as exc:
        report.errors.append((arcname, exc.errno or 0))
        return
    with handle:
        zip_open_file(zipf, arcname, handle, info)


def _print_skipped(report: WalkReport) -> None:
    print(
        f"Skipped {report.links} links, {len(report.bad_names)} bad names, "
        f"{len(report.errors)} unreadable"
    )
    for name in report.bad_names:
        print(f"  skipped name: {name!r}")


def _sqlite_copy(source: Path, work: Path) -> Path:
    fd, name = tempfile.mkstemp(suffix=".sqlite", dir=work)
    os.close(fd)
    FactualStore.copy_sqlite_snapshot(source, Path(name))
    return Path(name)


def _qdrant_snapshot(qdrant_dir: Path, work: Path) -> tuple[bytes, dict[str, Path]]:
    """複製 meta.json 列出的每個 collection；前後讀到的 meta.json 一樣才算數，否則重來。"""
    meta_path = qdrant_dir / QDRANT_META
    for _ in range(QDRANT_SNAPSHOT_ATTEMPTS):
        meta = meta_path.read_bytes()
        try:
            copies = {}
            for name in json.loads(meta)["collections"]:
                if name in {"", ".", ".."} or Path(name).name != name:
                    msg = f"CRITICAL: unexpected Qdrant collection name: {name!r}"
                    raise RuntimeError(msg)
                storage = qdrant_dir / "collection" / name / QDRANT_STORAGE
                if storage.is_file():  # 列了卻沒有 storage：照實不收，還原時 QdrantLocal 會建空的
                    copies[name] = _sqlite_copy(storage, work)
        except (ValueError, KeyError, TypeError, FileNotFoundError):
            continue  # meta.json 正在改寫、或 collection 剛被刪：重來
        if meta_path.read_bytes() == meta:
            return meta, copies
    msg = "CRITICAL: Qdrant meta.json kept changing during the snapshot"
    raise RuntimeError(msg)


def _publish_new(tmp: Path, backup_dir: Path, stem: str) -> Path:
    """先以 O_EXCL 佔一個沒人用過的檔名，再用暫存檔取代自己剛佔的空檔；絕不覆蓋既有快照。

    改名失敗時刪掉自己佔的空檔再往外丟：留著的 0 位元組 zip 會被修剪算成一份備份。
    """
    for suffix in range(1, BACKUP_NAME_MAX_SUFFIX + 1):
        final = backup_dir / (f"{stem}.zip" if suffix == 1 else f"{stem}_{suffix}.zip")
        try:
            os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        try:
            _replace_with_retry(tmp, final)
        except BaseException:
            _drop_empty_placeholder(final)
            raise
        _fsync_dir(backup_dir)
        return final
    msg = f"CRITICAL: no free backup name for {stem}"
    raise FileExistsError(msg)


def _replace_with_retry(tmp: Path, final: Path) -> None:
    """Windows 上剛 fsync 完的大檔常被防毒或索引暫時占用：PermissionError 時隔一下再試。"""
    for attempt in range(1, PUBLISH_RENAME_ATTEMPTS + 1):
        try:
            tmp.replace(final)
        except PermissionError:
            if attempt == PUBLISH_RENAME_ATTEMPTS:
                raise
            time.sleep(PUBLISH_RETRY_DELAY_S)
        else:
            return


def _discard_partial(tmp: Path) -> None:
    """刪掉寫到一半的暫存；被占用刪不掉時只警告，讓原本的錯誤照樣往外丟（清除會掃掉它）。"""
    try:
        tmp.unlink(missing_ok=True)
    except OSError as exc:
        print(f"WARNING: could not remove the partial backup {tmp.name}: {exc}",
              file=sys.stderr)


def _drop_empty_placeholder(final: Path) -> None:
    """刪掉自己佔的空檔；已經不是 0 位元組就不碰（那不是我們寫的）。刪不掉只警告。"""
    try:
        if final.stat().st_size == 0:
            final.unlink()
    except FileNotFoundError:
        return  # 已經不在了，沒有要清的
    except OSError as exc:
        print(f"WARNING: could not remove the empty placeholder {final.name}: {exc}",
              file=sys.stderr)


def _fsync_dir(path: Path) -> None:
    """改名後把資料夾項目寫到磁碟（同 ResourceManager.fsync_dir）；失敗只警告，備份仍算完成。"""
    if not _SUPPORTS_DIR_FSYNC:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"WARNING: could not sync the backup folder {path}: {exc}", file=sys.stderr)


class RestoreEngine:
    """Handles the restore process with path re-mapping."""

    def __init__(self, resolver: PathResolver):
        """初始化還原引擎.

        Args:
            resolver (PathResolver): 用於查找系統物理路徑的解析器實例。
        """
        self.resolver = resolver

    def run_restore(self, zip_path: str, new_data_path: str | None = None):
        """從 zip 還原 .env／設定／資料；可選擇將資料重映射到 new_data_path。"""
        if not Path(zip_path).exists():
            print(f"Error: Backup file {zip_path} not found.")
            return

        proxy_config = Path(self.resolver.get_proxy_config_dir())
        env_path = Path(self.resolver.get_env_path())
        has_runtime_store = any(
            (proxy_config / filename).exists()
            for filename in OAUTH_RUNTIME_FILENAMES
        )
        has_injected_key = any(
            os.environ.get(key, "").strip() for key in OAUTH_SECRET_ENV_KEYS
        )
        if has_runtime_store or _env_has_oauth_key(env_path) or has_injected_key:
            raise OAuthRestoreConflictError("MCP_OAUTH_RESTORE_CONFLICT")

        with zipfile.ZipFile(zip_path, "r") as zipf:
            # 1. Restore .env first（寫回 resolver 的 .env 位置；Proxy 呼叫時是參數給的那份）
            Path(self.resolver.get_env_path()).write_bytes(zipf.read(".env"))

            # 2. If new data path provided, update .env
            current_data_path = self.resolver.get_sanctuary_path()
            target_data_path = new_data_path or current_data_path

            if new_data_path:
                print(f"Updating SANCTUARY_DATA_PATH to: {new_data_path}")
                env_path = self.resolver.get_env_path()
                with Path(env_path).open(encoding="utf-8") as f:
                    lines = f.readlines()
                with Path(env_path).open("w", encoding="utf-8") as f:
                    for line in lines:
                        if line.startswith("SANCTUARY_DATA_PATH="):
                            f.write(f"SANCTUARY_DATA_PATH={new_data_path.replace(os.sep, '/')}\n")
                        elif line.startswith("MEMORY_APP_DATA_DIR="):
                            f.write(f"MEMORY_APP_DATA_DIR={new_data_path.replace(os.sep, '/')}\n")
                        else:
                            f.write(line)

            # 3. Restore Configs
            report = WalkReport()
            for member in zipf.namelist():
                if member.startswith("config/proxy/"):
                    arcname = member.replace("config/proxy/", "")
                    # 和 data 一樣：不是一般相對路徑的會搬到設定資料夾以外，略過（ADR-0293）
                    if not _plain_relative(arcname):
                        report.bad_names.append(member)
                        continue
                    zipf.extract(member, self.resolver.get_proxy_config_dir())
                    # Move to correct location if needed (Zip preserves structure)
                    src = Path(self.resolver.get_proxy_config_dir()) / member
                    dst = Path(self.resolver.get_proxy_config_dir()) / arcname
                    if Path(src).exists():
                        shutil.move(src, dst)
                elif member == "config/heartbeat/config.json":
                    hb_parent = Path(self.resolver.get_heartbeat_config_path()).parent
                    zipf.extract(member, hb_parent)
                    src = hb_parent / member
                    dst = self.resolver.get_heartbeat_config_path()
                    if Path(src).exists():
                        shutil.move(src, dst)
                elif member.startswith("data/"):
                    target = Path(target_data_path).absolute()
                    if _restore_agent_member(zipf, member, target, report):
                        continue
                    arcname = member.removeprefix("data/")
                    # 雙斜線、``.``、``..`` 或根開頭的名稱會讓落點離開 data 路徑、比不到資料夾名稱：
                    # 略過（ADR-0293）
                    if not _plain_relative(arcname):
                        report.bad_names.append(member)
                        continue
                    # 別名（短檔名、大小寫不分的掛載）比不到代理人資料夾的名稱：落點以身分判斷在它
                    # 裡面或包含它就略過，不走會跟隨連結的舊路（ADR-0293）
                    roots = ZoneRoots(target, None)
                    if zone_of(target / arcname, roots) or contains_zone(target / arcname, roots):
                        report.bad_names.append(member)
                        continue
                    # Extract to the TARGET data path
                    Path(target_data_path).mkdir(parents=True, exist_ok=True)
                    zipf.extract(member, target_data_path)
                    # Move to flat structure under data/
                    src = Path(target_data_path) / member
                    dst = Path(target_data_path) / arcname
                    # Ensure parent dir exists
                    Path(Path(dst).parent).mkdir(parents=True, exist_ok=True)
                    if Path(src).exists():
                        if Path(src).is_dir():
                            if Path(dst).exists():
                                shutil.rmtree(dst)
                            shutil.move(src, dst)
                        else:
                            shutil.move(src, dst)

            # Cleanup empty extracted structure
            config_tmp = Path(self.resolver.get_proxy_config_dir()) / "config"
            if Path(config_tmp).exists():
                shutil.rmtree(config_tmp)
            data_tmp = Path(target_data_path) / "data"
            if Path(data_tmp).exists():
                shutil.rmtree(data_tmp)
            hb_tmp = Path(self.resolver.get_heartbeat_config_path()).parent / "config"
            if Path(hb_tmp).exists():
                shutil.rmtree(hb_tmp)

        _print_skipped(report)
        print(f"Restore completed from: {zip_path}")


def _plain_relative(name: str) -> bool:
    """一般的相對路徑：沒有根或磁碟代號，也沒有空段、``.``、``..``."""
    parts = name.removesuffix("/").split("/")
    return not Path(name).anchor and all(part not in ("", ".", "..") for part in parts)


def _restore_agent_member(
    zipf: zipfile.ZipFile, member: str, target: Path, report: WalkReport
) -> bool:
    """代理人資料夾的成員不跟隨連結地寫回（ADR-0293）；不是代理人資料夾的回 False.

    名稱只用 ``/`` 拆、每段都要能當區裡的一段（不合格的略過並計數）；資料夾項目略過（寫檔時建）。
    ``custom/<id>`` 的設定與人格也一樣：寫入時從根檢查整條路徑。
    """
    head, _, rest = member.removeprefix("data/").partition("/")
    anchor = _AGENT_FOLDERS.get(os.path.normcase(head))
    if anchor is None:
        return False
    if not rest or member.endswith("/"):
        return True
    segments = rest.split("/")
    if not all(is_zone_segment(segment) for segment in segments):
        report.bad_names.append(member)
        return True
    base = Path(ensure_anchor(ZoneRoots(target, None), anchor))
    try:
        with zipf.open(member) as source:
            write_stream_no_follow(base.joinpath(*segments), source)
    except LinkRefusedError:
        report.links += 1
    return True


def _env_contains_key(env_path: Path, key: str) -> bool:
    """Return True if the ``.env`` at ``env_path`` defines ``key`` (a ``KEY=`` line)."""
    try:
        with env_path.open(encoding="utf-8") as f:
            return any(line.lstrip().startswith(f"{key}=") for line in f)
    except OSError:
        return False


def find_project_root(start_dir: Path, marker_key: str = "SANCTUARY_DATA_PATH") -> Path:
    """Locate the Touka project root in an env-aware multi-.env layout (ADR-0052).

    Walk upward from ``start_dir`` and return the first ancestor whose ``.env``
    actually defines ``marker_key``. This skips subproject envs such as
    ``ToukaProxy/.env`` (which lack ``SANCTUARY_DATA_PATH``) and lands on the ROOT
    ``.env``. Falls back to the outermost ``.env`` seen, otherwise two levels up.
    """
    start_dir = Path(start_dir).resolve()
    outermost_with_env: Path | None = None
    root = start_dir
    while True:
        if (root / ".env").exists():
            if _env_contains_key(root / ".env", marker_key):
                return root
            outermost_with_env = root
        if root.parent == root:
            break
        root = root.parent
    if outermost_with_env is not None:
        return outermost_with_env
    return start_dir.parent.parent


def main():
    """CLI 進入點：解析參數並執行 backup 或 restore。"""
    parser = argparse.ArgumentParser(description="Touka System Backup & Restore Tool")
    parser.add_argument("action", choices=["backup", "restore"], help="Action to perform")
    parser.add_argument("--file", help="Backup file path (for restore)")
    parser.add_argument("--new-data-path", help="New SANCTUARY_DATA_PATH for restore (optional)")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Don't stop/start docker containers",
    )
    parser.add_argument(
        "--prefix",
        default="Touka_Backup",
        help="Backup filename prefix (auto-backups use a distinct prefix, e.g. Touka_AutoBackup)",
    )

    path_args = ("data_root", "env_file", "config_dir", "output_dir")
    for name in path_args:
        parser.add_argument(f"--{name.replace('_', '-')}", help="Path from the running Proxy")

    args = parser.parse_args()
    given = [getattr(args, name) for name in path_args]
    if any(given) and not all(given):
        parser.error("--data-root, --env-file, --config-dir and --output-dir go together")

    if all(given):
        # Proxy 呼叫：路徑全由參數給，不往上找 .env（容器裡找不到，也不能讀到別份 .env）。
        root = Path(__file__).resolve().parents[2]
        resolver = PathResolver(
            root, env_path=args.env_file, data_root=args.data_root, config_dir=args.config_dir
        )
        backup_dir = Path(args.output_dir)
    else:
        # Env-aware project-root detection (ADR-0052 multi-.env): pick the .env that
        # actually defines SANCTUARY_DATA_PATH (the ROOT), not the first subproject .env
        # found while walking up (e.g. ToukaProxy/.env, which lacks it).
        root = find_project_root(Path(__file__).resolve().parent)
        resolver = PathResolver(root)
        backup_dir = Path(root) / "backups"
    docker = DockerController(root)

    if args.action == "backup":
        # 檔名到秒；同一秒撞名加序號，絕不覆蓋。prefix 區分自動/人工備份。
        if not args.no_docker:
            docker.stop_containers()
        try:
            BackupEngine(resolver).run_backup(backup_dir, args.prefix)
        finally:
            if not args.no_docker:
                docker.start_containers()

    elif args.action == "restore":
        if not args.file:
            print("Error: --file is required for restore.")
            return

        if not args.no_docker:
            docker.stop_containers()
        try:
            RestoreEngine(resolver).run_restore(args.file, args.new_data_path)
        finally:
            if not args.no_docker:
                docker.start_containers()


if __name__ == "__main__":
    main()
