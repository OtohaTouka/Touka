"""換家交接紀錄與子對話回報位置（3d PR 2a，ADR-0306）.

兩張表都在 ``ui_sessions.db``，由 ``async_db.init_db_async`` 呼叫 ``create_tables`` 建立：

- ``soul_home_transitions``：每次換家、取消家記一筆「舊家 → 新家（可空）」。
- ``soul_child_report_marks``：子對話的回報寫到哪（2b 寫回報時用；2a 只建表與升級時的初始位置）。

這裡只放「在呼叫端已開的交易裡」做的事，參數一律是連線與字串；不 import ``async_db``
（``async_db`` 的設家、存檔閘、刪除與准入要在自己的交易裡用它們）。

- 紀錄的範圍版本同前情段落：1 有效，同名重建與還原備份改成 0（失效：不處理、不算舊家、
  不擋刪除，ADR-0276／0277）。
- 「是不是舊家」由紀錄推導：某筆有效、沒取消的紀錄的舊家，而且現在不是家。
- ``old_handled_by``：「舊家已由第 n 筆交接」的紀錄只記新家是誰（2b 交接時只寫換家標記），
  不讓舊家多算一次舊家，也不擋刪除。
- 時間一律 epoch 毫秒，由呼叫端給。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.utils.session_scope import can_be_home

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

# 交接紀錄的狀態：待交接、交接中、完成、取消、失敗（失效另以範圍版本 0 表示）
PENDING = "pending"
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"

_CREATE_TRANSITIONS = (
    "CREATE TABLE IF NOT EXISTS soul_home_transitions ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, "
    "scope_version INTEGER NOT NULL DEFAULT 1, old_session TEXT NOT NULL, new_session TEXT, "
    "new_started INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', "
    "step INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
    "retired_summary_pending INTEGER NOT NULL DEFAULT 0, old_handled_by INTEGER, "
    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
)
_CREATE_TRANSITIONS_OLD_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_soul_home_transitions_old "
    "ON soul_home_transitions (old_session)"
)
_CREATE_TRANSITIONS_AGENT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_soul_home_transitions_agent "
    "ON soul_home_transitions (agent_id, status)"
)
_CREATE_MARKS = (
    "CREATE TABLE IF NOT EXISTS soul_child_report_marks ("
    "session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
    "scope_version INTEGER NOT NULL DEFAULT 1, last_node TEXT, updated_at INTEGER NOT NULL)"
)
_HAS_MARKS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'soul_child_report_marks'"
_INSERT_MARK = (
    "INSERT OR IGNORE INTO soul_child_report_marks "
    "(session_id, agent_id, scope_version, last_node, updated_at) VALUES (?, ?, 1, ?, ?)"
)

_INSERT = (
    "INSERT INTO soul_home_transitions "
    "(agent_id, old_session, new_session, old_handled_by, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_SET_NEW = "UPDATE soul_home_transitions SET new_session = ?, updated_at = ? WHERE id = ?"
_SET_STATUS = "UPDATE soul_home_transitions SET status = ?, updated_at = ? WHERE id = ?"
# 還沒開始用的新家：有效、待交接、新家沒用過；改回去或改設別段時改寫這些紀錄，當沒換過
# （既有資料有兩個家時會有兩筆指向同一個新家，一起改）
_UNUSED_FOR_NEW = (
    "SELECT id, old_session, new_session, old_handled_by FROM soul_home_transitions "
    "WHERE agent_id = ? AND scope_version = 1 AND status = 'pending' AND new_started = 0 "
    "AND new_session = ? ORDER BY id"
)
# 取消家之後、還沒開始交接、還沒有新家的紀錄（既有資料有兩個家時會有兩筆，一起改指新家）
_UNHOMED_WAITING = (
    "SELECT id, old_session, new_session, old_handled_by FROM soul_home_transitions "
    "WHERE agent_id = ? AND scope_version = 1 AND status = 'pending' AND new_session IS NULL "
    "AND old_handled_by IS NULL ORDER BY id"
)
# 交接中或失敗（失敗的會再試、仍算舊家）
_LATEST_UNHOMED_STARTED = (
    "SELECT id, old_session, new_session, old_handled_by FROM soul_home_transitions "
    "WHERE agent_id = ? AND scope_version = 1 AND status IN ('running', 'failed') "
    "AND new_session IS NULL AND old_handled_by IS NULL ORDER BY id DESC LIMIT 1"
)
# 開始用只記在「新家目前就是家」的紀錄上：已經回到普通對話的那段不算
_MARK_STARTED = (
    "UPDATE soul_home_transitions SET new_started = 1, updated_at = ? "
    "WHERE new_session = ? AND scope_version = 1 AND status = 'pending' AND new_started = 0 "
    "AND EXISTS (SELECT 1 FROM user_sessions s WHERE s.id = new_session AND s.is_home = 1)"
)
_UNUSED_NEW_HOME = (
    "SELECT 1 FROM soul_home_transitions t JOIN user_sessions s ON s.id = t.new_session "
    "WHERE t.new_session = ? AND t.scope_version = 1 AND t.status = 'pending' "
    "AND t.new_started = 0 AND s.is_home = 1 LIMIT 1"
)
# 舊家：有效、沒取消、不是「只記新家」的紀錄的舊家，而且那段對話現在不是家。
# 下面幾句都是固定字面量（禁令 24），只差最後的條件
_IS_OLD_HOME = (
    "SELECT 1 FROM soul_home_transitions t JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.scope_version = 1 AND t.status != 'cancelled' AND t.old_handled_by IS NULL "
    "AND s.is_home = 0 AND t.old_session = ? LIMIT 1"
)
_BLOCKS_DELETE = (
    "SELECT 1 FROM soul_home_transitions t JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.scope_version = 1 AND t.status != 'cancelled' AND t.old_handled_by IS NULL "
    "AND s.is_home = 0 AND t.old_session = ? "
    "AND (t.status IN ('pending', 'running', 'failed') OR t.retired_summary_pending = 1) LIMIT 1"
)
# 交接已開始：交接中，或已經做到某一步（之後重試回到待交接也算）；完成或失敗只看這段舊家
# 最新一筆有效紀錄（以前那一任交接完、回任用過再換走時，這一任還沒開始）
_HANDOVER_STARTED = (
    "SELECT 1 FROM soul_home_transitions t JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.scope_version = 1 AND t.status != 'cancelled' AND t.old_handled_by IS NULL "
    "AND s.is_home = 0 AND t.old_session = ? "
    "AND (t.status = 'running' OR (t.status = 'pending' AND t.step > 0) "
    "OR (t.status IN ('done', 'failed') AND t.id = (SELECT MAX(u.id) "
    "FROM soul_home_transitions u WHERE u.old_session = t.old_session "
    "AND u.scope_version = 1 AND u.status != 'cancelled' AND u.old_handled_by IS NULL))) LIMIT 1"
)
_ALL_OLD_HOMES = (
    "SELECT DISTINCT t.old_session FROM soul_home_transitions t "
    "JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.scope_version = 1 AND t.status != 'cancelled' AND t.old_handled_by IS NULL "
    "AND s.is_home = 0"
)
# 刪掉一段對話：它的回報位置跟著刪，它當舊家的已完成或取消紀錄失效（同一個編號重用時
# 不是舊家）；它當新家的紀錄不動（前面那段舊家照舊唯讀）
_FORGET_MARK = "DELETE FROM soul_child_report_marks WHERE session_id = ?"
_FORGET_RECORDS = (
    "UPDATE soul_home_transitions SET scope_version = 0 "
    "WHERE scope_version = 1 AND status IN ('done', 'cancelled') AND old_session = ?"
)
# 還沒交接完的舊家（主線要帶它們的原文）：待交接、交接中、失敗，依紀錄順序
_UNFINISHED_OLD_HOMES = (
    "SELECT t.old_session FROM soul_home_transitions t "
    "JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.scope_version = 1 AND t.old_handled_by IS NULL AND s.is_home = 0 "
    "AND t.agent_id = ? AND t.status IN ('pending', 'running', 'failed') ORDER BY t.id"
)


@dataclass(frozen=True)
class _Record:
    id: int
    old_session: str
    new_session: str | None
    old_handled_by: int | None


async def create_tables(db: aiosqlite.Connection) -> bool:
    """建兩張表與索引；回傳回報位置表是不是這次才建（升級時要設初始位置）."""
    cursor = await db.execute(_HAS_MARKS)
    created = await cursor.fetchone() is None
    for statement in (
        _CREATE_TRANSITIONS,
        _CREATE_TRANSITIONS_OLD_INDEX,
        _CREATE_TRANSITIONS_AGENT_INDEX,
        _CREATE_MARKS,
    ):
        await db.execute(statement)
    return created


async def set_initial_marks(
    db: aiosqlite.Connection, marks: Sequence[tuple[str, str, str | None]], now: int
) -> None:
    """升級時既有子對話的回報位置設在當下內容（不補寫歷史）；``marks``：（對話、靈魂、節點）."""
    await db.executemany(_INSERT_MARK, [(sid, agent, node, now) for sid, agent, node in marks])


async def _one(db: aiosqlite.Connection, sql: str, params: tuple[object, ...]) -> _Record | None:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return _Record(row[0], row[1], row[2], row[3]) if row is not None else None


async def _unused_for(db: aiosqlite.Connection, agent: str, new: str) -> list[_Record]:
    cursor = await db.execute(_UNUSED_FOR_NEW, (agent, new))
    return [_Record(row[0], row[1], row[2], row[3]) for row in await cursor.fetchall()]


async def _exists(db: aiosqlite.Connection, sql: str, session_id: str) -> bool:
    cursor = await db.execute(sql, (session_id,))
    return await cursor.fetchone() is not None


async def _insert(
    db: aiosqlite.Connection,
    agent: str,
    old: str,
    new: str | None,
    now: int,
    *,
    handled_by: int | None = None,
) -> None:
    await db.execute(_INSERT, (agent, old, new, handled_by, now, now))


async def _retarget(db: aiosqlite.Connection, record: _Record, new: str | None, now: int) -> None:
    """還沒開始用的那一筆改指新家；改成沒有新家、或改回舊家本身時取消（當沒換過）."""
    if new == record.old_session or (new is None and record.old_handled_by is not None):
        await db.execute(_SET_STATUS, (CANCELLED, now, record.id))
    else:
        await db.execute(_SET_NEW, (new, now, record.id))


async def _attach_to_unhomed(db: aiosqlite.Connection, agent: str, target: str, now: int) -> None:
    """沒有家時設家：取消家之後還沒開始交接的紀錄改指新家；交接中或失敗的另加一筆只記新家."""
    cursor = await db.execute(_UNHOMED_WAITING, (agent,))
    waiting = [_Record(row[0], row[1], row[2], row[3]) for row in await cursor.fetchall()]
    for record in waiting:
        await _retarget(db, record, target, now)
    if waiting:
        return
    started = await _one(db, _LATEST_UNHOMED_STARTED, (agent,))
    if started is not None and started.old_session != target:
        await _insert(db, agent, started.old_session, target, now, handled_by=started.id)


async def apply_home_change(
    db: aiosqlite.Connection,
    agent: str,
    target: str,
    *,
    make_home: bool,
    homes: Sequence[str],
    now: int,
) -> None:
    """照狀態對照表改交接紀錄（設家、取消家、心跳建家共用；ADR-0306）.

    Args:
        db: 呼叫端已開的交易（家標記也在同一個交易裡改）。
        agent: 靈魂的正式 ID。
        target: 要設成家、或要取消家的那段對話。
        make_home: True 設家，False 取消家。
        homes: 交易裡讀到、改動前這個靈魂的家（既有資料可能有兩個）。
        now: epoch 毫秒。

    升級前就當家的內部頻道（現在不能設成家）離開時不建紀錄：頻道不會變成舊家。
    """
    homes = [home for home in homes if can_be_home(home)]
    if not make_home:
        if target not in homes:  # 取消家只對現任家有作用
            return
        unused = await _unused_for(db, agent, target)
        for record in unused:
            await _retarget(db, record, None, now)
        if not unused:
            await _insert(db, agent, target, None, now)
        return
    others = [home for home in homes if home != target]
    if not others and target not in homes:
        await _attach_to_unhomed(db, agent, target, now)
    for home in others:
        unused = await _unused_for(db, agent, home)
        for record in unused:  # 那個家還沒開始用：改寫這些紀錄，它回到普通子對話
            await _retarget(db, record, target, now)
        if not unused:
            await _insert(db, agent, home, target, now)


async def mark_started(db: aiosqlite.Connection, session_id: str, now: int) -> None:
    """新家第一次開始一輪：之後改回去就不能當沒換過."""
    await db.execute(_MARK_STARTED, (now, session_id))


async def is_unused_new_home(db: aiosqlite.Connection, session_id: str) -> bool:
    """這段對話是某筆待交接紀錄還沒開始用的新家."""
    return await _exists(db, _UNUSED_NEW_HOME, session_id)


async def is_old_home(db: aiosqlite.Connection, session_id: str) -> bool:
    """舊家（唯讀）：不能開新的一輪、不能換靈魂."""
    return await _exists(db, _IS_OLD_HOME, session_id)


async def blocks_delete(db: aiosqlite.Connection, session_id: str) -> bool:
    """舊家交接完成前不能刪：待交接、交接中、失敗，或退任摘要還要補寫."""
    return await _exists(db, _BLOCKS_DELETE, session_id)


async def handover_started(db: aiosqlite.Connection, session_id: str) -> bool:
    """交接已經開始（含完成、失敗）：存檔閘擋所有內容寫入（唯讀第二段）."""
    return await _exists(db, _HANDOVER_STARTED, session_id)


async def forget_session(db: aiosqlite.Connection, session_id: str) -> None:
    """刪對話的同一個交易裡：刪回報位置、它當舊家的已完成或取消紀錄失效."""
    await db.execute(_FORGET_MARK, (session_id,))
    await db.execute(_FORGET_RECORDS, (session_id,))


async def all_old_homes(db: aiosqlite.Connection) -> set[str]:
    """全部舊家（對話清單標「舊家」用）."""
    cursor = await db.execute(_ALL_OLD_HOMES)
    return {row[0] for row in await cursor.fetchall()}


async def unfinished_old_homes(db: aiosqlite.Connection, agent: str) -> list[str]:
    """還沒交接完的舊家，依紀錄順序、不重複（主線帶原文用）."""
    cursor = await db.execute(_UNFINISHED_OLD_HOMES, (agent,))
    return list(dict.fromkeys(row[0] for row in await cursor.fetchall()))
