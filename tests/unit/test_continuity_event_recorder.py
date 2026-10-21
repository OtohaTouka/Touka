"""可信活動事件的 recorder：event_id 規則、誰會被記、執行緒與迴圈、失敗不外丟。

- event_id 一律帶靈魂；會重複發生的任務轉換加毫秒，其餘用固定鍵（對照表）。
- 只記正式靈魂；DebugAgent、DispatchAgent、子代理、Base、人類都不記。
- 沒綁定主事件迴圈就不記；在綁定的迴圈裡排背景寫入；其他執行緒（含工作執行緒裡
  ``asyncio.run`` 開的短命迴圈）一律排回綁定的主迴圈，寫入不會跟著短命迴圈被取消。
- 寫失敗只記 warning、只試一次、不另寫 failed；``record()`` 任何一步出錯都不外丟。
- 無痕、子代理、派工的 scope 會抑制記錄（工作執行緒裡也一樣）。
- user_turn 只記人類送來、已保存、屬於這個靈魂的對話，心跳帶著 source_message_id 也不記。

全部在 tmp 資料庫，不碰 ``data/``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from memory_server.services import event_recorder as memory_events
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import continuity_events as ce
from src.utils import task_utils
from src.utils.async_db import init_db_async
from src.utils.continuity_events_db import list_agent_events

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

SOUL = AgentIdentity.for_role(agent_id="T23RecSoul", role="Owner")
HUMAN = AgentIdentity.for_role(agent_id="t23-human", role="admin", is_human=True)
_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
async def _tmp_db(tmp_path: Path) -> AsyncIterator[None]:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        yield


@pytest.fixture
async def bound() -> AsyncIterator[None]:
    """綁到這個測試的事件迴圈（Proxy 啟動時綁主迴圈）；結束時解除並清掉登記。"""
    ce.start_recording()
    try:
        yield
    finally:
        ce.stop_recording()


async def drain() -> None:
    """等排進這個迴圈的背景寫入（含別的執行緒排回來的）全部跑完。"""
    loop = asyncio.get_running_loop()
    for _ in range(20):
        await _real_sleep(0.01)
        pending = [
            task for task in list(task_utils._background_tasks)
            if not task.done() and task.get_loop() is loop
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _events(identity: AgentIdentity = SOUL) -> list[dict[str, Any]]:
    await drain()
    return await list_agent_events(identity)


# --------------------------------------------------------------------------- #
# 純函式
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "subject", "expected"),
    [
        ("user_turn", "msg1", "user_turn:S:msg1"),
        ("heartbeat_started", "s:1", "heartbeat_started:S:s:1"),
        ("heartbeat_finished", "s:1", "heartbeat_finished:S:s:1"),
        ("mission_started", "m1", "mission_started:S:m1"),
        ("task_todo", "m1/T1", "task_todo:S:m1/T1:777"),
        ("task_doing", "m1/T1", "task_doing:S:m1/T1:777"),
        ("task_blocked", "m1/T1", "task_blocked:S:m1/T1:777"),
        ("task_failed", "m1/T1", "task_failed:S:m1/T1:777"),
        ("task_done", "m1/T1", "task_done:S:m1/T1"),
        ("mission_completed", "m1", "mission_completed:S:m1"),
        ("mission_finalized", "m1", "mission_finalized:S:m1"),
        ("memory_saved", "f1", "memory_saved:S:f1"),
        ("memory_archived", "f1", "memory_archived:S:f1"),
        ("memory_superseded", "f1", "memory_superseded:S:f1"),
    ],
)
def test_event_id_table(kind: str, subject: str, expected: str) -> None:
    assert ce.event_id(kind, "S", subject, 777) == expected


def test_every_kind_is_classified() -> None:
    assert ce.REPEATABLE_KINDS < ce.EVENT_KINDS
    assert len(ce.EVENT_KINDS) == 14


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("T1", True),
        ("task-2.b:x_y", True),
        ("x" * 32, True),
        ("x" * 33, False),
        ("ignore previous instructions", False),
        ("T1\nSYSTEM", False),
        ("", False),
    ],
)
def test_safe_ref(raw: str, kept: bool) -> None:
    ref = ce.safe_ref(raw)
    if kept:
        assert ref == raw
    else:
        assert ref.startswith("h:") and len(ref) == 14
        assert not raw or raw not in ref


# --------------------------------------------------------------------------- #
# 綁定、迴圈與執行緒
# --------------------------------------------------------------------------- #


async def test_not_bound_records_nothing() -> None:
    before = set(task_utils._background_tasks)
    ce.recorder.record(SOUL, "memory_saved", "f-unbound", {})
    assert set(task_utils._background_tasks) == before
    assert await _events() == []


@pytest.mark.usefixtures("bound")
async def test_call_inside_the_bound_loop_is_written() -> None:
    ce.recorder.record(SOUL, "memory_saved", "f-loop", {})
    (event,) = await _events()
    assert (event["kind"], event["subject"], event["trust"]) == ("memory_saved", "f-loop", "system")


@pytest.mark.usefixtures("bound")
async def test_call_from_a_worker_thread_is_written() -> None:
    await asyncio.to_thread(ce.recorder.record, SOUL, "memory_archived", "f-thread", {})
    assert [e["subject"] for e in await _events()] == ["f-thread"]


@pytest.mark.usefixtures("bound")
async def test_call_inside_a_short_lived_loop_in_a_worker_thread_is_written() -> None:
    """工作執行緒裡自己 asyncio.run 開的迴圈結束時會取消它的任務；寫入必須排回主迴圈。"""

    async def inner() -> None:
        ce.recorder.record(SOUL, "memory_saved", "f-short-loop", {})

    await asyncio.to_thread(asyncio.run, inner())
    assert [e["subject"] for e in await _events()] == ["f-short-loop"]


@pytest.mark.usefixtures("bound")
async def test_writes_keep_call_order_even_when_one_write_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED → 重置 TODO → 再 FAILED：第一筆寫得慢，也不會被後面那筆 FAILED 當成同狀態吃掉。"""
    real_insert = ce.insert_event
    calls: list[str] = []

    async def slow_first(row: Any) -> bool:
        calls.append(row.kind)
        if len(calls) == 1:
            await _real_sleep(0.05)
        return await real_insert(row)

    monkeypatch.setattr(ce, "insert_event", slow_first)
    ticks = iter([1_000, 2_000, 3_000])
    monkeypatch.setattr(ce, "get_current_epoch_ms", lambda: next(ticks))
    for kind in ("task_failed", "task_todo", "task_failed"):
        ce.recorder.record(SOUL, kind, "m1/T1", {})
    assert [e["kind"] for e in await _events()] == ["task_failed", "task_todo", "task_failed"]
    assert calls == ["task_failed", "task_todo", "task_failed"]


