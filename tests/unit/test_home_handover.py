"""換家交接工作與子對話回報（3d PR 2b，ADR-0307）.

全部離線：SQLite 只在測試沙盒，摘要員與舊對話庫都是假的。
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from memory_server.tools import get_manager
from src.core.exceptions import ToukaException
from src.models.session_persistence import SessionChanges
from src.models.sliding_window import SlidingWindowConfig
from src.services import destructive_ops, home_handover
from src.services.chat_trim_wait import closing_saves
from src.services.stream_buffer_service import stream_buffer_service
from src.utils import async_db, home_handover_db
from src.utils.soul_prelude_db import list_live_segments

from tests.support.session_scope import HUMAN, A, B, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db", "real_home_maintenance")]

service = async_db.session_service
REAL_ARCHIVE = home_handover._archive
HOUR_MS = 60 * 60 * 1000
BRANCHED = {
    "messages": {
        "u": {"id": "u", "role": "user", "content": "first", "parentId": None, "timestamp": 1000},
        "a": {
            "id": "a",
            "role": "assistant",
            "content": "reply",
            "parentId": "u",
            "timestamp": 2000,
            "tool_steps": [{"name": "search"}],
            "interjections": [{"text": "wait", "status": "delivered", "step": 1}],
            "childrenIds": [],
        },
        "b": {"id": "b", "role": "assistant", "content": "other", "parentId": "u"},
    },
    "currentId": "a",
}


class Summaries:
    """假的摘要員：記下每次請求，回固定文字."""

    def __init__(self):
        self.calls = []
        self.reply = "摘要內容"
        self.before = None

    async def __call__(self, identity, messages, budget, model, *, previous=None):
        self.calls.append((identity.agent_id, list(messages), budget))
        if self.before is not None:
            await self.before()
        return self.reply


class Archive:
    """假的舊對話庫：記下每次歸檔，回訊息數."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.fail_ids = set()

    def __call__(self, identity, payload, label):
        if self.error is not None:
            raise self.error
        if payload[0]["id"] in self.fail_ids:
            raise OSError("disk")
        self.calls.append(payload)
        return sum(len(item["messages"]) for item in payload)


@pytest.fixture
def config():
    return SlidingWindowConfig()


@pytest.fixture(autouse=True)
def fakes(monkeypatch, config):
    summaries = Summaries()
    archive = Archive()
    monkeypatch.setattr(home_handover, "summarize_dialogue", summaries)
    monkeypatch.setattr(home_handover, "_archive", archive)
    monkeypatch.setattr(home_handover, "load_sliding_window_config", lambda: config)
    monkeypatch.setattr(home_handover, "_REPORT_RETRY_AFTER", {})
    monkeypatch.setattr(home_handover, "_retried_failed", set())
    monkeypatch.setattr(home_handover, "_idle_checked", {})
    return summaries, archive


@pytest.fixture
def summaries(fakes):
    return fakes[0]


@pytest.fixture
def archive(fakes):
    return fakes[1]


async def records():
    """（舊家、新家、狀態、步驟、失敗次數、退任摘要待補、只記新家）依編號."""
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT old_session, new_session, status, step, failures, retired_summary_pending, "
            "old_handled_by FROM soul_home_transitions ORDER BY id"
        )
        return [tuple(row) for row in await cursor.fetchall()]


async def segments(identity=A):
    return [
        (seg.kind, seg.source_session, seg.content)
        for seg in await list_live_segments(identity.agent_id)
    ]


async def set_record(record_id, **values):
    async with async_db.get_async_db() as db:
        for column, value in values.items():
            await db.execute(
                f"UPDATE soul_home_transitions SET {column} = ? WHERE id = ?",  # noqa: S608
                (value, record_id),
            )
        await db.commit()


async def set_scope_one(sid):
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET session_scope_version = 1 WHERE id = ?", (sid,))
        await db.commit()


async def legacy_home(sid, identity=A):
    """升級前就是家（沒有任何紀錄）：直接改資料."""
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = ?",
            (identity.agent_id, sid),
        )
        await db.commit()


async def start(sid):
    await service.check_old_home(sid, mark_started=True, requester=None)


async def mark_of(sid):
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT agent_id, scope_version, last_node FROM soul_child_report_marks "
            "WHERE session_id = ?",
            (sid,),
        )
        row = await cursor.fetchone()
        return tuple(row) if row is not None else None


