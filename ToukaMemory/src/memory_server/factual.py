"""事實庫 (Factual Store) 服務.

負責代理人的無損事實記憶存儲。採用 SQLite 進行結構化存儲，
確保 Agent 存取的原始內容不被語義壓縮修改，並提供基於標籤與時間維度的快速索引檢索。
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from memory_server import _get_app_data_dir
from memory_server.config_utils import get_current_epoch, get_resource_proxy
from memory_server.models.source_ref import load_sources
from memory_server.utils.content_guard import SAFETY_FLAG_UNREADABLE

if TYPE_CHECKING:
    from collections.abc import Iterator

    from src.models.identity import AgentIdentity

    from memory_server.models.source_ref import SourceRef

logger = logging.getLogger(__name__)

# 舊版事實檔：遷移成功才會改名成 facts.jsonl.bak；沒改名的檔案代表內容從沒搬進記憶庫。
LEGACY_FACTS_JSONL_PATTERN = "*/fact/facts.jsonl"
_LEGACY_FACTS_JSONL_PARTS = ("fact", "facts.jsonl")

# --- 結構版本與升級備份（SSOT：FACTS_SCHEMA_STEPS；PRAGMA user_version 記錄已套用的版本）---
FACTS_BACKUP_DIR_NAME = "core_memory_backups"  # 在 core_memory.db 旁邊；和系統快照的 backups 分開
FACTS_BACKUP_KEEP = 3  # 升級備份最多留幾份（含這次新建的）
FACTS_BACKUP_SPACE_FACTOR = 2  # 可用空間 < 記憶庫大小 × 倍數，就不備份、不升級
FACTS_SCHEMA_LOCK_TIMEOUT_S = 5.0  # 等寫入鎖的秒數；等不到算「被佔用」
FACTS_STATUS_RECHECK_TIMEOUT_S = 0.5  # 失敗狀態複查檔頭時等鎖的秒數（/memory 端點會呼叫）
FACTS_BACKUP_NAME_MAX_SUFFIX = 99  # 同一秒撞名時最多試幾個序號
FACT_WRITE_FAILED_MESSAGE = "Could not save the fact to the memory store."
FACT_EMPTY_MESSAGE = "Fact content cannot be empty."
_BACKUP_NAME_RE = re.compile(r"^core_memory\.v\d+-to-v\d+\.(\d+)(?:\.(\d+))?\.db$")
_BACKUP_TMP_HEX_LEN = 8  # 暫存檔名：正式備份名 + "." + 這麼多個小寫 hex + _BACKUP_TMP_SUFFIX
_BACKUP_TMP_SUFFIX = ".tmp"
_BACKUP_TMP_RE = re.compile(
    rf"^core_memory\.v\d+-to-v\d+\.\d+(?:\.\d+)?\.db\.[0-9a-f]{{{_BACKUP_TMP_HEX_LEN}}}"
    rf"{re.escape(_BACKUP_TMP_SUFFIX)}(?:-journal)?$"
)
FACTS_DB_FILE_NAME = "core_memory.db"  # FactualStore 的檔名；快照成員比對也用它

# --- 清除連備份（重置時人類勾選）：只刪一個身分的列，副本都在記憶體裡處理 ---
MEMORY_SCRUB_MAX_IMAGE_BYTES = 256 * 1024 * 1024  # 讀進記憶體的資料庫上限；峰值約 2～3 倍
# 清除時寫在升級備份或快照旁的暫存（點開頭、不符上面兩種備份樣式；內容一定是清除後的）
SCRUB_TMP_RE = re.compile(r"^\.(.+)\.memscrub-[A-Za-z0-9_]{8}\.tmp(?:-journal)?$")
_LEGACY_ID_PREFIX = "ToukaSoul"  # 舊前綴 ID：不清，只計數回報
# 升級（備份＋輪替）與清除升級備份共用；不可重入，同一執行緒拿兩次就讓它逾時現形
_UPGRADE_BACKUP_LOCK = threading.Lock()


@dataclass(frozen=True)
class _SchemaStep:
    """一個版本：只准 ADD COLUMN（可 NULL 或常數預設）與 CREATE TABLE／INDEX IF NOT EXISTS."""

    version: int
    set_version_sql: str  # PRAGMA 不能綁參數：每版一句字面量
    add_columns: tuple[tuple[str, str], ...] = ()
    statements: tuple[str, ...] = ()  # 只放 CREATE TABLE／INDEX IF NOT EXISTS（不含 UNIQUE）


# 只增、凍結：一個版本合併後不再改，之後的變更往後加新版本。
FACTS_SCHEMA_STEPS: tuple[_SchemaStep, ...] = (
    _SchemaStep(
        version=1,
        set_version_sql="PRAGMA user_version = 1",
        add_columns=(
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
        ),
    ),
    # v2：記憶會演變（被取代的舊記憶保留並連到新的）＋安全標記；舊資料三欄都是 NULL
    _SchemaStep(
        version=2,
        set_version_sql="PRAGMA user_version = 2",
        add_columns=(
            ("status", "ALTER TABLE facts ADD COLUMN status TEXT DEFAULT NULL"),
            ("superseded_by", "ALTER TABLE facts ADD COLUMN superseded_by TEXT DEFAULT NULL"),
            ("safety_flags", "ALTER TABLE facts ADD COLUMN safety_flags TEXT DEFAULT NULL"),
        ),
        statements=("CREATE INDEX IF NOT EXISTS idx_superseded_by ON facts(superseded_by)",),
    ),
)

# v0 建表與索引：逐句 execute。不用另一種一次跑多句的 API：它會先提交進行中的交易、放掉鎖。
_BASELINE_SQL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT,"
    " tags TEXT, importance TEXT DEFAULT NULL, emotions TEXT DEFAULT NULL,"
    " created_at INTEGER, updated_at INTEGER, archived INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_agent_archived ON facts(agent_id, archived)",
    "CREATE INDEX IF NOT EXISTS idx_created ON facts(created_at)",
)

# 讀事實的欄位：v1 多一欄 sources、v2 再多三欄；目前版讀全部，其他狀態只讀 v0 欄
_FACT_SELECT_V0 = "SELECT id, content, tags, importance, emotions, created_at, updated_at, archived"
_FACT_SELECT_V1 = _FACT_SELECT_V0 + ", sources"
_FACT_SELECT_V2 = _FACT_SELECT_V1 + ", status, superseded_by, safety_flags"
_ALL_FACTS_SELECT_V0 = (
    "SELECT id, agent_id, content, tags, importance, created_at, updated_at, archived"
)
_ALL_FACTS_SELECT_V1 = _ALL_FACTS_SELECT_V0 + ", sources"
_ACTIVE_FACTS_BY_ID = (  # 語意結果回查：id 清單以 JSON 帶入（只綁一個參數，禁令 24）
    " FROM facts WHERE agent_id = ? AND archived = 0 AND id IN (SELECT value FROM json_each(?))"
)


class FactsSchemaStatus(StrEnum):
    """事實庫結構狀態；NO_SPACE 以下四個是失敗，各對應一個提示代碼（細節只進日誌）."""

    CURRENT = "current"
    NEWER = "newer"  # 比程式新：照用、不升級
    OUTDATED = "outdated"  # 落後，但呼叫者不准升級（只有離線工具）
    NO_SPACE = "no_space"
    LOCKED = "locked"
    UNREADABLE = "unreadable"
    FAILED_OTHER = "failed_other"

    @property
    def writable(self) -> bool:
        """可以新增記憶."""
        return self in (FactsSchemaStatus.CURRENT, FactsSchemaStatus.NEWER)

    @property
    def failed(self) -> bool:
        """升級或讀取失敗（會出提示）."""
        return self in _SCHEMA_FAILURES


_SCHEMA_FAILURES = frozenset(
    {
        FactsSchemaStatus.NO_SPACE,
        FactsSchemaStatus.LOCKED,
        FactsSchemaStatus.UNREADABLE,
        FactsSchemaStatus.FAILED_OTHER,
    }
)


class EmbeddingState(StrEnum):
    """事實在語意索引裡的狀態（v1 的 embedding_state 欄）；事實庫是正本，索引可重建."""

    UNVERIFIED = "unverified"  # 欄位預設：升級前的舊事實，或還沒核對過向量庫
    READY = "ready"
    FAILED = "failed"  # 寫向量失敗；事實照樣留著，等手動補索引


class FactStatus(StrEnum):
    """事實的生命週期（v2 的 status 欄）；NULL＝一般（有效，或只是被 forget 封存）."""

    SUPERSEDED = "superseded"  # 被新記憶取代：同時 archived=1，superseded_by 指向新的那筆


class ArchiveResult(StrEnum):
    """封存（forget／resolve）這次做了什麼；只有前兩種有改動，要記事件、同步索引."""

    ARCHIVED = "archived"  # 原本有效，這次封存
    DETACHED = "detached"  # 原本就是封存的舊版本，這次從歷史拿掉
    ALREADY_ARCHIVED = "already_archived"  # 原本就封存、也不在歷史裡：沒有改動
    NOT_FOUND = "not_found"  # 這個身分沒有這筆


class SupersedeTargetError(Exception):
    """要被取代的不是這個身分仍有效的記憶；不存在、別人的、已封存、已被取代都是同一個錯."""

    def __init__(self) -> None:
        super().__init__("The memory to supersede is not an active memory of this identity")


class FactsSchemaNotReadyError(Exception):
    """事實庫結構還不能用（狀態在 .status）；訊息固定，原因只進日誌."""

    def __init__(self, status: FactsSchemaStatus) -> None:
        super().__init__("Fact store is not ready")
        self.status = status


class FactIdConflictError(Exception):
    """要新增的事實 id 已存在；一般 INSERT 絕不覆蓋."""

    def __init__(self) -> None:
        super().__init__("Fact id already exists")


class FactWriteError(Exception):
    """事實寫入資料庫失敗；訊息固定，SQLite 原文只進日誌."""

    def __init__(self) -> None:
        super().__init__(FACT_WRITE_FAILED_MESSAGE)


class _NoSpaceForBackupError(OSError):
    """可用空間不夠先備份：不備份、不升級."""

    def __init__(self) -> None:
        super().__init__(errno.ENOSPC, "Not enough free space to back up the memory store")


def _target_version() -> int:
    """程式要的結構版本；每次呼叫才讀步驟表（測試替換步驟表時跟著變）."""
    return FACTS_SCHEMA_STEPS[-1].version


def _classify_failure(exc: BaseException) -> FactsSchemaStatus:
    """依 errno 與 SQLite 的固定英文訊息分類失敗（注入的例外沒有 sqlite_errorname）."""
    text = str(exc).lower()
    if (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or "disk is full" in text:
        return FactsSchemaStatus.NO_SPACE
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return FactsSchemaStatus.LOCKED
    if isinstance(exc, sqlite3.DatabaseError) and any(
        marker in text for marker in ("not a database", "malformed", "unable to open")
    ):
        return FactsSchemaStatus.UNREADABLE
    return FactsSchemaStatus.FAILED_OTHER


def _classify_read_failure(exc: BaseException) -> FactsSchemaStatus:
    """讀檔頭就失敗：除了被鎖住與空間不足，一律算讀不到（「其他」的提示保證記憶可以查）."""
    status = _classify_failure(exc)
    keep = (FactsSchemaStatus.LOCKED, FactsSchemaStatus.NO_SPACE)
    return status if status in keep else FactsSchemaStatus.UNREADABLE


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """還在交易裡才回滾；回滾本身失敗只記警告（已提交時什麼都不做）."""
    if not conn.in_transaction:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        logger.warning("[Factual] rollback failed", exc_info=True)


def _backup_sort_key(path: Path) -> tuple[int, int]:
    """輪替排序鍵：(epoch, 撞名序號)；沒有序號算 0."""
    match = _BACKUP_NAME_RE.match(path.name)
    epoch, suffix = match.groups() if match else ("0", None)
    return int(epoch), int(suffix or 0)


def _row_to_fact(row: sqlite3.Row) -> dict[str, Any]:
    """事實列 → dict；單一列的 tags／emotions 格式壞掉時當空值，不拖垮整個查詢."""
    item = dict(row)
    item["tags"] = _load_json_field(item, "tags", [])
    item["emotions"] = _load_json_field(item, "emotions", None)
    item["archived"] = bool(item["archived"])
    if "sources" in item:  # 只有 v1 的查詢帶這一欄；舊版結構時不加鍵（顯示「暫時無法讀取」）
        item["sources"] = load_sources(item["sources"], item.get("id"))
    if "safety_flags" in item:  # 只有目前版（v2）的查詢帶這一欄
        item["safety_flags"] = _load_safety_flags(item)
    return item


def _load_safety_flags(item: dict[str, Any]) -> list[str]:
    """NULL＝沒有標記；欄位有值卻讀不出名稱清單（壞掉或空清單）一律當成有標記（fail closed）."""
    if item.get("safety_flags") is None:
        return []
    return _load_json_field(item, "safety_flags", []) or [SAFETY_FLAG_UNREADABLE]


def _load_json_field(item: dict[str, Any], field: str, empty: Any) -> Any:
    raw = item.get(field)
    if not raw:
        return empty
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return value
    # 只記 id，不記內容
    logger.warning("[Factual] fact %s has a malformed %s field", item.get("id"), field)
    return empty


def _default_vault_dir() -> Path:
    """事實庫預設根目錄（與 FactualStore 同一來源）。"""
    return Path(_get_app_data_dir()) / "vault"


def find_unmigrated_jsonl_agents(base_dir: str | Path | None = None) -> list[str]:
    """只看檔名，列出還留著舊版 facts.jsonl 的 agent 目錄名；不讀、不改任何檔案。"""
    root = Path(base_dir) if base_dir else _default_vault_dir()
    proxy = get_resource_proxy()
    if not proxy.exists(str(root)):
        return []
    agents = {
        parts[0]
        for parts in (
            Path(relative).parts
            for relative in proxy.glob_search(str(root), LEGACY_FACTS_JSONL_PATTERN)
        )
        if parts[1:] == _LEGACY_FACTS_JSONL_PARTS
    }
    return sorted(agents)


class FactualStore:
    """無損事實資料庫中心.

    Attributes:
        base_dir (Path): 記憶檔案庫的物理根目錄。
        db_path (Path): SQLite 資料庫檔案路徑。
    """

    _active_connections: ClassVar[set[sqlite3.Connection]] = set()
    _connection_lock: ClassVar[threading.Lock] = threading.Lock()
    _shared: ClassVar[FactualStore | None] = None
    # 類別預設值：object.__new__ 建、只設 db_path 的實例也能跑 _init_db。
    _allow_schema_upgrade: bool = True
    _schema_status: FactsSchemaStatus = FactsSchemaStatus.CURRENT
    _read_failed = False  # 讀不到過、還沒成功讀過一次（檔頭複查不能清它）
    new_store_beside_backups = False  # 建了全新空庫，旁邊卻有升級備份

    @classmethod
    def shared(cls) -> FactualStore:
        """行程內共用的事實庫（MemoryManager 也用這一個）；啟動時不必載入向量模組."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def existing_shared(cls) -> FactualStore | None:
        """目前的共用實例；還沒建就回 None（不建構，給提示讀狀態用）."""
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        """丟掉共用實例；下一次取用重建（換嵌入模型、測試隔離）."""
        cls._shared = None

    @classmethod
    @contextlib.contextmanager
    def _connection(
        cls,
        db_path: Path | str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        isolation_level: str | None = "",
        commit_on_exit: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """建立 transaction context，並保證離開時實際關閉 SQLite handle。"""
        connection = sqlite3.connect(
            db_path, timeout=timeout, isolation_level=isolation_level, uri=uri
        )
        with cls._connection_lock:
            cls._active_connections.add(connection)

        try:
            connection.execute("PRAGMA secure_delete = ON")  # 刪除與搬動釋出的空間一律寫成 0
            with connection if commit_on_exit else contextlib.nullcontext(connection):
                yield connection
        finally:
            try:
                connection.close()
            finally:
                with cls._connection_lock:
                    cls._active_connections.discard(connection)

    # 清除連備份的模組函式用：和 _connection 同一個（一樣被追蹤、一樣開 secure_delete）
    tracked_connection = _connection

    @classmethod
    def copy_sqlite_snapshot(cls, source: Path, target: Path) -> None:
        """用 SQLite backup API 把（可能正被寫入的）資料庫複製成一致的一份，給系統快照用.

        來源只開唯讀連線、絕不改動；有寫入交易時 backup 會等它結束，拿到的是某個提交點。
        複本的 integrity_check 不是 ok 就丟例外；來源不存在丟 FileNotFoundError。
        """
        if not source.is_file():
            raise FileNotFoundError(errno.ENOENT, "SQLite source not found", str(source))
        source_uri = f"{source.absolute().as_uri()}?mode=ro"
        with (
            cls._connection(source_uri, uri=True) as reader,
            cls._connection(target) as copy,
        ):
            reader.backup(copy)
            verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            msg = f"snapshot integrity_check failed: {verdict}"
            raise sqlite3.DatabaseError(msg)

    @classmethod
    def close_all_connections(cls) -> None:
        """關閉所有仍存活的 SQLite handles；可重複呼叫且清理失敗不沉默。"""
        with cls._connection_lock:
            connections = tuple(cls._active_connections)
            cls._active_connections.clear()

        first_error: Exception | None = None
        failed_connections: list[sqlite3.Connection] = []
        for connection in connections:
            try:
                connection.close()
            except Exception as exc:
                failed_connections.append(connection)
                if first_error is None:
                    first_error = exc

        if failed_connections:
            with cls._connection_lock:
                cls._active_connections.update(failed_connections)

        if first_error is not None:
            raise first_error

    def __init__(
        self, base_dir: str | Path | None = None, *, allow_schema_upgrade: bool = True
    ) -> None:
        """初始化事實庫；版本落後且允許時，先備份再在單一交易內升級結構.

        舊版 facts.jsonl 的自動匯入已退役：檔案原地保留、不讀不改，
        只由 ``find_unmigrated_jsonl_agents`` 依檔名提示。

        Args:
            base_dir (str | Path | None): 指定的存儲根目錄。
            allow_schema_upgrade (bool): 離線工具傳 False：版本落後只回 OUTDATED、不寫任何東西。
        """
        self.base_dir = Path(base_dir) if base_dir else _default_vault_dir()
        self._allow_schema_upgrade = allow_schema_upgrade

        get_resource_proxy().make_dir(str(self.base_dir))
        self.db_path = self.base_dir / "core_memory.db"

        self._init_db()

    @property
    def _backup_dir(self) -> Path:
        return self.db_path.parent / FACTS_BACKUP_DIR_NAME

    @property
    def upgrade_backup_dir(self) -> Path:
        """升級備份資料夾（清除連備份要處理的位置）."""
        return self._backup_dir

    def _upgrade_with_backup_lock(self) -> FactsSchemaStatus:
        """升級（含備份與輪替）全程持有升級備份鎖；等不到就算被佔用，記憶庫不動."""
        if not _UPGRADE_BACKUP_LOCK.acquire(timeout=FACTS_SCHEMA_LOCK_TIMEOUT_S):
            logger.warning("[Factual] upgrade backups are being scrubbed; upgrade postponed")
            return FactsSchemaStatus.LOCKED
        try:
            return self._upgrade_locked()
        finally:
            _UPGRADE_BACKUP_LOCK.release()

    def _init_db(self) -> None:
        """建立或升級 facts 結構；任何失敗都收斂成狀態、不外拋（單例一定建得起來）."""
        try:
            status = self._settle(self._read_version(must_exist=not self._allow_schema_upgrade))
            if status is None:
                status = (
                    self._upgrade_with_backup_lock()
                    if self._allow_schema_upgrade
                    else FactsSchemaStatus.OUTDATED
                )
        except Exception as exc:
            logger.warning("[Factual] cannot read the memory store schema", exc_info=True)
            status = _classify_read_failure(exc)
        self._set_status(status)

    def _db_uri(self, mode: str) -> str:
        # absolute()（不是 resolve()）：網路磁碟機不會變 UNC；as_uri() 會編碼空白、#、%。
        return f"{self.db_path.absolute().as_uri()}?mode={mode}"

    def _read_version(self, *, must_exist: bool = False, timeout: float = 5.0) -> int:
        """一律用讀寫連線：當機殘留的 journal 會先被 SQLite 自動回滾；唯讀連線遇到它會報錯.

        must_exist：檔案不存在就失敗（不建出空的 core_memory.db）；離線工具與複查用。
        """
        target = self._db_uri("rw") if must_exist else self.db_path
        with self._connection(target, uri=must_exist, timeout=timeout) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    @staticmethod
    def _settle(version: int) -> FactsSchemaStatus | None:
        """版本落後回 None（要升級）；否則回 CURRENT 或 NEWER."""
        target = _target_version()
        if version < target:
            return None
        return FactsSchemaStatus.NEWER if version > target else FactsSchemaStatus.CURRENT

    def _set_status(self, status: FactsSchemaStatus) -> None:
        if status != self._schema_status:  # 只在轉換時記，/memory 輪詢不會洗版
            level = logging.WARNING if status.failed else logging.INFO
            logger.log(level, "[Factual] memory store schema status: %s", status.value)
        self._schema_status = status

    def _rechecked_status(self) -> FactsSchemaStatus:
        """結構狀態；失敗狀態時重讀一次檔頭（只複查、不重試升級、不建檔、不久等）."""
        if self._schema_status.failed:
            try:
                version = self._read_version(
                    must_exist=True, timeout=FACTS_STATUS_RECHECK_TIMEOUT_S
                )
                settled = self._settle(version)
                if settled is None and self._schema_status is FactsSchemaStatus.UNREADABLE:
                    settled = FactsSchemaStatus.FAILED_OTHER  # 讀得到了，但升級沒完成
            except Exception as exc:
                settled = _classify_read_failure(exc)
            if settled is not None:
                self._set_status(settled)
        return self._schema_status

    def current_schema_status(self) -> FactsSchemaStatus:
        """提示與寫入閘用的狀態：讀不到過、還沒成功讀過一次，就一律算讀不到."""
        status = self._rechecked_status()
        return FactsSchemaStatus.UNREADABLE if self._read_failed else status

    def _require_schema(self, *, write: bool) -> None:
        """寫入要目前版或較新版；查詢只在檔頭讀不到時擋（照常嘗試，成功一次才清旗標）."""
        status = self.current_schema_status() if write else self._rechecked_status()
        if (write and not status.writable) or status is FactsSchemaStatus.UNREADABLE:
            raise FactsSchemaNotReadyError(status)

    def _read_select(self, v0_sql: str, current_sql: str) -> str:
        """讀取的欄位前綴：只看結構版本選（讀不到旗標交給讀取本身）；檔頭讀不到時擋，同查詢閘.

        只有目前版保證有 v1／v2 欄；較新版（降版執行）與失敗狀態都用 v0 欄位。
        """
        status = self._rechecked_status()
        if status is FactsSchemaStatus.UNREADABLE:
            raise FactsSchemaNotReadyError(status)
        return current_sql if status is FactsSchemaStatus.CURRENT else v0_sql

    def _require_current(self) -> None:
        """只在目前版做的讀寫（v2 欄位）：較新版與失敗狀態都擋，不做欄位假設."""
        status = self._rechecked_status()
        if status is not FactsSchemaStatus.CURRENT:
            raise FactsSchemaNotReadyError(status)

    def _upgrade_locked(self) -> FactsSchemaStatus:
        """拿寫入鎖→鎖內重讀→（舊庫）先備份→同一個交易升級；失敗時記憶庫完全不變."""
        backup: Path | None = None
        status = FactsSchemaStatus.CURRENT
        # 不在離開時提交：ROLLBACK 也失敗時，關閉連線就由 SQLite 回滾，做了一半的升級不會被提交。
        with self._connection(
            self.db_path,
            timeout=FACTS_SCHEMA_LOCK_TIMEOUT_S,
            isolation_level=None,
            commit_on_exit=False,
        ) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                settled = self._settle(version)  # 雙重檢查：別的執行緒可能剛升完
                if settled is not None:
                    status = settled
                else:
                    backup = self._prepare_upgrade(conn, version)
                    self._apply_steps(conn, version)
                    conn.execute("COMMIT")
                    logger.info(
                        "[Factual] schema upgraded v%d->v%d (backup: %s)",
                        version,
                        _target_version(),
                        backup.name if backup else "none, new store",
                    )
            except Exception as exc:
                logger.warning(
                    "[Factual] schema upgrade failed; memory store unchanged", exc_info=True
                )
                status = _classify_failure(exc)
            _safe_rollback(conn)  # 已提交時不做事；雙重檢查或失敗時放掉鎖
        self._rotate_backups(backup)
        return status

    @staticmethod
    def _table_columns(conn: sqlite3.Connection) -> set[str]:
        return {row[1] for row in conn.execute("PRAGMA table_info(facts)")}

    def _prepare_upgrade(self, conn: sqlite3.Connection, version: int) -> Path | None:
        """持鎖、還沒改任何東西時：記現況、清中斷的暫存檔、舊庫先備份（回備份路徑）."""
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        logger.info(
            "[Factual] upgrading schema: journal_mode=%s user_version=%d page_count=%d",
            journal_mode,
            version,
            page_count,
        )
        listed = self._backup_dir_files()
        for leftover in (path for path in listed if _BACKUP_TMP_RE.match(path.name)):
            with contextlib.suppress(OSError):
                get_resource_proxy().remove_file(str(leftover))
        if self._table_columns(conn):
            return self._backup(version)
        existing = sum(1 for path in listed if _BACKUP_NAME_RE.match(path.name))
        self.new_store_beside_backups = existing > 0
        if existing:
            logger.warning(
                "[Factual] creating a new empty memory store, but %s holds %d upgrade "
                "backup(s); if you are restoring one, check its file name",
                FACTS_BACKUP_DIR_NAME,
                existing,
            )
        return None

    def _apply_steps(self, conn: sqlite3.Connection, version: int) -> None:
        """基準建表＋各版步驟（欄位先查再加）＋寫版本；全部在呼叫端的交易裡."""
        for statement in _BASELINE_SQL:
            conn.execute(statement)
        for step in FACTS_SCHEMA_STEPS:
            if step.version <= version:
                continue
            existing = self._table_columns(conn)
            for column, sql in step.add_columns:
                if column not in existing:
                    conn.execute(sql)
            for statement in step.statements:
                conn.execute(statement)
            conn.execute(step.set_version_sql)

    def _check_backup_space(self) -> None:
        try:
            usage = shutil.disk_usage(self.db_path.parent)
        except OSError:  # 查不到可用空間：視為未知，照常備份；真的寫滿會得到 SQLITE_FULL
            usage = None
        needed = self.db_path.stat().st_size * FACTS_BACKUP_SPACE_FACTOR
        if usage is not None and usage.total > 0 and usage.free < needed:
            raise _NoSpaceForBackupError

    def _free_backup_name(self, version: int) -> Path:
        """``core_memory.v{舊}-to-v{新}.{epoch}.db``；同一秒撞名就加 ``.{n}``（不覆蓋）."""
        stem = f"core_memory.v{version}-to-v{_target_version()}.{int(get_current_epoch())}"
        proxy = get_resource_proxy()
        for suffix in range(FACTS_BACKUP_NAME_MAX_SUFFIX + 1):
            candidate = self._backup_dir / (f"{stem}.{suffix}.db" if suffix else f"{stem}.db")
            if not proxy.exists(str(candidate)):
                return candidate
        raise FileExistsError(errno.EEXIST, "No free backup name", stem)

    def _backup(self, version: int) -> Path:
        """持鎖時用唯讀連線把升級前的記憶庫備份到暫存檔再改名；任何一步失敗都丟例外."""
        self._check_backup_space()
        proxy = get_resource_proxy()
        proxy.make_dir(str(self._backup_dir))
        final = self._free_backup_name(version)
        tmp = final.with_name(
            f"{final.name}.{uuid.uuid4().hex[:_BACKUP_TMP_HEX_LEN]}{_BACKUP_TMP_SUFFIX}"
        )
        try:
            # 來源不能用持鎖的升級連線：它在寫入交易裡，備份會一直等自己。
            with (
                self._connection(self._db_uri("ro"), uri=True) as source,
                self._connection(tmp) as target,
            ):
                source.backup(target)
            proxy.move_file(str(tmp), str(final))  # 兩條連線都已關：Windows 才能改名
        except Exception:
            for leftover in (tmp, tmp.with_name(f"{tmp.name}-journal")):
                with contextlib.suppress(OSError):
                    proxy.remove_file(str(leftover))
            raise
        return final

    def _backup_dir_files(self) -> list[Path]:
        """備份資料夾頂層的檔（iter_files 會遞迴，這裡只留頂層）；列不出來記警告、當成空的."""
        proxy = get_resource_proxy()
        try:
            if not proxy.exists(str(self._backup_dir)):
                return []
            files = proxy.iter_files(str(self._backup_dir))
        except OSError:
            logger.warning("[Factual] cannot list %s", FACTS_BACKUP_DIR_NAME, exc_info=True)
            return []
        return [path for path in files if path.parent == self._backup_dir]

    def _rotate_backups(self, new_backup: Path | None) -> None:
        """只留最新 FACTS_BACKUP_KEEP 份；本次新檔永遠不刪、不認得的檔不碰；出錯只記警告."""
        if new_backup is None:
            return
        try:
            backups = [p for p in self._backup_dir_files() if _BACKUP_NAME_RE.match(p.name)]
            if new_backup not in backups:
                logger.warning(
                    "[Factual] new backup %s not listed; rotation skipped", new_backup.name
                )
                return
            older = sorted(
                (p for p in backups if p != new_backup), key=_backup_sort_key, reverse=True
            )
            for path in older[FACTS_BACKUP_KEEP - 1 :]:
                get_resource_proxy().remove_file(str(path))
        except Exception:
            logger.warning("[Factual] backup rotation failed", exc_info=True)

    def _insert_record(self, identity: AgentIdentity, record: dict[str, Any]) -> None:
        """一般 INSERT 並提交；撞到既有 id 丟 FactIdConflictError，絕不覆蓋."""
        with self._connection(self.db_path) as conn:
            self._insert_row(conn, identity, record)
            conn.commit()

    def _insert_row(
        self, conn: sqlite3.Connection, identity: AgentIdentity, record: dict[str, Any]
    ) -> None:
        """在呼叫端的交易裡 INSERT 一列、不提交（寫 v1／v2 欄位，先過寫入閘）."""
        self._require_schema(write=True)
        values = (
            record.get("id"),
            identity.agent_id,
            record.get("content"),
            json.dumps(record.get("tags", []), ensure_ascii=False),
            record.get("importance"),
            json.dumps(record.get("emotions"), ensure_ascii=False)
            if record.get("emotions") is not None else None,
            record.get("created_at"),
            record.get("updated_at"),
            1 if record.get("archived") else 0,
        )
        sources, flags = record.get("sources"), record.get("safety_flags")
        try:
            if self._rechecked_status() is FactsSchemaStatus.CURRENT:
                conn.execute(
                    "INSERT INTO facts (id, agent_id, content, tags, importance, emotions,"
                    " created_at, updated_at, archived, sources, safety_flags)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *values,
                        json.dumps(sources, ensure_ascii=False) if sources is not None else None,
                        json.dumps(flags, ensure_ascii=False) if flags else None,
                    ),
                )
                return
            if sources is not None or flags:
                # 比程式新的結構：不做欄位假設，來源與安全標記都不記（同降版期間）
                logger.warning(
                    "[Factual] store is newer; source and safety flags of %s not kept",
                    record.get("id"),
                )
            conn.execute(
                "INSERT INTO facts (id, agent_id, content, tags, importance, emotions,"
                " created_at, updated_at, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise FactIdConflictError from exc

    @staticmethod
    def _new_record(
        content: str,
        tags: list[str] | None,
        importance: str | None,
        emotions: list[str] | None,
        source: SourceRef | None,
        safety_flags: list[str] | None,
    ) -> dict[str, Any]:
        """新事實的一列（save 與 supersede 共用）；沒有來源、沒有標記都存 NULL."""
        timestamp = int(get_current_epoch())
        return {
            "id": f"fact_{uuid.uuid4().hex[:12]}",
            "content": content.strip(),
            "tags": tags or [],
            "importance": importance,
            "emotions": emotions,
            "created_at": timestamp,
            "updated_at": timestamp,
            "archived": False,
            "sources": [source.model_dump(mode="json")] if source is not None else None,
            "safety_flags": list(safety_flags) if safety_flags else None,
        }

    def save_fact(
        self,
        identity: AgentIdentity,
        content: str,
        tags: list[str] | None = None,
        importance: str | None = None,
        emotions: list[str] | None = None,
        source: SourceRef | None = None,
        safety_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        """存儲一條事實記憶，具備基礎校驗邏輯.

        Args:
            identity (AgentIdentity): 代理人身分對象。
            content (str): 事實內容。
            tags (list[str] | None): 標籤。
            importance (str | None): 重要程度。
            emotions (list[str] | None): 情感標記。
            source (SourceRef | None): 系統記下的來源（只有位置與指紋）；沒有就存 NULL。
            safety_flags (list[str] | None): 系統算的安全標記；沒有就存 NULL。

        Returns:
            dict[str, Any]: 包含狀態與新生成紀錄的結果。
        """
        if not content or not content.strip():
            return {"status": "error", "message": FACT_EMPTY_MESSAGE}
        self._require_schema(write=True)

        record = self._new_record(content, tags, importance, emotions, source, safety_flags)
        try:
            self._insert_record(identity, record)
        except (FactIdConflictError, FactsSchemaNotReadyError):
            raise
        except Exception as exc:
            logger.exception(f"❌ [Factual] SQLite write failed for {identity.agent_id}")
            raise FactWriteError from exc
        else:
            return {"status": "success", "id": record["id"], "record": record}

    def supersede_fact(
        self,
        identity: AgentIdentity,
        old_fact_id: str,
        content: str,
        tags: list[str] | None = None,
        importance: str | None = None,
        emotions: list[str] | None = None,
        source: SourceRef | None = None,
        safety_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        """一步取代：同一個交易存新的一列、把舊的封存並連到新的；舊的不是有效記憶就整筆回滾.

        舊的那筆在交易裡用 ``archived = 0`` 再驗一次（同身分、仍有效），0 列就丟
        ``SupersedeTargetError``、新列一併回滾。只在目前版做：較新版不寫取代連結。
        """
        if not content or not content.strip():
            return {"status": "error", "message": FACT_EMPTY_MESSAGE}
        self._require_schema(write=True)
        self._require_current()
        record = self._new_record(content, tags, importance, emotions, source, safety_flags)
        try:
            with self._connection(self.db_path) as conn:
                self._insert_row(conn, identity, record)
                cursor = conn.execute(
                    "UPDATE facts SET archived = 1, status = ?, superseded_by = ?, updated_at = ?"
                    " WHERE id = ? AND agent_id = ? AND archived = 0",
                    (
                        FactStatus.SUPERSEDED.value,
                        record["id"],
                        record["updated_at"],
                        old_fact_id,
                        identity.agent_id,
                    ),
                )
                linked = cursor.rowcount == 1
                if linked:
                    conn.commit()
                else:  # 舊的已不是有效記憶：新列一併回滾，不留半套
                    conn.rollback()
        except (FactIdConflictError, FactsSchemaNotReadyError):
            raise
        except Exception as exc:
            logger.exception(f"❌ [Factual] SQLite supersede failed for {identity.agent_id}")
            raise FactWriteError from exc
        if not linked:
            raise SupersedeTargetError
        return {"status": "success", "id": record["id"], "record": record}

    def delete_agent_facts(self, identity: AgentIdentity) -> int:
        """刪除某 agent 的**全部**事實記憶（破壞性重置 Batch 6）。回刪除筆數。"""
        with self._connection(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM facts WHERE agent_id = ? COLLATE BINARY", (identity.agent_id,)
            )
            conn.commit()
            return cursor.rowcount or 0

    def count_agent_facts(self, identity: AgentIdentity) -> int:
        """某 agent 的事實記憶筆數（完整性自檢）。"""
        with self._connection(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE agent_id = ? COLLATE BINARY", (identity.agent_id,)
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def query_facts(
        self, 
        identity: AgentIdentity, 
        query: str = "", 
        include_archived: bool = False,
        tags_any: list[str] | None = None,
        importance: str | None = None,
        emotions_any: list[str] | None = None,
        date_start: int | None = None,
        date_end: int | None = None,
        exclude_flagged: bool = False,
    ) -> list[dict[str, Any]]:
        """快速查詢事實資料庫.

        支援全文模糊匹配 (LIKE) 與標籤、重要度、時間等多維度過濾。

        Args:
            identity (AgentIdentity): 代理人身分對象。
            query (str): 內容關鍵字。
            include_archived (bool): 是否包含已封存記憶。
            tags_any (list[str] | None): 標籤過濾。
            importance (str | None): 重要度過濾。
            emotions_any (list[str] | None): 情感過濾。
            date_start (int | None): 時間範圍開始。
            date_end (int | None): 時間範圍結束。
            exclude_flagged (bool): 排除有安全標記的（隨機回想用；只有目前版有這一欄）。

        Returns:
            list[dict[str, Any]]: 事實物件清單。
        """
        sql = (
            self._read_select(_FACT_SELECT_V0, _FACT_SELECT_V2) + " FROM facts WHERE agent_id = ?"
        )
        agent_id = identity.agent_id
        params: list[Any] = [agent_id]

        if not include_archived:
            sql += " AND archived = 0"

        if exclude_flagged and self._rechecked_status() is FactsSchemaStatus.CURRENT:
            sql += " AND safety_flags IS NULL"  # 不是 NULL 就算有標記（壞掉的也算）

        if query:
            sql += " AND (content LIKE ? OR tags LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])

        if importance:
            if importance.isdigit():
                sql += " AND CAST(importance AS INTEGER) >= ?"
                params.append(int(importance))
            else:
                sql += " AND importance = ?"
                params.append(importance)

        if date_start:
            sql += " AND created_at >= ?"
            params.append(date_start)
        
        if date_end:
            sql += " AND created_at <= ?"
            params.append(date_end)

        # 標籤與情感過濾 (JSON 陣列模擬匹配)
        for field, values in [("tags", tags_any), ("emotions", emotions_any)]:
            if values:
                clauses = [f"{field} LIKE ?" for _ in values]
                sql += f" AND ({' OR '.join(clauses)})"
                params.extend([f'%"{v}"%' for v in values])

        sql += " ORDER BY created_at DESC"
        return self._fetch_facts(sql, params)

    def _fetch_facts(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """讀事實列；讀檔出錯一律往外丟、不回空清單（讀不到時先轉狀態，畫面才會出提示）."""
        return [_row_to_fact(row) for row in self._fetch_rows(sql, params)]

    def _fetch_rows(self, sql: str, params: list[Any]) -> list[sqlite3.Row]:
        """所有讀取共用：讀不到時設旗標並轉狀態；真的讀成功一次才清旗標."""
        try:
            with self._connection(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            if _classify_failure(exc) is FactsSchemaStatus.UNREADABLE:
                self._read_failed = True
                self._set_status(FactsSchemaStatus.UNREADABLE)
                raise FactsSchemaNotReadyError(FactsSchemaStatus.UNREADABLE) from exc
            raise  # 被鎖住等暫時性錯誤：照樣往外丟，但不改狀態、不出提示
        if self._read_failed:
            logger.info("[Factual] memory store is readable again")
            self._read_failed = False
        return rows

    def find_active_fact_by_content(
        self, identity: AgentIdentity, content: str
    ) -> dict[str, Any] | None:
        """查重備援：同一身分、未封存、內容完全相同的事實（向量庫不能用時也查得到）."""
        rows = self._fetch_facts(
            self._read_select(_FACT_SELECT_V0, _FACT_SELECT_V2)
            + " FROM facts WHERE agent_id = ? AND archived = 0 AND content = ?"
            " ORDER BY created_at DESC LIMIT 1",
            [identity.agent_id, content],
        )
        return rows[0] if rows else None

    def get_active_facts(
        self, identity: AgentIdentity, fact_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """語意結果回查：只回這個身分、未封存的事實（id → 事實）；id 清單以 JSON 帶入固定 SQL."""
        rows = self._fetch_facts(
            self._read_select(_FACT_SELECT_V0, _FACT_SELECT_V2) + _ACTIVE_FACTS_BY_ID,
            [identity.agent_id, json.dumps(fact_ids)],
        )
        return {row["id"]: row for row in rows}

    def count_embedding_states(self, identity: AgentIdentity) -> dict[str, int]:
        """未封存事實在語意索引裡的各狀態筆數；還停在 v0 時全部算未核對（沒有狀態欄）."""
        status = self._rechecked_status()  # 只看結構版本選 SQL；讀不到旗標交給讀取本身處理
        if status in (FactsSchemaStatus.CURRENT, FactsSchemaStatus.NEWER):
            sql = (
                "SELECT COALESCE(embedding_state, 'unverified') AS state, COUNT(*) AS n"
                " FROM facts WHERE agent_id = ? AND archived = 0 GROUP BY state"
            )
        else:
            self._require_schema(write=False)
            sql = (
                "SELECT 'unverified' AS state, COUNT(*) AS n"
                " FROM facts WHERE agent_id = ? AND archived = 0"
            )
        rows = self._fetch_rows(sql, [identity.agent_id])
        return {row["state"]: row["n"] for row in rows if row["n"]}

    def superseded_history(
        self, identity: AgentIdentity, fact_id: str, max_versions: int
    ) -> list[dict[str, Any]]:
        """某筆有效記憶被取代前的版本（新到舊）：只沿 superseded_by 往回、只收同身分被取代的.

        單純 forget 的列沒有 superseded_by，永遠走不到；深度由 ``max_versions`` 限制。
        """
        self._require_current()
        return self._fetch_facts(  # 整句固定字面量；讀的欄位同 _FACT_SELECT_V2（測試核對）
            "WITH RECURSIVE chain(id, depth) AS ("
            "SELECT id, 1 FROM facts"
            " WHERE agent_id = ? AND superseded_by = ? AND archived = 1 AND status = ?"
            " UNION SELECT f.id, chain.depth + 1 FROM facts AS f"
            " JOIN chain ON f.superseded_by = chain.id"
            " WHERE f.agent_id = ? AND f.archived = 1 AND f.status = ? AND chain.depth < ?)"
            " SELECT id, content, tags, importance, emotions, created_at, updated_at, archived,"
            " sources, status, superseded_by, safety_flags"
            " FROM facts WHERE agent_id = ? AND id IN (SELECT id FROM chain)"
            " ORDER BY (SELECT MIN(depth) FROM chain WHERE chain.id = facts.id), id LIMIT ?",
            [
                identity.agent_id,
                fact_id,
                FactStatus.SUPERSEDED.value,
                identity.agent_id,
                FactStatus.SUPERSEDED.value,
                max_versions,
                identity.agent_id,
                max_versions,
            ],
        )

    def count_flagged_facts(self, identity: AgentIdentity) -> int:
        """有安全標記的有效記憶筆數（診斷用；欄位不是 NULL 就算）."""
        self._require_current()
        rows = self._fetch_rows(
            "SELECT COUNT(*) AS n FROM facts"
            " WHERE agent_id = ? AND archived = 0 AND safety_flags IS NOT NULL",
            [identity.agent_id],
        )
        return int(rows[0]["n"]) if rows else 0

    def list_archive_pending(self, identity: AgentIdentity, limit: int) -> list[str]:
        """封存了、但還沒確認語意索引也封存的事實 id（舊的先）；補索引會寫狀態，先過寫入閘."""
        self._require_schema(write=True)
        rows = self._fetch_rows(
            "SELECT id FROM facts WHERE agent_id = ? AND archived = 1"
            " AND (embedding_state IS NULL OR embedding_state IN ('unverified', 'failed'))"
            " ORDER BY updated_at, id LIMIT ?",
            [identity.agent_id, limit],
        )
        return [row["id"] for row in rows]

    def list_pending_index(self, identity: AgentIdentity, limit: int) -> list[dict[str, Any]]:
        """待補索引的未封存事實（未核對或失敗），舊的先；補索引會寫狀態，所以先過寫入閘."""
        self._require_schema(write=True)
        return self._fetch_facts(
            "SELECT id, content, tags, importance, emotions, created_at, updated_at, archived"
            " FROM facts WHERE agent_id = ? AND archived = 0"
            " AND (embedding_state IS NULL OR embedding_state IN ('unverified', 'failed'))"
            " ORDER BY created_at, id LIMIT ?",
            [identity.agent_id, limit],
        )

    def set_embedding_state(
        self, identity: AgentIdentity, fact_ids: list[str], state: str, model: str | None
    ) -> None:
        """記下語意索引狀態（時間單位秒）；只改這個身分的列."""
        self._require_schema(write=True)
        now = int(get_current_epoch())
        with self._connection(self.db_path) as conn:
            conn.executemany(
                "UPDATE facts SET embedding_state = ?, embedding_model = ?,"
                " embedding_updated_at = ? WHERE id = ? AND agent_id = ?",
                [(state, model, now, fact_id, identity.agent_id) for fact_id in fact_ids],
            )
            conn.commit()

    def query_all_facts(
        self,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查詢所有代理人的事實記憶（系統監控診斷用）。

        跨代理人查詢，僅供 observability 等系統層級監控使用，
        不應用於業務邏輯。呼叫方無需傳入 identity。

        Args:
            include_archived: 是否包含已封存記憶，預設 False。
            limit: 回傳上限筆數，預設 200。

        Returns:
            list[dict[str, Any]]: 事實物件清單，含 agent_id 欄位，
            依 created_at 降序排列。
        """
        sql = self._read_select(_ALL_FACTS_SELECT_V0, _ALL_FACTS_SELECT_V1) + " FROM facts"
        params: list[Any] = []
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self._fetch_facts(sql, params)

    def archive_fact(self, identity: AgentIdentity, fact_id: str) -> ArchiveResult:
        """邏輯封存事實記憶；同一個交易分三步，照實回這次做了什麼（見 ``ArchiveResult``）.

        先封存仍有效的；再（只在目前版）把已被取代的舊版本從歷史拿掉；兩步都沒改到才查
        這筆在不在。資料庫失敗丟 ``FactWriteError``（原文只進日誌），不說成查無。
        """
        now = int(get_current_epoch())
        # 只在目前版把被 forget 的舊版本從歷史拿掉；失敗狀態照常封存（只動 v0 欄）
        detach = self.current_schema_status() is FactsSchemaStatus.CURRENT
        try:
            with self._connection(self.db_path) as conn:
                archived = conn.execute(
                    "UPDATE facts SET archived = 1, updated_at = ?"
                    " WHERE id = ? AND agent_id = ? AND archived = 0",
                    (now, fact_id, identity.agent_id),
                ).rowcount
                detached = detach and self._detach_from_history(conn, identity, fact_id, now)
                exists = bool(archived or detached) or conn.execute(
                    "SELECT 1 FROM facts WHERE id = ? AND agent_id = ?",
                    (fact_id, identity.agent_id),
                ).fetchone() is not None
                conn.commit()
        except FactsSchemaNotReadyError:
            raise
        except Exception as exc:
            logger.exception(f"❌ [Factual] Failed to archive fact {fact_id}")
            raise FactWriteError from exc
        if archived:
            return ArchiveResult.ARCHIVED
        if detached:
            return ArchiveResult.DETACHED
        return ArchiveResult.ALREADY_ARCHIVED if exists else ArchiveResult.NOT_FOUND

    def _detach_from_history(
        self, conn: sqlite3.Connection, identity: AgentIdentity, fact_id: str, now: int
    ) -> bool:
        """被 forget 的舊版本從歷史拿掉：清掉它的 superseded_by，它與更早的版本都不再查得到.

        回這次有沒有拿掉（本來就不在歷史裡是 False）。
        """
        self._require_schema(write=True)
        cursor = conn.execute(
            "UPDATE facts SET superseded_by = NULL, updated_at = ?"
            " WHERE id = ? AND agent_id = ? AND superseded_by IS NOT NULL",
            (now, fact_id, identity.agent_id),
        )
        return cursor.rowcount > 0

    # --- 清除連備份與撞名判定（只有重置與建立時的檢查會用；不是工具） ---

    def list_agent_ids(self) -> set[str]:
        """事實庫裡出現過的所有 agent_id（任何狀態）；庫不存在回空集合、不建檔.

        讀不到就丟 MemoryScrubError（呼叫端據此拒絕，不能確認就不放行）。
        """
        if not get_resource_proxy().exists(str(self.db_path)):
            return set()
        try:
            with self._connection(self._db_uri("rw"), uri=True) as conn:
                if not _has_facts_table(conn):
                    return set()
                return {
                    row[0]
                    for row in conn.execute("SELECT DISTINCT agent_id FROM facts")
                    if isinstance(row[0], str)
                }
        except sqlite3.Error as exc:
            raise _scrub_error(exc) from exc

    def agent_fact_ids(self, identity: AgentIdentity) -> list[str]:
        """這個身分的所有 fact_id（任何狀態）；只用來算向量點 ID."""
        if not get_resource_proxy().exists(str(self.db_path)):
            return []
        try:
            with self._connection(self._db_uri("rw"), uri=True) as conn:
                if not _has_facts_table(conn):
                    return []
                return [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM facts WHERE agent_id = ? COLLATE BINARY ORDER BY id",
                        (identity.agent_id,),
                    )
                ]
        except sqlite3.Error as exc:
            raise _scrub_error(exc) from exc

    def compact_after_reset(self, identity: AgentIdentity) -> bool:
        """重置清完列之後整理線上庫（VACUUM），清掉以前刪除留下的殘片；只整理、不刪任何列.

        庫不存在回 False（不建檔）；做了回 True。這個身分又有列（重置後才寫入）就不整理。
        失敗丟 MemoryScrubError；VACUUM 之後結構或 integrity 不符是 LIVE_VERIFY_FAILED
        （檔案已整理過），又有這個身分的列是 REWRITTEN_AFTER_RESET。
        """
        if not get_resource_proxy().exists(str(self.db_path)):
            return False
        try:
            usage = shutil.disk_usage(self.db_path.parent)
        except OSError:  # 查不到可用空間當未知，照常整理（寫滿時會得到 SQLITE_FULL）
            usage = None
        try:
            needed = self.db_path.stat().st_size * FACTS_BACKUP_SPACE_FACTOR
        except OSError as exc:  # 檢查之後檔案不見或讀不到
            raise MemoryScrubError(ScrubFailure.UNREADABLE) from exc
        if usage is not None and usage.total > 0 and usage.free < needed:
            raise MemoryScrubError(ScrubFailure.NO_SPACE)
        try:
            with self._connection(
                self._db_uri("rw"),
                uri=True,
                timeout=FACTS_SCHEMA_LOCK_TIMEOUT_S,
                isolation_level=None,
                commit_on_exit=False,
            ) as conn:
                schema = _schema_rows(conn)
                version = _user_version(conn)
                _require_no_target(conn, schema, identity)
                conn.execute("VACUUM")
                healthy = _integrity_ok(conn)
                same = _schema_rows(conn) == schema and _user_version(conn) == version
                leftover = _count_target(conn, schema, identity)
        except MemoryScrubError:
            raise
        except sqlite3.Error as exc:
            raise _scrub_error(exc) from exc
        if not (healthy and same):
            raise MemoryScrubError(ScrubFailure.LIVE_VERIFY_FAILED)
        # 查過之後、VACUUM 之前別的連線寫進來（SQLite 不能在交易裡 VACUUM，這段鎖不住）；
        # VACUUM 不會多出資料列，多出來的只可能是重置後的新寫入（舊審查報告丙-3）
        if leftover:
            raise MemoryScrubError(ScrubFailure.REWRITTEN_AFTER_RESET)
        return True


# --- 清除連備份：記憶體裡的副本（升級備份與快照裡的記憶庫） -------------------------


class ScrubFailure(StrEnum):
    """清除失敗的原因代碼（固定字串，回應與稽核只帶代碼，不帶任何列的值）."""

    BUSY = "busy"
    NO_SPACE = "no_space"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    UNKNOWN_SCHEMA = "unknown_schema"
    VERIFY_FAILED = "verify_failed"
    LIVE_VERIFY_FAILED = "live_verify_failed"
    WRITE_FAILED = "write_failed"
    REWRITTEN_AFTER_RESET = "rewritten_after_reset"
    LOCK_TIMEOUT = "lock_timeout"


class MemoryScrubError(Exception):
    """清除失敗；訊息只有原因代碼."""

    def __init__(self, reason: ScrubFailure) -> None:
        super().__init__(f"memory scrub failed: {reason.value}")
        self.reason = reason


@dataclass(frozen=True)
class _ScrubTable:
    """一個帶 agent_id 的表：SQL 都是寫死的字面量（表名不能綁參數，禁令 24）."""

    count_target: str
    delete_target: str
    select_all: str
    agent_column: str = "agent_id"


# 事實庫裡所有的表都要在這裡；碰到沒登記的表、trigger 或 view 一律不處理（fail closed）。
MEMORY_SCRUB_TABLES: dict[str, _ScrubTable] = {
    "facts": _ScrubTable(
        count_target="SELECT COUNT(*) FROM facts WHERE agent_id = ? COLLATE BINARY",
        delete_target="DELETE FROM facts WHERE agent_id = ? COLLATE BINARY",
        select_all="SELECT * FROM facts ORDER BY id",
    ),
}


@dataclass(frozen=True)
class StoreInspection:
    """一份記憶庫副本的檢查結果（唯讀）."""

    user_version: int
    target_rows: int
    legacy_rows: int
    freelist_pages: int
    agent_ids: frozenset[str]
    others_digest: str = ""  # 非目標列多重集合的雜湊（換掉的成員要和原本的一樣）

    def needs_scrub(self, *, contains_id_bytes: bool) -> bool:
        """有目標列、有空閒頁，或檔案裡找得到目標 ID 的位元組，就要處理."""
        return self.target_rows > 0 or self.freelist_pages > 0 or contains_id_bytes


@dataclass(frozen=True)
class _Fingerprint:
    schema: tuple[tuple[Any, ...], ...]
    user_version: int
    target_rows: int
    legacy_rows: int
    others: tuple[str, ...]
    agent_ids: frozenset[str]


def memory_scrub_supported() -> bool:
    """這個 sqlite3 能不能在記憶體裡打開副本（清除的前提）."""
    return hasattr(sqlite3.Connection, "deserialize")


@contextlib.contextmanager
def upgrade_backup_lock(timeout: float) -> Iterator[None]:
    """清除升級備份時持有，和升級（備份＋輪替）互斥；等不到丟 LOCK_TIMEOUT."""
    if not _UPGRADE_BACKUP_LOCK.acquire(timeout=timeout):
        raise MemoryScrubError(ScrubFailure.LOCK_TIMEOUT)
    try:
        yield
    finally:
        _UPGRADE_BACKUP_LOCK.release()


def classify_upgrade_backup_name(name: str) -> str:
    """升級備份資料夾裡的檔名：backup／upgrade_tmp／scrub_tmp／unknown."""
    if _BACKUP_NAME_RE.match(name):
        return "backup"
    if _BACKUP_TMP_RE.match(name):
        return "upgrade_tmp"
    if SCRUB_TMP_RE.match(name):
        return "scrub_tmp"
    return "unknown"


def inspect_store_image(image: bytes, identity: AgentIdentity) -> StoreInspection:
    """在記憶體裡打開一份記憶庫的位元組，讀出要不要清除（不寫任何東西）."""
    _check_image_size(image)
    try:
        with _memory_db(image) as conn:
            found = _fingerprint(conn, identity)
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    except MemoryScrubError:
        raise
    except sqlite3.Error as exc:
        raise _scrub_error(exc) from exc
    return StoreInspection(
        user_version=found.user_version,
        target_rows=found.target_rows,
        legacy_rows=found.legacy_rows,
        freelist_pages=int(freelist),
        agent_ids=found.agent_ids,
        others_digest=hashlib.sha256(",".join(found.others).encode()).hexdigest(),
    )


def scrub_store_image(image: bytes, identity: AgentIdentity, out_path: Path) -> int:
    """在記憶體裡刪掉這個身分的列，``VACUUM INTO`` 到 out_path（空檔），驗證後回傳刪除筆數.

    刪除前的內容只在記憶體裡；out_path 只會有刪除後的內容。驗證不過丟 VERIFY_FAILED。
    """
    _check_image_size(image)
    try:
        with _memory_db(image) as conn:
            before = _fingerprint(conn, identity)
            conn.execute("BEGIN IMMEDIATE")
            for name in _table_names(before.schema):
                conn.execute(MEMORY_SCRUB_TABLES[name].delete_target, (identity.agent_id,))
            conn.execute("COMMIT")
            _vacuum_into(conn, out_path)
        _verify_scrubbed(Path(out_path), identity, before)
    except MemoryScrubError:
        raise
    except sqlite3.Error as exc:
        raise _scrub_error(exc) from exc
    return before.target_rows


def _vacuum_into(conn: sqlite3.Connection, out_path: Path) -> None:
    """輸出清除後的副本；寫不出去是 WRITE_FAILED（不是「讀不出來」），空間不夠是 NO_SPACE."""
    try:
        conn.execute("VACUUM INTO ?", (str(out_path),))
    except sqlite3.Error as exc:
        if _classify_failure(exc) is FactsSchemaStatus.NO_SPACE:
            raise MemoryScrubError(ScrubFailure.NO_SPACE) from exc
        raise MemoryScrubError(ScrubFailure.WRITE_FAILED) from exc


def _check_image_size(image: bytes) -> None:
    if len(image) > MEMORY_SCRUB_MAX_IMAGE_BYTES:
        raise MemoryScrubError(ScrubFailure.TOO_LARGE)


@contextlib.contextmanager
def _memory_db(image: bytes) -> Iterator[sqlite3.Connection]:
    """把位元組打開成記憶體資料庫：journal 與暫存都在記憶體，不寫磁碟."""
    with FactualStore.tracked_connection(
        ":memory:", isolation_level=None, commit_on_exit=False
    ) as conn:
        conn.deserialize(image)
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA secure_delete = ON")
        yield conn


def _scrub_error(exc: BaseException) -> MemoryScrubError:
    """SQLite 錯誤轉成固定代碼（原文只留在例外鏈，不進訊息）."""
    status = _classify_failure(exc)
    if status is FactsSchemaStatus.NO_SPACE:
        return MemoryScrubError(ScrubFailure.NO_SPACE)
    if status is FactsSchemaStatus.LOCKED:
        return MemoryScrubError(ScrubFailure.BUSY)
    return MemoryScrubError(ScrubFailure.UNREADABLE)


def _has_facts_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()
    return row is not None


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def _schema_rows(conn: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    """所有結構物件；trigger、view 或沒登記的表一律丟 UNKNOWN_SCHEMA."""
    rows = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
        )
    )
    for kind, name, _table, _sql in rows:
        if kind in ("trigger", "view"):
            raise MemoryScrubError(ScrubFailure.UNKNOWN_SCHEMA)
        if kind != "table":
            continue
        table = MEMORY_SCRUB_TABLES.get(name)
        if table is None:
            raise MemoryScrubError(ScrubFailure.UNKNOWN_SCHEMA)
        columns = {row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (name,))}
        if table.agent_column not in columns:
            raise MemoryScrubError(ScrubFailure.UNKNOWN_SCHEMA)
    return rows


def _table_names(schema: tuple[tuple[Any, ...], ...]) -> list[str]:
    return [name for kind, name, _table, _sql in schema if kind == "table"]


def _count_target(
    conn: sqlite3.Connection, schema: tuple[tuple[Any, ...], ...], identity: AgentIdentity
) -> int:
    params = (identity.agent_id,)
    return sum(
        int(conn.execute(MEMORY_SCRUB_TABLES[name].count_target, params).fetchone()[0])
        for name in _table_names(schema)
    )


def _require_no_target(
    conn: sqlite3.Connection, schema: tuple[tuple[Any, ...], ...], identity: AgentIdentity
) -> None:
    """重置後又有人寫入目標的記憶：不整理，交給人重來."""
    if _count_target(conn, schema, identity):
        raise MemoryScrubError(ScrubFailure.REWRITTEN_AFTER_RESET)


def _same_id(value: Any, wanted: str) -> bool:
    """位元組層的精確比較（不靠 SQL 的比較規則）."""
    if isinstance(value, bytes):
        return value == wanted.encode("utf-8", "surrogatepass")
    return isinstance(value, str) and value == wanted


def _fingerprint(conn: sqlite3.Connection, identity: AgentIdentity) -> _Fingerprint:
    """在 Python 端把列分成目標與非目標；非目標逐列雜湊成排序後的多重集合."""
    schema = _schema_rows(conn)
    target = identity.agent_id
    legacy = _LEGACY_ID_PREFIX + target
    target_rows = legacy_rows = 0
    others: list[str] = []
    agent_ids: set[str] = set()
    for name in _table_names(schema):
        table = MEMORY_SCRUB_TABLES[name]
        cursor = conn.execute(table.select_all)
        index = [column[0] for column in cursor.description].index(table.agent_column)
        for row in cursor:
            value = row[index]
            if _same_id(value, target):
                target_rows += 1
                continue
            if _same_id(value, legacy):
                legacy_rows += 1
            if isinstance(value, str):
                agent_ids.add(value)
            digest = hashlib.sha256(repr((name, *row)).encode("utf-8", "surrogatepass"))
            others.append(digest.hexdigest())
    return _Fingerprint(
        schema=schema,
        user_version=_user_version(conn),
        target_rows=target_rows,
        legacy_rows=legacy_rows,
        others=tuple(sorted(others)),
        agent_ids=frozenset(agent_ids),
    )


def _verify_scrubbed(out_path: Path, identity: AgentIdentity, before: _Fingerprint) -> None:
    """輸出檔逐項核對；任何一項不符就 VERIFY_FAILED（錯誤裡不帶列的值）."""
    source = f"{out_path.absolute().as_uri()}?mode=ro"
    try:
        with FactualStore.tracked_connection(source, uri=True) as conn:
            healthy = _integrity_ok(conn)
            after = _fingerprint(conn, identity)
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except (sqlite3.Error, MemoryScrubError) as exc:
        raise MemoryScrubError(ScrubFailure.VERIFY_FAILED) from exc
    if not (
        healthy
        and after.schema == before.schema
        and after.user_version == before.user_version
        and after.target_rows == 0
        and after.others == before.others
        and freelist == 0
    ):
        raise MemoryScrubError(ScrubFailure.VERIFY_FAILED)
