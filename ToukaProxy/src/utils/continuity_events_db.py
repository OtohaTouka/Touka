"""可信活動事件表（``ui_sessions.db`` 的 ``continuity_events``）的讀寫.

表在 ``async_db.init_db_async`` 建；這裡只經 ``get_async_db()`` 讀寫，不直接用 sqlite3。
只存 id、計數與固定列舉值，不存任何內容文字；trust 只寫 ``'system'``（寫在 SQL 裡，寫入介面
沒有這個參數）。時間一律 epoch 毫秒。讀寫失敗直接丟例外，由呼叫端決定怎麼處理（recorder 只記
warning、重置把它記成 store 錯誤、GDPR 匯出列進 skipped）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.utils.async_db import TRUSTED_TIMELINE_EPOCH_KEY, get_async_db

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

# 任務狀態的事件種類（寫入時同一任務最新一筆已經是同一狀態就不記）
TASK_KINDS: frozenset[str] = frozenset(
    {"task_todo", "task_doing", "task_blocked", "task_failed", "task_done"}
)

_INSERT = (
    "INSERT OR IGNORE INTO continuity_events "
    "(event_id, agent_id, kind, occurred_at, session_id, subject, trust, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, 'system', ?)"
)
# 同一任務最新一筆已經是同一個狀態（例如 DOING 中補產出）就不記；單一敘述，在 SQLite 內原子。
_INSERT_TASK = (
    "INSERT OR IGNORE INTO continuity_events "
    "(event_id, agent_id, kind, occurred_at, session_id, subject, trust, metadata) "
    "SELECT ?, ?, ?, ?, ?, ?, 'system', ? WHERE COALESCE(("
    "SELECT kind FROM continuity_events WHERE agent_id = ? AND subject = ? "
    "AND kind IN ('task_todo', 'task_doing', 'task_blocked', 'task_failed', 'task_done') "
    "ORDER BY occurred_at DESC, rowid DESC LIMIT 1), '') <> ?"
)
# 心跳在入口被擋：最新一筆心跳事件已經是同一個原因就不記（連續被擋只留第一次，ADR-0318）
_INSERT_SKIP = (
    "INSERT OR IGNORE INTO continuity_events "
    "(event_id, agent_id, kind, occurred_at, session_id, subject, trust, metadata) "
    "SELECT ?, ?, ?, ?, ?, ?, 'system', ? WHERE COALESCE(("
    "SELECT kind || ':' || subject FROM continuity_events WHERE agent_id = ? "
    "AND kind IN ('heartbeat_finished', 'heartbeat_skipped') "
    "ORDER BY occurred_at DESC, rowid DESC LIMIT 1), '') <> ?"
)
_COUNT = "SELECT COUNT(*) FROM continuity_events WHERE agent_id = ?"
_DELETE = "DELETE FROM continuity_events WHERE agent_id = ?"
_SET_START = (
    "INSERT INTO system_settings (key, value) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)
_LIST = (
    "SELECT event_id, kind, occurred_at, session_id, subject, trust, metadata "
    "FROM continuity_events WHERE agent_id = ? ORDER BY occurred_at, rowid"
)


@dataclass(frozen=True)
class EventRow:
    """要寫入的一筆事件；``agent`` 是動手的靈魂 ID，metadata 只放 id、計數與固定列舉值."""

    event_id: str
    agent: str
    kind: str
    occurred_at: int
    subject: str
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def agent_epoch_key(identity: AgentIdentity) -> str:
    """某靈魂重置後的「可信紀錄起點」在 system_settings 的鍵."""
    return f"{TRUSTED_TIMELINE_EPOCH_KEY}:{identity.agent_id}"


async def insert_event(row: EventRow) -> bool:
    """寫一筆事件；同一個 event_id 已存在（或任務同一狀態重寫）時不寫，回 False."""
    params = (
        row.event_id,
        row.agent,
        row.kind,
        row.occurred_at,
        row.session_id,
        row.subject,
        json.dumps(row.metadata, ensure_ascii=False, sort_keys=True),
    )
    async with get_async_db() as db:
        if row.kind in TASK_KINDS:
            cursor = await db.execute(_INSERT_TASK, (*params, row.agent, row.subject, row.kind))
        elif row.kind == "heartbeat_skipped":
            last = f"{row.kind}:{row.subject}"
            cursor = await db.execute(_INSERT_SKIP, (*params, row.agent, last))
        else:
            cursor = await db.execute(_INSERT, params)
        await db.commit()
        return bool(cursor.rowcount)


async def count_agent_events(identity: AgentIdentity) -> int:
    """某靈魂的事件筆數（重置自檢）."""
    async with get_async_db() as db, db.execute(_COUNT, (identity.agent_id,)) as cursor:
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def purge_agent_events(identity: AgentIdentity, *, marker_ms: int) -> int:
    """刪掉某靈魂的全部事件，同一個交易寫它的可信紀錄起點；回刪除筆數."""
    async with get_async_db() as db:
        cursor = await db.execute(_DELETE, (identity.agent_id,))
        await db.execute(_SET_START, (agent_epoch_key(identity), str(marker_ms)))
        await db.commit()
        return cursor.rowcount or 0


async def list_agent_events(identity: AgentIdentity) -> list[dict[str, Any]]:
    """某靈魂的全部事件（舊到新；GDPR 匯出用），metadata 解回 dict."""
    async with get_async_db() as db, db.execute(_LIST, (identity.agent_id,)) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) | {"metadata": json.loads(row["metadata"] or "{}")} for row in rows]


# ── continuity 的讀取（一條連線讀完；不含這一輪的訊息）──────────────────────
_LAST_TURN_HERE = (
    "SELECT MAX(occurred_at) FROM continuity_events "
    "WHERE agent_id = ? AND kind = 'user_turn' AND session_id = ? AND subject <> ?"
)
_LAST_HUMAN = (
    "SELECT MAX(occurred_at) FROM continuity_events "
    "WHERE agent_id = ? AND kind = 'user_turn' AND subject <> ?"
)
_LAST_RUN = (
    "SELECT kind, occurred_at, metadata FROM continuity_events "
    "WHERE agent_id = ? AND kind IN ('heartbeat_finished', 'heartbeat_skipped') "
    "ORDER BY occurred_at DESC, rowid DESC LIMIT 1"
)
_RECENT = (
    "SELECT kind, occurred_at, subject, metadata FROM continuity_events "
    "WHERE agent_id = ? AND kind IN ('task_done', 'mission_completed', 'mission_finalized') "
    "AND occurred_at >= ? ORDER BY occurred_at DESC, rowid DESC LIMIT ?"
)
# 最近在群聊與 SNS 說過幾次、最後一次（家裡那一行，ADR-0323）
_ELSEWHERE = (
    "SELECT kind, COUNT(*), MAX(occurred_at) FROM continuity_events "
    "WHERE agent_id = ? AND kind IN ('group_turn', 'sns_turn') AND occurred_at >= ? "
    "GROUP BY kind ORDER BY kind"
)
_STARTS = "SELECT value FROM system_settings WHERE key IN (?, ?)"


@dataclass(frozen=True)
class TimelineSnapshot:
    """continuity 一次讀到的活動事件（全部毫秒；沒有紀錄是 None 或空）."""

    last_turn_here_ms: int | None
    last_human_ms: int | None
    last_run: tuple[str, int, dict[str, Any]] | None  # 種類（結束或入口被擋）、毫秒、metadata
    recent: tuple[tuple[str, int, str, dict[str, Any]], ...]
    records_start_ms: int | None
    elsewhere: tuple[tuple[str, int, int], ...] = ()  # 種類、次數、最後一次的毫秒


async def _scalar(db: Any, sql: str, params: tuple[Any, ...]) -> int | None:
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def read_snapshot(
    identity: AgentIdentity,
    *,
    session_id: str | None,
    exclude_message_id: str | None,
    since_ms: int,
    limit: int,
    elsewhere_since_ms: int | None = None,
) -> TimelineSnapshot:
    """一條連線讀完 continuity 要的事件（不含這一輪的訊息）；讀不到丟例外."""
    agent, skip = identity.agent_id, exclude_message_id or ""
    async with get_async_db() as db:
        here = (
            await _scalar(db, _LAST_TURN_HERE, (agent, session_id, skip)) if session_id else None
        )
        human = await _scalar(db, _LAST_HUMAN, (agent, skip))
        async with db.execute(_LAST_RUN, (agent,)) as cursor:
            run = await cursor.fetchone()
        async with db.execute(_RECENT, (agent, since_ms, limit)) as cursor:
            recent = await cursor.fetchall()
        records_start = await _records_start(db, identity)
        elsewhere: list[Any] = []
        if elsewhere_since_ms is not None:
            async with db.execute(_ELSEWHERE, (agent, elsewhere_since_ms)) as cursor:
                elsewhere = list(await cursor.fetchall())
    return TimelineSnapshot(
        last_turn_here_ms=here,
        last_human_ms=human,
        last_run=(run[0], int(run[1]), json.loads(run[2] or "{}")) if run else None,
        recent=tuple(
            (row[0], int(row[1]), row[2], json.loads(row[3] or "{}")) for row in recent
        ),
        records_start_ms=records_start,
        elsewhere=tuple((row[0], int(row[1]), int(row[2])) for row in elsewhere),
    )


async def _records_start(db: Any, identity: AgentIdentity) -> int | None:
    async with db.execute(
        _STARTS, (TRUSTED_TIMELINE_EPOCH_KEY, agent_epoch_key(identity))
    ) as cursor:
        starts = [int(row[0]) for row in await cursor.fetchall() if str(row[0]).isdigit()]
    return max(starts) if starts else None


async def read_records_start(identity: AgentIdentity) -> int | None:
    """某靈魂的可信紀錄起點（全域與重置後兩個鍵取較晚的；毫秒）；讀不到丟例外."""
    async with get_async_db() as db:
        return await _records_start(db, identity)
