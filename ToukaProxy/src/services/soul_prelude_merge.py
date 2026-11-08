"""前情超過上限時合併（3d PR 1，ADR-0305）.

前情摘要開著、前情超過上限時，把最舊的幾段可合併段落合成一段 ``merged``；只有一行的來源註記不合併
（註記留給折疊）；退任摘要會合併，否則最舊的永遠被折掉、再也看不到（ADR-0307）。比合併段新的段落
已經把顯示段數用完時不合併（一寫進去就被折掉）。吃進子對話回報的合併段要標記：SNS 請求連它一起排除。
在禁令 28 的兩個觸發點剪完之後在背景做（剪裁的對話鎖外面），拿靈魂鎖
（鎖的順序：靈魂鎖在最外層）；摘要在交易外。重置或刪除中不做，摘要期間開始的也丟掉結果；任何失敗
都留著原本的段落，同一個靈魂冷卻一段時間再試。

合併段排在它取代的段落的位置：寫入時間取被合併段落中最新的，但早於後面第一段非舊摘要的段落
（舊摘要比剪裁晚搬進來時也不會排錯，審查第 2 輪 G4）；保留的尾段不跨進舊摘要（第 3 輪 H2）。
輸出上限＝前情上限扣掉比合併段新、會先放進前情的段落與餘量（比它舊的會先被折掉，不扣，第 3 輪 H1）；
模型真的 token 數和本機估算不同，拿到結果後再用估算截到上限內，合併完一定放得進前情（G3）。
送給摘要員的每段壓成一行（G8）。同一來源被取代的舊版（舊摘要、退任摘要）一起標成已合併，不會重新出現（G7）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.services import destructive_ops
from src.services.dialogue_summarizer import SUMMARY_REQUEST_TOKEN_CAP, summarize_entries
from src.services.soul_locks import soul_lock
from src.services.soul_prelude_service import (
    MERGED_WITH_REPORTS,
    PRELUDE_MAX_SEGMENTS,
    carries_child_reports,
    is_source_note,
    prelude_agent,
    render_segment,
    segment_cost,
    visible_segments,
)
from src.utils.model_text import single_line
from src.utils.soul_prelude_db import PreludeSegment, list_live_segments, merge_segments
from src.utils.time_utils import get_current_epoch_ms
from src.utils.token_estimator import truncate_to_tokens

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
# 能被合併的種類（來源註記另外排除）
_MERGEABLE = frozenset(
    {"child_report", "home_retired", "home_trim", "handover", "legacy_summary", "merged"}
)
# 同一來源只顯示最新一段的種類：合併時被取代的舊版一起標掉
_SUPERSEDABLE = frozenset({"home_retired", "legacy_summary"})
# 合併失敗的靈魂：到這個時間（毫秒）之前不再試
_RETRY_AFTER: dict[str, int] = {}


def _keep_from(visible: list[StoredSegment], max_tokens: int) -> int:
    """保留的最新段落從哪一段開始：留到上限的一半（最新一段一定留）.

    保留的尾段不跨進舊摘要：舊摘要一律排在最前面、內容都比剪裁舊，碰到時全部交給合併；否則合併段
    （不是舊摘要）會排到保留的舊摘要後面，內容較舊卻變成「最新」（審查第 3 輪 H2）。
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
    while keep_from < len(visible) and visible[keep_from].kind == "legacy_summary":
        keep_from += 1
    return keep_from


def _pick_for_merge(visible: list[StoredSegment], keep_from: int) -> list[StoredSegment]:
    """保留段落之前的可合併段落交給合併（照顯示順序，一次請求不超過上限）."""
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
    return picked


def _merged_time(visible: list[StoredSegment], picked: list[StoredSegment]) -> int:
    """合併段的寫入時間：被合併的最新一段，但早於後面第一段非舊摘要的段落（舊摘要不看寫入時間排序）."""
    newest = max(seg.created_at for seg in picked)
    after = visible[visible.index(picked[-1]) + 1 :]
    later = [seg.created_at for seg in after if seg.kind != "legacy_summary"]
    return min(newest, later[0] - 1) if later else newest


