"""剪裁寫進前情（3d PR 1，ADR-0305）：段落和剪過的樹在同一個交易，不再改 ``context_summary``。

- 剪成功：舊對話庫歸檔、樹剪短、寫一段 ``home_trim``（有摘要放摘要，沒有放來源註記），
  ``context_summary`` 不動（畫面照舊送它，``trim_graft`` 不受影響）。
- 樹被擋下（讀檔後換家、取消家）時段落也不寫；段落寫入失敗時樹也不寫。
- 摘要員拿到前情顯示順序的最後一段，避免重複；摘要長度不超過前情上限。
- 剪完才在背景做前情合併（拿靈魂鎖，在剪裁的對話鎖外面），剪裁不等它。
- 靈魂正在重置或刪除時不寫前情段落，樹也不剪（審查 F11）。

全部離線：SQLite 只在測試沙盒；舊對話庫、摘要與 hook 都是假的。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from src.models.session_persistence import SessionChanges
from src.models.sliding_window import SlidingWindowConfig
from src.services import destructive_ops, soul_prelude_merge, soul_prelude_service
from src.services import sliding_window_service as sliding
from src.utils import async_db, task_utils
from src.utils import soul_prelude_db as db_mod
from src.utils.soul_prelude_db import PreludeSegment

from tests.support.session_scope import HUMAN, A, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

service = async_db.session_service


@pytest.fixture
def trim(monkeypatch):
    """一定會剪掉第一則；舊對話庫、摘要、合併與 hook 換成假的."""
    svc = sliding.sliding_window_service
    config = SlidingWindowConfig(enabled=True, summary_budget_pct=5)
    monkeypatch.setattr(svc, "load_config", lambda: config)
    monkeypatch.setattr(svc, "_resolve_model", lambda _linear: "synthetic-model")
    monkeypatch.setattr(svc, "_check_model_cooldown", lambda *_a: False)
    monkeypatch.setattr(svc, "_current_total_tokens", lambda *_a: 100)
    monkeypatch.setattr(sliding, "get_context_window", lambda _model: 100)
    monkeypatch.setattr(
        svc, "compute_split_by_tokens", lambda linear, *_a: (linear[:1], linear[1:])
    )
    archive = Mock(return_value=1)
    manager = SimpleNamespace(
        get_legacy_sync_service=lambda: SimpleNamespace(archive_payload=archive)
    )
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    doubles = SimpleNamespace(
        service=svc,
        config=config,
        archive=archive,
        summarize=AsyncMock(return_value="new summary"),
        merge=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(sliding, "summarize_dialogue", doubles.summarize)
    monkeypatch.setattr(sliding, "notify_pre_compact", Mock())
    monkeypatch.setattr(sliding, "notify_post_compact", Mock())
    monkeypatch.setattr(soul_prelude_merge, "merge_if_needed", doubles.merge)
    return doubles


async def _drain() -> None:
    """等這個迴圈的背景工作（剪完的合併）做完."""
    loop = asyncio.get_running_loop()
    pending = [t for t in list(task_utils._background_tasks) if t.get_loop() is loop]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _live():
    return await db_mod.list_live_segments(A.agent_id)


async def test_trim_writes_a_segment_with_the_tree_and_leaves_context_summary(trim):
    await seed("chat", context_summary="old summary")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    row = await raw_row()
    assert list(json.loads(row["messages"])["messages"]) == ["a"]
    assert row["context_summary"] == "old summary"
    segments = await _live()
    assert [(s.kind, s.source_session, s.dedupe_key, s.content) for s in segments] == [
        ("home_trim", "chat", "u", "new summary")
    ]


async def test_trim_without_summary_writes_a_source_note(trim, monkeypatch):
    monkeypatch.setattr(
        trim.service, "load_config", lambda: SlidingWindowConfig(enabled=True, summary_budget_pct=0)
    )
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    trim.summarize.assert_not_awaited()
    assert [s.content for s in await _live()] == [soul_prelude_service.trim_note(1)]


@pytest.mark.parametrize("set_home", [("chat", False), ("other", True)], ids=["clear", "move"])
async def test_no_segment_when_the_tree_write_is_refused(trim, set_home):
    await seed("chat")
    await seed("other")
    assert await service.set_home("chat", A, True)

    async def change_home(*_args, **_kwargs):
        assert await service.set_home(set_home[0], A, set_home[1])
        return "new summary"

    trim.summarize.side_effect = change_home
    assert await trim.service.check_and_trim(A, "chat") is False
    await _drain()
    assert await _live() == []
    trim.merge.assert_not_awaited()


async def test_summarizer_gets_the_last_prelude_entry(trim):
    await db_mod.insert_segment(
        PreludeSegment(
            agent=A.agent_id,
            kind="home_trim",
            source_session="chat",
            dedupe_key="earlier",
            content="上一段前情",
            created_at=1,
        )
    )
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    assert trim.summarize.await_args.kwargs["previous"] == "上一段前情"


async def test_merge_runs_after_a_successful_trim_only(trim):
    await seed("chat")
    await seed("other")
    assert await trim.service.check_and_trim(A, "chat") is False  # 不是家：不剪也不合併
    trim.merge.assert_not_awaited()
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    await _drain()
    trim.merge.assert_awaited_once()
    assert trim.merge.await_args.args[0] is A
    assert trim.merge.await_args.kwargs["home_id"] == "chat"


async def test_merge_failure_does_not_undo_the_trim(trim):
    trim.merge.side_effect = RuntimeError("boom")
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    await _drain()
    assert len(await _live()) == 1


async def test_extra_write_failure_rolls_back_the_tree():
    await seed("chat")
    before = await raw_row()
    snapshot = await service.read_persistence_snapshot("chat", A)

    async def broken(_db):
        raise RuntimeError("segment write failed")

    result = await service.conditional_save_session(
        "chat", snapshot.revision, SessionChanges(title="changed"), A, after_write=broken
    )
    assert result.status == "unknown"
    assert await raw_row() == before


async def test_extra_write_is_skipped_when_the_revision_is_stale():
    await seed("chat")
    called = AsyncMock()
    result = await service.conditional_save_session(
        "chat", "stale", SessionChanges(title="changed"), HUMAN, after_write=called
    )
    assert result.status == "failed"
    called.assert_not_awaited()


async def test_trim_does_not_wait_for_the_merge(trim):
    """合併在背景：剪裁先回來，不等合併的模型呼叫（審查 F4）."""
    gate = asyncio.Event()

    async def slow_merge(*_args, **_kwargs):
        await gate.wait()
        return True

    trim.merge.side_effect = slow_merge
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await asyncio.wait_for(trim.service.check_and_trim(A, "chat"), timeout=5) is True
    gate.set()
    await _drain()
    trim.merge.assert_awaited_once()


async def test_summarizer_gets_the_last_shown_entry_not_the_last_written(trim):
    """接續摘要取顯示順序的最後一段：現任家自己的回報不顯示，不拿它（審查 F3）."""
    await db_mod.insert_segment(
        PreludeSegment(
            agent=A.agent_id,
            kind="home_trim",
            source_session="chat",
            dedupe_key="shown",
            content="顯示的最後一段",
            created_at=5,
        )
    )
    await db_mod.insert_segment(
        PreludeSegment(
            agent=A.agent_id,
            kind="child_report",
            source_session="chat",
            dedupe_key="hidden",
            content="家自己以前的回報",
            created_at=1,
        )
    )
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    assert trim.summarize.await_args.kwargs["previous"] == "顯示的最後一段"


async def test_trim_summary_is_no_longer_than_the_prelude_limit(trim, monkeypatch):
    """剪裁摘要的長度不超過前情上限，免得一段就擠掉整份前情（審查 F6）."""
    config = SlidingWindowConfig(enabled=True, summary_budget_pct=5, prelude_max_tokens=1_000)
    monkeypatch.setattr(trim.service, "load_config", lambda: config)
    monkeypatch.setattr(sliding, "get_context_window", lambda _model: 1_000_000)
    monkeypatch.setattr(trim.service, "_current_total_tokens", lambda *_a: 1_000_000)
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    assert trim.summarize.await_args.args[2] == 1_000


async def test_no_segment_while_the_soul_is_being_reset(trim):
    """重置或刪除進行中：不剪、不寫前情、不花摘要（審查 F11）."""
    await seed("chat")
    assert await service.set_home("chat", A, True)
    before = await raw_row()
    destructive_ops.hold(A)
    try:
        assert await trim.service.check_and_trim(A, "chat") is False
    finally:
        destructive_ops.release(A)
    trim.summarize.assert_not_awaited()
    assert await _live() == []
    assert await raw_row() == before


async def test_reset_starting_during_the_summary_rolls_back_the_trim(trim):
    """摘要期間開始重置：前情段落不寫，樹也不剪（同一個交易退回，審查 F11）."""
    await seed("chat")
    assert await service.set_home("chat", A, True)
    before = await raw_row()

    async def reset_starts(*_args, **_kwargs):
        destructive_ops.hold(A)
        return "new summary"

    trim.summarize.side_effect = reset_starts
    try:
        assert await trim.service.check_and_trim(A, "chat") is False
    finally:
        destructive_ops.release(A)
    await _drain()
    assert await _live() == []
    assert await raw_row() == before
    trim.merge.assert_not_awaited()