async def set_mark(sid, agent, node, scope=1):
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO soul_child_report_marks "
            "(session_id, agent_id, scope_version, last_node, updated_at) VALUES (?, ?, ?, ?, 1)",
            (sid, agent, scope, node),
        )
        await db.commit()


@pytest.fixture
async def unset_home(scope_db):
    """A 是用過的家，已取消：紀錄 A→（無）."""
    await seed("A", tree=BRANCHED)
    await legacy_home("A")
    assert await service.set_home("A", A, False)


@pytest.fixture
async def moved(scope_db):
    """A 是用過的家，換到 B，B 已開始用."""
    await seed("A", tree=BRANCHED)
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")


# --- 交接佇列 ---


@pytest.mark.usefixtures("unset_home")
async def test_a_handover_archives_summarizes_marks_and_finishes(summaries, archive):
    await home_handover.maintain(A)
    assert await records() == [("A", None, "done", 5, 0, 0, None)]
    assert len(archive.calls) == 1
    item = archive.calls[0][0]
    assert item["id"] == "A"
    nodes = {node["id"]: node for node in item["messages"]}
    assert set(nodes) == {"u", "a", "b"}  # 整棵樹，含分支
    assert nodes["a"]["parentId"] == "u"
    assert nodes["a"]["tool_steps"] == [{"name": "search"}]  # 工具步驟留在附註
    assert "wait" in nodes["a"]["content"]  # 插話併進內容
    assert "childrenIds" not in nodes["a"]
    assert [m["id"] for m in summaries.calls[0][1]] == ["u", "a"]  # 摘要只看目前這條路徑
    assert summaries.calls[0][2] == home_handover.RETIRED_MAX_TOKENS
    kinds = await segments()
    assert ("home_retired", "A", "摘要內容") in kinds
    assert [k for k in kinds if k[0] == "handover" and "unset" in k[2]]
    assert await service.is_old_home("A")  # 交接完照舊唯讀
    assert not await service.old_home_blocks_delete("A")  # 但可以刪了


@pytest.mark.usefixtures("moved")
async def test_a_used_new_home_lets_the_handover_run(summaries, archive):
    await home_handover.maintain(A)
    assert await records() == [("A", "B", "done", 5, 0, 0, None)]
    assert [k for k in await segments() if k[0] == "handover" and "from A to B" in k[2]]


async def test_a_new_home_not_used_yet_is_left_alone(summaries, archive):
    await seed("A")
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await home_handover.maintain(A)
    assert await records() == [("A", "B", "pending", 0, 0, 0, None)]
    assert archive.calls == []
    assert summaries.calls == []
    assert await segments() == []


async def test_the_handover_closes_the_old_home_while_it_runs(summaries, unset_home):
    seen = []

    async def during():
        current = await service.read_persistence_snapshot("A", HUMAN)
        seen.append(
            await service.conditional_save_session(
                "A", current.revision, SessionChanges(title="late"), HUMAN
            )
        )

    summaries.before = during
    await home_handover.maintain(A)
    assert seen[0].status == "failed"
    code = async_db.ErrorCode.SESSION_OLD_HOME_READ_ONLY
    assert seen[0].error_code == ToukaException(code).error_id


@pytest.mark.usefixtures("unset_home")
async def test_a_handover_stopped_halfway_goes_on_from_the_next_step(summaries, archive):
    await set_record(1, status="running", step=2)
    await home_handover.maintain(A)
    assert archive.calls == []  # 第 1 步做過了
    assert summaries.calls == []  # 第 2 步做過了
    assert await records() == [("A", None, "done", 5, 0, 0, None)]


@pytest.mark.usefixtures("unset_home")
async def test_redoing_a_step_writes_no_duplicate(summaries, archive):
    await home_handover.maintain(A)
    before = await segments()
    await set_record(1, status="running", step=1)
    await home_handover.maintain(A)
    assert await segments() == before


async def test_a_noted_record_only_writes_the_home_change(summaries, archive):
    await seed("X")
    assert await service.set_home("X", A, True)  # 第一個家：只記新家（第 0 筆）
    await start("X")
    assert (await records())[0][6] == 0
    async with async_db.get_async_db() as db:  # 它記的舊家就算不在了也不讀
        await db.execute("UPDATE soul_home_transitions SET old_session = 'gone' WHERE id = 1")
        await db.commit()
    await home_handover.maintain(A)
    assert archive.calls == []
    assert summaries.calls == []
    assert (await records())[0][2] == "done"
    marks = [k for k in await segments() if k[0] == "handover"]
    assert len(marks) == 1
    assert "X" in marks[0][2]
    assert "gone" not in marks[0][2]