def _room_taken(
    visible: list[StoredSegment], picked: list[StoredSegment], keep_from: int, merged_at: int
) -> int:
    """比合併段新、會先放進前情的段落成本：保留的最新段落，與排在合併段之後、不合併的段落.

    前情從最新往前放，比合併段舊的（較早的註記、退任摘要）會先被折掉，不佔它的位置；這一輪超過
    請求上限沒挑到的可合併段落下一輪會和合併段一起再併，也不扣。全部扣掉的話，舊段落累積之後上限
    變成負數，從此不再合併（審查第 3 輪 H1）。
    """
    chosen = {seg.id for seg in picked}
    room = 0
    for index, seg in enumerate(visible):
        if seg.id in chosen or seg.kind == "legacy_summary" or seg.created_at <= merged_at:
            continue  # 被合併的、舊摘要（排在最前面）、比合併段舊的
        if index >= keep_from or seg.kind not in _MERGEABLE or is_source_note(seg):
            room += segment_cost(seg)
    return room


def _superseded(
    segments: list[StoredSegment], visible: list[StoredSegment], picked: list[StoredSegment]
) -> list[int]:
    """被合併的舊摘要與退任摘要，同一來源被取代、沒顯示的舊版（一起標成已合併，不會重新出現）."""
    sources = {(seg.kind, seg.source_session) for seg in picked if seg.kind in _SUPERSEDABLE}
    shown = {seg.id for seg in visible}
    return [
        seg.id
        for seg in segments
        if (seg.kind, seg.source_session) in sources and seg.id not in shown
    ]


def _newer_than(visible: list[StoredSegment], picked: list[StoredSegment], merged_at: int) -> int:
    """比合併段新、顯示時排在它後面的段數（舊摘要一律排最前面，不算）."""
    chosen = {seg.id for seg in picked}
    return sum(
        1
        for seg in visible
        if seg.id not in chosen and seg.kind != "legacy_summary" and seg.created_at > merged_at
    )


def _fit_text(text: str, budget: int) -> str:
    """用本機估算截到上限內，截在換行處（模型真的 token 數和估算不同，審查第 2 輪 G3）."""
    cut = truncate_to_tokens(text, budget)
    if cut != text and "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut


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
    async with soul_lock(agent):
        if _RETRY_AFTER.get(agent, 0) > get_current_epoch_ms():
            return False
        since = destructive_ops.generation(identity)
        if since is None:  # 正在刪除、重置或清除
            return False
        cap = config.prelude_max_tokens
        segments = await list_live_segments(agent)
        visible = visible_segments(segments, home_id)
        total = sum(segment_cost(seg) for seg in visible)
        if total <= cap:
            return False
        keep_from = _keep_from(visible, cap)
        picked = _pick_for_merge(visible, keep_from)
        if len(picked) < 2:
            return False
        merged_at = _merged_time(visible, picked)
        if _newer_than(visible, picked, merged_at) >= PRELUDE_MAX_SEGMENTS - 1:
            return False  # 合併段一寫進去就被段數上限折掉，白花一次模型
        room = _room_taken(visible, picked, keep_from, merged_at)
        budget = min(MERGE_MAX_TOKENS, cap - room - _HEADER_MARGIN_TOKENS)
        if budget < MERGE_MIN_TOKENS:
            return False
        entries = [single_line(render_segment(seg)) for seg in picked]
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
            source_session=MERGED_WITH_REPORTS if any(map(carries_child_reports, picked)) else "",
            dedupe_key=f"{picked[0].id}-{picked[-1].id}-{len(picked)}",
            content=_fit_text(text, budget),
            created_at=merged_at,
            covers_from=min(starts) if starts else None,
            covers_to=max(ends) if ends else None,
        )
        ids = [seg.id for seg in picked] + _superseded(segments, visible, picked)
        return await merge_segments(agent, ids, merged)
