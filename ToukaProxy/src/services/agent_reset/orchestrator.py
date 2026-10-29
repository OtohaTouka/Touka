"""DebugAgent 破壞性重置協調器（軌B Batch 6，Phase 3）。

流程：① 過安全閘硬鎖 DebugAgent → ② **備份優先**（do_backup 時先全系統備份，失敗即中止、
絕不在無備份下刪除）→ ③ 依 `ALL_STORES` registry 逐一 purge（best-effort，單 store 失敗收進
報告不中斷）→ ④ 完整性自檢（清完仍有殘留→標 false-clean，防漏 store）。

刻意**只清資料、不動身分/註冊**（ALL_STORES 不含 identity_map/users/registry），確保 DebugAgent
重置後仍可用。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from memory_server import factual
from memory_server.factual import FACTS_BACKUP_DIR_NAME
from memory_server.utils.no_follow import LinkRefusedError

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.memory_scrub import MemoryScrubReport
from src.services import destructive_ops, memory_collection_guard, memory_scrub, system_backup
from src.services.agent_reset import last_result, primitives
from src.services.agent_reset.guard import assert_agent_resettable, assert_resettable
from src.services.agent_reset.primitives import ALL_STORES, RETAINED_STORES
from src.services.authorization_gateway import require_admin_human
from src.services.memory_scrub import orchestrator as scrub_orchestrator
from src.services.resource_manager import resource_manager
from src.tools import terminal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from memory_server.utils.zone_paths import ZonePath

    from src.models.memory_scrub import ScrubReason
    from src.services.memory_scrub.ports import ScrubFactStore

logger = logging.getLogger(__name__)

# 破壞性重置序列化鎖（ADR-0104 對抗式複審 R7）：避免兩個 reset 並行、或 reset 與
# 進行中的計分卡互踩造成 TOCTOU（清完→殘留→自檢仍報 clean）。第二個 reset 直接拒絕。
_reset_lock = asyncio.Lock()


def reset_in_progress() -> bool:
    """重置、清除或刪除靈魂是否正在進行（還原端點據此擋下；重置被取消後清除可能還在跑）."""
    return _reset_lock.locked() or memory_scrub.in_progress()


@asynccontextmanager
async def destructive_op(target: AgentIdentity) -> AsyncIterator[None]:
    """不等待地拿全系統一把的破壞性操作鎖，並標記目標（舊審查報告丙-4，ADR-0287）.

    重置、清除或刪除已經在跑就回 409 `AGENT_RESET_IN_PROGRESS`。標記期間這個靈魂的心跳
    不開跑、在下一個脈衝前停下。檢查、取鎖、標記之間沒有讓出事件迴圈的地方。

    前提：產品程式只在這裡取 `_reset_lock`，所以鎖沒被持有時也不會有人在等，`acquire()`
    立刻拿到、不讓出。別處若改成會等的 `async with _reset_lock`，這裡就可能變成排隊。
    """
    if reset_in_progress():
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)
    await _reset_lock.acquire()
    destructive_ops.hold(target)
    try:
        yield
    finally:
        destructive_ops.release(target)
        _reset_lock.release()


def refuse_linked_drive(drive: ZonePath | None) -> None:
    """破壞性操作的前置檢查（ADR-0293 R11）：個人空間根是連結就丟 ``AGENT_DRIVE_LINKED``.

    上層（``custom/<id>``）本身是連結時 ``zone_kind`` 丟連結拒絕，轉成同一個錯誤。
    """
    try:
        linked = drive is not None and resource_manager.zone_kind(drive) == "link"
    except LinkRefusedError as exc:
        raise ToukaException(ErrorCode.AGENT_DRIVE_LINKED) from exc
    if linked:
        raise ToukaException(ErrorCode.AGENT_DRIVE_LINKED)


def scrub_rejected(actor_label: str, target: str, reason: str) -> None:
    """勾選清除卻被擋下的稽核（路由用；格式只有清除協調器那一份）."""
    scrub_orchestrator.audit_rejected(actor_label, target, reason)


@dataclass
class StoreResult:
    """單一 store 的清除結果＋殘留（完整性自檢）。"""

    name: str
    purged: int = 0
    remaining: int = 0
    error: str = ""


@dataclass
class ResetReport:
    """一次破壞性重置的彙整報告（含備份資訊與完整性）。"""

    agent: str  # 已正規化目標 id（DebugAgent 或 per-agent 目標）；命名避 IDENTITY_SSOT 欄位禁則
    backup: dict[str, str | None] | None = None
    stores: list[StoreResult] = field(default_factory=list)
    clean: bool = False  # True iff 全部 store 殘留 0 且無錯誤
    scrub: MemoryScrubReport | None = None  # 勾選清除連備份時的結果

    def to_dict(self) -> dict[str, Any]:
        """序列化供端點回傳。

        `retained` 明列刻意保留、未清除的 store（稽核帳本等），使 `clean=True` 不被誤讀為
        「連審計軌跡也清光」（ADR-0104 R3/R4/R12 決策：保留並透明揭露）。清除連備份有跑時，
        升級備份那一列改由 ``scrub`` 呈現（不再說「保留」）。
        """
        scrubbed = self.scrub is not None and self.scrub.ran
        return {
            "agent_id": self.agent,
            "backup": self.backup,
            "clean": self.clean,
            "stores": [
                {"name": s.name, "purged": s.purged, "remaining": s.remaining, "error": s.error}
                for s in self.stores
            ],
            "retained": [
                {"name": n, "reason": r}
                for n, r in RETAINED_STORES
                if not (scrubbed and n == FACTS_BACKUP_DIR_NAME)
            ],
            "scrub": self.scrub.model_dump() if self.scrub is not None else None,
        }


async def _execute_reset(
    normalized: str,
    guard: Callable[[str], str],
    *,
    do_backup: bool,
    scrub: bool = False,
    actor_label: str = "",
) -> ResetReport:
    """已驗證目標 → 序列化 → 備份優先 → 逐 store 清除 → 完整性自檢（兩條重置路徑共用核心）。

    ``guard`` 為呼叫端的安全閘（DebugAgent 路徑＝`assert_resettable`；per-agent 路徑＝綁定 actor
    的 `assert_agent_resettable` 閉包），每個 scoped 原語會各自再過閘（縱深防禦不變）。
    """
    # 清除進行中也擋：重置被取消時 _reset_lock 會先放開，但清除還在跑。
    if reset_in_progress():
        if scrub:
            scrub_orchestrator.audit_rejected(actor_label, normalized, "reset_in_progress")
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)

    target = AgentIdentity(agent_id=normalized)
    async with destructive_op(target):
        run = last_result.start(target)
        report = ResetReport(agent=normalized)
        session: memory_scrub.ScrubSession | None = None
        # 交給清除工作之後，最後結果由清除工作在清旗標前記（請求斷線也一樣）。
        with last_result.recording(run, lambda: session is not None and session.handed_over):
            identity = _checked_identity(normalized, guard, scrub=scrub, actor_label=actor_label)
            if scrub:
                identity, store, known = await _prepare_scrub(normalized, guard, actor_label)
            # 預檢沒過的重置什麼都沒做，不叫停；標記已經設好，這段期間新的心跳與指令開不了。
            # 放在設清除旗標之前：旗標要靠下面 try 的 finally 才清得掉。
            stopped = destructive_ops.stop_heartbeats(target)
            if stopped:
                logger.info("[reset] asked %d heartbeat(s) of %s to stop", len(stopped), normalized)
            killed = await terminal.stop_agent_commands(identity)  # R11：個人空間裡沒有在跑的指令
            if killed:
                logger.info("[reset] stopped %d command(s) of %s", killed, normalized)
            if scrub:
                session = memory_scrub.begin(target)
            try:
                await _reset_stores(report, normalized, guard, do_backup=do_backup)
                if session is not None:
                    report.scrub = await _scrub_after(
                        report, identity, actor_label, store, known, session, run
                    )
            except asyncio.CancelledError:
                if session is not None and not session.handed_over:
                    _audit_not_run(actor_label, normalized, "cancelled")
                raise
            except Exception as exc:
                # 還沒交給清除工作就結束（備份失敗等）：照樣留一行結果稽核再往外丟
                if session is not None and not session.handed_over:
                    _audit_not_run(actor_label, normalized, _not_run_reason(exc))
                raise
            finally:
                if session is not None and not session.handed_over:
                    session.end()
        run.done(report.to_dict())  # 還持有破壞性操作鎖：下一次重置蓋不掉
        return report


def _checked_identity(
    normalized: str, guard: Callable[[str], str], *, scrub: bool, actor_label: str
) -> AgentIdentity:
    """過閘並查個人空間根（R11，任何副作用之前）；勾選清除時被擋下先寫一行稽核."""
    try:
        identity = primitives.identity_for(normalized, guard)
        refuse_linked_drive(resource_manager.agent_drive(identity))
    except ToukaException as exc:
        if scrub:
            linked = exc.code is ErrorCode.AGENT_DRIVE_LINKED
            reason = "drive_linked" if linked else "not_resettable"
            scrub_orchestrator.audit_rejected(actor_label, normalized, reason)
        raise
    except Exception:
        if scrub:
            scrub_orchestrator.audit_rejected(actor_label, normalized, "internal_error")
        raise
    return identity


def _audit_not_run(actor_label: str, target: str, reason: ScrubReason) -> None:
    scrub_orchestrator.audit_result(MemoryScrubReport.skipped(reason), actor_label, target)


def _not_run_reason(exc: Exception) -> ScrubReason:
    if isinstance(exc, ToukaException) and exc.code is ErrorCode.SYSTEM_BACKUP_FAILED:
        return "backup_failed"
    return "internal_error"


async def _reset_stores(
    report: ResetReport, normalized: str, guard: Callable[[str], str], *, do_backup: bool
) -> None:
    """備份優先 → 逐 store 清除 → 完整性自檢（原本的重置本體，行為不變）."""
    # ① 備份優先：失敗則整個中止（create_backup 失敗會 raise SYSTEM_BACKUP_FAILED）。
    # 與手動／排程備份共用執行鎖（防併發備份），阻塞子程序移出事件迴圈；重置被取消時
    # 鎖仍持有到備份真的結束。
    if do_backup:
        report.backup = await system_backup.run_backup_job(system_backup.create_backup)

    # ② 逐 store 清除（best-effort：單 store 失敗不中斷其餘）。
    for store in ALL_STORES:
        res = StoreResult(name=store.name)
        try:
            res.purged = await store.purge(normalized, guard)
        except Exception:  # noqa: BLE001  # 單 store 失敗收進報告、續清其餘
            # 回傳泛用碼、不外洩原始例外（可能含絕對路徑/DB 細節）；完整細節僅進伺服器日誌。
            res.error = "purge_failed"
            logger.exception("[reset] store '%s' purge failed", store.name)
        report.stores.append(res)

    # ③ 完整性自檢：清完仍有殘留（或查詢出錯）→ false-clean（防漏 store）。
    by_name = {s.name: s for s in ALL_STORES}
    for res in report.stores:
        try:
            res.remaining = await by_name[res.name].remaining(normalized, guard)
        except Exception:  # noqa: BLE001  # 同上：泛用碼，不外洩內部細節
            res.error = res.error or "verify_failed"
            logger.exception("[reset] store '%s' remaining-check failed", res.name)

    report.clean = all(r.remaining == 0 and not r.error for r in report.stores)


def _scrub_dependencies() -> tuple[ScrubFactStore, set[str]]:
    """清除需要的事實庫與已知 ID；記憶系統不能用就丟 MEMORY_SCRUB_UNAVAILABLE."""
    if not factual.memory_scrub_supported():
        raise ToukaException(ErrorCode.MEMORY_SCRUB_UNAVAILABLE)
    store = primitives.memory_fact_store()
    return store, memory_collection_guard.known_memory_ids(store)


async def _prepare_scrub(
    normalized: str, guard: Callable[[str], str], actor_label: str
) -> tuple[AgentIdentity, ScrubFactStore, set[str]]:
    """勾選清除時、備份之前的準備；任何拒絕或意外都先寫一行稽核再往外丟."""
    try:
        identity = primitives.identity_for(normalized, guard)
    except ToukaException:
        scrub_orchestrator.audit_rejected(actor_label, normalized, "not_resettable")
        raise
    try:
        store, known = await _preflight_scrub(identity, actor_label)
    except ToukaException:
        raise  # 預檢自己寫過稽核
    except Exception:
        scrub_orchestrator.audit_rejected(actor_label, normalized, "internal_error")
        raise
    return identity, store, known


async def _preflight_scrub(
    identity: AgentIdentity, actor_label: str
) -> tuple[ScrubFactStore, set[str]]:
    """記憶系統可用、沒有撞名；任一不過就拒絕，什麼都不刪（讀事實庫移出事件迴圈）."""
    try:
        store, known = await asyncio.to_thread(_scrub_dependencies)
    except (ToukaException, factual.MemoryScrubError, OSError, ValueError) as exc:
        scrub_orchestrator.audit_rejected(actor_label, identity.agent_id, "unavailable")
        raise ToukaException(ErrorCode.MEMORY_SCRUB_UNAVAILABLE) from exc
    others = memory_collection_guard.colliding_ids(identity, known)
    if others:
        scrub_orchestrator.audit_rejected(actor_label, identity.agent_id, "collection_shared")
        raise ToukaException(
            ErrorCode.MEMORY_SCRUB_COLLECTION_SHARED, collides_with=sorted(others)[0]
        )
    return store, known


async def _scrub_after(
    report: ResetReport,
    identity: AgentIdentity,
    actor_label: str,
    store: ScrubFactStore,
    known: set[str],
    session: memory_scrub.ScrubSession,
    run: last_result.ResetRun,
) -> MemoryScrubReport:
    """重置完全成功才清除；沒成功就記成「沒有執行」並寫稽核."""
    if not report.clean:
        skipped = MemoryScrubReport.skipped("reset_not_clean")
        scrub_orchestrator.audit_result(skipped, actor_label, identity.agent_id)
        return skipped
    own = (report.backup or {}).get("filename")
    removed = sum(s.purged for s in report.stores if s.name == primitives.MEMORY_STORE_NAME)

    def finished(result: MemoryScrubReport) -> None:
        """清除工作寫完稽核、清旗標之前呼叫：記下含清除結果的最後一次重置."""
        report.scrub = result
        run.done(report.to_dict())

    return await scrub_orchestrator.run_after_reset(
        identity,
        actor_label=actor_label,
        store=store,
        known_ids=known,
        own_snapshot=own if isinstance(own, str) else None,
        session=session,
        reset_removed=removed,
        on_report=finished,
    )


async def reset_debug_agent_data(
    candidate: str = "DebugAgent", *, do_backup: bool = True
) -> ResetReport:
    """破壞性清除 DebugAgent 全部累積資料（保留身分/註冊）。

    candidate 非 DebugAgent → 立即 raise（安全閘）。do_backup=True（預設）時**先**全系統備份，
    備份失敗即 raise `SYSTEM_BACKUP_FAILED` 中止（不刪任何東西）。整個操作以 `_reset_lock`
    序列化：若已有一次 reset 進行中，第二個直接 raise（拒絕並行，避免 TOCTOU）。
    """
    normalized = assert_resettable(candidate)
    return await _execute_reset(normalized, assert_resettable, do_backup=do_backup)


async def reset_agent_data(
    candidate: str, actor: AgentIdentity, *, do_backup: bool = True, scrub: bool = False
) -> ResetReport:
    """ADR-0113：破壞性清除**單一真實 agent**全部累積執行期資料（保留身分/註冊）。

    經 `assert_agent_resettable`（存在＋被 actor 擁有＋非系統保護）驗證；身分（soul/legacy）受
    `is_protected_path` 物理保護、custom config 由原語範圍排除——**永不被觸及**。其餘同 DebugAgent
    路徑：備份優先、`_reset_lock` 序列化、逐 store best-effort＋完整性自檢。
    """
    if scrub:  # 不只靠路由：清除只給非代打的人類 admin
        try:
            require_admin_human(actor)
        except ToukaException:
            scrub_orchestrator.audit_rejected(actor.agent_id, candidate, "not_human")
            raise
    try:
        normalized = assert_agent_resettable(candidate, actor)
    except ToukaException:
        if scrub:
            scrub_orchestrator.audit_rejected(actor.agent_id, candidate, "not_resettable")
        raise

    def _guard(c: str) -> str:
        return assert_agent_resettable(c, actor)

    return await _execute_reset(
        normalized, _guard, do_backup=do_backup, scrub=scrub, actor_label=actor.agent_id
    )