@pytest.mark.usefixtures("unset_home")
async def test_an_old_home_that_no_longer_exists_leaves_a_note(summaries, archive):
    async with async_db.get_async_db() as db:
        await db.execute("DELETE FROM user_sessions WHERE id = 'A'")
        await db.commit()
    await home_handover.maintain(A)
    assert await records() == [("A", None, "done", 5, 0, 0, None)]
    assert archive.calls == []
    assert [k for k in await segments() if k[0] == "handover" and "no longer exists" in k[2]]


async def test_an_old_home_already_back_as_home_defers_its_summary(summaries, archive):
    await seed("A", tree=BRANCHED)
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("A", A, True)
    await home_handover.maintain(A)
    done = (await records())[0]
    assert done == ("A", "B", "done", 5, 0, 1, None)
    assert len(archive.calls) == 1
    assert summaries.calls == []
    assert not [k for k in await segments() if k[0] == "home_retired"]


async def test_a_deferred_summary_is_written_once_the_old_home_is_old_again(summaries, archive):
    await seed("A", tree=BRANCHED)
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("A", A, True)  # 紀錄 B→A（A 還沒開始用）
    await home_handover.maintain(A)
    assert await service.set_home("B", A, True)  # A 沒用過就換回 B：A 又是舊家
    await home_handover.maintain(A)
    assert [k for k in await segments() if k[0] == "home_retired" and k[1] == "A"]
    assert (await records())[0][5] == 0
    assert len(archive.calls) == 2  # 補寫前再歸檔一次
    assert not await service.old_home_blocks_delete("A")


async def test_a_new_record_for_the_same_old_home_writes_the_deferred_summary(summaries, archive):
    await seed("A", tree=BRANCHED)
    await seed("B")
    await seed("C")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("A", A, True)
    await home_handover.maintain(A)
    await start("A")  # A 開始用：B→A 也會交接；A 再換走時由新的一筆寫退任摘要
    assert await service.set_home("C", A, True)
    await start("C")
    await home_handover.maintain(A)
    retired = [k for k in await segments() if k[0] == "home_retired" and k[1] == "A"]
    assert len(retired) == 1
    assert all(r[5] == 0 for r in await records())


@pytest.mark.usefixtures("unset_home")
async def test_with_summaries_off_the_retired_entry_is_a_note(summaries, config):
    config.prelude_summary_enabled = False
    await home_handover.maintain(A)
    assert summaries.calls == []
    assert (await records())[0][2] == "done"
    retired = [k for k in await segments() if k[0] == "home_retired"]
    assert len(retired) == 1
    assert "legacy conversation archive" in retired[0][2]


@pytest.mark.usefixtures("unset_home")
async def test_a_failed_summary_also_leaves_a_note(summaries):
    summaries.reply = None
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"
    assert "legacy conversation archive" in [
        k for k in await segments() if k[0] == "home_retired"
    ][0][2]


@pytest.mark.usefixtures("unset_home")
async def test_a_failure_counts_and_keeps_the_step(archive):
    archive.error = OSError("disk")
    await home_handover.maintain(A)
    assert await records() == [("A", None, "pending", 0, 1, 0, None)]
    assert not await async_handover_started("A")  # 還沒開始：舊家照舊可以收尾保存
    archive.error = None
    await home_handover.maintain(A)
    assert (await records())[0][2:5] == ("done", 5, 1)


async def async_handover_started(sid):
    async with async_db.get_async_db() as db:
        return await async_db.home_transitions.handover_started(db, sid)


@pytest.mark.usefixtures("unset_home")
async def test_a_failure_after_a_step_keeps_the_old_home_closed(summaries, archive, monkeypatch):
    calls = []
    real = home_handover._write_reports

    async def broken(*args, **kwargs):
        calls.append(1)
        raise OSError("disk")

    monkeypatch.setattr(home_handover, "_write_reports", broken)
    await home_handover.maintain(A)
    assert calls
    assert await records() == [("A", None, "pending", 2, 1, 0, None)]
    assert await async_handover_started("A")
    monkeypatch.setattr(home_handover, "_write_reports", real)
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"


