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
- 子對話回報：交接時寫前幾段；另外隔一段時間檢查 6 小時沒動、有新內容的。
  SNS 頻道、群聊、舊家、無痕不寫。
- 模型呼叫都在交易外；每個寫入都查重置或刪除（``destructive_ops``）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memory_server.tools import get_manager
from memory_server.utils.legacy_time import format_taipei

from src.services import destructive_ops, soul_prelude_merge
from src.services.chat_trim_wait import closing_saves
from src.services.destructive_ops import running_heartbeats
from src.services.dialogue_summarizer import has_dialogue, summarize_dialogue
from src.services.sliding_window_config import load_sliding_window_config
from src.services.soul_locks import soul_lock
from src.services.soul_prelude_service import message_span, prelude_agent, retired_note
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
# 失敗的紀錄多久再試一次；重啟後第一次觸發也試
FAILED_RETRY_AFTER_MS = 24 * 60 * 60 * 1000

_REPORT_RETRY_AFTER: dict[str, int] = {}
_retried_failed: set[int] = set()
_idle_checked: dict[str, int] = {}
_active: set[str] = set()
_again: set[str] = set()


class _Stopped(Exception):
    """這一筆不能繼續：重置或刪除開始了，或紀錄被改了（不算失敗，下一次重讀）."""


class _NothingArchived(Exception):
    """舊家有內容，舊對話庫卻一則都沒存到（算失敗，之後再試）."""


@dataclass
class _Pass:
    """一次維護共用的東西."""

    identity: AgentIdentity
    agent: str
    agents: tuple[str, str]
    config: SlidingWindowConfig
    since: int
    wrote: bool = False

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
    )


async def _handle(run: _Pass, work: Work) -> bool:
    """做一筆；回 False 表示這次維護到此為止（舊家還在忙，或重置開始了）."""
    source: dict[str, Any] | None = None
    if not work.noted:
        try:
            source = await session_service.read_handover_source(work.old_session)
        except Exception:
            logger.exception("Handover: old home %s is unreadable", work.old_session)
            claimed = await claim(work, get_current_epoch_ms(), skip_if=run.disturbed)
            if claimed is not None:
                await _record_failure(run, claimed)
            return True
        if source is not None and not source["is_home"] and _busy(run, work.old_session):
            return False
    claimed = await claim(work, get_current_epoch_ms(), skip_if=run.disturbed)
    if claimed is None:
        return not run.disturbed()
    if work.status == FAILED:
        _retried_failed.add(work.id)
    try:
        await _run_steps(run, claimed, source)
    except _Stopped:
        logger.info("Handover %s stopped; the next pass picks it up", work.id)
        return not run.disturbed()
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
    if not await finish(
        work, from_step, get_current_epoch_ms(), segment=segment, skip_if=run.disturbed
    ):
        raise _Stopped
    if segment is not None:
        run.wrote = True


async def _run_steps(run: _Pass, work: Work, source: dict[str, Any] | None) -> None:
    if work.noted:
        # 同一筆交接的只記新家可能有好幾筆指向同一段（用過、取消又設回）：同一段只寫一行
        key = f"noted-{work.old_handled_by}-{work.new_session}"
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
            segment = await _retired_segment(run, source, str(work.id))
            await _advance(
                run,
                work,
                step,
                STEP_RETIRED,
                segment=segment,
                clear_retired_for=work.old_session,
            )
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


async def _retired_segment(run: _Pass, source: dict[str, Any], key: str) -> PreludeSegment:
    """舊家的退任摘要；前情摘要關閉或失敗時寫一行來源註記."""
    linear = TreeMessageManager.get_linear_path(source["messages"])
    text = None
    if run.config.prelude_summary_enabled and linear:
        text = await summarize_dialogue(
            run.identity, linear, RETIRED_MAX_TOKENS, run.config.summary_model
        )
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
    """替有新內容的子對話寫回報，一次最多幾段；前情摘要關閉或冷卻中不寫."""
    now = get_current_epoch_ms()
    if not run.config.prelude_summary_enabled or _REPORT_RETRY_AFTER.get(run.agent, 0) > now:
        return
    chosen: list[Child] = []
    for child in await child_candidates(run.agents):
        if child.mark_scope == 0:  # 失效（同名重建、還原）
            continue
        if child.mark_agent is not None and child.mark_agent != run.agent:
            if can_be_home(child.id):  # 換過靈魂：不補寫換之前的內容
                await move_mark(child.id, run.agent, child.node, now)
            continue
        if _needs_report(run, child, now, idle_only=idle_only):
            chosen.append(child)
            if len(chosen) == REPORTS_PER_PASS:
                break
    if not chosen:
        return
    gate = asyncio.Semaphore(REPORTS_IN_PARALLEL)
    await asyncio.gather(*[_report_one(run, child, gate) for child in chosen])


def _new_messages(linear: list[dict[str, Any]], mark: str | None) -> list[dict[str, Any]]:
    """位置之後的內容；位置不在目前這條路徑（換了分支）時整條都算."""
    for index, node in enumerate(linear):
        if node.get("id") == mark:
            return linear[index + 1 :]
    return linear


async def _report_one(run: _Pass, child: Child, gate: asyncio.Semaphore) -> None:
    try:
        source = await session_service.read_handover_source(child.id)
        if source is None:
            return
        linear = TreeMessageManager.get_linear_path(source["messages"])
        if not linear:
            return
        fresh = _new_messages(linear, child.mark_node)
        node = str(linear[-1].get("id"))
        now = get_current_epoch_ms()
        if not has_dialogue(fresh):
            await move_mark(child.id, run.agent, node, now)
            return
        async with gate:
            text = await summarize_dialogue(
                run.identity, fresh, REPORT_MAX_TOKENS, run.config.summary_model
            )
        if not text:
            _REPORT_RETRY_AFTER[run.agent] = get_current_epoch_ms() + REPORT_RETRY_AFTER_MS
            logger.warning("Child report for %s failed; trying again later", child.id)
            return
        covers_from, covers_to = message_span(fresh)
        segment = PreludeSegment(
            agent=run.agent,
            kind="child_report",
            source_session=child.id,
            dedupe_key=node,
            content=text,
            created_at=get_current_epoch_ms(),
            covers_from=covers_from,
            covers_to=covers_to,
        )
        if await write_report(segment, run.agents, node, now, skip_if=run.disturbed):
            run.wrote = True
    except Exception:
        logger.exception("Child report failed for %s", child.id)
