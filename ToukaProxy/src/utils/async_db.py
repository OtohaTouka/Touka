"""非同步資料庫管理模組.

負責對話 Session、專案審計與系統設定的持久化。
所有公開 API 均強制使用 AgentIdentity 物件 (Identity SSOT)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from memory_server.models.conversation_search import ConversationPage, ConversationSearchError

from src.core.exceptions import ErrorCode, ToukaException, raise_error
from src.models.history import TreeHistory
from src.models.identity import AgentIdentity
from src.models.session_persistence import (
    FailedResult,
    SavedResult,
    SessionChanges,
    UnknownResult,
)
from src.services.agent_service import agent_service
from src.services.authorization_gateway import can_access_session, can_drive_soul
from src.services.resource_manager import resource_manager
from src.services.ui_conversation_reader import conversation_page, project_conversation

from . import home_transitions
from .auth_mode import normalize_auth_mode
from .history_manager import (
    TreeMessageManager,
    count_rounds_from_linear,
    get_linear_messages_from_tree,
)
from .interjection_notes import annotate_node
from .session_scope import (
    can_be_home,
    can_rebind_session,
    channel_session_id,
    is_empty_draft,
    is_searchable_session,
    is_ui_turn_base,
    require_conversation_actor,
    require_session_scope,
)
from .time_utils import get_current_epoch, get_current_epoch_ms

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from src.models.session_persistence import PersistedStreamAdmission, SaveResult

    # 和對話列同一個交易裡的額外寫入（剪裁寫前情段落，ADR-0305）
    type AfterWrite = Callable[[aiosqlite.Connection], Awaitable[object]]

logger = logging.getLogger(__name__)

# 可信活動紀錄的起點（system_settings 的鍵）：第一次建活動事件表時寫入，之後不改。
TRUSTED_TIMELINE_EPOCH_KEY = "trusted_timeline_epoch"


def get_db_dir() -> Path:
    """獲取資料庫目錄 (SSOT)。"""
    path = Path(resource_manager.get_base_path())
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    """獲取資料庫實體路徑 (SSOT)。"""
    return Path(resource_manager.get_base_path()) / "ui_sessions.db"


def get_restore_pending_path() -> Path:
    """持久還原標記與對話庫共用 Proxy 當次的資料根。"""
    return Path(resource_manager.get_base_path()) / ".session-restore-pending"


def _require_no_pending_restore() -> None:
    if resource_manager.exists(get_restore_pending_path()):
        raise ConversationSearchError("internal_error")


@asynccontextmanager
async def get_async_db() -> AsyncIterator[aiosqlite.Connection]:
    """提供 aiosqlite 的非同步連線內容管理器。"""
    get_db_dir()
    db = await aiosqlite.connect(str(get_db_path()))
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


_session_locks: dict[str, asyncio.Lock] = {}
_persistence_epoch = uuid.uuid4().hex
_persistence_generations: dict[str, int] = {}


class SessionPersistenceError(RuntimeError):
    """strict 讀取失敗；不得把不明狀態降級為 absence 或洩漏資料。"""

    def __init__(self) -> None:
        super().__init__("Session persistence could not be confirmed")


@dataclass(frozen=True)
class SessionSnapshot:
    """內部快照保留 owner 供既有授權判斷；公開路由須移除該欄。"""

    session: dict[str, Any] | None
    revision: str


def _reject_json_constant(_value: str) -> None:
    raise SessionPersistenceError


async def _strict_session_row(db: aiosqlite.Connection, session_id: str) -> dict[str, Any] | None:
    cursor = await db.execute("SELECT * FROM user_sessions WHERE id = ?", (session_id,))
    raw = await cursor.fetchone()
    if raw is None:
        return None
    return _parse_session_row(raw)


def _parse_session_row(raw: aiosqlite.Row) -> dict[str, Any]:
    row = dict(raw)
    row["auth_level"] = normalize_auth_mode(row["auth_level"])  # 舊值讀取時對照（ADR-0246）
    for field in ("messages", "active_mcp_servers", "active_skills"):
        row[field] = json.loads(row[field], parse_constant=_reject_json_constant)
    if not isinstance(row["messages"], dict):
        # Legacy 列表可由舊 API 顯示；本 conditional 路徑不能以轉換後的值背書原列。
        raise SessionPersistenceError
    TreeHistory.model_validate(row["messages"])
    if row["is_home"] not in (0, 1):
        raise SessionPersistenceError
    for field in ("updatedAt", "last_activity_at"):
        if type(row[field]) is not int:
            raise SessionPersistenceError
    for field in ("active_mcp_servers", "active_skills"):
        if not isinstance(row[field], list) or any(
            not isinstance(item, str) for item in row[field]
        ):
            raise SessionPersistenceError
    json.dumps(row, allow_nan=False)
    return row


def _session_revision(
    session_id: str, row: dict[str, Any] | None, identity: AgentIdentity | None
) -> str:
    # 家的兩欄不算（ADR-0301）：只有設家改它、存檔不寫它，設家不能讓別處手上的版本失效
    content = (
        {k: v for k, v in row.items() if k not in {"is_home", "home_for_agent"}}
        if row is not None
        else None
    )
    canonical = json.dumps(
        [
            _persistence_epoch,
            session_id,
            identity.agent_id if identity else None,
            identity.is_human if identity else False,
            _persistence_generations.get(session_id, 0),
            content,
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _persistence_unknown() -> UnknownResult:
    return UnknownResult(
        status="unknown", error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id
    )


def _persistence_rejected(code: ErrorCode = ErrorCode.SESSION_SAVE_FAILED) -> FailedResult:
    return FailedResult(status="failed", error_code=ToukaException(code).error_id)


async def _write_session_row(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    """同一 BEGIN IMMEDIATE 交易內寫入；不開第二條連線，也不吞提交錯誤。"""
    await db.execute(
        """INSERT INTO user_sessions (
            id, title, messages, updatedAt, is_home, home_for_agent, agent_id, project_id,
            auth_level, active_mcp_servers, active_skills, last_activity_at,
            context_summary, owner_user_id, session_scope_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, messages=excluded.messages, updatedAt=excluded.updatedAt,
            is_home=excluded.is_home, home_for_agent=excluded.home_for_agent,
            agent_id=excluded.agent_id,
            project_id=excluded.project_id, auth_level=excluded.auth_level,
            active_mcp_servers=excluded.active_mcp_servers, active_skills=excluded.active_skills,
            last_activity_at=excluded.last_activity_at, context_summary=excluded.context_summary,
            session_scope_version=excluded.session_scope_version""",
        (
            row["id"],
            row["title"],
            json.dumps(row["messages"], ensure_ascii=False, allow_nan=False),
            row["updatedAt"],
            row["is_home"],
            row["home_for_agent"],
            row["agent_id"],
            row["project_id"],
            row["auth_level"],
            json.dumps(row["active_mcp_servers"], ensure_ascii=False),
            json.dumps(row["active_skills"], ensure_ascii=False),
            row["last_activity_at"],
            row["context_summary"],
            row["owner_user_id"],
            row["session_scope_version"],
        ),
    )


def _apply_session_changes(
    session_id: str,
    existing: dict[str, Any] | None,
    changes: SessionChanges,
    identity: AgentIdentity | None,
) -> dict[str, Any]:
    now = int(get_current_epoch() * 1000)
    row = (
        dict(existing)
        if existing is not None
        else {
            "id": session_id,
            "title": "",
            "messages": TreeMessageManager.create_empty_tree(),
            "updatedAt": now,
            "is_home": 0,
            "home_for_agent": None,
            "agent_id": None,
            "project_id": None,
            "auth_level": "Manual",
            "active_mcp_servers": [],
            "active_skills": [],
            "last_activity_at": now,
            "context_summary": None,
            "owner_user_id": identity.agent_id if identity and identity.is_human else None,
            "session_scope_version": 1,
        }
    )
    for field in changes.model_fields_set:
        value = getattr(changes, field)
        if field in {"agent_id", "home_for_agent"}:
            value = value.agent_id if value is not None else None
        elif field == "is_home":
            value = int(value)
        row[field] = value
    row["last_activity_at"] = now
    return row


def _canonical_session_agent(value: str | None) -> str | None:
    passport = agent_service.identify_agent(value) if value else None
    return passport["agent_id"] if passport and passport["is_toukasoul"] else value


def canonical_agent_id(identity: AgentIdentity) -> str:
    """靈魂的正式 ID（別名換成註冊名）；和對話歸屬、家歸屬同一條規則."""
    return _canonical_session_agent(identity.agent_id) or identity.agent_id


def _session_agent_candidates(identity: AgentIdentity) -> tuple[str, str]:
    target = _canonical_session_agent(identity.agent_id) or identity.agent_id
    return target, f"ToukaSoul{target}" if target != "DebugAgent" else target


def _check_session_write(
    existing: dict[str, Any] | None,
    row: dict[str, Any],
    identity: AgentIdentity | None,
    *,
    ui: bool = False,
    channel_id: str | None = None,
) -> None:
    bound = _canonical_session_agent(row["agent_id"])
    require_session_scope(row, bound, bound, _canonical_session_agent(row["home_for_agent"]))
    if existing is None and row["id"].startswith("touka.") and row["id"] != channel_id:
        raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
    if identity and identity.is_human:
        if existing and not can_access_session(
            identity, existing["agent_id"], existing["owner_user_id"]
        ):
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        for value in (existing["agent_id"] if existing else None, bound):
            passport = agent_service.identify_agent(value) if value else None
            if (
                passport
                and passport["is_toukasoul"]
                and not can_drive_soul(identity, AgentIdentity(agent_id=passport["agent_id"]))
            ):
                raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    elif bound != (_canonical_session_agent(identity.agent_id) if identity else None):
        raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
    if existing:
        old = _canonical_session_agent(existing["agent_id"])
        require_session_scope(
            existing, old, old, _canonical_session_agent(existing["home_for_agent"])
        )
        if old != bound:
            if not can_rebind_session(
                existing, human_ui=bool(ui and identity and identity.is_human), target=bound
            ):
                raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
            if not (is_empty_draft(existing) and is_empty_draft(row)):
                row["session_scope_version"] = 0
        else:
            row["agent_id"] = existing["agent_id"]


async def _may_use_session(
    db: aiosqlite.Connection, session_id: str, requester: AgentIdentity
) -> bool:
    """請求者能不能在這段對話開新的一輪；只回是或否，不丟權限錯誤.

    和存檔同一套檢查；另外能驅動這段對話（正規化後）的靈魂也算：舊前綴的列擁有者逐字比對
    會失敗，後面各層卻會正規化前綴後代打寫入（第 2 輪審查 K5）。
    """
    row = await _strict_session_row(db, session_id)
    if row is None:
        return False
    try:
        _check_session_write(row, dict(row), requester)
    except ToukaException:
        bound = _canonical_session_agent(row["agent_id"])
        passport = agent_service.identify_agent(bound) if bound else None
        return bool(
            passport
            and passport["is_toukasoul"]
            and can_drive_soul(requester, AgentIdentity(agent_id=passport["agent_id"]))
        )
    return True


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    """表在不在（還原或升級前的資料庫可能還沒建）."""
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return await cursor.fetchone() is not None


# 跟著對話一起失效的靈魂資料表（範圍版本 1 → 0；前情 ADR-0305、換家 ADR-0306）。
# 每句都是固定字面量（禁令 24）
_INVALIDATE_AGENT_SQL = {
    "soul_prelude_segments": (
        "UPDATE soul_prelude_segments SET scope_version = 0 WHERE agent_id IN (?, ?)"
    ),
    "soul_home_transitions": (
        "UPDATE soul_home_transitions SET scope_version = 0 WHERE agent_id IN (?, ?)"
    ),
    "soul_child_report_marks": (
        "UPDATE soul_child_report_marks SET scope_version = 0 WHERE agent_id IN (?, ?)"
    ),
}
_INVALIDATE_ALL_SQL = {
    "soul_prelude_segments": "UPDATE soul_prelude_segments SET scope_version = 0",
    "soul_home_transitions": "UPDATE soul_home_transitions SET scope_version = 0",
    "soul_child_report_marks": "UPDATE soul_child_report_marks SET scope_version = 0",
}
_COUNT_HOME_RECORDS_SQL = {
    "soul_home_transitions": "SELECT COUNT(*) FROM soul_home_transitions WHERE agent_id IN (?, ?)",
    "soul_child_report_marks": (
        "SELECT COUNT(*) FROM soul_child_report_marks WHERE agent_id IN (?, ?)"
    ),
}
_PURGE_HOME_RECORDS_SQL = {
    "soul_home_transitions": "DELETE FROM soul_home_transitions WHERE agent_id IN (?, ?)",
    "soul_child_report_marks": "DELETE FROM soul_child_report_marks WHERE agent_id IN (?, ?)",
}
_LIST_HOME_RECORDS_SQL = {
    "soul_home_transitions": (
        "SELECT * FROM soul_home_transitions WHERE agent_id IN (?, ?) ORDER BY id"
    ),
    "soul_child_report_marks": (
        "SELECT * FROM soul_child_report_marks WHERE agent_id IN (?, ?) ORDER BY session_id"
    ),
}
_SCOPED_SOUL_TABLES = tuple(_INVALIDATE_AGENT_SQL)
_HOME_RECORD_TABLES = tuple(_COUNT_HOME_RECORDS_SQL)


async def _homes_of(db: aiosqlite.Connection, agent_id: str) -> list[str]:
    cursor = await db.execute(
        "SELECT id FROM user_sessions WHERE is_home = 1 AND home_for_agent IN (?, ?) ORDER BY id",
        (agent_id, f"ToukaSoul{agent_id}"),
    )
    return [row[0] for row in await cursor.fetchall()]


def new_home_after_write(identity: AgentIdentity, session_id: str) -> AfterWrite:
    """心跳建家：新列寫入的同一個交易裡確認沒有別的家，再照狀態對照表改交接紀錄（ADR-0306）."""
    agent_id = _canonical_session_agent(identity.agent_id) or identity.agent_id

    async def _after_write(db: aiosqlite.Connection) -> None:
        if [sid for sid in await _homes_of(db, agent_id) if sid != session_id]:
            # 讀家之後有人設了家：整筆退回，不做出兩個家
            raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
        await home_transitions.apply_home_change(
            db, agent_id, session_id, make_home=True, homes=[], now=get_current_epoch_ms()
        )

    return _after_write


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


class SessionService:
    """統一管理對話 Session 的服務類別 (SSOT)。"""

    @staticmethod
    async def query_ui_conversations(
        identity: AgentIdentity,
        query: str,
        role: str | None,
        date_start: int | None,
        date_end: int | None,
        oldest_first: bool,
        limit: int,
        offset: int,
    ) -> ConversationPage:
        """家候選先於內容讀取，全部查詢及切頁共用一個普通讀取交易。"""
        try:
            _require_no_pending_restore()
            passport = agent_service.identify_agent(identity.agent_id)
            require_conversation_actor(identity, passport)
            target, alias = _session_agent_candidates(identity)
            if not resource_manager.exists(get_db_path()):
                return ConversationPage(0, [])
            async with get_async_db() as db:
                await db.execute("BEGIN")
                cursor = await db.execute(
                    "SELECT id, agent_id, is_home, home_for_agent, session_scope_version "
                    "FROM user_sessions WHERE is_home = 1 AND home_for_agent IN (?, ?)",
                    (target, f"ToukaSoul{target}"),
                )
                homes = await cursor.fetchall()
                if len(homes) > 1:
                    raise_error(ErrorCode.SESSION_SCOPE_MISMATCH)
                for home in homes:
                    SessionService.require_scope(dict(home), identity)
                cursor = await db.execute(
                    "SELECT id, agent_id, is_home, home_for_agent, session_scope_version, "
                    "title, messages FROM user_sessions "
                    "WHERE agent_id IN (?, ?) AND session_scope_version = 1",
                    (target, alias),
                )
                messages = []
                async for raw in cursor:
                    row = dict(raw)
                    if not is_searchable_session(row):
                        continue
                    SessionService.require_scope(row, identity)
                    messages.extend(project_conversation(row))
                page = conversation_page(
                    messages, query, role, date_start, date_end, oldest_first, limit, offset
                )
                await db.commit()
                return page
        except ConversationSearchError:
            raise
        except ToukaException as exc:
            reason = (
                "permission_denied"
                if exc.code == ErrorCode.SESSION_SCOPE_MISMATCH
                else "internal_error"
            )
            raise ConversationSearchError(reason) from None
        except Exception:
            raise ConversationSearchError("internal_error") from None

    @staticmethod
    def require_scope(row: dict[str, Any], identity: AgentIdentity) -> None:
        """讀取歷史前核對可信執行身分；與交易內寫入共用純規則。"""
        require_session_scope(
            row,
            _canonical_session_agent(row["agent_id"]),
            _canonical_session_agent(identity.agent_id),
            _canonical_session_agent(row["home_for_agent"]),
        )

    @staticmethod
    async def read_execution_snapshot(
        session_id: str, identity: AgentIdentity
    ) -> SessionSnapshot:
        """缺席可供建立；壞列與外靈魂不得當作空對話。"""
        snapshot = await SessionService.read_persistence_snapshot(session_id, identity)
        if snapshot.session is not None:
            SessionService.require_scope(snapshot.session, identity)
        return snapshot

    @staticmethod
    async def open_channel_session(
        kind: str, identity: AgentIdentity, *coordinates: str, title: str
    ) -> str:
        """內部頻道只能建立由可信身分與完整座標計算出的 ID。"""
        sid = channel_session_id(kind, _canonical_session_agent(identity.agent_id), *coordinates)
        snapshot = await SessionService.read_execution_snapshot(sid, identity)
        if snapshot.session is None:
            result = await SessionService._save_session(
                sid,
                SessionChanges(
                    title=title, messages=TreeMessageManager.create_empty_tree(), agent_id=identity
                ),
                identity,
                expected_revision=snapshot.revision,
                channel_id=sid,
            )
            if not isinstance(result, SavedResult):
                raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)
        return sid

    @staticmethod
    async def admit_ui_stream(
        session_id: str,
        admission: PersistedStreamAdmission,
        requester: AgentIdentity,
        target: AgentIdentity | None,
    ) -> SessionSnapshot | SaveResult:
        """同一交易驗證生成基底與換綁，回傳提交的精確快照。"""
        if not requester.is_human:
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _strict_session_row(db, session_id)
                if (
                    row is None
                    or admission.revision != _session_revision(session_id, row, requester)
                    or not is_ui_turn_base(
                        row, admission.user_parent_id, admission.assistant_node_id
                    )
                ):
                    return _persistence_rejected()
                # 舊家不能開新的一輪（最後判定，ADR-0306）；新家第一次開始用記在同一個交易
                if await home_transitions.is_old_home(db, session_id):
                    return _persistence_rejected(ErrorCode.SESSION_OLD_HOME_READ_ONLY)
                await home_transitions.mark_started(db, session_id, get_current_epoch_ms())
                changes = SessionChanges(agent_id=target) if target else SessionChanges()
                updated = _apply_session_changes(session_id, row, changes, requester)
                _check_session_write(row, updated, requester, ui=True)
                if row["agent_id"] != updated["agent_id"]:
                    await _write_session_row(db, updated)
                else:
                    updated = row
                result = SessionSnapshot(updated, _session_revision(session_id, updated, requester))
                await db.commit()
                return result
        except ToukaException:
            raise
        except Exception:
            return _persistence_unknown()

    @staticmethod
    async def read_persistence_snapshot(
        session_id: str, identity: AgentIdentity
    ) -> SessionSnapshot:
        """只從成功 strict 讀取核發 token；呼叫端仍須既有 owner/agent 授權。"""
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                row = await _strict_session_row(db, session_id)
                return SessionSnapshot(row, _session_revision(session_id, row, identity))
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def conditional_save_session(
        session_id: str,
        expected_revision: str,
        changes: SessionChanges,
        identity: AgentIdentity,
        *,
        representation: str = "full",
        require_home: bool = False,
        after_write: AfterWrite | None = None,
    ) -> SaveResult:
        """SQLite 層 compare+write 原子化，任何不明讀取／提交錯誤皆 unknown。

        ``require_home``：剪裁用，交易內那一列已經不是家就不寫（家標記不算進版本，ADR-0301）。
        ``after_write``：剪裁用，在同一個交易裡寫前情段落（ADR-0305）；樹沒寫它就不跑，它失敗
        樹也不寫。
        """
        return await SessionService._save_session(
            session_id,
            changes,
            identity,
            expected_revision=expected_revision,
            representation=representation,
            require_home=require_home,
            after_write=after_write,
        )

    @staticmethod
    async def save_ui_session(
        session_id: str,
        expected_revision: str,
        changes: SessionChanges,
        requester: AgentIdentity,
        target: AgentIdentity | None,
    ) -> SaveResult:
        """UI 換綁專用入口；只採伺服器解析的目標，權限在交易內重驗。"""
        return await SessionService._save_session(
            session_id,
            changes.model_copy(update={"agent_id": target}),
            requester,
            expected_revision=expected_revision,
            ui=True,
        )

    @staticmethod
    async def _save_session(
        session_id: str,
        changes: SessionChanges,
        identity: AgentIdentity | None,
        *,
        expected_revision: str | None = None,
        representation: str = "full",
        ui: bool = False,
        message: tuple[str, str, str | None] | None = None,
        owner_user_id: str | None = None,
        channel_id: str | None = None,
        require_home: bool = False,
        after_write: AfterWrite | None = None,
    ) -> SaveResult:
        """所有內容寫入共用鎖、嚴格讀取與同一個 IMMEDIATE 交易。"""
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _strict_session_row(db, session_id)
                if expected_revision is not None and expected_revision != _session_revision(
                    session_id, row, identity
                ):
                    return _persistence_rejected()
                if require_home and not (row and row["is_home"]):
                    return _persistence_rejected()
                # 已存在的對話，家標記只由 set_home 改（它先清掉同一個靈魂的舊家）；
                # 版本不含它（ADR-0301），帶版本的存檔改它一律擋下
                if (
                    expected_revision is not None
                    and row is not None
                    and {"is_home", "home_for_agent"} & changes.model_fields_set
                ):
                    return _persistence_rejected()
                if row is None and not {"title", "messages"}.issubset(changes.model_fields_set):
                    return _persistence_rejected()
                # 交接開始後舊家唯讀（第二段，含完成與失敗）：所有內容寫入都擋（ADR-0306）
                if row is not None and await home_transitions.handover_started(db, session_id):
                    return _persistence_rejected(ErrorCode.SESSION_OLD_HOME_READ_ONLY)
                updated = _apply_session_changes(session_id, row, changes, identity)
                _check_session_write(row, updated, identity, ui=ui, channel_id=channel_id)
                if (
                    row is not None
                    and updated["agent_id"] != row["agent_id"]
                    and await home_transitions.is_old_home(db, session_id)
                ):  # 舊家不能換靈魂
                    return _persistence_rejected(ErrorCode.SESSION_OLD_HOME_READ_ONLY)
                if message:
                    role, content, name = message
                    updated["messages"] = TreeMessageManager.append_node(
                        updated["messages"], role, content, name=name
                    )
                    updated["updatedAt"] = int(get_current_epoch() * 1000)
                if row is None and owner_user_id is not None:
                    updated["owner_user_id"] = owner_user_id
                await _write_session_row(db, updated)
                if after_write is not None:
                    await after_write(db)
                if row is None:
                    _persistence_generations[session_id] = (
                        _persistence_generations.get(session_id, 0) + 1
                    )
                # 先建立回覆值，確保後續型別／序列化錯誤不會發生於提交之後。
                result = SavedResult(
                    status="saved",
                    representation=representation,
                    revision=_session_revision(session_id, updated, identity),
                )
                await db.commit()
                return result
        except ToukaException as exc:
            if expected_revision is None:
                raise
            return _persistence_rejected(exc.code)
        except Exception:
            logger.exception("Session persistence could not be confirmed")
            return _persistence_unknown()

    @staticmethod
    async def conditional_delete_session(
        session_id: str,
        expected_revision: str,
        identity: AgentIdentity,
    ) -> SaveResult:
        """刪除也持同一把鎖；已缺席不能冒充刪除成功，home 保護保持。"""
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _strict_session_row(db, session_id)
                if row is None:
                    return _persistence_rejected(ErrorCode.SESSION_NOT_FOUND)
                if not expected_revision or expected_revision != _session_revision(
                    session_id, row, identity
                ):
                    return _persistence_rejected()
                if row["is_home"]:
                    return _persistence_rejected(ErrorCode.AUTH_PERMISSION_DENIED)
                if await home_transitions.blocks_delete(db, session_id):  # ADR-0306
                    return _persistence_rejected(ErrorCode.SESSION_OLD_HOME_NOT_DELETABLE)
                await db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
                await home_transitions.forget_session(db, session_id)  # ADR-0306
                # 保守先撤銷舊 generation；即使 commit outcome unknown 也不可重播旧 token。
                _persistence_generations[session_id] = (
                    _persistence_generations.get(session_id, 0) + 1
                )
                await db.commit()
                return SavedResult(
                    status="saved",
                    representation="full",
                    revision=_session_revision(session_id, None, identity),
                )
        except Exception:
            return _persistence_unknown()

    @staticmethod
    async def get_all_sessions() -> list[dict[str, Any]]:
        """獲取所有對話，按更新時間排序。"""
        try:
            async with get_async_db() as db:
                cursor = await db.execute("SELECT * FROM user_sessions ORDER BY updatedAt DESC")
                rows = await cursor.fetchall()
                try:  # 只是清單上的標記：讀不到時清單照常，擋寫入的各閘照舊失敗（ADR-0306）
                    old_homes = await home_transitions.all_old_homes(db)
                except Exception:
                    logger.warning("Old homes are unreadable; the list shows none", exc_info=True)
                    old_homes = set()

            sessions = []
            for row in rows:
                s = dict(row)
                try:
                    messages = json.loads(s["messages"])
                    s["messages"] = messages

                    # 計算對話圈數 (Round Count) - 以 User 訊息數量為基準
                    path = get_linear_messages_from_tree(messages)
                    s["round_count"] = count_rounds_from_linear(path)
                except Exception:
                    logger.exception("Error parsing messages for session %s", s.get("id"))
                    s["messages"] = TreeMessageManager.create_empty_tree()
                    s["round_count"] = 0

                s["is_home"] = bool(s["is_home"])
                s["is_old_home"] = s["id"] in old_homes  # 舊家（唯讀，ADR-0306）
                s["auth_level"] = normalize_auth_mode(s.get("auth_level"))
                try:
                    s["active_mcp_servers"] = json.loads(s.get("active_mcp_servers", "[]"))
                except Exception:
                    s["active_mcp_servers"] = []
                try:
                    s["active_skills"] = json.loads(s.get("active_skills", "[]"))
                except Exception:
                    s["active_skills"] = []
                sessions.append(s)
        except Exception:
            logger.exception("Error listing sessions")
            return []
        else:
            return sessions

    @staticmethod
    async def get_session_by_id(session_id: str) -> dict[str, Any] | None:
        """根據 ID 獲取單一對話。"""
        try:
            async with (
                get_async_db() as db,
                db.execute("SELECT * FROM user_sessions WHERE id = ?", (session_id,)) as cursor,
            ):
                row = await cursor.fetchone()

            if row:
                s = dict(row)
                try:
                    messages = json.loads(s["messages"])
                    s["messages"] = messages

                    path = get_linear_messages_from_tree(messages)
                    s["round_count"] = count_rounds_from_linear(path)
                except Exception:
                    logger.exception("Error parsing messages for session %s", session_id)
                    s["messages"] = TreeMessageManager.create_empty_tree()
                    s["round_count"] = 0

                s["is_home"] = bool(s["is_home"])
                s["auth_level"] = normalize_auth_mode(s.get("auth_level"))
                try:
                    s["active_mcp_servers"] = json.loads(s.get("active_mcp_servers", "[]"))
                except Exception:
                    s["active_mcp_servers"] = []
                try:
                    s["active_skills"] = json.loads(s.get("active_skills", "[]"))
                except Exception:
                    s["active_skills"] = []
                return s
        except Exception:
            logger.exception("Error getting session %s", session_id)
            return None
        else:
            return None

    @staticmethod
    async def get_all_projects() -> list[dict[str, Any]]:
        """獲取所有專案（依更新時間排序）。"""
        try:
            async with (
                get_async_db() as db,
                db.execute("SELECT * FROM projects ORDER BY updatedAt DESC") as cursor,
            ):
                rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Error listing projects")
            return []

    @staticmethod
    async def get_project_agent_ids(project_id: str) -> list[str]:
        """獲取某專案下所有會話的 agent_id（擁有權判定 SSOT）。"""
        try:
            async with (
                get_async_db() as db,
                db.execute(
                    "SELECT DISTINCT agent_id FROM user_sessions WHERE project_id = ?",
                    (project_id,),
                ) as cursor,
            ):
                rows = await cursor.fetchall()
            return [row["agent_id"] for row in rows if row["agent_id"]]
        except Exception:
            logger.exception("Error getting project agents for %s", project_id)
            return []

    @staticmethod
    async def get_project_owner(project_id: str) -> str | None:
        """ADR-0107：取得專案的明確擁有者 user_id（無 owner 或查詢失敗 → None）。"""
        if not project_id:
            return None
        try:
            async with (
                get_async_db() as db,
                db.execute(
                    "SELECT owner_user_id FROM projects WHERE id = ?", (project_id,)
                ) as cursor,
            ):
                row = await cursor.fetchone()
            return row["owner_user_id"] if row else None
        except Exception:
            logger.exception("Error getting project owner for %s", project_id)
            return None

    @staticmethod
    async def project_exists(project_id: str) -> bool:
        """專案登記列是否已存在於 DB（E-fix）。

        用於區分「建立新專案」(id 尚未持久化) 與「操作既有孤兒專案」(無 owner、無
        session 的遺留列)——後者不可由非 admin 修改/刪除。get_project_owner 回 None
        無法區分此二者 (無列 vs 列存在但 owner 為 NULL)，故另設此述詞。
        """
        if not project_id:
            return False
        try:
            async with (
                get_async_db() as db,
                db.execute("SELECT 1 FROM projects WHERE id = ? LIMIT 1", (project_id,)) as cursor,
            ):
                return await cursor.fetchone() is not None
        except Exception:
            logger.exception("Error checking project existence for %s", project_id)
            return False

    @staticmethod
    async def clear_projects_owned_by(user_id: str) -> int:
        """ADR-0107：刪除某使用者擁有專案的「設定列」，回傳清除的專案數。

        其底下 sessions 的 project_id 置 NULL（**保留對話內容**，只解除專案標籤）。
        ⚠️**不刪除實體 root_path 資料夾**——僅移除 Touka 的專案登記。
        """
        if not user_id:
            return 0
        async with get_async_db() as db:
            async with db.execute(
                "SELECT id FROM projects WHERE owner_user_id = ?", (user_id,)
            ) as cursor:
                pids = [row["id"] for row in await cursor.fetchall()]
            for pid in pids:
                # 解除對話與此專案的關聯（保留對話本身）
                await db.execute(
                    "UPDATE user_sessions SET project_id = NULL WHERE project_id = ?", (pid,)
                )
                # 刪除專案設定列（不觸碰 root_path 實體資料夾）
                await db.execute("DELETE FROM projects WHERE id = ?", (pid,))
            await db.commit()
        return len(pids)

    @staticmethod
    async def read_home_snapshot(identity: AgentIdentity) -> SessionSnapshot | None:
        """一次讀出全部候選與樹；absence、歸屬拒絕和儲存失敗不可混同。"""
        try:
            agent_id = _canonical_session_agent(identity.agent_id)
            async with get_async_db() as db:
                cursor = await db.execute(
                    "SELECT * FROM user_sessions WHERE is_home = 1 AND home_for_agent IN (?, ?)",
                    (agent_id, f"ToukaSoul{agent_id}"),
                )
                rows = await cursor.fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise_error(ErrorCode.SESSION_SCOPE_MISMATCH)
            row = _parse_session_row(rows[0])
            require_session_scope(
                row,
                _canonical_session_agent(row["agent_id"]),
                agent_id,
                _canonical_session_agent(row["home_for_agent"]),
            )
            return SessionSnapshot(row, _session_revision(row["id"], row, identity))
        except ToukaException:
            raise
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def get_home_session_id(identity: AgentIdentity) -> str | None:
        """顯示入口不回傳矛盾家；執行入口應使用嚴格快照。"""
        try:
            snapshot = await SessionService.read_home_snapshot(identity)
            return snapshot.session["id"] if snapshot else None
        except ToukaException:
            logger.warning("SESSION_SCOPE_MISMATCH")
            return None

    @staticmethod
    async def upsert_session(
        session_id: str,
        title: str,
        messages: dict | list,
        identity: AgentIdentity | None = None,
        project_id: str | None = None,
        auth_level: str | None = None,
        active_mcp_servers: list[str] | None = None,
        active_skills: list[str] | None = None,
        is_home: bool | None = None,
        home_for_agent: str | None = None,
        updatedAt: int | None = None,
        context_summary: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """新增或更新對話 (Identity SSOT)。

        內容寫入交由共用交易取得 session 鎖並驗證最新綁定。

        ADR-0092 Phase D：``context_summary`` 為 session 級摘要欄位（None=保留舊值）。

        ``identity`` 語意固定為「session 所屬 agent」；``owner_user_id`` 為明確人類
        歸屬（僅 INSERT 時寫入、UPDATE 永不改——比照 ADR-0107 projects）。
        """
        return await SessionService._upsert_session_unlocked(
            session_id=session_id,
            title=title,
            messages=messages,
            identity=identity,
            project_id=project_id,
            auth_level=auth_level,
            active_mcp_servers=active_mcp_servers,
            active_skills=active_skills,
            is_home=is_home,
            home_for_agent=home_for_agent,
            updatedAt=updatedAt,
            context_summary=context_summary,
            owner_user_id=owner_user_id,
        )

    @staticmethod
    async def _upsert_session_unlocked(
        session_id: str,
        title: str,
        messages: dict | list,
        identity: AgentIdentity | None = None,
        project_id: str | None = None,
        auth_level: str | None = None,
        active_mcp_servers: list[str] | None = None,
        active_skills: list[str] | None = None,
        is_home: bool | None = None,
        home_for_agent: str | None = None,
        updatedAt: int | None = None,
        context_summary: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """整理舊式 upsert 參數；由共用保存交易取得鎖。"""
        values = {
            "title": title,
            "messages": messages,
            "updatedAt": updatedAt or int(get_current_epoch() * 1000),
            "project_id": project_id or None,
            "auth_level": auth_level,
            "active_mcp_servers": active_mcp_servers,
            "active_skills": active_skills,
            "is_home": is_home,
            "context_summary": context_summary,
            "agent_id": identity if identity and not identity.is_human else None,
            "home_for_agent": AgentIdentity(agent_id=home_for_agent) if home_for_agent else None,
        }
        if isinstance(messages, list):
            values["messages"] = TreeHistory.from_flat_list(messages).model_dump(mode="json")
        try:
            result = await SessionService._save_session(
                session_id,
                SessionChanges(**{k: v for k, v in values.items() if v is not None}),
                identity,
                owner_user_id=owner_user_id,
            )
        except ToukaException:
            raise
        except Exception:
            logger.exception("Error upserting session %s", session_id)
            return False
        else:
            return result.status == "saved"

    @staticmethod
    async def append_message_to_session(
        session_id: str,
        role: str,
        content: str,
        name: str | None = None,
        identity: AgentIdentity | None = None,
    ) -> bool:
        """向現有對話追加訊息 (Identity SSOT)。"""
        result = await SessionService._save_session(
            session_id, SessionChanges(), identity, message=(role, content, name)
        )
        return result.status == "saved"

    @staticmethod
    async def delete_session(session_id: str) -> bool:
        """刪除對話。家對話禁止刪除。"""
        session = await SessionService.get_session_by_id(session_id)
        if not session:
            return False
        if session["is_home"]:
            msg = "Home session cannot be deleted"
            raise ValueError(msg)

        try:
            async with get_async_db() as db:
                await db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
                await db.commit()
        except Exception:
            logger.exception("Error deleting session %s", session_id)
            raise
        else:
            return True

    @staticmethod
    async def invalidate_agent_sessions(identity: AgentIdentity) -> None:
        """新靈魂可用前撤銷舊列來源標記；不修改內容、綁定或人類歸屬。"""
        try:
            target, alias = _session_agent_candidates(identity)
            async with get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "UPDATE user_sessions SET session_scope_version = 0 "
                    "WHERE agent_id IN (?, ?) OR home_for_agent IN (?, ?)",
                    (target, alias, target, alias),
                )
                # 舊前情與換家紀錄不給同名的新靈魂看（ADR-0305、0306）
                for table in _SCOPED_SOUL_TABLES:
                    if await _has_table(db, table):
                        await db.execute(_INVALIDATE_AGENT_SQL[table], (target, alias))
                await db.commit()
        except Exception:
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None

    @staticmethod
    def mark_restore_pending() -> None:
        """子程序覆蓋資料前先原子寫入，讓中斷後的啟動仍能補降標記。"""
        resource_manager.write_file_text(get_restore_pending_path(), "pending\n")

    @staticmethod
    async def invalidate_restored_sessions() -> None:
        """撤銷全部列的來源標記，成功後才解除持久搜尋封鎖。"""
        try:
            if resource_manager.exists(get_db_path()):
                async with get_async_db() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute("PRAGMA table_info(user_sessions)")
                    columns = {row["name"] for row in await cursor.fetchall()}
                    # 缺表或欄位留給正常啟動 migration，新增標記預設即為 0。
                    if "session_scope_version" in columns:
                        await db.execute("UPDATE user_sessions SET session_scope_version = 0")
                    # 還原的對話來源不可信，前情與換家紀錄一起失效
                    for table in _SCOPED_SOUL_TABLES:
                        if await _has_table(db, table):
                            await db.execute(_INVALIDATE_ALL_SQL[table])
                    await db.commit()
            resource_manager.remove_file(get_restore_pending_path())
        except Exception:
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None

    @staticmethod
    async def recover_pending_restore() -> None:
        """啟動補降失敗時繼續服務，但保留持久標記讓搜尋維持拒絕。"""
        try:
            if resource_manager.exists(get_restore_pending_path()):
                await SessionService.invalidate_restored_sessions()
        except Exception:
            logger.warning("SESSION_RESTORE_RECOVERY_FAILED")

    @staticmethod
    async def purge_agent_sessions(identity: AgentIdentity) -> int:
        """刪除某 agent 的**全部**會話（含 home；破壞性重置 Batch 6 用）。回刪除筆數。"""
        async with get_async_db() as db:
            cur = await db.execute(
                "DELETE FROM user_sessions WHERE agent_id IN (?, ?)",
                _session_agent_candidates(identity),
            )
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def count_agent_sessions(identity: AgentIdentity) -> int:
        """某 agent 的會話筆數（完整性自檢用）。"""
        async with (
            get_async_db() as db,
            db.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE agent_id IN (?, ?)",
                _session_agent_candidates(identity),
            ) as cur,
        ):
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def set_home(session_id: str, identity: AgentIdentity, is_home: bool) -> bool:
        """設定或取消家對話 (Identity SSOT)；同一個交易裡照狀態對照表改交接紀錄（ADR-0306）.

        取消家只對現任家有作用；換家時舊家留下紀錄（之後是唯讀的舊家），還沒開始用的新家
        改回去或改設別段時當沒換過。
        """
        agent_id = _canonical_session_agent(identity.agent_id) or identity.agent_id
        if is_home and not can_be_home(session_id):
            raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _strict_session_row(db, session_id)
                if row is None:
                    raise_error(ErrorCode.SESSION_NOT_FOUND)
                _check_session_write(row, dict(row), identity)
                homes = await _homes_of(db, agent_id)
                await home_transitions.apply_home_change(
                    db,
                    agent_id,
                    session_id,
                    make_home=is_home,
                    homes=homes,
                    now=get_current_epoch_ms(),
                )
                if is_home:
                    await db.execute(
                        "UPDATE user_sessions SET is_home = 0, home_for_agent = NULL "
                        "WHERE home_for_agent IN (?, ?) AND id != ?",
                        (agent_id, f"ToukaSoul{agent_id}", session_id),
                    )
                    await db.execute(
                        "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = ?",
                        (agent_id, session_id),
                    )
                elif session_id in homes:
                    await db.execute(
                        "UPDATE user_sessions SET is_home = 0, home_for_agent = NULL WHERE id = ?",
                        (session_id,),
                    )
                await db.commit()
        except ToukaException:
            raise
        except Exception:
            logger.exception("Error setting home for %s", agent_id)
            return False
        else:
            return True

    @staticmethod
    async def is_old_home(session_id: str) -> bool:
        """舊家（唯讀）：由交接紀錄推導；讀不到時丟 SessionPersistenceError（ADR-0306）."""
        try:
            async with get_async_db() as db:
                return await home_transitions.is_old_home(db, session_id)
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def old_home_blocks_delete(session_id: str) -> bool:
        """舊家交接完成前不能刪；讀不到時丟 SessionPersistenceError."""
        try:
            async with get_async_db() as db:
                return await home_transitions.blocks_delete(db, session_id)
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def check_old_home(
        session_id: str, *, mark_started: bool, requester: AgentIdentity | None
    ) -> bool:
        """開新的一輪之前的檢查：舊家回 True（要擋）；``mark_started`` 時順便記新家開始用.

        ``requester``（已驗證的請求者）要能用這段對話（和存檔同一套歸屬與權限規則）；不能用時
        什麼都不寫、回 False（不透露是不是舊家），由後面的層照原樣拒絕。沒有身分的內部路徑
        （SNS 閘道）照舊。只有真的是還沒開始用的新家才開寫入交易；讀寫不到時丟
        SessionPersistenceError。
        """
        try:
            async with get_async_db() as db:
                old = await home_transitions.is_old_home(db, session_id)
                fresh = (
                    not old
                    and mark_started
                    and await home_transitions.is_unused_new_home(db, session_id)
                )
                if not (old or fresh):
                    return False
                if requester is not None and not await _may_use_session(
                    db, session_id, requester
                ):
                    return False
                if fresh:
                    await db.execute("BEGIN IMMEDIATE")
                    await home_transitions.mark_started(db, session_id, get_current_epoch_ms())
                    await db.commit()
                return old
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def read_unfinished_old_homes(identity: AgentIdentity) -> list[dict[str, Any]]:
        """還沒交接完的舊家（依紀錄順序、不含現任家）；主線帶它們的原文（ADR-0306）."""
        try:
            agent_id = _canonical_session_agent(identity.agent_id) or identity.agent_id
            async with get_async_db() as db:
                sessions = []
                for sid in await home_transitions.unfinished_old_homes(db, agent_id):
                    try:
                        row = await _strict_session_row(db, sid)
                    except (SessionPersistenceError, ValueError, TypeError):
                        # 一段舊家壞掉不能讓整個靈魂每一輪都失敗：跳過、記下來
                        logger.warning("Old home %s is unreadable; not sent", sid)
                        continue
                    if row is None:
                        continue
                    try:
                        require_session_scope(
                            row,
                            _canonical_session_agent(row["agent_id"]),
                            agent_id,
                            _canonical_session_agent(row["home_for_agent"]),
                        )
                    except ToukaException:
                        logger.warning("Old home %s is out of scope; not sent", sid)
                        continue
                    sessions.append(row)
                return sessions
        except Exception:
            raise SessionPersistenceError from None

    @staticmethod
    async def count_agent_home_records(identity: AgentIdentity) -> int:
        """某靈魂的換家紀錄與回報位置筆數（重置殘留自檢用）."""
        async with get_async_db() as db:
            total = 0
            for table in _HOME_RECORD_TABLES:
                cursor = await db.execute(
                    _COUNT_HOME_RECORDS_SQL[table], _session_agent_candidates(identity)
                )
                row = await cursor.fetchone()
                total += int(row[0]) if row else 0
            return total

    @staticmethod
    async def purge_agent_home_records(identity: AgentIdentity) -> int:
        """刪除某靈魂的換家紀錄與回報位置（破壞性重置用）；回刪除筆數."""
        async with get_async_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            total = 0
            for table in _HOME_RECORD_TABLES:
                cursor = await db.execute(
                    _PURGE_HOME_RECORDS_SQL[table], _session_agent_candidates(identity)
                )
                total += cursor.rowcount or 0
            await db.commit()
            return total

    @staticmethod
    async def list_agent_home_records(identity: AgentIdentity) -> dict[str, list[dict[str, Any]]]:
        """某靈魂的換家紀錄與回報位置（GDPR 匯出用；只有對話編號與狀態，沒有內容）."""
        async with get_async_db() as db:
            result: dict[str, list[dict[str, Any]]] = {}
            for table in _HOME_RECORD_TABLES:
                cursor = await db.execute(
                    _LIST_HOME_RECORDS_SQL[table], _session_agent_candidates(identity)
                )
                result[table] = [dict(row) for row in await cursor.fetchall()]
            return result


session_service = SessionService()

# --- Helpers ---


def home_messages_of(
    session: dict[str, Any], current_session_id: str | None = None
) -> list[dict[str, Any]]:
    """已讀到的家對話要送的訊息（不含 system）；它就是目前這段對話時回空（ADR-0305 審查 F16）."""
    if session["id"] == current_session_id:
        return []
    return [  # 插話放回那一則 AI 回覆（ADR-0249）
        annotate_node(m)
        for m in TreeMessageManager.get_linear_path(session["messages"])
        if m.get("role") != "system"
    ]


async def get_project_context(project_id: str) -> str:
    """獲取專案上下文描述。"""
    if not project_id or not get_db_path().exists():
        return ""
    try:
        async with (
            get_async_db() as db,
            db.execute(
                "SELECT name, root_path FROM projects WHERE id = ?",
                (project_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
            if row:
                return (
                    f"\n\n[Project Context: 📁 {row['name']}]\n"
                    f"Working Directory: {row['root_path']}\n"
                    "Note: You have CRUD permissions within this directory."
                )
    except Exception:
        logger.exception("Error fetching project context for %s", project_id)
        return ""
    else:
        return ""


async def get_project_root_path(project_id: str) -> str | None:
    """獲取特定專案的物理根目錄路徑 (SSOT)。"""
    if not project_id or not get_db_path().exists():
        return None
    try:
        async with (
            get_async_db() as db,
            db.execute("SELECT root_path FROM projects WHERE id = ?", (project_id,)) as cursor,
        ):
            row = await cursor.fetchone()
            return row["root_path"] if row else None
    except Exception:
        logger.exception("Error fetching project root path for %s", project_id)
        return None


async def list_project_root_paths() -> list[str]:
    """所有專案的根目錄（ADR-0292 hook 路徑界線）；讀不到時丟例外，不回空清單（fail-closed）。"""
    if not get_db_path().exists():
        return []
    async with (
        get_async_db() as db,
        db.execute("SELECT root_path FROM projects") as cursor,
    ):
        rows = await cursor.fetchall()
    return [row["root_path"] for row in rows if row["root_path"]]


async def add_project_event(
    project_id: str, identity: AgentIdentity, action: str, summary: str
) -> None:
    """記錄專案審計事件 (Identity SSOT)。"""
    if not project_id:
        return
    agent_id = identity.agent_id
    try:
        # H-fix: store milliseconds to match every sibling timestamp (save_project /
        # upsert_session / ProjectModel.updatedAt all use get_current_epoch()*1000).
        now = int(get_current_epoch() * 1000)
        async with get_async_db() as db:
            await db.execute(
                "INSERT INTO project_audit "
                "(project_id, agent_id, action, summary, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, agent_id, action, summary, now),
            )
            await db.commit()
    except Exception:
        logger.exception("Error recording project event")


async def get_system_setting(key: str, default: Any = None) -> Any:
    """獲取系統設定。"""
    if not get_db_path().exists():
        return default
    try:
        async with (
            get_async_db() as db,
            db.execute("SELECT value FROM system_settings WHERE key = ?", (key,)) as cursor,
        ):
            row = await cursor.fetchone()
            return row["value"] if row else default
    except Exception:
        return default


async def set_system_setting(key: str, value: str) -> bool:
    """設置系統設定；寫不進去時記 log、回 False。"""
    try:
        async with get_async_db() as db:
            await db.execute(
                "INSERT INTO system_settings (key, value) "
                "VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                (key, value),
            )
            await db.commit()
    except Exception:
        logger.exception("Error setting system setting %s", key)
        return False
    return True


# 升級時既有子對話的回報位置：只取字串的目前節點（不是字串、內容壞掉都當沒有），
# 由 SQLite 取值，不把整份對話讀進記憶體
_INITIAL_REPORT_MARKS_SQL = (
    "SELECT id, agent_id, CASE WHEN json_valid(messages) THEN "
    "CASE WHEN json_type(messages, '$.currentId') = 'text' "
    "THEN json_extract(messages, '$.currentId') END END "
    "FROM user_sessions WHERE is_home = 0 AND agent_id IS NOT NULL"
)


async def _initial_report_marks(db: aiosqlite.Connection) -> list[tuple[str, str, str | None]]:
    cursor = await db.execute(_INITIAL_REPORT_MARKS_SQL)
    return [
        (sid, _canonical_session_agent(agent) or agent, node)
        for sid, agent, node in await cursor.fetchall()
    ]


async def init_db_async() -> None:
    """初始化資料庫表結構並執行遷移。"""
    async with get_async_db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                messages TEXT,
                updatedAt INTEGER,
                is_home INTEGER DEFAULT 0,
                home_for_agent TEXT,
                agent_id TEXT,
                project_id TEXT,
                auth_level TEXT DEFAULT 'Manual',
                active_mcp_servers TEXT DEFAULT '[]',
                active_skills TEXT DEFAULT '[]',
                last_activity_at INTEGER,
                context_summary TEXT,
                owner_user_id TEXT,
                session_scope_version INTEGER NOT NULL DEFAULT 0
            )
        """
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS projects ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "root_path TEXT NOT NULL, description TEXT, "
            "owner_user_id TEXT, "  # ADR-0107: explicit human owner (set on create by humans)
            "updatedAt INTEGER NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS project_audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "project_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
            "action TEXT NOT NULL, summary TEXT, "
            "timestamp INTEGER NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # 可信活動事件：只有 id、計數與固定列舉值，不存內容；只由 continuity_events 的 recorder
        # 寫（trust 一律 'system'），讀寫在 utils/continuity_events_db.py。
        await db.execute(
            "CREATE TABLE IF NOT EXISTS continuity_events ("
            "event_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "occurred_at INTEGER NOT NULL, session_id TEXT, subject TEXT NOT NULL, "
            "trust TEXT NOT NULL DEFAULT 'system', metadata TEXT NOT NULL DEFAULT '{}')"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_continuity_events_agent_kind_time "
            "ON continuity_events (agent_id, kind, occurred_at)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            (TRUSTED_TIMELINE_EPOCH_KEY, str(get_current_epoch_ms())),
        )
        # 靈魂前情段落（3d，ADR-0305）：讀寫在 utils/soul_prelude_db.py；範圍版本和對話一起失效
        await db.execute(
            "CREATE TABLE IF NOT EXISTS soul_prelude_segments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, "
            "scope_version INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL, "
            "source_session TEXT NOT NULL, dedupe_key TEXT NOT NULL, "
            "covers_from INTEGER, covers_to INTEGER, content TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, merged_into INTEGER)"
        )
        # 只有有效列（範圍版本 1）要唯一：失效列可以重複，失效兩次才不會撞（審查 F1）
        await db.execute("DROP INDEX IF EXISTS idx_soul_prelude_unique")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_soul_prelude_live_unique "
            "ON soul_prelude_segments (agent_id, kind, source_session, dedupe_key) "
            "WHERE scope_version = 1"
        )
        await db.commit()

        # Migration: Add missing columns
        cursor = await db.execute("PRAGMA table_info(user_sessions)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "agent_id" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN agent_id TEXT")
        if "project_id" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN project_id TEXT")
        if "auth_level" not in columns:
            await db.execute(
                "ALTER TABLE user_sessions ADD COLUMN auth_level TEXT DEFAULT 'Manual'"
            )
        if "active_mcp_servers" not in columns:
            await db.execute(
                "ALTER TABLE user_sessions ADD COLUMN active_mcp_servers TEXT DEFAULT '[]'"
            )
        if "active_skills" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN active_skills TEXT DEFAULT '[]'")
        if "last_activity_at" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN last_activity_at INTEGER")
        if "context_summary" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN context_summary TEXT")
        # Session 明確人類歸屬：owner_user_id（比照 ADR-0107 projects 遷移 pattern；
        # human 建立時記錄、UPDATE 永不改；NULL＝legacy 沿用 agent-derived 判定）。
        if "owner_user_id" not in columns:
            await db.execute("ALTER TABLE user_sessions ADD COLUMN owner_user_id TEXT")
        if "session_scope_version" not in columns:
            await db.execute(
                "ALTER TABLE user_sessions ADD COLUMN "
                "session_scope_version INTEGER NOT NULL DEFAULT 0"
            )

        # 換家交接紀錄與子對話回報位置（3d PR 2a，ADR-0306）：升級時既有子對話的回報位置
        # 設在當下內容，不補寫歷史；之後新建的子對話沒有位置（全部算新內容）。
        # 建表與初始位置在同一個明確交易：中途失敗就整個重來，不留下空的位置表
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        if await home_transitions.create_tables(db):
            await home_transitions.set_initial_marks(
                db, await _initial_report_marks(db), get_current_epoch_ms()
            )
        await db.commit()

        # ADR-0107: projects gain an explicit human owner column (migrate existing DBs).
        proj_cursor = await db.execute("PRAGMA table_info(projects)")
        proj_columns = [row[1] for row in await proj_cursor.fetchall()]
        if "owner_user_id" not in proj_columns:
            await db.execute("ALTER TABLE projects ADD COLUMN owner_user_id TEXT")

        await db.commit()
    logger.info("📁 Async Database initialized at %s", get_db_path())