async def test_three_failures_mark_it_failed_and_the_queue_goes_on(archive, summaries):
    await seed("A", tree=BRANCHED)
    await seed("B", tree=BRANCHED)
    await legacy_home("A")
    assert await service.set_home("A", A, False)
    await legacy_home("B")
    assert await service.set_home("B", A, False)

    archive.fail_ids.add("A")
    for _ in range(3):
        await home_handover.maintain(A)
    rows = await records()
    assert rows[0][2:5] == ("failed", 0, 3)
    assert rows[1][2] == "done"
    notes = [k for k in await segments() if k[0] == "handover" and "failed" in k[2]]
    assert len(notes) == 1
    assert await service.is_old_home("A")  # 失敗的仍算舊家、仍不能刪
    assert await service.old_home_blocks_delete("A")


@pytest.mark.usefixtures("unset_home")
async def test_a_failed_handover_retries_after_a_restart_and_after_a_day(archive, monkeypatch):
    await set_record(1, status="failed", failures=3, updated_at=1)
    now = [10 * HOUR_MS]
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: now[0])
    archive.error = OSError("disk")
    await home_handover.maintain(A)  # 重啟後第一次：再試
    assert len(home_handover._retried_failed) == 1
    assert (await records())[0][2:5] == ("failed", 0, 4)
    await home_handover.maintain(A)  # 同一次啟動、還沒滿 24 小時：不試
    assert (await records())[0][4] == 4
    await set_record(1, updated_at=now[0])
    now[0] += 24 * HOUR_MS + 1
    await home_handover.maintain(A)  # 滿 24 小時：再試一次
    assert (await records())[0][4] == 5
    archive.error = None
    await set_record(1, updated_at=1)
    home_handover._retried_failed.clear()
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"


@pytest.mark.usefixtures("unset_home")
@pytest.mark.parametrize("busy", ["stream", "heartbeat", "closing"])
async def test_a_busy_old_home_waits_for_the_next_turn(archive, monkeypatch, busy):
    if busy == "stream":
        monkeypatch.setattr(stream_buffer_service, "is_busy", lambda sid: sid == "A")
    elif busy == "heartbeat":
        monkeypatch.setattr(home_handover, "running_heartbeats", lambda _identity: ["A"])
    else:
        monkeypatch.setattr(closing_saves, "is_waiting", lambda sid: sid == "A", raising=False)
    await home_handover.maintain(A)
    assert archive.calls == []
    assert await records() == [("A", None, "pending", 0, 0, 0, None)]


async def test_an_old_home_back_as_home_does_not_wait_for_its_turns(archive, monkeypatch):
    await seed("A", tree=BRANCHED)
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")
    assert await service.set_home("A", A, True)
    monkeypatch.setattr(stream_buffer_service, "is_busy", lambda sid: sid == "A")
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"


@pytest.mark.usefixtures("unset_home")
async def test_a_reset_in_progress_stops_the_handover(archive, summaries, monkeypatch):
    await seed("kid", tree=BRANCHED)
    await set_scope_one("kid")
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: 7 * HOUR_MS)
    destructive_ops.hold(A)
    try:
        await home_handover.maintain(A)
    finally:
        destructive_ops.release(A)
    assert archive.calls == []
    assert summaries.calls == []  # 重置中連模型都不叫
    assert await segments() == []


@pytest.mark.usefixtures("unset_home")
async def test_a_reset_started_during_the_summary_writes_nothing(summaries):
    async def reset():
        destructive_ops.stop_heartbeats(A)

    summaries.before = reset
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "home_retired"]
    assert (await records())[0][3] == 1  # 第 2 步沒做完


async def test_each_step_checks_the_record_did_not_change(summaries, archive):
    await seed("A", tree=BRANCHED)
    await seed("B")
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")

    async def moved_on():
        await set_record(1, new_session="C")

    summaries.before = moved_on
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "home_retired"]
    assert (await records())[0][2:4] == ("running", 1)


# --- 子對話回報 ---


@pytest.fixture
async def child(scope_db):
    """靈魂 A 的子對話 kid，有新內容（沒有回報位置、範圍有效）."""
    await seed("kid", tree=BRANCHED)
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET session_scope_version = 1 WHERE id = 'kid'")
        await db.commit()


