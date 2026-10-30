"""DebugAgent 破壞性重置的逐 store scoped 清除原語（軌B Batch 6，Phase 2a：ToukaProxy stores）。

每個 purge_* 先過安全閘 `assert_resettable` → 建 DebugAgent 身分 → 委派 owning module 的
scoped 刪除（DB 刪除留在 owning module 以遵守 SQLITE3_IMPORT_BAN；檔案樹經 resource_manager，
受 is_protected_path 保護）。remaining_* 供 orchestrator（Phase 3）做完整性自檢（漏清＝false-clean）。
"""
from __future__ import annotations

import contextlib
import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server.factual import FACTS_BACKUP_DIR_NAME
from memory_server.services.personal_catalog import CATALOG_DIR_NAME, CATALOG_FILE_NAME
from memory_server.utils.collection_names import fact_point_id
from memory_server.utils.memory_worker import (
    run_memory_job,
    run_memory_job_blocking,
    run_memory_job_to_end,
)
from memory_server.utils.no_follow import WalkReport

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services import memory_collection_guard
from src.services.agent_reset.guard import assert_resettable
from src.services.feedback_store import FeedbackStore
from src.services.resource_manager import resource_manager
from src.utils import brain_persistence
from src.utils.async_db import session_service
from src.utils.continuity_events_db import count_agent_events, purge_agent_events
from src.utils.time_utils import get_current_epoch_ms

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from memory_server.utils.zone_paths import ZonePath

    GuardFn = Callable[[str], str]

logger = logging.getLogger(__name__)


def _identity(candidate: str, guard: GuardFn = assert_resettable) -> AgentIdentity:
    """過安全閘 ``guard`` 並建已驗證身分（任何 purge 入口的唯一身分來源；不過閘即 raise）。

    ADR-0113：``guard`` 預設為 DebugAgent 硬鎖（``assert_resettable``，原路徑零變動）；per-agent
    重置由 orchestrator 傳入 ``assert_agent_resettable`` 的閉包，使同一組 scoped 清除原語同時服務
    兩條路徑、且每個原語仍各自過閘（縱深防禦不變）。role 固定非-Owner，避免誤判 vault 路徑類型。
    """
    return AgentIdentity.for_role(agent_id=guard(candidate), role="Debugger")


def identity_for(candidate: str, guard: GuardFn = assert_resettable) -> AgentIdentity:
    """清除連備份用的身分：和各清除原語同一個來源（過同一道閘）."""
    return _identity(candidate, guard)


