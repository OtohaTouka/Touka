"""交接工作與子對話回報的資料庫操作（3d PR 2b，ADR-0307）.

狀態機（設家、取消家怎麼改紀錄）在 ``home_transitions``；這裡是背景交接工作怎麼一筆筆做完。
所有 SQL 是固定字面量（禁令 24）。

- 每一步的寫入都核對「這一筆還是交接中、做到第 n 步、新家與開始用沒變」：設家不拿靈魂鎖，會改寫
  還沒開始的紀錄。核對不過整筆不寫，回 False。
- 段落和步驟同一個交易：中途停下時，下一次從第一個沒做完的步驟接著做；段落有唯一索引，重做不重複。
- ``skip_if``：拿到寫鎖之後再問一次要不要放棄（靈魂開始重置或刪除）。
- 時間一律 epoch 毫秒，由呼叫端給。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.utils.async_db import get_async_db
from src.utils.home_transitions import FAILED, PENDING, RUNNING, set_report_mark
from src.utils.soul_prelude_db import insert_segment_tx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import aiosqlite

    from src.utils.soul_prelude_db import PreludeSegment

# 同一筆累計失敗幾次標成失敗（不讓一筆卡住整條隊）
HANDOVER_MAX_FAILURES = 3
# 做到哪一步：歸檔、退任摘要、子對話回報、換家標記、完成
STEP_ARCHIVED = 1
STEP_RETIRED = 2
STEP_REPORTED = 3
STEP_MARKED = 4
STEP_DONE = 5

_QUEUE = (
    "SELECT id, old_session, new_session, old_handled_by, new_started, status, step, failures, "
    "created_at, updated_at FROM soul_home_transitions "
    "WHERE agent_id = ? AND scope_version = 1 AND status IN ('pending', 'running', 'failed') "
    "ORDER BY id"
)
_CLAIM = (
    "UPDATE soul_home_transitions SET status = 'running', updated_at = ? "
    "WHERE id = ? AND scope_version = 1 AND status = ? AND step = ? "
    "AND new_session IS ? AND new_started = ?"
)
_ADVANCE = (
    "UPDATE soul_home_transitions SET step = ?, updated_at = ? "
    "WHERE id = ? AND scope_version = 1 AND status = 'running' AND step = ? "
    "AND new_session IS ? AND new_started = ?"
)
_FINISH = (
    "UPDATE soul_home_transitions SET status = 'done', step = ?, updated_at = ? "
    "WHERE id = ? AND scope_version = 1 AND status = 'running' AND step = ? "
    "AND new_session IS ? AND new_started = ?"
)
_FAIL = (
    "UPDATE soul_home_transitions SET failures = failures + 1, updated_at = ?, status = "
    "CASE WHEN failures + 1 >= ? THEN 'failed' ELSE 'pending' END "
    "WHERE id = ? AND scope_version = 1 AND status = 'running'"
)
_FAILURES = "SELECT failures FROM soul_home_transitions WHERE id = ?"
_SET_RETIRED_PENDING = (
    "UPDATE soul_home_transitions SET retired_summary_pending = 1, updated_at = ? WHERE id = ?"
)
_CLEAR_RETIRED_PENDING = (
    "UPDATE soul_home_transitions SET retired_summary_pending = 0, updated_at = ? "
    "WHERE agent_id = ? AND old_session = ? AND scope_version = 1 "
    "AND retired_summary_pending = 1"
)
# 待補的退任摘要：那段已不是現任家（對話已刪的也算，要清掉待補），也沒有任何以它為舊家、
# 還沒完成的一般紀錄（有的話交給那一筆寫）
_PENDING_RETIRED = (
    "SELECT t.old_session, MAX(t.id) FROM soul_home_transitions t "
    "LEFT JOIN user_sessions s ON s.id = t.old_session "
    "WHERE t.agent_id = ? AND t.scope_version = 1 AND t.retired_summary_pending = 1 "
    "AND COALESCE(s.is_home, 0) = 0 "
    "AND NOT EXISTS (SELECT 1 FROM soul_home_transitions u "
    "WHERE u.old_session = t.old_session AND u.scope_version = 1 AND u.old_handled_by IS NULL "
    "AND u.status IN ('pending', 'running', 'failed')) "
    "GROUP BY t.old_session ORDER BY MAX(t.id)"
)
_LIST_FAILED = (
    "SELECT agent_id AS agent, old_session, new_session, failures, updated_at "
    "FROM soul_home_transitions WHERE scope_version = 1 AND status = 'failed' ORDER BY id"
)
# 子對話回報的候選：綁這個靈魂、不是家、不是舊家；頻道由呼叫端用 can_be_home 排除
_CHILD_CANDIDATES = (
    "SELECT s.id, s.updatedAt, s.session_scope_version, "
    "CASE WHEN json_valid(s.messages) AND json_type(s.messages, '$.currentId') = 'text' "
    "THEN json_extract(s.messages, '$.currentId') END AS node, "
    "m.last_node, m.agent_id AS mark_agent, m.scope_version AS mark_scope, "
    "m.updated_at AS mark_at "
    "FROM user_sessions s LEFT JOIN soul_child_report_marks m ON m.session_id = s.id "
    "WHERE s.is_home = 0 AND s.agent_id IN (?, ?) "
    "AND NOT EXISTS (SELECT 1 FROM soul_home_transitions t WHERE t.old_session = s.id "
    "AND t.scope_version = 1 AND t.status != 'cancelled' AND t.old_handled_by IS NULL) "
    "ORDER BY COALESCE(s.updatedAt, 0) DESC, s.id"
)
_SESSION_AGENT = "SELECT agent_id FROM user_sessions WHERE id = ?"
_REPORT_EXISTS = (
    "SELECT 1 FROM soul_prelude_segments WHERE agent_id = ? AND kind = 'child_report' "
    "AND source_session = ? AND dedupe_key = ? AND scope_version = 1 LIMIT 1"
)
# 交接後來成功：先前寫的失敗註記失效（還沒併進合併段的）
_DROP_FAILURE_NOTE = (
    "UPDATE soul_prelude_segments SET scope_version = 0 "
    "WHERE agent_id = ? AND kind = 'handover' AND dedupe_key = ? AND scope_version = 1"
)


@dataclass(frozen=True)
class Work:
    """交接佇列裡的一筆."""

    id: int
    old_session: str
    new_session: str | None
    old_handled_by: int | None
    new_started: int
    status: str
    step: int
    failures: int
    created_at: int
    updated_at: int

    @property
    def noted(self) -> bool:
        """只記新家：舊家由別筆交接（或沒有前任），只寫換家標記."""
        return self.old_handled_by is not None


@dataclass(frozen=True)
class Child:
    """子對話回報的一個候選."""

    id: str
    updated_at: int | None
    scope_version: int
    node: str | None
    mark_node: str | None
    mark_agent: str | None
    mark_scope: int | None
    mark_at: int | None = None


async def list_queue(agent: str) -> list[Work]:
    """這個靈魂還沒做完的紀錄，依編號."""
    async with get_async_db() as db, db.execute(_QUEUE, (agent,)) as cursor:
        return [Work(*row) for row in await cursor.fetchall()]


async def claim(work: Work, now: int, *, skip_if: Callable[[], bool]) -> Work | None:
    """開始做這一筆（待交接或失敗 → 交接中）；狀態或新家已變就回 None."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if skip_if():
            await db.rollback()
            return None
        cursor = await db.execute(
            _CLAIM, (now, work.id, work.status, work.step, work.new_session, work.new_started)
        )
        if not cursor.rowcount:
            await db.rollback()
            return None
        await db.commit()
    return Work(**{**work.__dict__, "status": RUNNING})


