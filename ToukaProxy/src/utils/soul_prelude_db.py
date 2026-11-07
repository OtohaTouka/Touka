"""靈魂前情段落表（``ui_sessions.db`` 的 ``soul_prelude_segments``）的讀寫（3d PR 1，ADR-0305）.

前情＝一個靈魂累積的一段段摘要或來源註記；每段標它從哪段對話來、涵蓋什麼時間。
表在 ``async_db.init_db_async`` 建；這裡只經 ``get_async_db()`` 讀寫，不直接用 sqlite3。

- 同一靈魂、種類、來源、去重值、範圍版本只留一筆（INSERT OR IGNORE）；重做不會重複。
- 讀前情只看範圍版本 1、還沒被合併的段落。範圍版本由 ``async_db`` 的兩個失效函式改成 0
  （同名重建、還原備份），和對話的 ``session_scope_version`` 同一套。
- 合併只在被合併的段落上標 ``merged_into``，不刪；資料只新增。
- 讀寫失敗直接丟例外，由呼叫端決定怎麼處理。時間一律 epoch 毫秒。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.core.exceptions import ErrorCode, ToukaException
from src.utils.async_db import get_async_db

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from src.models.identity import AgentIdentity

# 段落種類：剪裁、退任摘要、子對話回報、換家標記、合併、升級前留下的舊摘要
SEGMENT_KINDS: frozenset[str] = frozenset(
    {"home_trim", "home_retired", "child_report", "handover", "merged", "legacy_summary"}
)

_INSERT = (
    "INSERT OR IGNORE INTO soul_prelude_segments "
    "(agent_id, scope_version, kind, source_session, dedupe_key, covers_from, covers_to, "
    "content, created_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)"
)
_LIST_LIVE = (
    "SELECT id, agent_id, kind, source_session, dedupe_key, covers_from, covers_to, content, "
    "created_at, merged_into FROM soul_prelude_segments "
    "WHERE agent_id = ? AND scope_version = 1 AND merged_into IS NULL ORDER BY id"
)
_HAS = (
    "SELECT 1 FROM soul_prelude_segments WHERE agent_id = ? AND scope_version = 1 "
    "AND kind = ? AND source_session = ? AND dedupe_key = ? LIMIT 1"
)
# 被合併的來源一次標完：一句固定 SQL，編號清單綁成一個 JSON 陣列參數（禁令 24）
_MARK_MERGED = (
    "UPDATE soul_prelude_segments SET merged_into = ? "
    "WHERE id IN (SELECT value FROM json_each(?)) AND agent_id = ? "
    "AND scope_version = 1 AND merged_into IS NULL"
)
_COUNT = "SELECT COUNT(*) FROM soul_prelude_segments WHERE agent_id = ?"
_DELETE = "DELETE FROM soul_prelude_segments WHERE agent_id = ?"
_LIST_ALL = (
    "SELECT id, agent_id, kind, source_session, dedupe_key, covers_from, covers_to, content, "
    "created_at, merged_into, scope_version FROM soul_prelude_segments "
    "WHERE agent_id = ? ORDER BY id"
)


@dataclass(frozen=True)
class PreludeSegment:
    """要寫入的一段前情；``agent`` 是靈魂的正式 ID，``source_session`` 沒有來源時是空字串."""

    agent: str
    kind: str
    source_session: str
    dedupe_key: str
    content: str
    created_at: int
    covers_from: int | None = None
    covers_to: int | None = None


@dataclass(frozen=True)
class StoredSegment:
    """讀回的一段前情（還沒被合併、範圍有效）."""

    id: int
    kind: str
    source_session: str
    dedupe_key: str
    content: str
    created_at: int
    covers_from: int | None
    covers_to: int | None


def _params(seg: PreludeSegment) -> tuple[Any, ...]:
    if seg.kind not in SEGMENT_KINDS:
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    return (
        seg.agent,
        seg.kind,
        seg.source_session,
        seg.dedupe_key,
        seg.covers_from,
        seg.covers_to,
        seg.content,
        seg.created_at,
    )


async def insert_segment_tx(db: aiosqlite.Connection, seg: PreludeSegment) -> bool:
    """在呼叫端已開的交易裡寫一段；同一段已存在時不寫，回 False（提交與否由呼叫端決定）."""
    cursor = await db.execute(_INSERT, _params(seg))
    return (cursor.rowcount or 0) > 0


async def insert_segment(seg: PreludeSegment) -> bool:
    """自己開交易寫一段；同一段已存在時不寫，回 False."""
    params = _params(seg)
    async with get_async_db() as db:
        cursor = await db.execute(_INSERT, params)
        await db.commit()
        return (cursor.rowcount or 0) > 0


async def has_segment(agent: str, kind: str, source_session: str, dedupe_key: str) -> bool:
    """這一段（範圍有效的）是否已經在；用來省掉重複的寫入交易."""
    async with get_async_db() as db, db.execute(
        _HAS, (agent, kind, source_session, dedupe_key)
    ) as cursor:
        return await cursor.fetchone() is not None


async def list_live_segments(agent: str) -> list[StoredSegment]:
    """某靈魂範圍有效、還沒被合併的段落，依寫入順序."""
    async with get_async_db() as db, db.execute(_LIST_LIVE, (agent,)) as cursor:
        rows = await cursor.fetchall()
    return [
        StoredSegment(
            id=row["id"],
            kind=row["kind"],
            source_session=row["source_session"],
            dedupe_key=row["dedupe_key"],
            content=row["content"],
            created_at=row["created_at"],
            covers_from=row["covers_from"],
            covers_to=row["covers_to"],
        )
        for row in rows
    ]


async def merge_segments(agent: str, source_ids: Sequence[int], merged: PreludeSegment) -> bool:
    """同一個交易寫合併段、標記來源；任何一個來源已不在、已被合併或不是這個靈魂的，整筆不做."""
    ids = sorted(set(source_ids))
    params = _params(merged)
    if merged.agent != agent or not ids:
        return False
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(_INSERT, params)
        if not cursor.rowcount:
            await db.rollback()
            return False
        merged_id = cursor.lastrowid
        cursor = await db.execute(_MARK_MERGED, (merged_id, json.dumps(ids), agent))
        if cursor.rowcount != len(ids):
            await db.rollback()
            return False
        await db.commit()
        return True


async def count_agent_segments(identity: AgentIdentity) -> int:
    """某靈魂的段落數（含失效、已合併；重置的完整性自檢與刪除前檢查用）."""
    async with get_async_db() as db, db.execute(_COUNT, (identity.agent_id,)) as cursor:
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def purge_agent_segments(identity: AgentIdentity) -> int:
    """刪掉某靈魂的全部段落（重置用）；回刪除筆數."""
    async with get_async_db() as db:
        cursor = await db.execute(_DELETE, (identity.agent_id,))
        await db.commit()
        return cursor.rowcount or 0


async def list_agent_segments(identity: AgentIdentity) -> list[dict[str, Any]]:
    """某靈魂的全部段落（含失效、已合併；個人資料匯出用）."""
    async with get_async_db() as db, db.execute(_LIST_ALL, (identity.agent_id,)) as cursor:
        return [dict(row) for row in await cursor.fetchall()]
