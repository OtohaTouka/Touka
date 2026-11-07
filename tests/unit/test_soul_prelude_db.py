"""靈魂前情段落表（``ui_sessions.db`` 的 ``soul_prelude_segments``）的建表與讀寫（3d PR 1）。

- 建表在 ``init_db_async``；範圍有效的列裡，同一靈魂、種類、來源、去重值只留一筆（INSERT OR IGNORE）。
- 讀前情只看範圍版本 1、還沒被合併的段落；合併只標記被合併的段落，不刪。
- 同名重建（``invalidate_agent_sessions``）只讓那個靈魂的段落失效；還原備份
  （``invalidate_restored_sessions``）讓全部失效。失效的段落不再出現在前情裡。
- 重置清除只刪該靈魂的段落；匯出列出該靈魂全部段落。

全部在 tmp 資料庫（``async_db.get_db_path`` 指到 tmp_path），不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.utils import soul_prelude_db as db_mod
from src.utils.async_db import get_async_db, init_db_async, session_service
from src.utils.soul_prelude_db import PreludeSegment

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SOUL = AgentIdentity.for_role(agent_id="PreludeDbSoul", role="Owner")
OTHER = AgentIdentity.for_role(agent_id="PreludeDbOther", role="Owner")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path) -> Iterator[None]:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        yield


def _seg(
    key: str,
    *,
    kind: str = "home_trim",
    agent: AgentIdentity = SOUL,
    source: str = "home-1",
    content: str = "要點",
    at: int = 1_000,
) -> PreludeSegment:
    return PreludeSegment(
        agent=agent.agent_id,
        kind=kind,
        source_session=source,
        dedupe_key=key,
        content=content,
        created_at=at,
    )


@pytest.mark.asyncio
async def test_init_creates_table_and_unique_index() -> None:
    await init_db_async()
    async with get_async_db() as db:
        cur = await db.execute("PRAGMA index_list(soul_prelude_segments)")
        indexes = {row["name"]: row["unique"] for row in await cur.fetchall()}
    assert any(unique for unique in indexes.values())


@pytest.mark.asyncio
async def test_insert_is_idempotent_per_kind_source_and_key() -> None:
    await init_db_async()
    assert await db_mod.insert_segment(_seg("m9")) is True
    assert await db_mod.insert_segment(_seg("m9", content="不同內容也不重寫")) is False
    assert await db_mod.insert_segment(_seg("m9", source="home-2")) is True
    assert await db_mod.insert_segment(_seg("m9", kind="legacy_summary")) is True
    live = await db_mod.list_live_segments(SOUL.agent_id)
    assert [s.content for s in live if s.source_session == "home-1" and s.kind == "home_trim"] == [
        "要點"
    ]
    assert len(live) == 3


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected() -> None:
    await init_db_async()
    with pytest.raises(ToukaException):
        await db_mod.insert_segment(_seg("k", kind="whatever"))


@pytest.mark.asyncio
async def test_live_segments_are_per_soul() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    await db_mod.insert_segment(_seg("b", agent=OTHER))
    assert [s.dedupe_key for s in await db_mod.list_live_segments(SOUL.agent_id)] == ["a"]
    assert [s.dedupe_key for s in await db_mod.list_live_segments(OTHER.agent_id)] == ["b"]


@pytest.mark.asyncio
async def test_insert_in_transaction_rolls_back_with_it() -> None:
    await init_db_async()
    async with get_async_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await db_mod.insert_segment_tx(db, _seg("tx")) is True
        await db.rollback()
    assert await db_mod.list_live_segments(SOUL.agent_id) == []


@pytest.mark.asyncio
async def test_merge_marks_sources_and_adds_merged_segment() -> None:
    await init_db_async()
    for key in ("a", "b", "c"):
        await db_mod.insert_segment(_seg(key))
    live = await db_mod.list_live_segments(SOUL.agent_id)
    ids = [s.id for s in live if s.dedupe_key in {"a", "b"}]
    merged = _seg("batch-1", kind="merged", source="", content="合併後")
    assert await db_mod.merge_segments(SOUL.agent_id, ids, merged) is True
    after = await db_mod.list_live_segments(SOUL.agent_id)
    assert sorted(s.dedupe_key for s in after) == ["batch-1", "c"]
    # 被合併的段落還在（只標記，不刪），匯出看得到
    exported = await db_mod.list_agent_segments(SOUL)
    assert len(exported) == 4
    assert sum(1 for row in exported if row["merged_into"] is not None) == 2


@pytest.mark.asyncio
async def test_merge_refuses_when_a_source_is_gone_or_already_merged() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    await db_mod.insert_segment(_seg("b"))
    live = await db_mod.list_live_segments(SOUL.agent_id)
    ids = [s.id for s in live]
    assert await db_mod.merge_segments(SOUL.agent_id, ids, _seg("m1", kind="merged")) is True
    # 同一批再合併一次：來源已被合併，整筆不做
    assert await db_mod.merge_segments(SOUL.agent_id, ids, _seg("m2", kind="merged")) is False
    keys = [s.dedupe_key for s in await db_mod.list_live_segments(SOUL.agent_id)]
    assert keys == ["m1"]


@pytest.mark.asyncio
async def test_merge_refuses_other_souls_rows() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("x", agent=OTHER))
    other_ids = [s.id for s in await db_mod.list_live_segments(OTHER.agent_id)]
    assert await db_mod.merge_segments(SOUL.agent_id, other_ids, _seg("m", kind="merged")) is False
    assert [s.dedupe_key for s in await db_mod.list_live_segments(OTHER.agent_id)] == ["x"]


@pytest.mark.asyncio
async def test_same_name_rebuild_invalidates_only_that_soul() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    await db_mod.insert_segment(_seg("b", agent=OTHER))
    await session_service.invalidate_agent_sessions(SOUL)
    assert await db_mod.list_live_segments(SOUL.agent_id) == []
    assert len(await db_mod.list_live_segments(OTHER.agent_id)) == 1
    # 失效後同一個去重值可以重新寫（新的範圍）
    assert await db_mod.insert_segment(_seg("a")) is True


@pytest.mark.asyncio
async def test_restore_invalidates_every_soul() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    await db_mod.insert_segment(_seg("b", agent=OTHER))
    await session_service.invalidate_restored_sessions()
    assert await db_mod.list_live_segments(SOUL.agent_id) == []
    assert await db_mod.list_live_segments(OTHER.agent_id) == []


@pytest.mark.asyncio
async def test_purge_and_count_are_per_soul() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    await db_mod.insert_segment(_seg("b"))
    await db_mod.insert_segment(_seg("c", agent=OTHER))
    await session_service.invalidate_agent_sessions(SOUL)  # 失效的也要清、也要算
    await db_mod.insert_segment(_seg("a"))
    assert await db_mod.count_agent_segments(SOUL) == 3
    assert await db_mod.purge_agent_segments(SOUL) == 3
    assert await db_mod.count_agent_segments(SOUL) == 0
    assert await db_mod.count_agent_segments(OTHER) == 1


@pytest.mark.asyncio
async def test_has_segment_checks_live_rows_only() -> None:
    await init_db_async()
    await db_mod.insert_segment(_seg("h", kind="legacy_summary"))
    assert await db_mod.has_segment(SOUL.agent_id, "legacy_summary", "home-1", "h") is True
    await session_service.invalidate_agent_sessions(SOUL)
    assert await db_mod.has_segment(SOUL.agent_id, "legacy_summary", "home-1", "h") is False


@pytest.mark.asyncio
async def test_invalidating_twice_after_rewriting_the_same_key_works() -> None:
    """失效 → 同一個去重值又寫回 → 再失效：不撞唯一索引（還原、同名重建都一樣；審查 F1）."""
    await init_db_async()
    await db_mod.insert_segment(_seg("k", kind="legacy_summary"))
    await session_service.invalidate_restored_sessions()
    assert await db_mod.insert_segment(_seg("k", kind="legacy_summary")) is True
    await session_service.invalidate_restored_sessions()  # 以前在這裡丟 SESSION_SAVE_FAILED
    assert await db_mod.insert_segment(_seg("k", kind="legacy_summary")) is True
    await session_service.invalidate_agent_sessions(SOUL)
    assert await db_mod.list_live_segments(SOUL.agent_id) == []
    assert await db_mod.count_agent_segments(SOUL) == 3


@pytest.mark.asyncio
async def test_export_lists_every_row_with_its_scope() -> None:
    """匯出帶全部列（含失效、已合併），和對話匯出一樣；範圍版本照實帶著（審查第 2 輪 G11）."""
    await init_db_async()
    await db_mod.insert_segment(_seg("old", content="舊的"))
    await session_service.invalidate_agent_sessions(SOUL)
    await db_mod.insert_segment(_seg("new", content="新的"))
    rows = await db_mod.list_agent_segments(SOUL)
    assert [(row["content"], row["scope_version"]) for row in rows] == [("舊的", 0), ("新的", 1)]


@pytest.mark.asyncio
async def test_was_invalidated_finds_only_invalidated_rows() -> None:
    """搬過又被失效的舊摘要查得到；範圍有效的、從沒寫過的都查不到（審查第 2 輪 G1）."""
    await init_db_async()
    await db_mod.insert_segment(_seg("h", kind="legacy_summary"))
    assert await db_mod.was_invalidated(SOUL.agent_id, "legacy_summary", "home-1", "h") is False
    await session_service.invalidate_restored_sessions()
    assert await db_mod.was_invalidated(SOUL.agent_id, "legacy_summary", "home-1", "h") is True
    assert await db_mod.was_invalidated(SOUL.agent_id, "legacy_summary", "home-1", "x") is False


@pytest.mark.asyncio
async def test_insert_checks_skip_inside_the_write_lock() -> None:
    """寫入前在寫鎖裡再問一次要不要放棄：回 True 就不寫（重置剛開始時，審查第 2 輪 G6）."""
    await init_db_async()
    asked: list[bool] = []

    def skip() -> bool:
        asked.append(True)
        return True

    assert await db_mod.insert_segment(_seg("a"), skip_if=skip) is False
    assert asked == [True]
    assert await db_mod.list_live_segments(SOUL.agent_id) == []
    assert await db_mod.insert_segment(_seg("a"), skip_if=lambda: False) is True


@pytest.mark.asyncio
async def test_lifecycle_helpers_use_the_canonical_soul_id() -> None:
    """計數、清除、列出和寫入同一條正式化規則（別名換成註冊名；審查 F19）."""
    await init_db_async()
    await db_mod.insert_segment(_seg("a"))
    alias = AgentIdentity.for_role(agent_id="ToukaSoulPreludeDbSoul", role="Owner")
    with patch.object(db_mod, "canonical_agent_id", return_value=SOUL.agent_id) as canon:
        assert await db_mod.count_agent_segments(alias) == 1
        assert len(await db_mod.list_agent_segments(alias)) == 1
        assert await db_mod.purge_agent_segments(alias) == 1
    assert canon.call_count == 3
