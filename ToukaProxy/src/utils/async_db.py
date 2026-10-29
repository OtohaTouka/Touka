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

from .auth_mode import normalize_auth_mode
from .history_manager import (
    TreeMessageManager,
    count_rounds_from_linear,
    get_linear_messages_from_tree,
)
from .interjection_notes import annotate_node
from .session_scope import (
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
    from collections.abc import AsyncIterator

    from src.models.session_persistence import PersistedStreamAdmission, SaveResult

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
    canonical = json.dumps(
        [
            _persistence_epoch,
            session_id,
            identity.agent_id if identity else None,
            identity.is_human if identity else False,
            _persistence_generations.get(session_id, 0),
            row,
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
    ) -> SaveResult:
        """SQLite 層 compare+write 原子化，任何不明讀取／提交錯誤皆 unknown。"""
        return await SessionService._save_session(
            session_id,
            changes,
            identity,
            expected_revision=expected_revision,
            representation=representation,
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
                if row is None and not {"title", "messages"}.issubset(changes.model_fields_set):
                    return _persistence_rejected()
                updated = _apply_session_changes(session_id, row, changes, identity)
                _check_session_write(row, updated, identity, ui=ui, channel_id=channel_id)
                if message:
                    role, content, name = message
                    updated["messages"] = TreeMessageManager.append_node(
                        updated["messages"], role, content, name=name
                    )
                    updated["updatedAt"] = int(get_current_epoch() * 1000)
                if row is None and owner_user_id is not None:
                    updated["owner_user_id"] = owner_user_id
                await _write_session_row(db, updated)
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
                await db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
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
            async with (
                get_async_db() as db,
                db.execute("SELECT * FROM user_sessions ORDER BY updatedAt DESC") as cursor,
            ):
                rows = await cursor.fetchall()

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
        """設定或取消家對話 (Identity SSOT)。"""
        agent_id = _canonical_session_agent(identity.agent_id)
        try:
            async with _get_session_lock(session_id), get_async_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _strict_session_row(db, session_id)
                if row is None:
                    raise_error(ErrorCode.SESSION_NOT_FOUND)
                _check_session_write(row, dict(row), identity)
                await db.execute(
                    "UPDATE user_sessions SET is_home = 0, "
                    "home_for_agent = NULL WHERE home_for_agent IN (?, ?)",
                    (agent_id, f"ToukaSoul{agent_id}"),
                )
                if is_home:
                    await db.execute(
                        "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = ?",
                        (agent_id, session_id),
                    )
                await db.commit()
        except ToukaException:
            raise
        except Exception:
            logger.exception("Error setting home for %s", agent_id)
            return False
        else:
            return True


session_service = SessionService()

# --- Helpers ---


async def get_home_messages(
    identity: AgentIdentity, current_session_id: str | None = None
) -> list[dict[str, Any]]:
    """獲取特定 Agent 的家對話訊息 (Identity SSOT)。"""
    try:
        snapshot = await session_service.read_home_snapshot(identity)
    except ToukaException:
        logger.warning("SESSION_SCOPE_MISMATCH")
        return []
    if snapshot is None or snapshot.session["id"] == current_session_id:
        return []
    session = snapshot.session
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


async def set_system_setting(key: str, value: str) -> None:
    """設置系統設定。"""
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

        # ADR-0107: projects gain an explicit human owner column (migrate existing DBs).
        proj_cursor = await db.execute("PRAGMA table_info(projects)")
        proj_columns = [row[1] for row in await proj_cursor.fetchall()]
        if "owner_user_id" not in proj_columns:
            await db.execute("ALTER TABLE projects ADD COLUMN owner_user_id TEXT")

        await db.commit()
    logger.info("📁 Async Database initialized at %s", get_db_path())