# ── sessions（ui_sessions.db）─────────────────────────────────────────
async def purge_sessions(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return await session_service.purge_agent_sessions(_identity(candidate, guard))


async def remaining_sessions(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return await session_service.count_agent_sessions(_identity(candidate, guard))


# ── feedback（feedback.db）────────────────────────────────────────────
async def purge_feedback(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return await FeedbackStore.purge_agent(_identity(candidate, guard))


async def remaining_feedback(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return await FeedbackStore.count_agent(_identity(candidate, guard))


# ── brain_settings（vault/brain_settings.json）───────────────────────
async def purge_brain(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return 1 if brain_persistence.clear_agent(_identity(candidate, guard)) else 0


async def remaining_brain(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return 1 if brain_persistence.has_agent(_identity(candidate, guard)) else 0


# ── stats（data/stats/DebugAgent：token_usage / autonomy_log）─────────
async def purge_stats(candidate: str, guard: GuardFn = assert_resettable) -> int:
    path = Path(resource_manager.get_stats_path(_identity(candidate, guard)))
    existed = path.exists()
    resource_manager.remove_dir(path, missing_ok=True)
    return 1 if existed else 0


async def remaining_stats(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return 1 if Path(resource_manager.get_stats_path(_identity(candidate, guard))).exists() else 0


# ── outputs（agent 主權產出區：含 docgen 產出）──────────────────────
# 掃描缺口 E65（ADR-0175）：outputs/uploads 是「使用者」聊天上傳檔（刻意巢狀於 outputs 下供
# 相對路徑存取）＝使用者資產、非 agent 產出——purge 與 remaining 一致排除（只清不計；不一致會使
# remaining 自檢把保留的上傳誤判為漏清）。
# ADR-0293：個人空間裡的東西都不跟隨連結。outputs／missions 本身是連結只刪連結；裡面的連結也只刪
# 本身、不進入。
_PRESERVED_OUTPUT_DIRS = ("uploads",)
_MISSIONS_DIR = "missions"


@dataclass
class _Scan:
    kind: str  # 資料夾本身：missing／file／dir／link／other
    count: int = 0
    names: set[str] = field(default_factory=set)  # 要刪的第一層名稱


def _scan(zp: ZonePath, keep: tuple[str, ...] = ()) -> _Scan:
    """看資料夾裡（``keep`` 那幾項除外）有什麼，不跟隨連結.

    ``count`` 算檔案、連結、名稱不合格與讀不到的項（讀不到的也算，自檢只會偏向「沒清乾淨」）；
    不存在是 0，本身不是資料夾（連結等）是 1。
    """
    kind = resource_manager.zone_kind(zp)
    if kind != "dir":
        return _Scan(kind, 0 if kind == "missing" else 1)
    scan, report = _Scan(kind), WalkReport()
    walk = resource_manager.zone_walk(
        zp, report, skip_dir=lambda parts: parts[0] in keep, show_links=True
    )
    with contextlib.closing(walk):
        for entry in walk:
            if entry.parts[0] in keep:
                continue
            scan.names.add(entry.parts[0])
            scan.count += entry.kind != "dir"
    scan.count += len(report.bad_names) + len(report.errors)
    return scan


def _purge_folder(zp: ZonePath, keep: tuple[str, ...] = ()) -> int:
    """刪掉資料夾裡 ``keep`` 以外的每一項，回刪掉幾項；沒有要留的就連資料夾一起刪.

    本身是連結（或不是資料夾）只刪本身；整棵樹都不跟隨連結（R11）。
    """
    scan = _scan(zp, keep)
    if scan.kind == "missing":
        return 0
    if scan.kind != "dir" or all(
        resource_manager.zone_kind(zp.child(name)) == "missing" for name in keep
    ):
        resource_manager.zone_remove_dir(zp)  # 沒有保留項：連空殼一併移除（既有「目錄消失」契約）
        return scan.count
    for name in sorted(scan.names):
        resource_manager.zone_remove_dir(zp.child(name))
    return scan.count


async def purge_outputs(candidate: str, guard: GuardFn = assert_resettable) -> int:
    outputs = resource_manager.outputs_dir(_identity(candidate, guard))
    return 0 if outputs is None else _purge_folder(outputs, _PRESERVED_OUTPUT_DIRS)


async def remaining_outputs(candidate: str, guard: GuardFn = assert_resettable) -> int:
    outputs = resource_manager.outputs_dir(_identity(candidate, guard))
    return 0 if outputs is None else _scan(outputs, _PRESERVED_OUTPUT_DIRS).count


# ── memory（ToukaMemory：事實 core_memory.db ＋ 向量 Qdrant）──────────
# 經整合橋接（importlib，避開 import-linter 與 LOCAL_IMPORT_BAN）。
def _memory_manager() -> object:
    """取得記憶管理器；不可用即 **raise**（破壞性重置嚴禁在無法確認下假裝清乾淨）。

    本系統記憶為 in-process 經 importlib 載入（main.py），故載入失敗＝真實故障（降級啟動、
    Qdrant 鎖、相依缺失…），非預期模式。ADR-0104 對抗式複審 R11：舊版於不可用時靜默回 0，
    使 purge 與 remaining 同時歸零 → clean=True 卻仍有 DebugAgent 記憶殘留磁碟（false-clean）。
    改 raise：orchestrator 的 best-effort 容錯把它收進 store error → clean=False，誠實回報
    「記憶無法確認清除」。
    """
    try:
        return importlib.import_module("memory_server.tools").get_manager()
    except Exception as exc:  # noqa: BLE001  # 任何載入失敗→轉明確錯誤，禁止 false-clean
        logger.warning("[reset] 記憶系統不可用，無法確認 DebugAgent 記憶已清除")
        raise ToukaException(
            ErrorCode.SYSTEM_ERROR,
            detail_override="記憶系統不可用，無法確認 DebugAgent 記憶已清除",
        ) from exc


def memory_fact_store() -> Any:
    """重置用的事實庫實例（和 purge／remaining 同一個管理器；清除連備份要用）."""
    return _memory_manager().get_factual_store()


def _colliding(identity: AgentIdentity, manager: Any) -> set[str]:
    """和目標共用 collection 名稱的其他已知 ID；判定出錯就往外丟（絕不退回整個刪除）."""
    known = memory_collection_guard.known_memory_ids(manager.get_factual_store())
    return memory_collection_guard.colliding_ids(identity, known)


async def purge_memory(candidate: str, guard: GuardFn = assert_resettable) -> int:
    """清掉目標的記憶（在記憶工作者上做，ADR-0288）.

    等待被取消（重置中途取消、HTTP 斷線）時，已經開始的清除做完才往外丟取消：重置鎖不會在
    清到一半時就放掉。
    """
    return await run_memory_job_to_end(_purge_memory_now, _identity(candidate, guard))


def _purge_memory_now(identity: AgentIdentity) -> int:
    manager = _memory_manager()
    if not _colliding(identity, manager):
        result = manager.purge_agent(identity)
        return int(result.get("facts", 0)) + int(result.get("vectors", 0))
    # 撞名：collection 是共用的，只刪目標自己的點（先刪向量，事實還在才算得出點 ID，可重跑）
    store, vector = manager.get_factual_store(), manager.get_vector_store()
    points = [fact_point_id(fact_id) for fact_id in store.agent_fact_ids(identity)]
    removed_vectors = vector.delete_points(identity, points)
    return int(store.delete_agent_facts(identity)) + int(removed_vectors)


def count_remaining_memory(identity: AgentIdentity) -> int:
    """記憶殘留（事實＋向量）；給清除工作自己的執行緒用：同步等記憶工作者（ADR-0288）."""
    return run_memory_job_blocking(_count_remaining_now, identity)


def _count_remaining_now(identity: AgentIdentity) -> int:
    """撞名時只數不屬於別人任何事實的點（只會偏向「沒清乾淨」）."""
    manager = _memory_manager()
    others = _colliding(identity, manager)
    if not others:
        result = manager.count_agent_memory(identity)
        return int(result.get("facts", 0)) + int(result.get("vectors", 0))
    store, vector = manager.get_factual_store(), manager.get_vector_store()
    keep = memory_collection_guard.point_ids_of(store, others)
    return int(store.count_agent_facts(identity)) + int(vector.count_points_outside(identity, keep))


async def remaining_memory(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return await run_memory_job(_count_remaining_now, _identity(candidate, guard))


# ── mission（personal vault /missions/current_mission.json）──────────
def _mission_dir(identity: AgentIdentity) -> ZonePath | None:
    drive = resource_manager.agent_drive(identity)
    return None if drive is None else drive.child(_MISSIONS_DIR)


async def purge_mission(candidate: str, guard: GuardFn = assert_resettable) -> int:
    missions = _mission_dir(_identity(candidate, guard))
    return 0 if missions is None else _purge_folder(missions)


async def remaining_mission(candidate: str, guard: GuardFn = assert_resettable) -> int:
    missions = _mission_dir(_identity(candidate, guard))
    return 0 if missions is None else _scan(missions).count


# ── catalog（personal vault /.touka/catalog.json：個人空間目錄）──────────
# outputs 在個人空間裡，目錄會記下其中的檔名、hash 與 Markdown 標題；重置時一併刪掉。
# 它是快取：下次讀時由實際檔案整份重建（刪掉後不在信任紀錄裡）。``.touka`` 是連結時這一項
# 失敗、不刪另一端（ADR-0293）。
def _catalog_path(identity: AgentIdentity) -> ZonePath | None:
    drive = resource_manager.agent_drive(identity)
    return None if drive is None else drive.child(CATALOG_DIR_NAME, CATALOG_FILE_NAME)


async def purge_catalog(candidate: str, guard: GuardFn = assert_resettable) -> int:
    """刪掉個人空間的目錄檔；不存在回 0（不算錯）。"""
    path = _catalog_path(_identity(candidate, guard))
    if path is None or resource_manager.zone_kind(path) == "missing":
        return 0
    resource_manager.zone_remove_file(path)
    return 1


async def remaining_catalog(candidate: str, guard: GuardFn = assert_resettable) -> int:
    """目錄檔還在回 1，否則 0（重置報告的完整性自檢）。"""
    path = _catalog_path(_identity(candidate, guard))
    return 0 if path is None or resource_manager.zone_kind(path) == "missing" else 1


# ── context snapshot（data/snapshots/<id>.md：debug 匯出的情境快照）──────
# ADR-0104 對抗式複審 R5：/debug/context-snapshot/export 會把 DebugAgent 組裝情境寫成
# 檔案；屬 agent 衍生內容（非稽核軌跡），重置時應清除（與稽核帳本的刻意保留區隔，見下）。
def _snapshot_path(identity: AgentIdentity) -> Path:
    return Path(resource_manager.get_base_path()) / "snapshots" / f"{identity.agent_id}.md"


async def purge_snapshot(candidate: str, guard: GuardFn = assert_resettable) -> int:
    path = _snapshot_path(_identity(candidate, guard))
    existed = path.exists()
    resource_manager.remove_file(path, missing_ok=True)  # 經 is_protected_path 保護
    return 1 if existed else 0


async def remaining_snapshot(candidate: str, guard: GuardFn = assert_resettable) -> int:
    return 1 if _snapshot_path(_identity(candidate, guard)).exists() else 0


# ── timeline（ui_sessions.db 的可信活動事件；畫面上屬「記憶」類別）──────────
async def purge_timeline(candidate: str, guard: GuardFn = assert_resettable) -> int:
    """刪掉活動事件，同一個交易寫這個 ID 的「可信紀錄起點」（之前的紀錄已不在）。"""
    identity = _identity(candidate, guard)
    return await purge_agent_events(identity, marker_ms=get_current_epoch_ms())


async def remaining_timeline(candidate: str, guard: GuardFn = assert_resettable) -> int:
    """還剩幾筆活動事件（重置的完整性自檢）。"""
    return await count_agent_events(_identity(candidate, guard))


@dataclass(frozen=True)
class StorePurger:
    """一個 store 的清除＋殘留查詢（orchestrator 依此 registry 逐一執行＋完整性自檢）。"""

    name: str
    purge: Callable[..., Awaitable[int]]  # (candidate, guard=assert_resettable) -> int
    remaining: Callable[..., Awaitable[int]]


# ToukaProxy 自包含 store。
PROXY_STORES: tuple[StorePurger, ...] = (
    StorePurger("sessions", purge_sessions, remaining_sessions),
    StorePurger("feedback", purge_feedback, remaining_feedback),
    StorePurger("brain_settings", purge_brain, remaining_brain),
    StorePurger("stats", purge_stats, remaining_stats),
    StorePurger("outputs", purge_outputs, remaining_outputs),
    StorePurger("snapshot", purge_snapshot, remaining_snapshot),
)

MEMORY_STORE_NAME = "memory"  # 清除連備份據此取「重置清掉的記憶筆數」

# ToukaMemory（經整合橋接）＋ 跨 vault 的 mission。
MEMORY_STORES: tuple[StorePurger, ...] = (
    StorePurger(MEMORY_STORE_NAME, purge_memory, remaining_memory),
    StorePurger("timeline", purge_timeline, remaining_timeline),
    StorePurger("mission", purge_mission, remaining_mission),
    StorePurger("catalog", purge_catalog, remaining_catalog),
)

# orchestrator（Phase 3）依此逐一清除＋完整性自檢的 SSOT。
ALL_STORES: tuple[StorePurger, ...] = PROXY_STORES + MEMORY_STORES

# ⚠️ 刻意保留、不清除的 store（ADR-0104 決策；orchestrator 於報告 to_dict 列出，使 clean=True
# 不被誤讀為「連稽核軌跡也清光」）：
#   • legacy   — 對話庫（受 is_protected_path 保護，不可由 reset 刪；多為匯入舊對話）。
#                註：legacy 亦由 sliding_window 在 home-session 觸發寫入，故並非「永不累積」。
#   • project_audit — 專案操作審計表（async_db）：審計軌跡，刻意保留完整性（agent-scoped 可查）。
#   • decisions.csv — 權限決策帳本（audit_service）：跨 agent 共用審計帳本，刻意保留。
RETAINED_STORES: tuple[tuple[str, str], ...] = (
    ("legacy", "對話庫；受 is_protected_path 保護，不可由 reset 刪除"),
    ("project_audit", "專案操作審計表；刻意保留審計完整性"),
    ("decisions.csv", "權限決策審計帳本（跨 agent 共用）；刻意保留"),
    ("outputs/uploads", "聊天上傳檔（使用者資產、非 agent 產出）；reset 保留（ADR-0175）"),
    (FACTS_BACKUP_DIR_NAME, "記憶庫升級備份（所有靈魂共用一個檔）；重置不清除"),
    ("sandbox_home", "隔離環境裡的家目錄與暫存（終端指令的 ~）；刪除靈魂時才清"),
)