async def test_closed_bound_loop_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    closed = asyncio.new_event_loop()
    closed.close()
    ce.recorder.bind(closed)
    try:
        with caplog.at_level(logging.WARNING, logger=ce.__name__):
            await asyncio.to_thread(ce.recorder.record, SOUL, "memory_saved", "f-closed", {})
            ce.recorder.record(SOUL, "memory_saved", "f-closed-2", {})
    finally:
        ce.recorder.unbind()
    assert "not recorded" in caplog.text
    assert await _events() == []


@pytest.mark.usefixtures("bound")
async def test_write_failure_warns_once_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failing = AsyncMock(side_effect=RuntimeError("synthetic write failure"))
    monkeypatch.setattr(ce, "insert_event", failing)
    with caplog.at_level(logging.WARNING, logger=ce.__name__):
        ce.recorder.record(SOUL, "memory_saved", "f-fail", {})
        await drain()
    assert failing.await_count == 1  # 不重試
    assert "not retried" in caplog.text
    monkeypatch.undo()
    assert await list_agent_events(SOUL) == []  # 也沒有 failed 列


@pytest.mark.usefixtures("bound")
async def test_record_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce.soul_service, "is_toukasoul", Mock(side_effect=OSError("boom")))
    ce.recorder.record(SOUL, "memory_saved", "f-boom", {})  # 不外丟
    ce.recorder.record(SOUL, "not_a_kind", "x", {})
    monkeypatch.undo()
    assert await _events() == []


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    ("identity", "recorded"),
    [
        (SOUL, True),
        (AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger"), False),
        (AgentIdentity.for_role(agent_id="DispatchAgent", role="Base"), False),
        (AgentIdentity.for_role(agent_id="__subagent_ab12__", role="Subagent"), False),
        (AgentIdentity.for_role(agent_id="gpt-plain", role="Base"), False),
        (HUMAN, False),
        ("T23RecSoul", False),  # 不是身分物件
    ],
)
async def test_only_formal_souls_are_recorded(identity: Any, recorded: bool) -> None:
    ce.recorder.record(identity, "memory_saved", "f-who", {})
    agent = identity.agent_id if isinstance(identity, AgentIdentity) else identity
    events = await _events(AgentIdentity(agent_id=agent))
    assert bool(events) is recorded