async def _apply(
    db: aiosqlite.Connection, work: Work, sql: str, step: int, now: int, *, from_step: int
) -> bool:
    cursor = await db.execute(
        sql, (step, now, work.id, from_step, work.new_session, work.new_started)
    )
    return bool(cursor.rowcount)


async def advance(
    work: Work,
    from_step: int,
    to_step: int,
    now: int,
    *,
    segment: PreludeSegment | None = None,
    retired_pending: bool = False,
    clear_retired_for: str | None = None,
    agent: str = "",
    skip_if: Callable[[], bool],
) -> bool:
    """做完一步：同一個交易寫段落、記步驟；核對不過或該放棄就整筆不寫，回 False."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if skip_if() or not await _apply(db, work, _ADVANCE, to_step, now, from_step=from_step):
            await db.rollback()
            return False
        if segment is not None:
            await insert_segment_tx(db, segment)
        if retired_pending:
            await db.execute(_SET_RETIRED_PENDING, (now, work.id))
        if clear_retired_for is not None:
            await db.execute(_CLEAR_RETIRED_PENDING, (now, agent, clear_retired_for))
        await db.commit()
        return True


async def finish(
    work: Work,
    from_step: int,
    now: int,
    *,
    segment: PreludeSegment | None = None,
    stale_note: str | None = None,
    agent: str = "",
    skip_if: Callable[[], bool],
) -> bool:
    """這一筆完成（可同時寫一段，例如換家標記）；核對不過或該放棄回 False.

    ``stale_note`` 是這一筆先前寫的失敗註記：既然成功了就失效。
    """
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if skip_if() or not await _apply(db, work, _FINISH, STEP_DONE, now, from_step=from_step):
            await db.rollback()
            return False
        if segment is not None:
            await insert_segment_tx(db, segment)
        if stale_note is not None:
            await db.execute(_DROP_FAILURE_NOTE, (agent, stale_note))
        await db.commit()
        return True


async def record_failure(work: Work, now: int, *, max_failures: int) -> tuple[int, str]:
    """這一筆這次沒做完：累計失敗次數；回傳（失敗次數、新狀態）."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(_FAIL, (now, max_failures, work.id))
        async with db.execute(_FAILURES, (work.id,)) as cursor:
            row = await cursor.fetchone()
        await db.commit()
    failures = int(row[0]) if row else 0
    return failures, FAILED if failures >= max_failures else PENDING