@pytest.mark.usefixtures("unset_home", "child")
async def test_the_handover_reports_children_with_new_content(summaries):
    await home_handover.maintain(A)
    report = [k for k in await segments() if k[0] == "child_report"]
    assert report == [("child_report", "kid", "摘要內容")]
    assert await mark_of("kid") == (A.agent_id, 1, "a")
    assert any(budget == home_handover.REPORT_MAX_TOKENS for _a, _m, budget in summaries.calls)


@pytest.mark.usefixtures("unset_home", "child")
async def test_only_what_came_after_the_mark_is_reported(summaries):
    await set_mark("kid", A.agent_id, "u")
    await home_handover.maintain(A)
    report = [m for _a, m, budget in summaries.calls if budget == home_handover.REPORT_MAX_TOKENS]
    assert [[n["id"] for n in msgs] for msgs in report] == [["a"]]


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_mark_off_the_current_branch_reports_the_whole_path(summaries):
    await set_mark("kid", A.agent_id, "b")
    await home_handover.maintain(A)
    report = [m for _a, m, budget in summaries.calls if budget == home_handover.REPORT_MAX_TOKENS]
    assert [[n["id"] for n in msgs] for msgs in report] == [["u", "a"]]


@pytest.mark.usefixtures("unset_home", "child")
async def test_nothing_new_means_no_report(summaries):
    await set_mark("kid", A.agent_id, "a")
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]


@pytest.mark.usefixtures("unset_home", "child")
async def test_an_invalidated_mark_is_skipped(summaries):
    await set_mark("kid", A.agent_id, "u", scope=0)
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") == (A.agent_id, 0, "u")


@pytest.mark.usefixtures("unset_home")
async def test_an_old_conversation_with_no_mark_is_skipped(summaries):
    await seed("old-kid", tree=BRANCHED)  # 範圍版本 0、沒有位置：不是升級後才有的新內容
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET session_scope_version = 0 WHERE id = 'old-kid'")
        await db.commit()
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_child_that_changed_souls_only_moves_the_mark(summaries):
    await set_mark("kid", B.agent_id, "u")
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") == (A.agent_id, 1, "a")


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_child_with_no_dialogue_only_moves_the_mark(summaries):
    tree = copy.deepcopy(BRANCHED)
    tree["messages"] = {"s": {"id": "s", "role": "system", "content": "x", "parentId": None}}
    tree["currentId"] = "s"
    await seed("kid", tree=tree)
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") == (A.agent_id, 1, "s")


@pytest.mark.usefixtures("unset_home")
@pytest.mark.parametrize("excluded", ["touka.sns.v1.x", "touka.hb.v1.x", "sns_old"])
async def test_channels_are_not_reported(summaries, excluded):
    await seed("kid", tree=BRANCHED)
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET id = ?, session_scope_version = 1 WHERE id = 'kid'",
            (excluded,),
        )
        await db.commit()
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]


async def test_homes_and_old_homes_are_not_reported(summaries):
    await seed("A", tree=BRANCHED)
    await seed("B", tree=BRANCHED)
    await legacy_home("A")
    assert await service.set_home("B", A, True)
    await start("B")
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_busy_child_is_not_reported(summaries, monkeypatch):
    monkeypatch.setattr(stream_buffer_service, "is_busy", lambda sid: sid == "kid")
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") is None


@pytest.mark.usefixtures("unset_home")
async def test_a_handover_reports_at_most_five_at_three_at_a_time(summaries, monkeypatch):
    for index in range(7):
        sid = f"kid{index}"
        await seed(sid, tree=BRANCHED)
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET session_scope_version = 1 WHERE id LIKE 'kid%'"
        )
        await db.commit()
    running = [0]
    peak = [0]

    async def track():
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running[0] -= 1

    summaries.before = track
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: 1000 + HOUR_MS)
    await home_handover.maintain(A)
    reports = [k for k in await segments() if k[0] == "child_report"]
    assert len(reports) == home_handover.REPORTS_PER_PASS
    assert peak[0] <= home_handover.REPORTS_IN_PARALLEL


