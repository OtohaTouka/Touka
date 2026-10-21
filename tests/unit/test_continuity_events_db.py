"""可信活動事件表（``ui_sessions.db`` 的 ``continuity_events``）的建表與讀寫。

- 建表與索引在 ``init_db_async``；全域「可信紀錄起點」第一次寫入後不再改。
- 同一個 event_id 只留一筆；任務狀態同一任務最新一筆已經是同一個狀態就不記，
  會重複發生的轉換（FAILED → 重置 → 再 FAILED）每次都留。
- 重置只刪該靈魂的事件，同一個交易寫它自己的起點；查詢與匯出都只看該靈魂。
- 寫入介面沒有 trust 參數，讀回一律是 ``system``；時間是毫秒整數。

全部在 tmp 資料庫（``async_db.get_db_path`` 指到 tmp_path），不碰 ``data/``。
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity
from src.utils import async_db
from src.utils import continuity_events_db as db_mod
from src.utils.async_db import get_async_db, get_system_setting, init_db_async
from src.utils.continuity_events_db import EventRow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SOUL = AgentIdentity.for_role(agent_id="T23DbSoul", role="Owner")
OTHER = AgentIdentity.for_role(agent_id="T23DbOther", role="Owner")
FIRST_EPOCH_MS = 1_760_000_000_000


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path) -> Iterator[None]:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        yield


def _row(
    event_id: str,
    kind: str,
    *,
    at: int,
    subject: str = "m1/T1",
    agent: AgentIdentity = SOUL,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EventRow:
    return EventRow(
        event_id=event_id,
        agent=agent.agent_id,
        kind=kind,
        occurred_at=at,
        subject=subject,
        session_id=session_id,
        metadata=metadata or {},
    )


async def _all() -> list[dict[str, Any]]:
    async with get_async_db() as db, db.execute(
        "SELECT * FROM continuity_events ORDER BY occurred_at, rowid"
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def test_init_creates_table_index_and_epoch_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(async_db, "get_current_epoch_ms", lambda: FIRST_EPOCH_MS)
    await init_db_async()
    monkeypatch.setattr(async_db, "get_current_epoch_ms", lambda: FIRST_EPOCH_MS + 999)
    await init_db_async()

    async with get_async_db() as db, db.execute(
        "SELECT type, name FROM sqlite_master WHERE tbl_name = 'continuity_events'"
    ) as cur:
        objects = {(row["type"], row["name"]) for row in await cur.fetchall()}
    assert ("table", "continuity_events") in objects
    assert ("index", "idx_continuity_events_agent_kind_time") in objects
    stored = await get_system_setting(async_db.TRUSTED_TIMELINE_EPOCH_KEY)
    assert stored == str(FIRST_EPOCH_MS)  # 第二次 init 不改起點


async def test_same_event_id_is_kept_once() -> None:
    await init_db_async()
    row = _row("memory_saved:T23DbSoul:f1", "memory_saved", at=1, subject="f1")
    assert await db_mod.insert_event(row) is True
    assert await db_mod.insert_event(row) is False
    assert len(await _all()) == 1


@pytest.mark.parametrize(
    ("sequence", "kept"),
    [
        (["task_doing", "task_doing"], 1),  # DOING 中補產出：同一狀態不另記
        (["task_doing", "task_blocked", "task_doing"], 3),
        (["task_doing", "task_failed", "task_todo", "task_doing", "task_failed"], 5),
        (["task_doing", "task_done", "task_done"], 2),
    ],
)
async def test_task_status_rewrites_skip_but_repeated_transitions_stay(
    sequence: list[str], kept: int
) -> None:
    await init_db_async()
    for index, kind in enumerate(sequence, start=1):
        await db_mod.insert_event(_row(f"{kind}:T23DbSoul:m1/T1:{index}", kind, at=index))
    assert len(await _all()) == kept


async def test_task_dedup_is_per_task_and_per_agent() -> None:
    await init_db_async()
    await db_mod.insert_event(_row("a", "task_doing", at=1, subject="m1/T1"))
    await db_mod.insert_event(_row("b", "task_doing", at=2, subject="m1/T2"))
    await db_mod.insert_event(_row("c", "task_doing", at=3, subject="m1/T1", agent=OTHER))
    assert len(await _all()) == 3


async def test_purge_only_that_soul_and_writes_its_start(monkeypatch: pytest.MonkeyPatch) -> None:
    await init_db_async()
    for index in range(3):
        await db_mod.insert_event(_row(f"s{index}", "memory_saved", at=index, subject=f"f{index}"))
    await db_mod.insert_event(_row("o1", "memory_saved", at=9, subject="f9", agent=OTHER))

    assert await db_mod.purge_agent_events(SOUL, marker_ms=FIRST_EPOCH_MS) == 3
    assert await db_mod.count_agent_events(SOUL) == 0
    assert await db_mod.count_agent_events(OTHER) == 1
    assert await get_system_setting(db_mod.agent_epoch_key(SOUL)) == str(FIRST_EPOCH_MS)
    assert await get_system_setting(db_mod.agent_epoch_key(OTHER)) is None

    # 再重置一次：沒有事件也不出錯，起點更新
    assert await db_mod.purge_agent_events(SOUL, marker_ms=FIRST_EPOCH_MS + 5) == 0
    assert await get_system_setting(db_mod.agent_epoch_key(SOUL)) == str(FIRST_EPOCH_MS + 5)


async def test_list_is_scoped_and_parses_metadata() -> None:
    await init_db_async()
    await db_mod.insert_event(
        _row("t1", "task_done", at=5, session_id="s1", metadata={"mission_id": "m1", "n": 2})
    )
    await db_mod.insert_event(_row("o1", "task_done", at=6, agent=OTHER))

    events = await db_mod.list_agent_events(SOUL)
    assert events == [
        {
            "event_id": "t1",
            "kind": "task_done",
            "occurred_at": 5,
            "session_id": "s1",
            "subject": "m1/T1",
            "trust": "system",
            "metadata": {"mission_id": "m1", "n": 2},
        }
    ]
    assert await db_mod.count_agent_events(OTHER) == 1


async def test_only_system_trust_and_millisecond_integers() -> None:
    assert list(inspect.signature(db_mod.insert_event).parameters) == ["row"]
    assert "trust" not in {item.name for item in fields(EventRow)}
    await init_db_async()
    await db_mod.insert_event(_row("u1", "user_turn", at=FIRST_EPOCH_MS, subject="msg"))
    (row,) = await _all()
    assert row["trust"] == "system"
    assert isinstance(row["occurred_at"], int) and row["occurred_at"] == FIRST_EPOCH_MS


async def test_snapshot_reads_only_this_soul_and_leaves_out_this_turn() -> None:
    await init_db_async()
    for row in (
        _row("u1", "user_turn", at=10, subject="m-old", session_id="chat-a"),
        _row("u2", "user_turn", at=20, subject="m-other-chat", session_id="chat-b"),
        _row("u3", "user_turn", at=30, subject="m-now", session_id="chat-a"),  # 這一輪
        _row("h1", "heartbeat_started", at=40, subject="run-2"),
        _row("h2", "heartbeat_finished", at=35, subject="run-1", metadata={"end": "completed"}),
        _row("t1", "task_done", at=5, subject="m/T0"),  # 在時間窗之前
        _row("t2", "task_doing", at=50, subject="m/T1"),  # 不是完成
        _row("t3", "task_done", at=60, subject="m/T2"),
        _row("t4", "mission_completed", at=70, subject="m"),
        _row("o1", "user_turn", at=99, subject="m-x", session_id="chat-a", agent=OTHER),
    ):
        await db_mod.insert_event(row)
    await async_db.set_system_setting(async_db.TRUSTED_TIMELINE_EPOCH_KEY, "3")
    await async_db.set_system_setting(db_mod.agent_epoch_key(SOUL), "7")

    snapshot = await db_mod.read_snapshot(
        SOUL, session_id="chat-a", exclude_message_id="m-now", since_ms=6, limit=3
    )

    assert snapshot.last_turn_here_ms == 10
    assert snapshot.last_human_ms == 20
    assert snapshot.last_run == (35, {"end": "completed"})
    assert [(kind, at) for kind, at, _subject, _meta in snapshot.recent] == [
        ("mission_completed", 70),
        ("task_done", 60),
    ]
    assert snapshot.records_start_ms == 7  # 全域與這個靈魂的起點取較晚的
    empty = await db_mod.read_snapshot(
        OTHER, session_id=None, exclude_message_id=None, since_ms=0, limit=3
    )
    assert (empty.last_turn_here_ms, empty.last_human_ms, empty.recent) == (None, 99, ())
