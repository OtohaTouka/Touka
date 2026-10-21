"""使用者對 AI 回覆的細粒度回饋儲存 (HAX G15/G6，ADR-0084)。

完全隔離於獨立的 `feedback.db`；**永不**接觸 agent soul (`data/soul/`)、legacy
(`data/legacy/`) 或記憶系統 (FactualStore／向量庫)。回饋僅供人工檢視，不自動回灌
agent 學習——杜絕自我優化迴圈、不牴觸 AGENT_SOUL_PROTECTION。儲存範式仿 SessionService；
身分以 AgentIdentity 物件流動 (Identity SSOT)，目錄解析重用 async_db 的 get_db_dir。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiosqlite

from src.utils.async_db import get_db_dir
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from src.models.identity import AgentIdentity

_VALID_TYPES = frozenset({"up", "down"})
_MAX_REASON = 500

_feedback_locks: dict[str, asyncio.Lock] = {}


def get_feedback_db_path() -> Path:
    """回饋資料庫實體路徑 (SSOT)；獨立於 ui_sessions.db。

    目錄解析重用 async_db.get_db_dir()（已確保存在的 SSOT 基底），不自行 IO。
    """
    return get_db_dir() / "feedback.db"


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _feedback_locks:
        _feedback_locks[session_id] = asyncio.Lock()
    return _feedback_locks[session_id]


@asynccontextmanager
async def _get_db() -> AsyncIterator[aiosqlite.Connection]:
    """提供已建表、設好 row_factory 的 feedback.db 連線（自癒：CREATE IF NOT EXISTS）。"""
    db = await aiosqlite.connect(str(get_feedback_db_path()))
    db.row_factory = aiosqlite.Row
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_feedback (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(session_id, message_id, agent_id)
            )
            """
        )
        await db.commit()
        yield db
    finally:
        await db.close()


class FeedbackStore:
    """回饋持久化服務 (SSOT)。靜態方法；自管隔離的 feedback.db。"""

    @staticmethod
    async def purge_agent(identity: AgentIdentity) -> int:
        """刪除某 agent 的全部回饋（破壞性重置 Batch 6 用）。回刪除筆數。"""
        async with _get_db() as db:
            cur = await db.execute(
                "DELETE FROM message_feedback WHERE agent_id = ?", (identity.agent_id,)
            )
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def count_agent(identity: AgentIdentity) -> int:
        """某 agent 的回饋筆數（完整性自檢）。"""
        async with _get_db() as db, db.execute(
            "SELECT COUNT(*) FROM message_feedback WHERE agent_id = ?", (identity.agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def submit(
        session_id: str,
        message_id: str,
        identity: AgentIdentity,
        feedback_type: str,
        reason: str | None = None,
    ) -> str | None:
        """新增/更新/撤回一筆回饋（身分以 AgentIdentity 物件傳入）。

        feedback_type='none' 代表撤回（刪列）；'up'/'down' 為 upsert。回傳 feedback id，
        撤回或無效型別回傳 None。同 (session_id, message_id, agent_id) 僅留一筆。
        """
        agent_id = identity.agent_id
        clean_reason = (reason or "").strip()[:_MAX_REASON] or None
        now = int(get_current_epoch() * 1000)

        async with _get_lock(session_id), _get_db() as db:
            if feedback_type == "none":
                await db.execute(
                    "DELETE FROM message_feedback "
                    "WHERE session_id = ? AND message_id = ? AND agent_id = ?",
                    (session_id, message_id, agent_id),
                )
                await db.commit()
                return None

            if feedback_type not in _VALID_TYPES:
                return None

            async with db.execute(
                "SELECT id FROM message_feedback "
                "WHERE session_id = ? AND message_id = ? AND agent_id = ?",
                (session_id, message_id, agent_id),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                fid = existing["id"]
                await db.execute(
                    "UPDATE message_feedback "
                    "SET feedback_type = ?, reason = ?, updated_at = ? WHERE id = ?",
                    (feedback_type, clean_reason, now, fid),
                )
            else:
                fid = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO message_feedback "
                    "(id, session_id, message_id, agent_id, feedback_type, reason, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fid, session_id, message_id, agent_id, feedback_type, clean_reason, now, now),
                )
            await db.commit()
            return fid

    @staticmethod
    async def get_session_map(
        identity: AgentIdentity, session_id: str
    ) -> dict[str, dict[str, Any]]:
        """回傳某身分於某 session 的 {message_id: {type, reason}}（供前端還原狀態）。"""
        agent_id = identity.agent_id
        async with _get_db() as db, db.execute(
            "SELECT message_id, feedback_type, reason FROM message_feedback "
            "WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            row["message_id"]: {"type": row["feedback_type"], "reason": row["reason"]}
            for row in rows
        }

    @staticmethod
    async def get_recent(limit: int = 50) -> list[dict[str, Any]]:
        """回傳最近的回饋（檢視頁用），依 updated_at 由新到舊。"""
        safe_limit = max(1, min(int(limit), 500))
        async with _get_db() as db, db.execute(
            "SELECT id, session_id, message_id, feedback_type, reason, "
            "created_at, updated_at FROM message_feedback "
            "ORDER BY updated_at DESC LIMIT ?",
            (safe_limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