@pytest.mark.usefixtures("unset_home", "child")
async def test_with_summaries_off_there_are_no_reports(summaries, config):
    config.prelude_summary_enabled = False
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") is None


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_failed_report_keeps_the_mark_and_cools_down(summaries, monkeypatch):
    summaries.reply = None
    await home_handover.maintain(A)
    assert await mark_of("kid") is None
    calls = len(summaries.calls)
    await home_handover.maintain(A)  # 冷卻中：不再花模型
    assert len(summaries.calls) == calls


@pytest.mark.usefixtures("unset_home", "child")
async def test_a_child_moved_during_the_report_writes_nothing(summaries):
    async def rebind():
        if summaries.calls[-1][2] != home_handover.REPORT_MAX_TOKENS:
            return
        async with async_db.get_async_db() as db:
            await db.execute("UPDATE user_sessions SET agent_id = 'ScopeB' WHERE id = 'kid'")
            await db.commit()

    summaries.before = rebind
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") is None


@pytest.mark.usefixtures("child")
async def test_idle_children_get_a_report_without_a_handover(summaries, monkeypatch):
    now = 1000 + 6 * HOUR_MS + 1
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: now)
    await home_handover.maintain(A)
    assert [k for k in await segments() if k[0] == "child_report"]


@pytest.mark.usefixtures("child")
async def test_children_active_recently_wait_for_six_hours(summaries, monkeypatch):
    now = 1000 + 6 * HOUR_MS - 1
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: now)
    await home_handover.maintain(A)
    assert summaries.calls == []


async def test_idle_reports_are_capped_at_five_per_pass(summaries, monkeypatch):
    for index in range(7):
        await seed(f"kid{index}", tree=BRANCHED)
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET session_scope_version = 1 WHERE id LIKE 'kid%'"
        )
        await db.commit()
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: 7 * HOUR_MS)
    await home_handover.maintain(A)
    assert len([k for k in await segments() if k[0] == "child_report"]) == 5


# --- 排程 ---


async def test_one_soul_runs_one_maintenance_and_a_later_request_runs_once_more(monkeypatch):
    runs = []
    gate = asyncio.Event()

    async def maintain(identity):
        runs.append(identity.agent_id)
        if len(runs) == 1:
            await gate.wait()

    monkeypatch.setattr(home_handover, "maintain", maintain)
    home_handover.schedule(A)
    await asyncio.sleep(0)
    home_handover.schedule(A)
    home_handover.schedule(A)
    await asyncio.sleep(0)
    assert runs == [A.agent_id]
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert runs == [A.agent_id, A.agent_id]
    assert A.agent_id not in home_handover._active


async def test_a_maintenance_that_fails_does_not_stop_later_ones(monkeypatch, caplog):
    runs = []

    async def maintain(identity):
        runs.append(identity.agent_id)
        raise OSError("disk")

    monkeypatch.setattr(home_handover, "maintain", maintain)
    home_handover.schedule(A)
    for _ in range(3):
        await asyncio.sleep(0)
    home_handover.schedule(A)
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(runs) == 2
    assert A.agent_id not in home_handover._active


async def test_a_handover_merges_the_prelude_afterwards(summaries, unset_home, monkeypatch):
    merged = []

    async def merge(identity, config, *, home_id=None):
        merged.append((identity.agent_id, home_id))
        return False

    monkeypatch.setattr(home_handover.soul_prelude_merge, "merge_if_needed", merge)
    await home_handover.maintain(A)
    assert merged == [(A.agent_id, None)]
    merged.clear()
    await home_handover.maintain(A)  # 沒寫新段落：不合併
    assert merged == []


# --- 2a 留下的：只記新家重複、新家後來被刪 ---


async def test_noted_records_of_one_handover_and_one_home_write_one_line(summaries, archive):
    """同一筆交接的只記新家可能有好幾筆指向同一段（用過、取消又設回）：換家標記只寫一行."""
    await seed("D")
    await seed("E")
    async with async_db.get_async_db() as db:
        for record_id, new_session in ((1, "D"), (2, "D"), (3, "E")):
            await db.execute(
                "INSERT INTO soul_home_transitions (id, agent_id, old_session, new_session, "
                "old_handled_by, new_started, created_at, updated_at) "
                "VALUES (?, ?, 'A', ?, 7, 1, ?, ?)",
                (record_id, A.agent_id, new_session, 1000 * record_id, 1000 * record_id),
            )
        await db.commit()
    await home_handover.maintain(A)
    assert [row[2] for row in await records()] == ["done", "done", "done"]
    marks = [k[2] for k in await segments() if k[0] == "handover"]
    assert len(marks) == 2
    assert sum("D became" in mark for mark in marks) == 1
    assert sum("E became" in mark for mark in marks) == 1