async def write_note(segment: PreludeSegment, *, skip_if: Callable[[], bool]) -> bool:
    """只寫一段（失敗註記）；該放棄就不寫."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if skip_if():
            await db.rollback()
            return False
        written = await insert_segment_tx(db, segment)
        await db.commit()
        return written


async def list_pending_retired(agent: str) -> list[tuple[str, int]]:
    """現在可以補寫退任摘要的舊家（舊家、最新一筆紀錄的編號）."""
    async with get_async_db() as db, db.execute(_PENDING_RETIRED, (agent,)) as cursor:
        return [(row[0], int(row[1])) for row in await cursor.fetchall()]


async def write_deferred_retired(
    agent: str,
    old_session: str,
    segment: PreludeSegment | None,
    now: int,
    *,
    skip_if: Callable[[], bool],
) -> bool:
    """補寫退任摘要並清掉這段舊家所有待補（舊家已不存在時 ``segment`` 是 None）.

    該放棄或紀錄已失效時整筆不寫，回 False。
    """
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if skip_if():
            await db.rollback()
            return False
        cursor = await db.execute(_CLEAR_RETIRED_PENDING, (now, agent, old_session))
        if not cursor.rowcount:
            await db.rollback()
            return False
        if segment is not None:
            await insert_segment_tx(db, segment)
        await db.commit()
        return True


async def list_failed_handovers() -> list[dict[str, object]]:
    """全部失敗的紀錄（``/v1/stats`` 顯示用，ADR-0078）."""
    async with get_async_db() as db, db.execute(_LIST_FAILED) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def child_candidates(agents: Sequence[str]) -> list[Child]:
    """綁這個靈魂、不是家也不是舊家的對話，最近動過的在前；``agents`` 是（正式 ID、舊前綴 ID）."""
    async with get_async_db() as db, db.execute(_CHILD_CANDIDATES, tuple(agents)) as cursor:
        return [
            Child(
                id=row["id"],
                updated_at=row["updatedAt"],
                scope_version=row["session_scope_version"],
                node=row["node"],
                mark_node=row["last_node"],
                mark_agent=row["mark_agent"],
                mark_scope=row["mark_scope"],
                mark_at=row["mark_at"],
            )
            for row in await cursor.fetchall()
        ]


async def report_exists(agent: str, session_id: str, node: str) -> bool:
    """這段子對話的這個節點已經寫過回報（切分支又切回來）."""
    async with get_async_db() as db, db.execute(
        _REPORT_EXISTS, (agent, session_id, node)
    ) as cursor:
        return await cursor.fetchone() is not None


async def _still_bound(
    db: aiosqlite.Connection,
    session_id: str,
    agents: Sequence[str],
    skip_if: Callable[[], bool],
) -> bool:
    """寫回報或位置之前（寫鎖裡）：對話還在、還綁這個靈魂、沒有開始重置."""
    async with db.execute(_SESSION_AGENT, (session_id,)) as cursor:
        row = await cursor.fetchone()
    return not skip_if() and row is not None and row[0] in agents


async def move_mark(
    session_id: str,
    agents: Sequence[str],
    agent: str,
    node: str | None,
    now: int,
    *,
    skip_if: Callable[[], bool],
) -> None:
    """回報位置移到這個節點（不寫回報）；對話已不在、換了靈魂或該放棄就不動."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if not await _still_bound(db, session_id, agents, skip_if):
            await db.rollback()
            return
        await set_report_mark(db, session_id, agent, node, now)
        await db.commit()


async def write_report(
    segment: PreludeSegment,
    agents: Sequence[str],
    node: str,
    now: int,
    *,
    skip_if: Callable[[], bool],
) -> bool:
    """回報段落和回報位置同一個交易；對話已不在、換了靈魂、位置已失效或該放棄就不寫，回 False."""
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if not await _still_bound(
            db, segment.source_session, agents, skip_if
        ) or not await set_report_mark(db, segment.source_session, segment.agent, node, now):
            await db.rollback()
            return False
        await insert_segment_tx(db, segment)
        await db.commit()
        return True
