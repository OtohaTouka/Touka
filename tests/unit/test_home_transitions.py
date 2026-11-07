"""換家交接紀錄（3d PR 2a，ADR-0306）：設家、取消家照狀態對照表改紀錄，舊家由紀錄推導.

2a 不做交接本身（2b）；「交接中」「完成」「失敗」由測試直接改狀態模擬。
全部離線：SQLite 只在測試沙盒。
"""

from __future__ import annotations

import pytest
from src.utils import async_db

from tests.support.session_scope import A, B, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

service = async_db.session_service


async def records():
    """（舊家、新家、開始用、狀態、只記新家）依編號."""
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT old_session, new_session, new_started, status, old_handled_by "
            "FROM soul_home_transitions ORDER BY id"
        )
        return [tuple(row) for row in await cursor.fetchall()]


async def set_status(record_id, status, **extra):
    columns = ", ".join(f"{key} = ?" for key in ("status", *extra))
    async with async_db.get_async_db() as db:
        await db.execute(
            f"UPDATE soul_home_transitions SET {columns} WHERE id = ?",  # noqa: S608 - 測試固定欄位
            (status, *extra.values(), record_id),
        )
        await db.commit()


async def homes():
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT id FROM user_sessions WHERE is_home = 1 ORDER BY id")
        return [row[0] for row in await cursor.fetchall()]


async def seed_all(*sids):
    for sid in sids:
        await seed(sid)


@pytest.fixture
async def home_a():
    await seed_all("A", "B", "C", "D")
    assert await service.set_home("A", A, True)
    assert await records() == []


# --- 狀態對照表 ---


async def test_switching_home_records_old_to_new(home_a):
    assert await service.set_home("B", A, True)
    assert await records() == [("A", "B", 0, "pending", None)]
    assert await homes() == ["B"]


async def test_switching_back_before_the_new_home_is_used_is_as_if_nothing_happened(home_a):
    assert await service.set_home("B", A, True)
    assert await service.set_home("A", A, True)
    assert await records() == [("A", "B", 0, "cancelled", None)]
    assert await homes() == ["A"]
    assert not await service.is_old_home("A")


async def test_picking_another_before_the_new_home_is_used_retargets(home_a):
    assert await service.set_home("B", A, True)
    assert await service.set_home("C", A, True)
    assert await records() == [("A", "C", 0, "pending", None)]
    assert await homes() == ["C"]
    assert not await service.is_old_home("B")


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_switching_back_after_the_new_home_was_used_keeps_and_adds(home_a, status):
    assert await service.set_home("B", A, True)
    await service.mark_home_started("B")
    await set_status(1, status)
    assert await service.set_home("A", A, True)
    assert await records() == [("A", "B", 1, status, None), ("B", "A", 0, "pending", None)]
    assert await homes() == ["A"]
    assert not await service.is_old_home("A")
    assert await service.is_old_home("B")


async def test_picking_another_after_the_new_home_was_used_queues_another(home_a):
    assert await service.set_home("B", A, True)
    await service.mark_home_started("B")
    assert await service.set_home("C", A, True)
    assert await records() == [("A", "B", 1, "pending", None), ("B", "C", 0, "pending", None)]


async def test_unsetting_the_home_records_old_to_nothing(home_a):
    assert await service.set_home("A", A, False)
    assert await records() == [("A", None, 0, "pending", None)]
    assert await homes() == []


async def test_unsetting_an_unused_new_home_leaves_old_to_nothing(home_a):
    assert await service.set_home("B", A, True)
    assert await service.set_home("B", A, False)
    assert await records() == [("A", None, 0, "pending", None)]
    assert await homes() == []


async def test_unsetting_a_segment_that_is_not_the_home_changes_nothing(home_a):
    """以前對任何一段按取消家都會清掉家（async_db 舊行為）."""
    assert await service.set_home("B", A, False)
    assert await records() == []
    assert await homes() == ["A"]


async def test_setting_the_same_home_again_changes_nothing(home_a):
    assert await service.set_home("A", A, True)
    assert await records() == []


async def test_after_unsetting_picking_a_home_before_handover_retargets(home_a):
    assert await service.set_home("A", A, False)
    assert await service.set_home("D", A, True)
    assert await records() == [("A", "D", 0, "pending", None)]


async def test_after_unsetting_picking_the_old_home_again_cancels(home_a):
    assert await service.set_home("A", A, False)
    assert await service.set_home("A", A, True)
    assert await records() == [("A", None, 0, "cancelled", None)]
    assert not await service.is_old_home("A")


async def test_picking_a_home_while_the_unset_handover_runs_only_notes_the_new_home(home_a):
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("D", A, True)
    assert await records() == [("A", None, 0, "running", None), ("A", "D", 0, "pending", 1)]
    assert await service.is_old_home("A")
    assert not await service.old_home_blocks_delete("D")


async def test_the_old_home_returning_while_its_unset_handover_runs_adds_nothing(home_a):
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("A", A, True)
    assert await records() == [("A", None, 0, "running", None)]
    assert not await service.is_old_home("A")


async def test_unsetting_an_unused_noted_home_cancels_the_note(home_a):
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("D", A, True)
    assert await service.set_home("D", A, False)
    assert await records() == [("A", None, 0, "running", None), ("A", "D", 0, "cancelled", 1)]


async def test_a_handed_over_home_coming_back_queues_the_current_home(home_a):
    assert await service.set_home("B", A, True)
    await set_status(1, "done")
    assert await service.set_home("A", A, True)
    assert await records() == [("A", "B", 0, "done", None), ("B", "A", 0, "pending", None)]
    assert not await service.is_old_home("A")