@pytest.mark.usefixtures("moved")
async def test_a_new_home_deleted_later_does_not_stop_the_handover(summaries, archive):
    """A→B 還沒做完時 B 已被刪（B 交接完才能刪）：舊家 A 照常交接，只有標記提到 B."""
    async with async_db.get_async_db() as db:
        await db.execute("DELETE FROM user_sessions WHERE id = 'B'")
        await db.commit()
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"
    assert len(archive.calls) == 1
    assert any(k[0] == "handover" and "from A to B" in k[2] for k in await segments())


async def test_failed_handovers_are_listed_for_the_stats_page(archive):
    await seed("A", tree=BRANCHED)
    await legacy_home("A")
    assert await service.set_home("A", A, False)
    archive.fail_ids.add("A")
    for _ in range(3):
        await home_handover.maintain(A)
    failed = await home_handover_db.list_failed_handovers()
    assert len(failed) == 1
    assert failed[0]["agent"] == A.agent_id
    assert failed[0]["old_session"] == "A"
    assert failed[0]["failures"] == 3


# --- 變異檢查補的 ---


async def test_a_pending_retired_summary_waits_for_an_open_record_of_that_home(summaries):
    """同一段舊家還有沒做完的紀錄時，退任摘要交給那一筆寫，這裡不補."""
    await seed("A", tree=BRANCHED)
    await seed("B")
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO soul_home_transitions (id, agent_id, old_session, new_session, status, "
            "step, retired_summary_pending, created_at, updated_at) "
            "VALUES (1, ?, 'A', NULL, 'done', 5, 1, 1, 1)",
            (A.agent_id,),
        )
        await db.execute(
            "INSERT INTO soul_home_transitions (id, agent_id, old_session, new_session, status, "
            "created_at, updated_at) VALUES (2, ?, 'A', 'B', 'pending', 2, 2)",
            (A.agent_id,),
        )
        await db.commit()
    await home_handover.maintain(A)
    assert not [k for k in await segments() if k[0] == "home_retired"]
    assert (await records())[0][5] == 1  # 還標著待補


@pytest.mark.usefixtures("child")
async def test_a_reset_started_during_a_report_writes_nothing(summaries, monkeypatch):
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: 7 * HOUR_MS)

    async def reset():
        destructive_ops.hold(A)

    summaries.before = reset
    try:
        await home_handover.maintain(A)
    finally:
        destructive_ops.release(A)
    assert summaries.calls  # 真的送去摘要了，是寫入時才擋下
    assert not [k for k in await segments() if k[0] == "child_report"]
    assert await mark_of("kid") is None


@pytest.mark.usefixtures("child")
async def test_idle_children_are_only_checked_every_so_often(summaries, monkeypatch):
    """候選要解析每段子對話的內容：不是每一輪都查，隔一段時間查一次."""
    clock = {"now": 7 * HOUR_MS}
    monkeypatch.setattr(home_handover, "get_current_epoch_ms", lambda: clock["now"])
    await home_handover.maintain(A)
    assert len([k for k in await segments() if k[0] == "child_report"]) == 1

    await seed("kid2", tree=BRANCHED)
    await set_scope_one("kid2")
    clock["now"] += home_handover.IDLE_CHECK_EVERY_MS - 1
    await home_handover.maintain(A)
    assert len([k for k in await segments() if k[0] == "child_report"]) == 1

    clock["now"] += 1
    await home_handover.maintain(A)
    assert len([k for k in await segments() if k[0] == "child_report"]) == 2


@pytest.mark.usefixtures("unset_home")
async def test_the_real_archive_keeps_every_branch_and_the_interjections(monkeypatch):
    """不換假的舊對話庫：整棵樹進得去，分支和併進內容的插話都查得到."""
    monkeypatch.setattr(home_handover, "_archive", REAL_ARCHIVE)
    await home_handover.maintain(A)
    assert (await records())[0][2] == "done"
    store = get_manager().get_legacy_sync_service().get_store(A)
    for node in ("u", "a", "b"):
        assert store.get_message(node)["session_id"] == "A"
    assert "wait" in store.get_message("a")["content"]
