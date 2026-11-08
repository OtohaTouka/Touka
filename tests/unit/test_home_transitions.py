"""換家交接紀錄（3d PR 2a，ADR-0306）：設家、取消家照狀態對照表改紀錄，舊家由紀錄推導.

2a 不做交接本身（2b）；「交接中」「完成」「失敗」由測試直接改狀態模擬。
全部離線：SQLite 只在測試沙盒。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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


async def set_status(record_id, status, *, retired_summary_pending=0, step=0):
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE soul_home_transitions SET status = ?, retired_summary_pending = ?, step = ? "
            "WHERE id = ?",
            (status, retired_summary_pending, step, record_id),
        )
        await db.commit()


async def start(sid):
    """新家開始一輪（內部路徑：沒有請求者身分）."""
    return await service.check_old_home(sid, mark_started=True, requester=None)


async def scope_versions():
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT scope_version FROM soul_home_transitions ORDER BY id")
        return [row[0] for row in await cursor.fetchall()]


async def homes():
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT id FROM user_sessions WHERE is_home = 1 ORDER BY id")
        return [row[0] for row in await cursor.fetchall()]


async def seed_all(*sids):
    for sid in sids:
        await seed(sid)


async def legacy_homes(*sids):
    """升級前就是家（沒有任何紀錄、算用過的家）：直接改資料."""
    async with async_db.get_async_db() as db:
        for sid in sids:
            await db.execute(
                "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = ?",
                (A.agent_id, sid),
            )
        await db.commit()


@pytest.fixture
async def home_a():
    await seed_all("A", "B", "C", "D")
    await legacy_homes("A")
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
    await start("B")
    await set_status(1, status)
    assert await service.set_home("A", A, True)
    assert await records() == [("A", "B", 1, status, None), ("B", "A", 0, "pending", None)]
    assert await homes() == ["A"]
    assert not await service.is_old_home("A")
    assert await service.is_old_home("B")


async def test_picking_another_after_the_new_home_was_used_queues_another(home_a):
    assert await service.set_home("B", A, True)
    await start("B")
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


async def test_the_first_home_ever_is_noted_so_an_unused_one_can_be_undone():
    """原本沒有家時設 X：記一筆只記新家（沒有前任，第 0 筆）；沒用過就取消家當沒換過，
    X 回到普通對話（第 3 輪審查 K6 乙、L2）."""
    await seed_all("X")
    assert await service.set_home("X", A, True)
    assert await records() == [("X", "X", 0, "pending", 0)]
    assert not await service.is_old_home("X")
    assert await service.set_home("X", A, False)
    assert await records() == [("X", "X", 0, "cancelled", 0)]
    assert not await service.is_old_home("X")
    assert await homes() == []


async def test_an_unused_first_home_moved_elsewhere_goes_back_to_an_ordinary_chat():
    await seed_all("X", "Y")
    assert await service.set_home("X", A, True)
    assert await service.set_home("Y", A, True)
    assert await records() == [("X", "Y", 0, "pending", 0)]
    assert not await service.is_old_home("X")
    assert await service.set_home("X", A, True)  # Y 也沒用過：改回 X，X 照樣只記新家
    assert await records() == [("X", "X", 0, "pending", 0)]
    assert await service.set_home("X", A, False)
    assert not await service.is_old_home("X")


async def test_a_used_first_home_becomes_an_old_home():
    await seed_all("X", "Y")
    assert await service.set_home("X", A, True)
    await start("X")
    assert await service.set_home("Y", A, True)
    assert await records() == [("X", "X", 1, "pending", 0), ("X", "Y", 0, "pending", None)]
    assert await service.is_old_home("X")


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


async def test_picking_a_home_after_the_unset_handover_failed_only_notes_the_new_home(home_a):
    """失敗的會再試、仍算舊家：比照交接中，另加一筆只記新家（審查 J15）."""
    assert await service.set_home("A", A, False)
    await set_status(1, "failed")
    assert await service.set_home("D", A, True)
    assert await records() == [("A", None, 0, "failed", None), ("A", "D", 0, "pending", 1)]


async def test_the_old_home_returning_while_its_unset_handover_runs_is_only_noted(home_a):
    """交接中的舊家回任：不加新的交接，只記一筆「只記新家」追蹤有沒有用過（第 3 輪審查 L2）."""
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("A", A, True)
    assert await records() == [("A", None, 0, "running", None), ("A", "A", 0, "pending", 1)]
    assert not await service.is_old_home("A")


@pytest.mark.parametrize("status", ["running", "failed", "done"])
@pytest.mark.parametrize("leave", ["unset", "move"])
async def test_an_old_home_back_from_no_home_and_left_unused_adds_no_handover(
    home_a, status, leave
):
    """取消家後交接已開始或完成，設回舊家但沒用過就離開：不建新的交接，照舊唯讀（L2）."""
    assert await service.set_home("A", A, False)
    await set_status(1, status)
    assert await service.set_home("A", A, True)
    if leave == "unset":
        assert await service.set_home("A", A, False)
    else:
        assert await service.set_home("C", A, True)
    real = [r for r in await records() if r[4] is None]
    assert real == [("A", None, 0, status, None)]
    assert await service.is_old_home("A")
    async with async_db.get_async_db() as db:
        assert await async_db.home_transitions.handover_started(db, "A")
        assert await async_db.home_transitions.unfinished_old_homes(db, A.agent_id) == (
            [] if status == "done" else ["A"]
        )


async def test_an_old_home_back_from_no_home_and_used_starts_a_new_tenure(home_a):
    assert await service.set_home("A", A, False)
    await set_status(1, "done")
    assert await service.set_home("A", A, True)
    await start("A")
    assert await service.set_home("A", A, False)
    assert [r for r in await records() if r[4] is None] == [
        ("A", None, 0, "done", None),
        ("A", None, 0, "pending", None),
    ]
    async with async_db.get_async_db() as db:
        assert not await async_db.home_transitions.handover_started(db, "A")


async def test_a_handover_that_took_a_step_is_not_undone_by_picking_the_old_home(home_a):
    """做到某一步、重試回到待交接的紀錄算已開始：設回舊家不取消它（第 3 輪審查 L3）."""
    assert await service.set_home("A", A, False)
    await set_status(1, "pending", step=2)
    assert await service.set_home("A", A, True)
    assert await records() == [("A", None, 0, "pending", None), ("A", "A", 0, "pending", 1)]
    assert await service.set_home("A", A, False)
    assert await service.set_home("D", A, True)
    assert [r for r in await records() if r[4] is None] == [("A", None, 0, "pending", None)]


async def test_a_waiting_and_a_started_unset_record_both_reach_the_next_home():
    """兩個家：一筆已開始交接、一筆等待中時設家，等待的改指新家，已開始的另記新家（L4）."""
    await seed_all("A", "B", "D")
    await legacy_homes("A", "B")
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("B", A, False)
    assert await service.set_home("D", A, True)
    assert await records() == [
        ("A", None, 0, "running", None),
        ("B", "D", 0, "pending", None),
        ("A", "D", 0, "pending", 1),
    ]


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
    await legacy_homes("A", "B")
    assert await service.set_home("X", A, True)
    assert await records() == [("A", "X", 0, "pending", None), ("B", "X", 0, "pending", None)]
    assert await homes() == ["X"]


async def test_with_two_old_homes_picking_another_retargets_both_records():
    """兩筆都指向還沒開始用的 X：改設 Y 時兩筆一起改，不留指向 X 的紀錄（審查 J13）."""
    await seed_all("A", "B", "X", "Y")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id IN ('A', 'B')",
            (A.agent_id,),
        )
        await db.commit()
    assert await service.set_home("X", A, True)
    assert await service.set_home("Y", A, True)
    assert await records() == [("A", "Y", 0, "pending", None), ("B", "Y", 0, "pending", None)]
    await start("X")  # X 回到普通對話：之後用它不算新家開始用
    assert await records() == [("A", "Y", 0, "pending", None), ("B", "Y", 0, "pending", None)]


async def test_after_unsetting_two_homes_the_next_home_takes_both_records():
    """兩個家 → 設 X → 取消 X（兩筆都 →無）→ 設 D：兩筆都改指 D（K9）."""
    await seed_all("A", "B", "X", "D")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id IN ('A', 'B')",
            (A.agent_id,),
        )
        await db.commit()
    assert await service.set_home("X", A, True)
    assert await service.set_home("X", A, False)
    assert await records() == [("A", None, 0, "pending", None), ("B", None, 0, "pending", None)]
    assert await service.set_home("D", A, True)
    assert await records() == [("A", "D", 0, "pending", None), ("B", "D", 0, "pending", None)]


async def legacy_channel_home(channel):
    """升級前的資料：內部頻道已經是家（現在設家會擋，所以直接改資料）."""
    await seed("legacy-channel")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET id = ?, is_home = 1, home_for_agent = ? "
            "WHERE id = 'legacy-channel'",
            (channel, A.agent_id),
        )
        await db.commit()


@pytest.mark.parametrize("channel", ["touka.hb.v1.deadbeef", "touka.sns.v1.cafe"])
async def test_leaving_a_channel_that_was_made_home_before_leaves_no_record(channel):
    """升級前就當家的內部頻道：換走或取消都不建紀錄，頻道不會變成卡死的舊家（K7）."""
    await seed_all("X")
    await legacy_channel_home(channel)
    assert await service.set_home("X", A, True)
    assert await records() == [("X", "X", 0, "pending", 0)]  # 頻道沒有紀錄；X 當第一個家記
    assert not await service.is_old_home(channel)
    assert await homes() == ["X"]


async def test_unsetting_a_channel_that_was_made_home_before_leaves_no_record():
    await legacy_channel_home("touka.hb.v1.deadbeef")
    assert await service.set_home("touka.hb.v1.deadbeef", A, False)
    assert await records() == []
    assert not await service.is_old_home("touka.hb.v1.deadbeef")


async def test_a_conversation_that_is_no_longer_home_is_never_marked_started(home_a):
    assert await service.set_home("B", A, True)
    async with async_db.get_async_db() as db:  # 舊資料：紀錄還指向 B，但 B 已經不是家
        await db.execute("UPDATE user_sessions SET is_home = 0, home_for_agent = NULL")
        await db.commit()
    await start("B")
    assert await records() == [("A", "B", 0, "pending", None)]


async def test_records_are_per_soul():
    await seed_all("A1", "A2")
    await seed("B1", identity=B)
    await seed("B2", identity=B)
    assert await service.set_home("A1", A, True)
    assert await service.set_home("B1", B, True)
    assert await service.set_home("A2", A, True)  # A1 沒用過：那筆只記新家改指 A2
    assert await records() == [("A1", "A2", 0, "pending", 0), ("B1", "B1", 0, "pending", 0)]
    assert await homes() == ["A2", "B1"]


# --- 開始用 ---


async def test_starting_marks_only_the_unused_new_home(home_a):
    assert await service.set_home("B", A, True)
    await start("A")
    assert await records() == [("A", "B", 0, "pending", None)]
    await start("B")
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


@pytest.mark.parametrize(
    ("status", "step", "started"),
    [
        ("pending", 0, False),
        ("pending", 2, True),
        ("running", 0, True),
        ("done", 0, True),
        ("failed", 0, True),
        ("cancelled", 3, False),
    ],
)
async def test_handover_has_started_once_it_runs_or_takes_a_step(home_a, status, step, started):
    """唯讀第二段從交接開始起算：交接中、完成、失敗，或已經做到某一步（審查 J6）."""
    assert await service.set_home("B", A, True)
    await set_status(1, status, step=step)
    async with async_db.get_async_db() as db:
        assert await async_db.home_transitions.handover_started(db, "A") is started


async def test_only_the_latest_tenure_counts_as_handed_over(home_a):
    """以前那一任已交接完，回任用過再換走：這一任還沒開始交接（第 2 輪審查 K1）."""
    assert await service.set_home("B", A, True)
    await set_status(1, "done")
    assert await service.set_home("A", A, True)  # 回任，加 B→A
    await start("A")
    assert await service.set_home("C", A, True)  # 再換走，加 A→C（待交接、步驟 0）
    async with async_db.get_async_db() as db:
        assert not await async_db.home_transitions.handover_started(db, "A")
    snapshot = await service.read_persistence_snapshot("A", A)
    result = await service.conditional_save_session(
        "A", snapshot.revision, async_db.SessionChanges(title="收尾"), A
    )
    assert result.status == "saved"  # 收尾保存照舊


@pytest.mark.parametrize("earlier", ["failed", "done"])
async def test_coming_back_unused_keeps_the_finished_tenure_read_only(home_a, earlier):
    """回任但沒用過就換走：最新一筆仍是那筆完成或失敗的，照舊是第二段."""
    assert await service.set_home("B", A, True)
    await set_status(1, earlier)
    assert await service.set_home("A", A, True)
    assert await service.set_home("B", A, True)  # B→A 沒用過：取消
    async with async_db.get_async_db() as db:
        assert await async_db.home_transitions.handover_started(db, "A")


@pytest.mark.parametrize("earlier", [("running", 0), ("pending", 2), ("failed", 2)])
async def test_an_earlier_record_still_running_does_not_close_the_new_tenure(home_a, earlier):
    """較早那筆在交接（或做到某一步、失敗）、這段回任用過再換走：只看最新一筆，這一任的
    收尾保存照舊（第 3 輪審查 L1；較早那筆的歸檔一致性交給 2b 開始前等收尾保存）."""
    assert await service.set_home("B", A, True)
    await start("B")
    status, step = earlier
    await set_status(1, status, step=step)
    assert await service.set_home("A", A, True)  # A→B 已開始用：保留，加 B→A
    await start("A")
    assert await service.set_home("C", A, True)  # 加 A→C
    async with async_db.get_async_db() as db:
        assert not await async_db.home_transitions.handover_started(db, "A")
    snapshot = await service.read_persistence_snapshot("A", A)
    result = await service.conditional_save_session(
        "A", snapshot.revision, async_db.SessionChanges(title="收尾"), A
    )
    assert result.status == "saved"


async def test_an_unset_handover_running_does_not_close_the_tenure_after_coming_back(home_a):
    assert await service.set_home("A", A, False)
    await set_status(1, "running")
    assert await service.set_home("A", A, True)
    await start("A")
    assert await service.set_home("A", A, False)
    async with async_db.get_async_db() as db:
        assert not await async_db.home_transitions.handover_started(db, "A")


async def test_deleting_a_later_home_keeps_the_earlier_old_home(home_a):
    """A→B、B→C 都完成後刪 B：只有 B 當舊家的紀錄失效，A 照舊是舊家（K2）."""
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("C", A, True)
    await set_status(1, "done")
    await set_status(2, "done")
    snapshot = await service.read_persistence_snapshot("B", A)
    assert (await service.conditional_delete_session("B", snapshot.revision, A)).status == "saved"
    assert await scope_versions() == [1, 0]
    assert await service.is_old_home("A")


async def test_deleting_a_handed_over_old_home_forgets_it(home_a):
    """刪掉已完成交接的舊家：回報位置跟著刪、紀錄失效；同一個編號重用時不是舊家（審查 J19）."""
    assert await service.set_home("B", A, True)
    await set_status(1, "done")
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO soul_child_report_marks (session_id, agent_id, last_node, updated_at) "
            "VALUES ('A', ?, 'a', 1)",
            (A.agent_id,),
        )
        await db.commit()
    snapshot = await service.read_persistence_snapshot("A", A)
    assert (await service.conditional_delete_session("A", snapshot.revision, A)).status == "saved"
    assert await scope_versions() == [0]
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT 1 FROM soul_child_report_marks WHERE session_id = 'A'")
        assert await cursor.fetchone() is None
    await seed("A")
    assert not await service.is_old_home("A")


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
    await start("B1")
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


async def test_unreadable_records_only_drop_the_old_home_mark_from_the_list(
    home_a, monkeypatch, caplog
):
    """紀錄表讀不到：清單照常（只有舊家標記當成否）；擋寫入的各閘照舊失敗（第 2 輪審查 K15）."""
    assert await service.set_home("B", A, True)
    monkeypatch.setattr(
        async_db.home_transitions, "all_old_homes", AsyncMock(side_effect=RuntimeError("disk"))
    )
    listed = {s["id"]: s for s in await service.get_all_sessions()}
    assert set(listed) == {"A", "B", "C", "D"}
    assert listed["A"]["is_old_home"] is False
    assert "Old homes are unreadable" in caplog.text


async def test_unfinished_old_homes_in_record_order_without_the_current_home(home_a):
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("C", A, True)
    await start("C")
    assert await service.set_home("D", A, True)
    await set_status(2, "done")  # B→C 做完了：B 不再帶原文
    olds = await service.read_unfinished_old_homes(A)
    assert [s["id"] for s in olds] == ["A", "C"]
    assert olds[0]["messages"]["currentId"] == "a"
    assert await service.read_unfinished_old_homes(B) == []


async def test_an_unreadable_old_home_is_skipped_not_fatal(home_a, caplog):
    """某段舊家那一列壞了：記 warning、跳過，靈魂照常能聊（審查 J14）."""
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("C", A, True)
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET messages = 'not json' WHERE id = 'A'")
        await db.commit()
    olds = await service.read_unfinished_old_homes(A)
    assert [s["id"] for s in olds] == ["B"]
    assert "unreadable" in caplog.text


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
    await legacy_homes("home")
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


async def test_an_upgrade_that_fails_halfway_is_redone_on_the_next_start(scope_db, monkeypatch):
    """建表與寫初始位置在同一個交易：中途失敗就整個重來，不會留下空的位置表（審查 J8）."""
    await seed_all("home", "child")
    async with async_db.get_async_db() as db:
        await db.execute("DROP TABLE soul_child_report_marks")
        await db.commit()
    real = async_db.home_transitions.set_initial_marks
    monkeypatch.setattr(
        async_db.home_transitions, "set_initial_marks", AsyncMock(side_effect=RuntimeError)
    )
    with pytest.raises(RuntimeError):
        await async_db.init_db_async()
    monkeypatch.setattr(async_db.home_transitions, "set_initial_marks", real)
    await async_db.init_db_async()
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT session_id FROM soul_child_report_marks ORDER BY 1")
        assert [row[0] for row in await cursor.fetchall()] == ["child", "home"]


async def test_the_upgrade_only_takes_a_text_current_node(scope_db):
    await seed_all("text", "number", "nested")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET messages = json_set(messages, '$.currentId', 5) "
            "WHERE id = 'number'"
        )
        await db.execute(
            "UPDATE user_sessions SET messages = json_set(messages, '$.currentId', json('[1]')) "
            "WHERE id = 'nested'"
        )
        await db.execute("DROP TABLE soul_child_report_marks")
        await db.commit()
    await async_db.init_db_async()
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT session_id, last_node FROM soul_child_report_marks ORDER BY 1"
        )
        marks = [tuple(row) for row in await cursor.fetchall()]
    assert marks == [("nested", None), ("number", None), ("text", "a")]


async def test_records_survive_with_the_raw_row(home_a):
    """設家不改舊家內容：只動家標記與紀錄."""
    before = await raw_row("A")
    assert await service.set_home("B", A, True)
    after = await raw_row("A")
    assert {k: v for k, v in after.items() if k not in {"is_home", "home_for_agent"}} == {
        k: v for k, v in before.items() if k not in {"is_home", "home_for_agent"}
    }
