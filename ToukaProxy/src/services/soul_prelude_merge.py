"""前情超過上限時合併（3d PR 1，ADR-0305）.

前情摘要開著、前情超過上限時，把最舊的幾段可合併段落合成一段 ``merged``；退任摘要不合併。
在禁令 28 的兩個觸發點剪完之後做（剪裁的對話鎖外面），拿靈魂鎖（鎖的順序：靈魂鎖在最外層）；
摘要在交易外。重置或刪除中不做，摘要期間開始的也丟掉結果；任何失敗都留著原本的段落。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.services import destructive_ops
from src.services.dialogue_summarizer import SUMMARY_REQUEST_TOKEN_CAP, summarize_entries
from src.services.soul_prelude_service import (
    prelude_agent,
    render_segment,
    segment_cost,
    visible_segments,
)
from src.utils.soul_prelude_db import PreludeSegment, list_live_segments, merge_segments
from src.utils.time_utils import get_current_epoch_ms

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.models.sliding_window import SlidingWindowConfig
    from src.utils.soul_prelude_db import StoredSegment

logger = logging.getLogger(__name__)

# 一次合併的摘要輸出上限（token）
MERGE_MAX_TOKENS = 2_000
# 最新的段落留到上限的 1/_KEEP_NEWEST_SHARE，其餘交給合併段
_KEEP_NEWEST_SHARE = 2
# 能被合併的種類：退任摘要不合併（它代表一整段舊家，要留著原樣）
_MERGEABLE = frozenset({"child_report", "home_trim", "handover", "legacy_summary", "merged"})

# 每個靈魂一把鎖：合併（之後的交接與回報也是）一次只做一件
_SOUL_LOCKS: dict[str, asyncio.Lock] = {}


def _soul_lock(agent: str) -> asyncio.Lock:
    lock = _SOUL_LOCKS.get(agent)
    if lock is None:
        lock = _SOUL_LOCKS[agent] = asyncio.Lock()
    return lock


def _pick_for_merge(visible: list[StoredSegment], max_tokens: int) -> list[StoredSegment]:
    """最新的段落留到上限的一半（最新一段一定留），更早的可合併段落交給合併."""
    keep_budget = max_tokens // _KEEP_NEWEST_SHARE
    kept = 0
    keep_from = len(visible)
    for index in range(len(visible) - 1, -1, -1):
        cost = segment_cost(visible[index])
        if keep_from < len(visible) and kept + cost > keep_budget:
            break
        kept += cost
        keep_from = index
    picked: list[StoredSegment] = []
    used = 0
    for seg in visible[:keep_from]:
        if seg.kind not in _MERGEABLE:
            continue
        cost = segment_cost(seg)
        if used + cost > SUMMARY_REQUEST_TOKEN_CAP:
            break
        picked.append(seg)
        used += cost
    return picked


async def merge_if_needed(
    identity: AgentIdentity, config: SlidingWindowConfig, *, home_id: str | None = None
) -> bool:
    """前情超過上限、前情摘要開著時，把最舊的幾段合成一段；有合併回 True.

    拿靈魂鎖；摘要在交易外。重置或刪除中（``destructive_ops``）不做，摘要期間開始的也丟掉結果。
    任何失敗都留著原本的段落（下次再試），不影響聊天。
    """
    if not config.prelude_summary_enabled:
        return False
    agent = prelude_agent(identity)
    async with _soul_lock(agent):
        since = destructive_ops.generation(identity)
        if since is None:  # 正在刪除、重置或清除
            return False
        visible = visible_segments(await list_live_segments(agent), home_id)
        if sum(segment_cost(seg) for seg in visible) <= config.prelude_max_tokens:
            return False
        picked = _pick_for_merge(visible, config.prelude_max_tokens)
        if len(picked) < 2:
            return False
        entries = [render_segment(seg) for seg in picked]
        text = await summarize_entries(identity, entries, MERGE_MAX_TOKENS, config.summary_model)
        if not text:
            return False
        if destructive_ops.disturbed(identity, since):
            logger.info("Prelude merge for %s dropped: reset or delete started", agent)
            return False
        times = [seg.covers_to or seg.created_at for seg in picked]
        starts = [seg.covers_from for seg in picked if seg.covers_from is not None]
        merged = PreludeSegment(
            agent=agent,
            kind="merged",
            source_session="",
            dedupe_key=f"{picked[0].id}-{picked[-1].id}-{len(picked)}",
            content=text,
            created_at=get_current_epoch_ms(),
            covers_from=min(starts) if starts else None,
            covers_to=max(times),
        )
        return await merge_segments(agent, [seg.id for seg in picked], merged)
