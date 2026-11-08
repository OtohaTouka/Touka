"""靈魂前情（3d PR 1，ADR-0305）：組前情、舊摘要搬家、剪裁段落.

不匯入摘要員：主線組法經群聊派送也會用到這裡（摘要員會繞回派送）；合併在 soul_prelude_merge。

前情＝一個靈魂累積的一段段摘要或來源註記（``soul_prelude_segments``），每段標從哪段對話來、
涵蓋什麼時間。主線＝前情＋現任家；子對話、心跳、群聊都看同一份前情。

顯示規則（``visible_segments``）：依寫入順序、升級前留下的舊摘要排最前面；同一來源的退任摘要與舊摘要只顯示
最新一段；來源是現任家的退任摘要與子對話回報不顯示（舊家回任；新家以前當子對話時寫的回報），剪裁
段落與舊摘要不管來源都顯示（那些原文已經不在家裡）。超過上限時最舊的折成一行；前情摘要開著時，
背景合併（``soul_prelude_merge``）把最舊的幾段合成一段（一行來源註記不合併）。

前情一律包在歷史資料界線裡（``wrap_historical_data``＋``blank_control_chars``，同舊摘要）。

SNS 來的請求（外人觸發）不帶子對話回報，吃進回報的合併段也不帶（ADR-0307）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from memory_server.utils.historical_data import wrap_historical_data
from memory_server.utils.legacy_time import format_taipei, to_epoch_ms

from src.services import destructive_ops
from src.services.prompt_blocks import ALL_BLOCK_TAGS, SOUL_PRELUDE, wrap_block
from src.utils.async_db import canonical_agent_id
from src.utils.model_text import blank_control_chars
from src.utils.soul_prelude_db import (
    PreludeSegment,
    has_segment,
    insert_segment,
    list_live_segments,
    was_invalidated,
)
from src.utils.time_utils import get_current_epoch_ms
from src.utils.token_estimator import estimate_tokens, truncate_to_tokens

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.models.sliding_window import SlidingWindowConfig
    from src.utils.soul_prelude_db import StoredSegment

logger = logging.getLogger(__name__)

# 前情最多顯示幾段；更早的折成一行
PRELUDE_MAX_SEGMENTS = 50
# 搬進前情的舊摘要最多幾 token（畫面能寫很長的 context_summary；審查 F8）
LEGACY_SUMMARY_MAX_TOKENS = 2_000
# 現任家自己的這兩種不顯示（內容就在家裡）
_HIDDEN_FROM_OWN_HOME = frozenset({"home_retired", "child_report"})
# 同一來源只顯示最新一段的種類
_NEWEST_PER_SOURCE = frozenset({"home_retired", "legacy_summary"})
_INTRO = (
    "Earlier context you carried across conversations, oldest first. Each entry says where "
    "its originals are; use conversation search when you need the details."
)
_TRIM_NOTE = (
    "[{count} older messages were trimmed from the home conversation; the originals are "
    "in the legacy conversation archive]"
)
_TRIM_NOTE_PATTERN = re.compile(re.escape(_TRIM_NOTE).replace(re.escape("{count}"), r"\d+"))
_RETIRED_NOTE = (
    "[This former home conversation has no summary; the originals are in the legacy "
    "conversation archive]"
)
_DISPLAY_CUT_NOTE = "[Truncated here to fit the prelude limit]"
# 合併段吃進子對話回報時來源欄記這個值（不會是對話編號）：SNS 請求要連它一起排除
MERGED_WITH_REPORTS = "#child-reports"


def prelude_agent(identity: AgentIdentity) -> str:
    """前情歸屬的靈魂 ID（別名換成註冊名）."""
    return canonical_agent_id(identity)


def folded_note(count: int) -> str:
    """更早段落折起來的那一行."""
    return (
        f"[{count} earlier entries are not shown here; their originals are in the legacy "
        "conversation archive]"
    )


def trim_note(count: int) -> str:
    """剪裁沒有摘要時的來源註記."""
    return _TRIM_NOTE.format(count=count)


def retired_note() -> str:
    """退任摘要沒有內容（前情摘要關閉或失敗）時的來源註記."""
    return _RETIRED_NOTE


def is_source_note(seg: StoredSegment) -> bool:
    """只是一行來源註記（沒有摘要）：合併不拿它花模型呼叫，留給折疊（審查 F15）."""
    return (seg.kind == "home_trim" and bool(_TRIM_NOTE_PATTERN.fullmatch(seg.content))) or (
        seg.kind == "home_retired" and seg.content == _RETIRED_NOTE
    )


_LEGACY_CUT_NOTE = (
    "[Truncated here to fit; the original messages behind this summary are in the legacy "
    "conversation archive]"
)


def legacy_summary_key(summary: str) -> str:
    """舊摘要的去重值：內容雜湊（版本含每次啟動隨機的值，不能用）."""
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def carries_child_reports(seg: StoredSegment) -> bool:
    """這段有子對話回報的內容（回報本身，或吃進回報的合併段）."""
    return seg.kind == "child_report" or (
        seg.kind == "merged" and seg.source_session == MERGED_WITH_REPORTS
    )


def _span(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return ""
    return f", {format_taipei(start, unknown='unknown time')} to " + format_taipei(
        end, unknown="unknown time"
    )


def _header(seg: StoredSegment) -> str:
    span = _span(seg.covers_from, seg.covers_to)
    source = seg.source_session
    headers = {
        "home_trim": f"[Trimmed from home conversation {source}{span}]",
        "legacy_summary": f"[Earlier summary carried over from conversation {source}]",
        "merged": f"[Merged summary of earlier entries{span}]",
        "home_retired": f"[Summary of former home conversation {source}{span}]",
        "child_report": f"[Report from conversation {source}{span}]",
        "handover": "[Home conversation change]",
    }
    return headers.get(seg.kind, f"[{seg.kind}]")


def render_segment(seg: StoredSegment) -> str:
    """一段前情送給模型的樣子：標頭（種類、來源、時間）＋內容."""
    return f"{_header(seg)}\n{seg.content}"


def _order(seg: StoredSegment) -> tuple[bool, int, int]:
    # 只用寫入時間：訊息時間可能沒有或比寫入早，混用會顛倒（審查 F3）；涵蓋時間只供顯示
    return (seg.kind != "legacy_summary", seg.created_at, seg.id)


def visible_segments(segments: list[StoredSegment], home_id: str | None) -> list[StoredSegment]:
    """顯示規則：排除現任家自己的退任摘要與回報、同一來源只留最新的退任摘要與舊摘要，再依寫入順序排."""
    newest: dict[tuple[str, str], StoredSegment] = {}
    for seg in segments:
        if seg.kind in _NEWEST_PER_SOURCE:
            slot = (seg.kind, seg.source_session)
            kept = newest.get(slot)
            if kept is None or (seg.created_at, seg.id) > (kept.created_at, kept.id):
                newest[slot] = seg
    shown = [
        seg
        for seg in segments
        if not (seg.kind in _HIDDEN_FROM_OWN_HOME and home_id and seg.source_session == home_id)
        and not (
            seg.kind in _NEWEST_PER_SOURCE and newest.get((seg.kind, seg.source_session)) is not seg
        )
    ]
    shown.sort(key=_order)
    return shown


def segment_cost(seg: StoredSegment) -> int:
    """一段前情的估算 token 數（含標頭）."""
    return estimate_tokens(render_segment(seg))


def _cut(seg: StoredSegment, max_tokens: int) -> StoredSegment:
    """單段就超過上限時截短內容（只影響這次送出的樣子，資料庫不動）."""
    overhead = estimate_tokens(_header(seg)) + estimate_tokens(_DISPLAY_CUT_NOTE) + 2
    content = truncate_to_tokens(seg.content, max(1, max_tokens - overhead))
    return replace(seg, content=f"{content}\n{_DISPLAY_CUT_NOTE}")


def _fit(visible: list[StoredSegment], max_tokens: int) -> tuple[list[StoredSegment], int]:
    """從最新往前放，超過上限或段數就停；最新一段一定放，單段就超過上限時截短（審查 F6、F8）."""
    shown: list[StoredSegment] = []
    total = 0
    for seg in reversed(visible):
        if len(shown) >= PRELUDE_MAX_SEGMENTS:
            break
        cost = segment_cost(seg)
        if shown and total + cost > max_tokens:
            break
        if not shown and cost > max_tokens:
            seg = _cut(seg, max_tokens)
            cost = segment_cost(seg)
        shown.append(seg)
        total += cost
    shown.reverse()
    return shown, len(visible) - len(shown)


async def migrate_legacy_summary(identity: AgentIdentity, session: dict[str, Any]) -> None:
    """把一段對話升級前的 ``context_summary`` 搬成前情（內容雜湊當去重值；重做不重複）.

    不搬的情況：同一段搬過、後來被還原或同名重建失效（F2；不看對話的範圍版本：2026-10-17 以前的家、
    換綁過的家永遠是 0，審查第 2 輪 G1）；靈魂正在重置或刪除（寫鎖裡再看一次，G6）。
    太長時截到 ``LEGACY_SUMMARY_MAX_TOKENS`` 並註明（F8）；那段對話自己照舊插全文（主線看這一輪
    前情有沒有原文照登同一段，``summaries_shown_for``）。
    """
    summary = session.get("context_summary")
    if not isinstance(summary, str) or not summary.strip():
        return
    since = destructive_ops.generation(identity)
    if since is None:
        return
    agent = prelude_agent(identity)
    source = str(session.get("id") or "")
    key = legacy_summary_key(summary)
    if await has_segment(agent, "legacy_summary", source, key) or await was_invalidated(
        agent, "legacy_summary", source, key
    ):
        return
    content = summary
    if estimate_tokens(summary) > LEGACY_SUMMARY_MAX_TOKENS:
        budget = LEGACY_SUMMARY_MAX_TOKENS - estimate_tokens(_LEGACY_CUT_NOTE) - 1
        content = f"{truncate_to_tokens(summary, budget)}\n{_LEGACY_CUT_NOTE}"
    await insert_segment(
        PreludeSegment(
            agent=agent,
            kind="legacy_summary",
            source_session=source,
            dedupe_key=key,
            content=content,
            created_at=get_current_epoch_ms(),
        ),
        skip_if=lambda: destructive_ops.disturbed(identity, since),
    )


async def prelude_view(
    identity: AgentIdentity,
    *,
    home_id: str | None,
    config: SlidingWindowConfig,
    home_session: dict[str, Any] | None = None,
    without_reports: bool = False,
) -> tuple[list[StoredSegment], int]:
    """這一輪要顯示的段落（舊到新）與折掉的段數.

    ``home_session`` 有給時，先把現任家升級前的舊摘要搬進前情（只搬一次）；上下文預覽不給，
    整個流程不寫資料。``without_reports``：SNS 來的請求，不帶子對話回報。
    """
    if home_session is not None:
        await migrate_legacy_summary(identity, home_session)
    visible = visible_segments(await list_live_segments(prelude_agent(identity)), home_id)
    if without_reports:
        visible = [seg for seg in visible if not carries_child_reports(seg)]
    return _fit(visible, config.prelude_max_tokens)


def render_prelude(shown: list[StoredSegment], folded: int) -> str | None:
    """把要顯示的段落組成 ``<soul_prelude>`` 區塊；沒有段落回 None."""
    if not shown:
        return None
    parts = ([folded_note(folded)] if folded else []) + [render_segment(seg) for seg in shown]
    historical = wrap_historical_data(
        SOUL_PRELUDE, blank_control_chars("\n\n".join(parts)), extra_tags=ALL_BLOCK_TAGS
    )
    return wrap_block(SOUL_PRELUDE, f"{_INTRO}\n{historical}")


def summaries_shown_for(shown: list[StoredSegment], session_id: str) -> frozenset[str]:
    """這一輪前情原文照登的、那段對話自己的舊摘要（截短過、被折掉、被合併的都不算）."""
    return frozenset(
        seg.content
        for seg in shown
        if seg.kind == "legacy_summary" and seg.source_session == session_id
    )


def message_span(messages: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """這些訊息涵蓋的時間（最早、最晚，epoch 毫秒）；都沒有時間時回兩個 None."""
    times = sorted(
        stamp
        for message in messages
        if isinstance(message, dict) and (stamp := to_epoch_ms(message.get("timestamp")))
    )
    return (times[0], times[-1]) if times else (None, None)


def trim_segment(
    identity: AgentIdentity,
    session_id: str,
    trimmed: list[dict[str, Any]],
    summary: str | None,
    *,
    now_ms: int,
) -> PreludeSegment:
    """剪裁寫進前情的那一段：有摘要放摘要，沒有放一行來源註記；去重值是剪到的最後一則."""
    covers_from, covers_to = message_span(trimmed)
    last_id = str(trimmed[-1].get("id", "")) if trimmed else ""
    return PreludeSegment(
        agent=prelude_agent(identity),
        kind="home_trim",
        source_session=session_id,
        dedupe_key=last_id,
        content=summary or trim_note(len(trimmed)),
        created_at=now_ms,
        covers_from=covers_from,
        covers_to=covers_to,
    )
