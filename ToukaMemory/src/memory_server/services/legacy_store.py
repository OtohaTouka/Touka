"""舊對話紀錄存儲服務 (Legacy Store).

本模組負責管理從舊系統遷移或歸檔的對話紀錄。
採用 SQLite 作為後端，支援訊息間的父子關係 (Parent-Child) 以還原對話樹，
並提供全文檢索、隨機回想與統計功能。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from memory_server.config_utils import get_current_epoch
from memory_server.models.conversation_search import ConversationPage, ConversationRow
from memory_server.utils.legacy_time import to_epoch_ms

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_SESSION_UPSERT = """
    INSERT INTO sessions (id, agent_id, filename, title, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        title=excluded.title,
        updated_at=excluded.updated_at
"""
_MESSAGE_UPSERT = """
    INSERT INTO messages (id, session_id, parent_id, role, content, timestamp, metadata)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        content=excluded.content,
        metadata=excluded.metadata
"""
# 歸檔只新增：已歸檔的訊息不被同 id 的新內容蓋掉（寫進去就不再被系統改動）；
# 只略過 id 衝突，其他限制（例如 NOT NULL）照樣失敗、整筆 rollback
_MESSAGE_INSERT_ONLY = """
    INSERT INTO messages (id, session_id, parent_id, role, content, timestamp, metadata)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO NOTHING
"""
# 歸檔後讀回這批 id 存著的內容：一句固定 SQL，只綁一個 JSON 陣列參數（禁令 24）
_MESSAGES_BY_IDS = """
    SELECT id, session_id, role, content FROM messages
    WHERE id IN (SELECT value FROM json_each(?))
"""
_PROCESSED_INSERT = """
    INSERT INTO processed_files (filename, content_sha256, processed_at) VALUES (?, ?, ?)
    ON CONFLICT(filename, content_sha256) DO NOTHING
"""
_RESERVED_KEYS = ("id", "session_id", "parentId", "role", "content", "timestamp", "createdAt")
# 先正規化再篩選、計數與切頁，避免異常時間佔掉有效候選。
_SEARCH_NORMALIZED = """
    WITH normalized_messages AS (
        SELECT id, session_id, role, content, to_epoch_ms(timestamp) AS timestamp FROM messages
    )
"""
_SEARCH_MATCH = """
    FROM normalized_messages m LEFT JOIN sessions s ON s.id = m.session_id
    WHERE (? = '' OR m.content LIKE ? ESCAPE '\\') AND (? IS NULL OR m.role = ?)
"""
_SEARCH_FROM = (
    _SEARCH_MATCH
    + """
      AND (? IS NULL OR m.timestamp >= ?) AND (? IS NULL OR m.timestamp <= ?)
"""
)
_SEARCH_COUNT = _SEARCH_NORMALIZED + "SELECT COUNT(*)" + _SEARCH_FROM
_SEARCH_UNKNOWN = (
    _SEARCH_NORMALIZED + "SELECT COUNT(*)" + _SEARCH_MATCH + " AND m.timestamp IS NULL"
)
_SEARCH_PAGE = (
    _SEARCH_NORMALIZED
    + "SELECT m.id, m.session_id, m.role, m.content, m.timestamp, s.title"
    + _SEARCH_FROM
)
_SEARCH_OLDEST = (
    _SEARCH_PAGE
    + """
    ORDER BY m.timestamp IS NULL ASC, m.timestamp ASC, m.id ASC, m.session_id ASC LIMIT ? OFFSET ?
"""
)
_SEARCH_NEWEST = (
    _SEARCH_PAGE
    + """
    ORDER BY m.timestamp IS NULL ASC, m.timestamp DESC, m.id DESC, m.session_id ASC LIMIT ? OFFSET ?
"""
)


class ArchiveOutcome(NamedTuple):
    """一次歸檔的結果."""

    confirmed: int  # 確認在庫的訊息數（含另存成版本的）
    versions: int  # 同 id 已存著不同內容、另存成版本的筆數


class LegacyArchiveMismatchError(RuntimeError):
    """歸檔後讀回的內容和要存的不一樣（版本 id 已被別的內容占用）；整筆 rollback."""


def _content_text(content: Any) -> str:
    """訊息內容存成文字：字串原樣、None 是空字串、其他（多模態 list 等）存成 JSON."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


def _message_row(msg: dict[str, Any]) -> tuple[Any, ...]:
    """訊息轉成資料列：其餘欄位存成 metadata JSON；時間一律毫秒，沒有才用寫入當下.

    id 與 session id 先換成文字：欄位是 TEXT，SQLite 本來就把數字存成文字，讀回比對時型別才一致
    （匯入檔的 id 可能是數字；舊審查報告乙-1 審查）。
    """
    metadata = {k: v for k, v in msg.items() if k not in _RESERVED_KEYS}
    ts = (
        to_epoch_ms(msg.get("timestamp"))
        or to_epoch_ms(msg.get("createdAt"))
        or int(get_current_epoch() * 1000)
    )
    return (
        str(msg["id"]),
        str(msg["session_id"]),
        msg.get("parentId"),
        msg["role"],
        _content_text(msg["content"]),
        ts,
        json.dumps(metadata, ensure_ascii=False),
    )


def _row_key(row: tuple[Any, ...]) -> tuple[Any, Any, Any]:
    """比對「同一則訊息」用的欄位：session、角色、內容（messages.id 是全域主鍵）."""
    return row[1], row[3], row[4]


def _version_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """同 id 已存著不同內容時另存的版本列：id 由原 id 與內容算出，metadata 記 version_of."""
    msg_id, session_id, parent_id, role, content, ts, metadata = row
    key = json.dumps([session_id, role, content], ensure_ascii=False)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    meta = {**json.loads(metadata), "version_of": msg_id}
    return (
        f"{msg_id}~v{digest}",
        session_id,
        parent_id,
        role,
        content,
        ts,
        json.dumps(meta, ensure_ascii=False),
    )


def _stored(conn: sqlite3.Connection, ids: list[Any]) -> dict[Any, tuple[Any, Any, Any]]:
    """這批 id 目前存著的 (session, 角色, 內容)."""
    rows = conn.execute(_MESSAGES_BY_IDS, (json.dumps(ids, ensure_ascii=False),))
    return {msg_id: (session_id, role, content) for msg_id, session_id, role, content in rows}


class LegacyStore:
    """舊對話紀錄資料庫引擎.

    負責會話 (Sessions) 與訊息 (Messages) 的持久化與查詢。
    訊息表支援 parent_id 欄位，可用於構建非線性的對話分支。

    Attributes:
        db_path (str): SQLite 資料庫檔案的實體路徑。
    """

    def __init__(self, db_path: str) -> None:
        """初始化 LegacyStore；資料庫在第一次寫入時才建立，只查看不會產生檔案.

        Args:
            db_path (str): 實體資料庫路徑。
        """
        self.db_path = db_path

    def _db_exists(self) -> bool:
        """資料庫檔案是否已存在（讀取前檢查，避免 sqlite 自動建立空檔）."""
        return Path(self.db_path).is_file()

    def _init_db(self) -> None:
        """建立資料庫表結構與索引；失敗直接往上拋 (Internal)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # 1. 會話表 (Sessions)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    filename TEXT,
                    title TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
            """)

            # 2. 訊息表 (Messages) - 支援樹狀分支
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp INTEGER,
                    metadata TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
            """)

            # 建立索引以提升搜尋與樹狀查找速度
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_parent ON messages(parent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_timestamp ON messages(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_content ON messages(content)")

            # 3. 已處理的同步來源（檔名＋內容雜湊）：同步不搬檔，重跑靠這張表跳過
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_files (
                    filename TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    processed_at INTEGER NOT NULL,
                    PRIMARY KEY (filename, content_sha256)
                )
            """)

            conn.commit()

    def upsert_session(
        self,
        session_id: str,
        identity: AgentIdentity,
        filename: str,
        title: str,
        created_at: int,
        updated_at: int
    ) -> None:
        """新增或更新會話資訊；寫入失敗往上拋，由呼叫端決定是否保留原檔.

        Args:
            session_id (str): 會話唯一識別碼。
            identity (AgentIdentity): 所屬代理人身分。
            filename (str): 原始檔名備註。
            title (str): 會話標題。
            created_at (int): 建立時間戳。
            updated_at (int): 更新時間戳。
        """
        self._init_db()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                _SESSION_UPSERT,
                (session_id, identity.agent_id, filename, title, created_at, updated_at),
            )

    def upsert_messages(self, messages: list[dict[str, Any]]) -> None:
        """批次新增或更新訊息內容；寫入失敗往上拋，由呼叫端決定是否保留原檔.

        自動從輸入字典中分離元數據 (Metadata) 並轉換為 JSON 存儲。

        Args:
            messages (list[dict[str, Any]]): 訊息物件列表。
        """
        self._init_db()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(_MESSAGE_UPSERT, [_message_row(msg) for msg in messages])

    def archive(
        self,
        identity: AgentIdentity,
        sessions: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        source: tuple[str, str] | None = None,
    ) -> ArchiveOutcome:
        """歸檔：一個交易寫完 sessions、訊息與已處理紀錄；任一列失敗整筆 rollback 並往上拋.

        sessions 的標題等中繼資料照 upsert；訊息只新增，已歸檔的不被覆寫。同一個交易裡讀回比對：
        同 id 已存著同樣的 session、角色與內容算確認在庫；不同的另存成這則訊息的版本列（重跑不
        重複存），也讀回確認；讀回對不上丟 ``LegacyArchiveMismatchError``（舊審查報告乙-1）。
        ``source`` 是同步來源 ``(檔名, 內容 SHA-256)``；剪裁與匯出端點沒有來源檔，傳 None。
        """
        session_rows = [
            (s["id"], identity.agent_id, s["filename"], s["title"], s["created_at"],
             s["updated_at"])
            for s in sessions
        ]
        message_rows = [_message_row(msg) for msg in messages]
        self._init_db()
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executemany(_SESSION_UPSERT, session_rows)
            conn.executemany(_MESSAGE_INSERT_ONLY, message_rows)
            stored = _stored(conn, [row[0] for row in message_rows])
            version_rows = [
                _version_row(row) for row in message_rows if stored.get(row[0]) != _row_key(row)
            ]
            if version_rows:
                conn.executemany(_MESSAGE_INSERT_ONLY, version_rows)
                stored = _stored(conn, [row[0] for row in version_rows])
                if any(stored.get(row[0]) != _row_key(row) for row in version_rows):
                    raise LegacyArchiveMismatchError
            if source is not None:
                conn.execute(_PROCESSED_INSERT, (*source, int(get_current_epoch() * 1000)))
        return ArchiveOutcome(confirmed=len(message_rows), versions=len(version_rows))

    def processed_keys(self) -> set[tuple[str, str]]:
        """已處理的 (檔名, 內容雜湊)；沒有庫、或舊庫還沒有這張表回空集合（只讀，不建檔不建表）."""
        if not self._db_exists():
            return set()
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'processed_files'"
            ).fetchone()
            if not exists:
                return set()
            rows = conn.execute("SELECT filename, content_sha256 FROM processed_files")
            return {(name, digest) for name, digest in rows.fetchall()}

    def query_messages(
        self,
        query: str = "",
        role: str | None = None,
        date_start: int | None = None,
        date_end: int | None = None,
        limit: int = 10,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> ConversationPage:
        """搜尋舊對話：回總數、這一頁與未知時間數；每列帶所屬對話的標題.

        修正前寫入的列可能是秒：篩選與排序一律先換成毫秒再比（門檻見 legacy_time，ADR-0258）。
        只讀：沒有資料庫回空頁、不建檔；以唯讀模式開啟；讀取失敗往上拋。
        時間是毫秒（含頭含尾）；同時間的列再依訊息 ID 排，翻頁才不會重複或漏掉。
        """
        if not self._db_exists():
            return ConversationPage(0, [])
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matching = (query, pattern, role, role)
        where = (*matching, date_start, date_start, date_end, date_end)
        page_sql = _SEARCH_OLDEST if oldest_first else _SEARCH_NEWEST
        uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.create_function("to_epoch_ms", 1, to_epoch_ms, deterministic=True)
            conn.execute("BEGIN")
            total = conn.execute(_SEARCH_COUNT, where).fetchone()[0]
            unknown = conn.execute(_SEARCH_UNKNOWN, matching).fetchone()[0]
            rows = conn.execute(page_sql, (*where, limit, offset)).fetchall()
        return ConversationPage(
            total,
            [
                ConversationRow(
                    "legacy",
                    row["id"],
                    row["session_id"],
                    row["timestamp"],
                    row["role"],
                    row["content"],
                    row["title"],
                )
                for row in rows
            ],
            unknown,
        )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """依 ID 取一則訊息（記憶來源查回用）；資料庫不存在回 None、不建檔，讀取失敗往上拋."""
        if not self._db_exists():
            return None
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, session_id, role, content FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_random_messages(self, limit: int = 3) -> list[dict[str, Any]]:
        """隨機挑選訊息 (用於代理人的自發回想機制)；資料庫不存在回空清單，讀取失敗往上拋.

        Args:
            limit (int): 挑選筆數。

        Returns:
            list[dict[str, Any]]: 隨機訊息列表。
        """
        sql = "SELECT * FROM messages ORDER BY RANDOM() LIMIT ?"
        results = []
        if not self._db_exists():
            return results
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql, (limit,))
            for row in cursor.fetchall():
                item = dict(row)
                meta_raw = item.get("metadata")
                item["metadata"] = json.loads(meta_raw) if meta_raw else {}
                results.append(item)
        return results

    def get_stats(self) -> dict[str, Any]:
        """獲取資料庫統計數據；資料庫不存在回 0，讀取失敗往上拋.

        Returns:
            dict[str, Any]: 包含 session_count 與 message_count 的字典。
        """
        if not self._db_exists():
            return {"session_count": 0, "message_count": 0}
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            session_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM messages WHERE role='user'")
            message_count = cursor.fetchone()[0]
            return {"session_count": session_count, "message_count": message_count}