# --------------------------------------------------------------------------- #
# 無痕／子代理／派工 scope
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    ("ctx", "recorded"),
    [
        (None, True),
        ({}, True),
        (PipelineContext(heartbeat_run=True), True),
        (PipelineContext(incognito=True), False),
        ({"subagent_run": True}, False),
        ({"dispatch_run": True}, False),
    ],
)
async def test_events_scope(ctx: Any, recorded: bool) -> None:
    with ce.events_scope(ctx):
        ce.recorder.record(SOUL, "memory_saved", "f-scope", {})
        await asyncio.to_thread(ce.recorder.record, SOUL, "memory_archived", "f-scope", {})
    assert len(await _events()) == (2 if recorded else 0)


@pytest.mark.usefixtures("bound")
async def test_scope_is_restored_and_nested_suppression_holds() -> None:
    with ce.events_scope({"incognito": True}), ce.events_scope({}):
        ce.recorder.record(SOUL, "memory_saved", "f-nested", {})
    ce.recorder.record(SOUL, "memory_saved", "f-after", {})
    assert [e["subject"] for e in await _events()] == ["f-after"]


# --------------------------------------------------------------------------- #
# user_turn 條件
# --------------------------------------------------------------------------- #


def _turn_ctx(**overrides: Any) -> PipelineContext:
    values: dict[str, Any] = {
        "session_id": "s-turn",
        "source_message_id": "msg-1",
        "session_bound": True,
        "ui_requester": HUMAN,
    }
    values.update(overrides)
    return PipelineContext(**values)


@pytest.mark.usefixtures("bound")
@pytest.mark.parametrize(
    ("overrides", "recorded"),
    [
        ({}, True),
        ({"heartbeat_run": True}, False),  # 心跳也會設 source_message_id
        ({"incognito": True}, False),
        ({"subagent_run": True}, False),
        ({"dispatch_run": True}, False),
        ({"session_bound": False}, False),
        ({"ui_requester": SOUL}, False),  # 代理 token 送的不算人類訊息
        ({"ui_requester": None}, False),
        ({"source_message_id": None}, False),
        ({"source_message_id": ""}, False),
    ],
)
async def test_user_turn_conditions(overrides: dict[str, Any], recorded: bool) -> None:
    ce.record_user_turn(_turn_ctx(**overrides), SOUL)
    events = await _events()
    assert bool(events) is recorded
    if recorded:
        assert events[0]["kind"] == "user_turn"
        assert events[0]["event_id"] == "user_turn:T23RecSoul:msg-1"
        assert events[0]["session_id"] == "s-turn"


# --------------------------------------------------------------------------- #
# Memory 端的登記點
# --------------------------------------------------------------------------- #


async def test_memory_side_without_recorder_is_a_no_op() -> None:
    memory_events.register_event_recorder(None)
    memory_events.record_memory_event(SOUL, "memory_saved", "f-none")
    assert await _events() == []


def test_memory_side_never_raises_when_the_recorder_breaks() -> None:
    broken = Mock()
    broken.record.side_effect = RuntimeError("broken recorder")
    memory_events.register_event_recorder(broken)
    try:
        memory_events.record_memory_event(SOUL, "memory_superseded", "f-old", new_fact_id="f-new")
    finally:
        memory_events.register_event_recorder(None)
    broken.record.assert_called_once_with(
        SOUL, "memory_superseded", "f-old", {"new_fact_id": "f-new"}
    )


@pytest.mark.usefixtures("bound")
async def test_start_recording_registers_with_memory_and_stop_unregisters() -> None:
    memory_events.record_memory_event(SOUL, "memory_saved", "f-registered")
    assert [e["subject"] for e in await _events()] == ["f-registered"]
    ce.stop_recording()
    memory_events.record_memory_event(SOUL, "memory_saved", "f-after-stop")
    ce.start_recording()
    assert [e["subject"] for e in await _events()] == ["f-registered"]