async def test_two_existing_homes_each_get_a_record():
    await seed_all("A", "B", "X")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id IN ('A', 'B')",
            (A.agent_id,),
        )
        await db.commit()
    assert await service.set_home("X", A, True)
    assert await records() == [("A", "X", 0, "pending", None), ("B", "X", 0, "pending", None)]
    assert await homes() == ["X"]


async def test_records_are_per_soul():
    await seed_all("A1", "A2")
    await seed("B1", identity=B)
    await seed("B2", identity=B)
    assert await service.set_home("A1", A, True)
    assert await service.set_home("B1", B, True)
    assert await service.set_home("A2", A, True)
    assert await records() == [("A1", "A2", 0, "pending", None)]
    assert await homes() == ["A2", "B1"]


# --- 開始用 ---


async def test_starting_marks_only_the_unused_new_home(home_a):
    assert await service.set_home("B", A, True)
    await service.mark_home_started("A")
    assert await records() == [("A", "B", 0, "pending", None)]
    await service.mark_home_started("B")
    assert await records() == [("A", "B", 1, "pending", None)]


# --- 舊家與刪除由紀錄推導 ---


@pytest.mark.parametrize(
    ("status", "summary_pending", "old", "blocks"),
    [
        ("pending", 0, True, True),
        ("running", 0, True, True),
        ("failed", 0, True, True),
        ("done", 0, True, False),
        ("done", 1, True, True),
        ("cancelled", 0, False, False),
    ],
)
async def test_old_home_and_delete_follow_the_record(home_a, status, summary_pending, old, blocks):
    assert await service.set_home("B", A, True)
    await set_status(1, status, retired_summary_pending=summary_pending)
    assert await service.is_old_home("A") is old
    assert await service.old_home_blocks_delete("A") is blocks
    assert not await service.is_old_home("B")


async def test_invalidated_records_are_not_old_homes(home_a):
    assert await service.set_home("B", A, True)
    await service.invalidate_agent_sessions(A)
    assert not await service.is_old_home("A")
    assert not await service.old_home_blocks_delete("A")


async def test_restore_invalidates_every_record(home_a):
    await seed("B1", identity=B)
    await seed("B2", identity=B)
    assert await service.set_home("B", A, True)
    assert await service.set_home("B1", B, True)
    assert await service.set_home("B2", B, True)
    await service.invalidate_restored_sessions()
    assert not await service.is_old_home("A")
    assert not await service.is_old_home("B1")


async def test_same_name_rebuild_invalidates_only_that_soul(home_a):
    await seed("B1", identity=B)
    await seed("B2", identity=B)
    assert await service.set_home("B", A, True)
    assert await service.set_home("B1", B, True)
    assert await service.set_home("B2", B, True)
    await service.invalidate_agent_sessions(A)
    assert not await service.is_old_home("A")
    assert await service.is_old_home("B1")


async def test_session_list_marks_old_homes(home_a):
    assert await service.set_home("B", A, True)
    listed = {s["id"]: s for s in await service.get_all_sessions()}
    assert listed["A"]["is_old_home"] is True
    assert listed["B"]["is_old_home"] is False
    assert listed["C"]["is_old_home"] is False


async def test_unfinished_old_homes_in_record_order_without_the_current_home(home_a):
    assert await service.set_home("B", A, True)
    await service.mark_home_started("B")
    assert await service.set_home("C", A, True)
    await service.mark_home_started("C")
    assert await service.set_home("D", A, True)
    await set_status(2, "done")  # B→C 做完了：B 不再帶原文
    olds = await service.read_unfinished_old_homes(A)
    assert [s["id"] for s in olds] == ["A", "C"]
    assert olds[0]["messages"]["currentId"] == "a"
    assert await service.read_unfinished_old_homes(B) == []


# --- 生命週期 ---


async def test_purge_and_count_are_per_soul(home_a):
    await seed("B1", identity=B)
    await seed("B2", identity=B)
    assert await service.set_home("B", A, True)
    assert await service.set_home("B1", B, True)
    assert await service.set_home("B2", B, True)
    assert await service.count_agent_home_records(A) == 1
    assert await service.purge_agent_home_records(A) == 1
    assert await service.count_agent_home_records(A) == 0
    assert await service.count_agent_home_records(B) == 1


async def test_upgrade_sets_report_marks_at_current_content_once(scope_db):
    await seed_all("home", "child")
    assert await service.set_home("home", A, True)
    async with async_db.get_async_db() as db:
        await db.execute("DROP TABLE soul_child_report_marks")
        await db.commit()
    await async_db.init_db_async()  # 升級：表這次才建
    await seed("later")
    await async_db.init_db_async()  # 再啟動一次：不重設、不補新對話
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT session_id, agent_id, last_node FROM soul_child_report_marks ORDER BY 1"
        )
        marks = [tuple(row) for row in await cursor.fetchall()]
    assert marks == [("child", A.agent_id, "a")]
    assert await service.count_agent_home_records(A) == 1  # 回報位置也算這個靈魂的紀錄


async def test_records_survive_with_the_raw_row(home_a):
    """設家不改舊家內容：只動家標記與紀錄."""
    before = await raw_row("A")
    assert await service.set_home("B", A, True)
    after = await raw_row("A")
    assert {k: v for k, v in after.items() if k not in {"is_home", "home_for_agent"}} == {
        k: v for k, v in before.items() if k not in {"is_home", "home_for_agent"}
    }
