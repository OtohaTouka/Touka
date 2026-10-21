"""清除連備份的協調：在備份鎖與工作執行緒裡依序處理四類，結束時重查、組報告、寫稽核.

只由 `services/agent_reset/orchestrator.py` 在重置完全成功（clean=True）後呼叫。整段在
``system_backup.run_backup_job`` 裡跑（持有 backup lock、shield 保護），呼叫端被取消時清除照樣跑完，
重查、報告與稽核也在工作執行緒的 finally 裡寫（不靠 await 之後的程式）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from memory_server.factual import FACTS_DB_FILE_NAME, MemoryScrubError
from memory_server.utils.collection_names import (
    QDRANT_COLLECTION_DIR,
    QDRANT_DIR_NAME,
    collection_name_for,
    names_fold_equal,
)

from src.models.memory_scrub import (
    SCRUB_CATEGORIES,
    MemoryScrubReport,
    ScrubCategoryResult,
    ScrubItemResult,
)
from src.services import system_backup
from src.services.agent_reset import primitives
from src.services.memory_scrub import leftovers, snapshots, upgrade_backups
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from src.models.identity import AgentIdentity
    from src.models.memory_scrub import ScrubCategoryName
    from src.services.memory_scrub import ScrubSession
    from src.services.memory_scrub.ports import ScrubFactStore

logger = logging.getLogger(__name__)


@dataclass
class ScrubContext:
    """一次清除需要的全部依賴（在拿任何鎖之前就取得）."""

    identity: AgentIdentity
    actor_label: str
    store: ScrubFactStore
    backups_dir: Path
    known_ids: set[str]
    own_snapshot: str | None
    started_at: float
    reset_removed: int = 0  # 這次重置清掉的記憶筆數（>0 時自己那份快照一定要真的清掉東西）


@dataclass
class ScrubProgress:
    """完成一項記一項；任何時候都能組出報告."""

    items: list[ScrubItemResult] = field(default_factory=list)
    categories: dict[str, ScrubCategoryResult] = field(
        default_factory=lambda: {name: ScrubCategoryResult(name=name) for name in SCRUB_CATEGORIES}
    )
    internal_error: bool = False
    memory_remaining: int | None = None
    recheck_failed: bool = False
    audited: bool = False

    def category(
        self, name: ScrubCategoryName, work: Callable[[], Iterable[ScrubItemResult]]
    ) -> None:
        """跑一個類別；類別本身出錯也一定留下一個 kind=category 的失敗項目."""

        def consume() -> None:
            for item in work():  # 完成一項記一項：中途出錯時已完成的留著
                self.items.append(item)

        reason = _attempt(consume)
        if reason is None:
            self.categories[name] = ScrubCategoryResult(name=name, state="done")
            return
        self.categories[name] = ScrubCategoryResult(name=name, state="failed", reason=reason)
        self.items.append(
            ScrubItemResult(kind="category", name=name, status="failed", reason=reason)
        )

    def recheck(self, count: Callable[[], int]) -> None:
        """重查線上殘留；失敗記成未知."""
        holder: list[int] = []
        if _attempt(lambda: holder.append(count())) is None:
            self.memory_remaining = holder[0]
        else:
            self.recheck_failed = True

    def build(self, own_snapshot: str | None, reset_removed: int = 0) -> MemoryScrubReport:
        """組報告（complete 與 reason 依 MemoryScrubReport.build 的規則）."""
        return MemoryScrubReport.build(
            categories=list(self.categories.values()),
            items=list(self.items),
            own_snapshot=own_snapshot,
            memory_remaining=self.memory_remaining,
            internal_error=self.internal_error,
            recheck_failed=self.recheck_failed,
            reset_removed=reset_removed,
        )


def _attempt(action: Callable[[], object]) -> str | None:
    """跑一段工作；成功回 None，失敗回原因代碼（意外只記日誌）."""
    try:
        action()
    except MemoryScrubError as exc:
        return exc.reason.value
    except OSError:
        logger.warning("[memory-scrub] listing failed", exc_info=True)
        return "list_failed"
    except Exception:  # noqa: BLE001 - 類別與重查的邊界：意外只記日誌、回報代碼
        logger.exception("[memory-scrub] unexpected failure")
        return "internal_error"
    return None


def _live(ctx: ScrubContext) -> list[ScrubItemResult]:
    """線上記憶庫整理；再核對磁碟上沒有留著目標的向量 collection 資料夾."""
    try:
        done = ctx.store.compact_after_reset(ctx.identity)
    except MemoryScrubError as exc:
        reason = exc.reason.value
        live = ScrubItemResult(
            kind="live_store", name=FACTS_DB_FILE_NAME, status="failed", reason=reason
        )
    except OSError:
        logger.warning("[memory-scrub] live store unreadable", exc_info=True)
        live = ScrubItemResult(
            kind="live_store", name=FACTS_DB_FILE_NAME, status="failed", reason="unreadable"
        )
    else:
        status = "scrubbed" if done else "not_needed"
        live = ScrubItemResult(kind="live_store", name=FACTS_DB_FILE_NAME, status=status)
    return [live, *_leftover_collections(ctx)]


def _leftover_collections(ctx: ScrubContext) -> list[ScrubItemResult]:
    """向量庫說 collection 不在了還不夠：資料夾若刪不掉（Windows 上被占用），舊點仍在磁碟上."""
    root = ctx.store.base_dir / QDRANT_DIR_NAME / QDRANT_COLLECTION_DIR
    kind = system_backup.path_kind(root)
    if kind == "missing":
        return []
    if kind != "dir":  # 連結或 junction：不跟進去，也不能當成沒有殘留（和其他三類同一套標準）
        return [
            ScrubItemResult(
                kind="live_store", name=root.name, status="failed", reason="live_verify_failed"
            )
        ]
    target = collection_name_for(ctx.identity)
    return [
        ScrubItemResult(
            kind="live_store", name=entry.name, status="failed", reason="live_verify_failed"
        )
        for entry in sorted(root.iterdir(), key=lambda p: p.name)
        if names_fold_equal(entry.name, target)
    ]


def run_memory_scrub(ctx: ScrubContext, progress: ScrubProgress) -> MemoryScrubReport:
    """工作執行緒裡跑：四類、重查、組報告、寫稽核；finally 保證稽核一定寫."""
    report: MemoryScrubReport | None = None
    try:
        progress.category(
            "leftovers", lambda: leftovers.iter_snapshot_leftovers(ctx.backups_dir, ctx.started_at)
        )
        progress.category("live", lambda: _live(ctx))
        progress.category(
            "upgrade_backups",
            lambda: upgrade_backups.iter_upgrade_backups(
                ctx.identity, ctx.store.upgrade_backup_dir
            ),
        )
        progress.category(
            "snapshots",
            lambda: snapshots.iter_snapshots(ctx.identity, ctx.backups_dir, ctx.known_ids),
        )
    finally:
        progress.recheck(lambda: primitives.count_remaining_memory(ctx.identity))
        report = progress.build(ctx.own_snapshot, ctx.reset_removed)
        audit_result(report, ctx.actor_label, ctx.identity.agent_id)
        progress.audited = True
    return report


async def run_after_reset(
    identity: AgentIdentity,
    *,
    actor_label: str,
    store: ScrubFactStore,
    known_ids: set[str],
    own_snapshot: str | None,
    session: ScrubSession,
    reset_removed: int = 0,
) -> MemoryScrubReport:
    """重置成功後清除；任何一般意外都改成 ran=True、complete=False 的報告，不往外丟."""
    progress = ScrubProgress()
    try:  # 準備清除（例如解析快照目錄）出錯也在安全網裡：重置已經成功，照常回報告
        ctx = ScrubContext(
            identity=identity,
            actor_label=actor_label,
            store=store,
            backups_dir=system_backup.backup_dir(),
            known_ids=known_ids,
            own_snapshot=own_snapshot,
            started_at=get_current_epoch(),
            reset_removed=reset_removed,
        )

        def job() -> MemoryScrubReport:
            try:
                return run_memory_scrub(ctx, progress)
            finally:
                session.end()

        session.handed_over = True
        return await system_backup.run_backup_job(job)
    except asyncio.CancelledError:
        raise  # 工作在 shield 裡照樣跑完，稽核與清旗標由工作自己做
    except Exception:  # noqa: BLE001 - 清除開始後的安全網：回報告（照常 200），不讓端點 500
        logger.exception("[memory-scrub] scrub job failed outside its own boundaries")
        progress.internal_error = True
        report = progress.build(own_snapshot, reset_removed)
        if not progress.audited:  # 工作沒走到寫稽核那一步才補
            audit_result(report, actor_label, identity.agent_id)
        session.end()
        return report


def _counts(items: list[ScrubItemResult], kind: str) -> tuple[int, int]:
    mine = [i for i in items if i.kind == kind]
    return sum(1 for i in mine if i.status in ("scrubbed", "not_needed")), len(mine)


def audit_result(report: MemoryScrubReport, actor_label: str, target: str) -> None:
    """結果稽核：一行固定格式，只有 ID、檔名、代碼與筆數（沒有內容、沒有路徑）."""
    live = next(
        (
            i.status if i.status != "failed" else f"failed:{i.reason}"
            for i in report.items
            if i.kind == "live_store"
        ),
        "none",
    )
    upgrade_ok, upgrade_total = _counts(report.items, "upgrade_backup")
    snap_ok, snap_total = _counts(report.items, "snapshot")
    # 檔名一律 repr：含換行或引號的檔名不能偽造出另一行稽核
    failed = ",".join(f"{i.name!r}={i.reason}" for i in report.items if i.status == "failed")
    info = ",".join(f"{i.name!r}={i.reason}:{i.count}" for i in report.items if i.kind == "info")
    logger.warning(
        "AUDIT: memory-scrub result actor=%r target=%r requested=%s ran=%s complete=%s reason=%s"
        " memory_remaining=%s own_snapshot=%r live=%s upgrade=%d/%d snapshots=%d/%d"
        " facts_removed=%d collections_removed=%d leftovers=%d failed=[%s] info=[%s]",
        actor_label,
        target,
        report.requested,
        report.ran,
        report.complete,
        report.reason,
        report.memory_remaining,
        report.own_snapshot,
        live,
        upgrade_ok,
        upgrade_total,
        snap_ok,
        snap_total,
        sum(i.facts_removed for i in report.items),
        sum(1 for i in report.items if i.collection_removed),
        report.leftovers_removed,
        failed,
        info,
    )


def audit_rejected(actor_label: str, target: str, reason: str) -> None:
    """勾選清除、還沒開始重置就被擋下的稽核（格式只有這一份；路由也經這裡寫）.

    reason：not_human、not_acknowledged、not_confirmed、bad_origin、bad_password、
    restore_in_progress、reset_in_progress、not_resettable、collection_shared、unavailable、
    internal_error。重置開始之後的結局（含取消、備份失敗）一律寫結果行。
    """
    logger.warning(
        "AUDIT: memory-scrub rejected actor=%r target=%r reason=%s", actor_label, target, reason
    )
