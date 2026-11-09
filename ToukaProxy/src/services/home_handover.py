"""前情維護：換家交接與子對話回報（3d PR 2b，ADR-0307）.

換家時（``home_transitions``，交易裡）只記一筆紀錄；真正的交接在這裡，由禁令 28 的兩處在背景排：
網頁、IDE 的一輪存好之後（綁靈魂的對話，非無痕），以及心跳結束時（至少完成一個脈衝）。

- 每個靈魂最多一個維護在跑；跑的時候再來的只留記號，跑完再檢查一次（``schedule``）。
- 紀錄依編號一筆筆做（靈魂鎖）。步驟：舊家整棵樹進舊對話庫 → 退任摘要 → 子對話回報 → 換家標記 →
  完成。``step`` 記做到哪，中途停下時下一次接著做；舊對話庫只新增、段落有唯一索引，重做不重複。
- 新家還沒開始用的紀錄不做（改回去要能當沒換過）。舊家還有回覆、心跳在跑或收尾保存在等時先停，
  等下一次觸發；舊家已經又是現任家時不等、退任摘要標待補。
- 只記新家的紀錄只寫換家標記，不讀舊家。
- 累計失敗三次標成失敗、寫一行註記、繼續做後面的；失敗的每次啟動後第一次觸發、或滿 24 小時再試。
  退任摘要失敗不算交接失敗，放回佇列隔一段時間再試，試到第三次才寫來源註記。
- 子對話回報：交接時寫前幾段；另外隔一段時間檢查 6 小時沒動、有新內容的。一次維護總共最多幾段。
  SNS 頻道、群聊、舊家、無痕不寫。同一段送出去卻一直寫不出摘要時，試幾次就寫一行註記、位置移到當下；
  請求根本沒送出（原生路線沒金鑰、超預算、模型不可用）不算這一段的失敗，這個靈魂冷卻一段時間再試。
- 模型呼叫都在交易外；每個寫入都查重置或刪除（``destructive_ops``）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memory_server.tools import get_manager
from memory_server.utils.legacy_time import format_taipei, to_epoch_ms

from src.services import destructive_ops, soul_prelude_merge
from src.services.chat_trim_wait import closing_saves
from src.services.destructive_ops import running_heartbeats
from src.services.dialogue_summarizer import NOT_SENT_FAILURES, has_dialogue, summarize_dialogue
from src.services.reply_in_flight import is_replying
from src.services.sliding_window_config import load_sliding_window_config
from src.services.soul_locks import soul_lock
from src.services.soul_prelude_service import (
    message_span,
    prelude_agent,
    report_note,
    retired_note,
)
from src.services.stream_buffer_service import stream_buffer_service
from src.utils.async_db import session_service, soul_session_agents
from src.utils.history_manager import TreeMessageManager
from src.utils.home_handover_db import (
    HANDOVER_MAX_FAILURES,
    STEP_ARCHIVED,
    STEP_MARKED,
    STEP_REPORTED,
    STEP_RETIRED,
    advance,
    child_candidates,
    claim,
    finish,
    list_pending_retired,
    list_queue,
    move_mark,
    record_failure,
    release,
    report_exists,
    write_deferred_retired,
    write_note,
    write_report,
)
from src.utils.home_transitions import FAILED, RUNNING
from src.utils.interjection_notes import annotate_node
from src.utils.session_scope import can_be_home
from src.utils.soul_prelude_db import PreludeSegment
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch, get_current_epoch_ms

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.models.sliding_window import SlidingWindowConfig
    from src.utils.home_handover_db import Child, Work

logger = logging.getLogger(__name__)

# 退任摘要、子對話回報的輸出上限（token）
RETIRED_MAX_TOKENS = 2_000
REPORT_MAX_TOKENS = 800
# 一次最多寫幾段回報、同時送幾段
REPORTS_PER_PASS = 5
REPORTS_IN_PARALLEL = 3
# 子對話多久沒動算閒置（毫秒）
CHILD_IDLE_MS = 6 * 60 * 60 * 1000
# 閒置子對話多久檢查一次：候選要解析這個靈魂每段子對話的內容，不必每一輪都做（記在記憶體）
IDLE_CHECK_EVERY_MS = 30 * 60 * 1000
# 回報摘要失敗後同一個靈魂多久再試（記在記憶體，重啟就清掉）
REPORT_RETRY_AFTER_MS = 6 * 60 * 60 * 1000
# 同一段子對話寫不出回報幾次就放棄（寫一行註記、位置移到當下；記在記憶體，重啟就清掉）
REPORT_MAX_FAILURES = 3
# 退任摘要失敗後多久再試、總共試幾次（記在記憶體，重啟就清掉）；試完還是失敗才寫來源註記
RETIRED_RETRY_AFTER_MS = 60 * 60 * 1000
RETIRED_MAX_TRIES = 3
# 失敗的紀錄多久再試一次；重啟後第一次觸發也試
FAILED_RETRY_AFTER_MS = 24 * 60 * 60 * 1000

_REPORT_RETRY_AFTER: dict[str, int] = {}
# （靈魂、子對話）→（失敗次數、這個時間之前不再試）
_report_failures: dict[tuple[str, str], tuple[int, int]] = {}
# 紀錄編號 →（退任摘要失敗次數、這個時間之前不再試）
_retired_failures: dict[int, tuple[int, int]] = {}
_retried_failed: set[int] = set()
_idle_checked: dict[str, int] = {}
_active: set[str] = set()
_again: set[str] = set()


class _Stopped(Exception):
    """這一筆不能繼續：重置或刪除開始了，或紀錄被改了（不算失敗，下一次重讀）."""


class _NothingArchived(Exception):
    """舊家有內容，舊對話庫卻一則都沒存到（算失敗，之後再試）."""


class _SummaryFailed(Exception):
    """退任摘要這次寫不出來（不算交接失敗，隔一段時間再試；試到最後一次才寫來源註記）."""


@dataclass
class _Pass:
    """一次維護共用的東西."""

    identity: AgentIdentity
    agent: str
    agents: tuple[str, str]
    config: SlidingWindowConfig
    since: int
    wrote: bool = False
    reports: int = 0

    def disturbed(self) -> bool:
        return destructive_ops.disturbed(self.identity, self.since)


def schedule(identity: AgentIdentity) -> None:
    """排一次前情維護（禁令 28 的兩處呼叫）；已經有一個在跑就留記號，不會同時跑兩個."""
    agent = prelude_agent(identity)
    if agent in _active:
        _again.add(agent)
        return
    _active.add(agent)
    spawn_logged(_run(identity, agent), name=f"home-maintenance:{agent}")


async def _run(identity: AgentIdentity, agent: str) -> None:
    try:
        while True:
            _again.discard(agent)
            try:
                await maintain(identity)
            except Exception:
                logger.exception("Home maintenance failed for %s", agent)
            if agent not in _again:
                return
    finally:
        _active.discard(agent)
        _again.discard(agent)


async def maintain(identity: AgentIdentity) -> None:
    """做一次維護：交接佇列、補寫退任摘要、閒置子對話回報；有寫新段落就試著合併前情."""
    agent = prelude_agent(identity)
    run: _Pass | None = None
    async with soul_lock(agent):
        since = destructive_ops.generation(identity)
        if since is None:  # 正在刪除、重置或清除
            return
        run = _Pass(
            identity,
            agent,
            soul_session_agents(identity),
            load_sliding_window_config(),
            since,
        )
        await _process_queue(run)
        await _write_deferred_retired(run)
        await _report_idle_children(run)
    if run.wrote:  # 鎖外做：合併自己會拿靈魂鎖
        await soul_prelude_merge.merge_if_needed(
            identity, run.config, home_id=await session_service.get_home_session_id(identity)
        )


async def _process_queue(run: _Pass) -> None:
    now = get_current_epoch_ms()
    for work in await list_queue(run.agent):
        if not _is_due(work, now):
            continue
        if not await _handle(run, work):
            return


def _is_due(work: Work, now: int) -> bool:
    if _retired_failures.get(work.id, (0, 0))[1] > now:
        return False
    if work.status == FAILED:
        return work.id not in _retried_failed or now - work.updated_at >= FAILED_RETRY_AFTER_MS
    return (
        work.status == RUNNING
        or work.step > 0
        or work.new_session is None
        or bool(work.new_started)
    )


def _busy(run: _Pass, session_id: str) -> bool:
    """這段對話還有回覆、心跳在跑，或收尾保存還在等."""
    return (
        stream_buffer_service.is_busy(session_id)
        or session_id in running_heartbeats(run.identity)
        or closing_saves.is_waiting(session_id)
        or is_replying(session_id)
    )


async def _claim(run: _Pass, work: Work) -> Work | None:
    claimed = await claim(work, get_current_epoch_ms(), skip_if=run.disturbed)
    if claimed is not None and work.status == FAILED:
        _retried_failed.add(work.id)
    return claimed


async def _handle(run: _Pass, work: Work) -> bool:
    """做一筆；回 False 表示這次維護到此為止（舊家還在忙，或重置開始了）."""
    if not work.noted:
        try:
            source = await session_service.read_handover_source(work.old_session)
        except Exception:
            logger.exception("Handover: old home %s is unreadable", work.old_session)
            claimed = await _claim(run, work)
            if claimed is not None:
                await _record_failure(run, claimed)
            return True
        if source is not None and not source["is_home"] and _busy(run, work.old_session):
            return False
    claimed = await _claim(run, work)
    if claimed is None:
        return not run.disturbed()
    try:
        # 認領之後存檔閘才關上：這時讀到的才是最終內容（上面那次只用來判斷要不要等）
        source = (
            None if work.noted else await session_service.read_handover_source(work.old_session)
        )
        await _run_steps(run, claimed, source)
    except _Stopped:
        logger.info("Handover %s stopped; the next pass picks it up", work.id)
        return not run.disturbed()
    except _SummaryFailed:
        tries = _retired_failures.get(work.id, (0, 0))[0] + 1
        _retired_failures[work.id] = (tries, get_current_epoch_ms() + RETIRED_RETRY_AFTER_MS)
        await release(claimed, get_current_epoch_ms())
    except Exception:
        logger.exception("Handover %s failed", work.id)
        await _record_failure(run, claimed)
    return True


async def _record_failure(run: _Pass, work: Work) -> None:
    failures, status = await record_failure(
        work, get_current_epoch_ms(), max_failures=HANDOVER_MAX_FAILURES
    )
    if status == FAILED:
        written = await write_note(
            _handover_segment(
                run,
                work,
                f"failed-{work.id}",
                f"[Moving former home conversation {work.old_session} to the archive failed "
                f"{failures} times; it stays read-only and will be retried]",
            ),
            skip_if=run.disturbed,
        )
        run.wrote = run.wrote or written


def _handover_segment(run: _Pass, work: Work, key: str, content: str) -> PreludeSegment:
    return PreludeSegment(
        agent=run.agent,
        kind="handover",
        source_session=work.old_session,
        dedupe_key=key,
        content=content,
        created_at=get_current_epoch_ms(),
    )


def _marker(work: Work) -> str:
    """一行換家標記；只記新家的紀錄不提舊家（可能已刪或已是普通對話）."""
    when = format_taipei(work.created_at)
    if work.new_session is None:
        return f"Home was unset on {when} (former home: {work.old_session})."
    if work.noted:
        return f"{work.new_session} became the home conversation on {when}."
    return f"Home moved from {work.old_session} to {work.new_session} on {when}."


async def _advance(run: _Pass, work: Work, from_step: int, to_step: int, **kwargs: Any) -> None:
    done = await advance(
        work,
        from_step,
        to_step,
        get_current_epoch_ms(),
        agent=run.agent,
        skip_if=run.disturbed,
        **kwargs,
    )
    if not done:
        raise _Stopped
    if kwargs.get("segment") is not None:
        run.wrote = True


async def _finish(run: _Pass, work: Work, from_step: int, segment: PreludeSegment | None) -> None:
    stale_note = f"failed-{work.id}" if work.failures >= HANDOVER_MAX_FAILURES else None
    if not await finish(
        work,
        from_step,
        get_current_epoch_ms(),
        segment=segment,
        stale_note=stale_note,
        agent=run.agent,
        skip_if=run.disturbed,
    ):
        raise _Stopped
    if segment is not None:
        run.wrote = True


async def _run_steps(run: _Pass, work: Work, source: dict[str, Any] | None) -> None:
    if work.noted:
        # 同一筆交接的只記新家可能有好幾筆指向同一段（用過、取消又設回）：同一段只寫一行；
        # 第 0 筆（沒有前任）沒有「同一筆交接」可言，一筆一行
        scope = work.old_handled_by or f"first-{work.id}"
        key = f"noted-{scope}-{work.new_session}"
        await _finish(run, work, work.step, _handover_segment(run, work, key, _marker(work)))
        return
    if source is None:
        await _finish(
            run,
            work,
            work.step,
            _handover_segment(
                run,
                work,
                f"missing-{work.id}",
                f"[Former home conversation {work.old_session} no longer exists; "
                "its original text is gone]",
            ),
        )
        return
    step = work.step
    if step < STEP_ARCHIVED:
        await _archive_old_home(run, work.id, source)
        await _advance(run, work, step, STEP_ARCHIVED)
        step = STEP_ARCHIVED
    if step < STEP_RETIRED:
        if source["is_home"]:  # 已經又是現任家：退任摘要延後
            await _advance(run, work, step, STEP_RETIRED, retired_pending=True)
        else:
            last_try = _retired_failures.get(work.id, (0, 0))[0] >= RETIRED_MAX_TRIES - 1
            segment = await _retired_segment(run, source, str(work.id), last_try=last_try)
            await _advance(
                run,
                work,
                step,
                STEP_RETIRED,
                segment=segment,
                clear_retired_for=work.old_session,
            )
            _retired_failures.pop(work.id, None)
        step = STEP_RETIRED
    if step < STEP_REPORTED:
        await _write_reports(run)
        await _advance(run, work, step, STEP_REPORTED)
        step = STEP_REPORTED
    if step < STEP_MARKED:
        await _advance(
            run,
            work,
            step,
            STEP_MARKED,
            segment=_handover_segment(run, work, str(work.id), _marker(work)),
        )
        step = STEP_MARKED
    await _finish(run, work, step, None)


def _archive(identity: AgentIdentity, payload: list[dict[str, Any]], label: str) -> int:
    return get_manager().get_legacy_sync_service().archive_payload(identity, payload, label)


def _export_node(node: dict[str, Any]) -> dict[str, Any]:
    """一個節點進舊對話庫的樣子：插話併進內容，工具步驟等其他欄位留在附註."""
    exported = {key: value for key, value in annotate_node(node).items() if key != "childrenIds"}
    if exported.get("content") is None:
        exported["content"] = ""
    return exported


async def _archive_old_home(run: _Pass, work_id: int, source: dict[str, Any]) -> None:
    """舊家整棵樹進舊對話庫（含分支）；只新增，同一則只存一次."""
    if run.disturbed():
        raise _Stopped
    nodes = source["messages"].get("messages", {})
    if not nodes:
        return
    payload = [
        {
            "id": source["id"],
            "title": source.get("title") or source["id"],
            "agent_id": run.identity.agent_id,
            "exported_at": int(get_current_epoch()),
            "messages": [_export_node(node) for node in nodes.values() if isinstance(node, dict)],
        }
    ]
    label = f"{run.identity.agent_id}_handover_{work_id}.json"
    archived = await asyncio.to_thread(_archive, run.identity, payload, label)
    if not archived:
        raise _NothingArchived


async def _retired_segment(
    run: _Pass, source: dict[str, Any], key: str, *, last_try: bool = True
) -> PreludeSegment:
    """舊家的退任摘要；前情摘要關閉，或這是最後一次機會還是失敗時，寫一行來源註記."""
    if run.disturbed():
        raise _Stopped
    linear = TreeMessageManager.get_linear_path(source["messages"])
    text = None
    if run.config.prelude_summary_enabled and linear:
        text = await summarize_dialogue(
            run.identity, linear, RETIRED_MAX_TOKENS, run.config.summary_model
        )
        if not text and not last_try and has_dialogue(linear):
            raise _SummaryFailed
    covers_from, covers_to = message_span(linear)
    return PreludeSegment(
        agent=run.agent,
        kind="home_retired",
        source_session=source["id"],
        dedupe_key=key,
        content=text or retired_note(),
        created_at=get_current_epoch_ms(),
        covers_from=covers_from,
        covers_to=covers_to,
    )


async def _write_deferred_retired(run: _Pass) -> None:
    """補寫延後的退任摘要：那段舊家已不是現任家，也沒有別筆紀錄會寫."""
    for old_session, record_id in await list_pending_retired(run.agent):
        try:
            source = await session_service.read_handover_source(old_session)
            if source is None:
                await write_deferred_retired(
                    run.agent, old_session, None, get_current_epoch_ms(), skip_if=run.disturbed
                )
                continue
            if source["is_home"] or _busy(run, old_session):
                continue
            await _archive_old_home(run, record_id, source)  # 補上歸檔之後被改過的內容
            segment = await _retired_segment(run, source, f"deferred-{record_id}")
            if await write_deferred_retired(
                run.agent, old_session, segment, get_current_epoch_ms(), skip_if=run.disturbed
            ):
                run.wrote = True
        except _Stopped:
            return
        except Exception:
            logger.exception("Deferred retired summary failed for %s", old_session)


async def _report_idle_children(run: _Pass) -> None:
    now = get_current_epoch_ms()
    last = _idle_checked.get(run.agent)
    if last is not None and now - last < IDLE_CHECK_EVERY_MS:
        return
    _idle_checked[run.agent] = now
    await _report_children(run, idle_only=True)


async def _write_reports(run: _Pass) -> None:
    await _report_children(run, idle_only=False)


def _needs_report(run: _Pass, child: Child, now: int, *, idle_only: bool) -> bool:
    if not can_be_home(child.id) or child.node is None or child.node == child.mark_node:
        return False
    if child.mark_agent is None and child.scope_version != 1:
        return False  # 沒有位置的舊資料：升級時既有子對話都建了位置，這種不是新內容
    if _busy(run, child.id):
        return False
    return not idle_only or (child.updated_at or 0) <= now - CHILD_IDLE_MS


async def _report_children(run: _Pass, *, idle_only: bool) -> None:
    """替有新內容的子對話寫回報，一次維護總共最多幾段；前情摘要關閉或冷卻中不寫."""
    now = get_current_epoch_ms()
    room = REPORTS_PER_PASS - run.reports
    if (
        room <= 0
        or run.disturbed()
        or not run.config.prelude_summary_enabled
        or _REPORT_RETRY_AFTER.get(run.agent, 0) > now
    ):
        return
    chosen: list[Child] = []
    for child in await child_candidates(run.agents):
        if child.mark_scope == 0:  # 失效（同名重建、還原）
            continue
        if child.mark_agent is not None and child.mark_agent != run.agent:
            if can_be_home(child.id):  # 升級前換過靈魂的舊資料：不補寫換之前的內容
                await move_mark(
                    child.id, run.agents, run.agent, child.node, now, skip_if=run.disturbed
                )
            continue
        if _report_failures.get((run.agent, child.id), (0, 0))[1] > now:  # 這段失敗過：不佔名額
            continue
        if _needs_report(run, child, now, idle_only=idle_only):
            chosen.append(child)
            if len(chosen) == room:
                break
    if not chosen:
        return
    run.reports += len(chosen)
    gate = asyncio.Semaphore(REPORTS_IN_PARALLEL)
    await asyncio.gather(*[_report_one(run, child, gate) for child in chosen])


def _new_messages(
    tree: dict[str, Any], linear: list[dict[str, Any]], child: Child
) -> list[dict[str, Any]]:
    """這次要回報的內容.

    位置在目前路徑上：位置之後的。位置在別的分支（重新生成、切分支）：兩條路分岔之後的；
    完全沒有共同祖先才整條都算。位置那則已被刪（刪了最後一輪）：看不出分岔點，算位置移動之後
    才有的（節點時間可能來自使用者裝置，寧可少報）。
    """
    if child.mark_node is None:
        return linear
    nodes = tree.get("messages", {})
    if child.mark_node not in nodes:
        marked_at = child.mark_at or 0
        return [node for node in linear if (to_epoch_ms(node.get("timestamp")) or 0) > marked_at]
    on_path = {node.get("id"): index for index, node in enumerate(linear)}
    cursor: str | None = child.mark_node
    seen: set[str] = set()
    while cursor in nodes and cursor not in seen:
        if cursor in on_path:
            return linear[on_path[cursor] + 1 :]
        seen.add(cursor)
        cursor = nodes[cursor].get("parentId")
    return linear


def _report_segment(
    run: _Pass, child_id: str, node: str, text: str, messages: list[dict[str, Any]]
) -> PreludeSegment:
    covers_from, covers_to = message_span(messages)
    return PreludeSegment(
        agent=run.agent,
        kind="child_report",
        source_session=child_id,
        dedupe_key=node,
        content=text,
        created_at=get_current_epoch_ms(),
        covers_from=covers_from,
        covers_to=covers_to,
    )


async def _write_report(run: _Pass, child: Child, gate: asyncio.Semaphore) -> bool:
    """寫這段子對話的回報；送出去卻寫不出摘要時回 False，其他情況回 True.

    請求根本沒送出（原生路線沒金鑰、超預算、模型不可用）不算這一段的失敗：靈魂冷卻，恢復後照常補寫。
    """
    source = await session_service.read_handover_source(child.id)
    if source is None:
        return True
    linear = TreeMessageManager.get_linear_path(source["messages"])
    if not linear:
        return True
    fresh = _new_messages(source["messages"], linear, child)
    node = str(linear[-1].get("id"))
    now = get_current_epoch_ms()
    if not has_dialogue(fresh) or await report_exists(run.agent, child.id, node):
        await move_mark(child.id, run.agents, run.agent, node, now, skip_if=run.disturbed)
        return True
    reasons: set[str] = set()
    async with gate:
        if run.disturbed():
            return True
        text = await summarize_dialogue(
            run.identity,
            fresh,
            REPORT_MAX_TOKENS,
            run.config.summary_model,
            on_failure=reasons.add,
        )
    if not text:
        logger.warning("Child report for %s failed; trying again later", child.id)
        if reasons - NOT_SENT_FAILURES:
            return False
        _REPORT_RETRY_AFTER[run.agent] = get_current_epoch_ms() + REPORT_RETRY_AFTER_MS
        return True
    _report_failures.pop((run.agent, child.id), None)
    segment = _report_segment(run, child.id, node, text, fresh)
    if await write_report(segment, run.agents, node, now, skip_if=run.disturbed):
        run.wrote = True
    return True


async def _report_failed(run: _Pass, child: Child) -> None:
    """這次寫不出回報：這段晚點再試；累計太多次就放棄，寫一行註記、位置移到當下."""
    key = (run.agent, child.id)
    failures = _report_failures.get(key, (0, 0))[0] + 1
    now = get_current_epoch_ms()
    if failures < REPORT_MAX_FAILURES or child.node is None:
        _report_failures[key] = (failures, now + REPORT_RETRY_AFTER_MS)
        return
    _report_failures.pop(key, None)
    segment = _report_segment(run, child.id, child.node, report_note(), [])
    if await write_report(segment, run.agents, child.node, now, skip_if=run.disturbed):
        run.wrote = True


async def _report_one(run: _Pass, child: Child, gate: asyncio.Semaphore) -> None:
    try:
        if not await _write_report(run, child, gate):
            await _report_failed(run, child)
    except Exception:
        logger.exception("Child report failed for %s", child.id)
        try:
            await _report_failed(run, child)
        except Exception:
            logger.exception("Child report note failed for %s", child.id)
