"""前情超過上限時合併（3d PR 1，ADR-0305）.

前情摘要開著、前情超過上限時，把最舊的幾段可合併段落合成一段 ``merged``；退任摘要與只有一行的
來源註記不合併（註記留給折疊）。在禁令 28 的兩個觸發點剪完之後在背景做（剪裁的對話鎖外面），拿靈魂鎖
（鎖的順序：靈魂鎖在最外層）；摘要在交易外。重置或刪除中不做，摘要期間開始的也丟掉結果；任何失敗
都留著原本的段落，同一個靈魂冷卻一段時間再試。

合併段排在它取代的段落的位置（寫入時間取被合併段落中最新的），輸出上限跟著前情上限走，合併完一定
放得進前情。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.services import destructive_ops
from src.services.dialogue_summarizer import SUMMARY_REQUEST_TOKEN_CAP, summarize_entries
from src.services.soul_prelude_service import (
    is_source_note,
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

# 一次合併的摘要輸出上限（token）；實際上限還要扣掉保留段落，放得進前情
MERGE_MAX_TOKENS = 2_000
# 扣完之後剩不到這麼多就不合併（合併出來也沒幾個字，折疊就好）
MERGE_MIN_TOKENS = 200
# 合併段標頭與估算誤差的餘量
_HEADER_MARGIN_TOKENS = 50
# 合併失敗後同一個靈魂多久再試（記在記憶體，重啟就清掉）
MERGE_RETRY_AFTER_MS = 6 * 60 * 60 * 1000
# 最新的段落留到上限的 1/_KEEP_NEWEST_SHARE，其餘交給合併段
_KEEP_NEWEST_SHARE = 2
# 能被合併的種類：退任摘要不合併（它代表一整段舊家，要留著原樣）
_MERGEABLE = frozenset({"child_report", "home_trim", "handover", "legacy_summary", "merged"})

# 每個靈魂一把鎖：合併（之後的交接與回報也是）一次只做一件
_SOUL_LOCKS: dict[str, asyncio.Lock] = {}
# 合併失敗的靈魂：到這個時間（毫秒）之前不再試
_RETRY_AFTER: dict[str, int] = {}


def _soul_lock(agent: str) -> asyncio.Lock:
    lock = _SOUL_LOCKS.get(agent)
    if lock is None:
        lock = _SOUL_LOCKS[agent] = asyncio.Lock()
    return lock


def _pick_for_merge(
    visible: list[StoredSegment], max_tokens: int
) -> tuple[list[StoredSegment], int]:
    """最新的段落留到上限的一半（最新一段一定留），更早的可合併段落交給合併.

    回傳要合併的段落與保留段落的估算 token 數（不合併的退任摘要與註記也算在保留裡）。
    """
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
        if seg.kind not in _MERGEABLE or is_source_note(seg):
            continue
        cost = segment_cost(seg)
        if used + cost > SUMMARY_REQUEST_TOKEN_CAP:
            break
        picked.append(seg)
        used += cost
    return picked, kept


async def merge_if_needed(
    identity: AgentIdentity, config: SlidingWindowConfig, *, home_id: str | None = None
) -> bool:
    """前情超過上限、前情摘要開著時，把最舊的幾段合成一段；有合併回 True.

    拿靈魂鎖；摘要在交易外。重置或刪除中（``destructive_ops``）不做，摘要期間開始的也丟掉結果。
    任何失敗都留著原本的段落，冷卻 ``MERGE_RETRY_AFTER_MS`` 再試，不影響聊天。
    """
    if not config.prelude_summary_enabled:
        return False
    agent = prelude_agent(identity)
    async with _soul_lock(agent):
        if _RETRY_AFTER.get(agent, 0) > get_current_epoch_ms():
            return False
        since = destructive_ops.generation(identity)
        if since is None:  # 正在刪除、重置或清除
            return False
        cap = config.prelude_max_tokens
        visible = visible_segments(await list_live_segments(agent), home_id)
        if sum(segment_cost(seg) for seg in visible) <= cap:
            return False
        picked, kept = _pick_for_merge(visible, cap)
        budget = min(MERGE_MAX_TOKENS, cap - kept - _HEADER_MARGIN_TOKENS)
        if len(picked) < 2 or budget < MERGE_MIN_TOKENS:
            return False
        entries = [render_segment(seg) for seg in picked]
        text = await summarize_entries(identity, entries, budget, config.summary_model)
        if not text:
            _RETRY_AFTER[agent] = get_current_epoch_ms() + MERGE_RETRY_AFTER_MS
            logger.warning("Prelude merge for %s failed; trying again later", agent)
            return False
        _RETRY_AFTER.pop(agent, None)
        if destructive_ops.disturbed(identity, since):
            logger.info("Prelude merge for %s dropped: reset or delete started", agent)
            return False
        starts = [seg.covers_from for seg in picked if seg.covers_from is not None]
        ends = [seg.covers_to for seg in picked if seg.covers_to is not None]
        merged = PreludeSegment(
            agent=agent,
            kind="merged",
            source_session="",
            dedupe_key=f"{picked[0].id}-{picked[-1].id}-{len(picked)}",
            content=text,
            created_at=max(seg.created_at for seg in picked),
            covers_from=min(starts) if starts else None,
            covers_to=max(ends) if ends else None,
        )
        return await merge_segments(agent, [seg.id for seg in picked], merged)
