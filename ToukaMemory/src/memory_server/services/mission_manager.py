# ruff: noqa: E501, TRY003, RUF100, I001
"""任務看板管理器 (Mission Manager).

本模組負責管理代理人的自主任務看板 (Mission Board)。
提供任務樹的拆解、狀態追蹤、以及持久化到 ``<data>/missions/<sha256(逐字編號)>.json``
（代理人碰不到的系統資料夾，ADR-0325）。``data/missions`` 整個資料夾只歸這裡管。
支援任務狀態更新、產出物 (Artifacts) 關聯以及總體進度判定。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server import _get_app_data_dir
from memory_server.config_utils import get_current_epoch, get_resource_proxy
from memory_server.models.mission import (
    REVIEW_PASSING_STATES,
    REVIEW_STATES,
    MissionState,
    Task,
    TaskStatus,
    review_passing,
)
from memory_server.services.mission_lock import MissionLock
from memory_server.utils.no_follow import LinkRefusedError
from memory_server.utils.zone_paths import ZoneSegmentError, is_zone_segment

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

# data 根底下放看板的資料夾（ADR-0325）。升級前看板在個人空間根目錄的同名資料夾
# （``missions/current_mission.json``）；檔案動詞照舊不能寫那裡（personal_catalog.SYSTEM_ROOT_DIRS，
# ADR-0324），升級後第一次啟動搬走（``import_legacy_board``）。
MISSIONS_DIR_NAME = "missions"
LEGACY_BOARD_FILE = "current_mission.json"
# 「舊看板搬過了」的記號；看板檔名都是 64 位十六進位，不會撞名
LEGACY_IMPORT_MARKER = ".migrated"


class MissionPersistenceError(RuntimeError):
    """保存提交無法確認；安全訊息不包含受管 I/O 的原始內容或路徑。"""

    def __init__(self) -> None:
        super().__init__("Mission persistence failed")


class MissionBoardExistsError(FileExistsError):
    """搬看板的目的地已經有看板：不覆蓋。"""

    def __init__(self) -> None:
        super().__init__("The target agent already has a mission board")


class LegacyImport(StrEnum):
    """搬一份舊看板的結果（``import_legacy_board``）."""

    ABSENT = "absent"  # 舊位置沒有看板
    KEPT = "kept"  # 新位置已有看板：不覆蓋、舊檔不動
    SKIPPED = "skipped"  # 讀不到、是連結、不合規則、別人的、讀回不一致：舊檔不動
    IMPORTED = "imported"


def _board_key(owner_id: str) -> str:
    """看板與鎖檔的檔名：逐字編號（不 casefold）UTF-8 的 sha256；不是單一段丟 ``ZoneSegmentError``.

    只差大小寫的兩個代理人各有各的檔；雜湊沒有大小寫、8.3 短檔名、Unicode 正規化的別名。
    """
    if not is_zone_segment(owner_id):
        raise ZoneSegmentError
    return hashlib.sha256(owner_id.encode("utf-8", "surrogatepass")).hexdigest()


def missions_dir() -> Path:
    """``<data>/missions``：看板與搬過了的記號（和鎖檔資料夾用同一個 data 根）."""
    return Path(_get_app_data_dir()) / MISSIONS_DIR_NAME


def mission_board_path(owner_id: str) -> Path:
    """某個代理人看板的位置（SSOT，ADR-0325）：只看編號，和存取者是誰無關."""
    return missions_dir() / f"{_board_key(owner_id)}.json"


def _check_board_rules(state: MissionState) -> None:
    """互審的落盤不變式（ADR-0137 §4.4）：做完的任務要審過、鎖住的驗證指令不能拿掉."""
    for task in state.tasks:
        if task.status == TaskStatus.DONE and not review_passing(task):
            raise ValueError(  # noqa: RAISE_STRING_BAN
                f"Review gate not passed for task '{task.id}' "
                f"(review_status={task.review_status!r}, risk={task.review_risk})"
            )
        if task.verification_cmd_locked and not task.verification_cmd:
            raise ValueError(  # noqa: RAISE_STRING_BAN
                f"locked verification_cmd cannot be stripped (task '{task.id}')"
            )


def _skip_legacy(owner_id: str, reason: str, exc: Exception | None) -> LegacyImport:
    """略過一份舊看板：記 ERROR（編號、原因、例外類別；不記內容），舊檔不動."""
    detail = f": {type(exc).__name__}" if exc is not None else ""
    logger.error(
        "❌ Old mission board of %s not moved (%s%s); the old file stays", owner_id, reason, detail
    )
    return LegacyImport.SKIPPED


class MissionManager:
    """自主任務看板管理員 (SOLID - SRP).

    負責代理人任務的生命週期管理，確保狀態更新與物理存儲的一致性。
    """

    def _get_lock(self, agent_id: str) -> MissionLock:  # noqa: IDENTITY_SSOT
        """獲取指定代理人任務看板的排他鎖（asyncio.Lock + FileLock，ADR-0049）.

        鎖檔是 ``<data>/temp/locks/missions/<雜湊>.lock``（和看板同一個雜湊，ADR-0325），
        不在代理人寫得到的資料夾（ADR-0293）。
        """
        key = _board_key(agent_id)
        lock_dir = Path(_get_app_data_dir()) / "temp" / "locks" / "missions"
        get_resource_proxy().make_dir(str(lock_dir))
        return MissionLock(agent_id, lock_dir / key)

    def _resolve_target_agent_id(
        self, identity: AgentIdentity, target_agent_id: str | None = None
    ) -> str:
        """根據執行者身分與目標 ID，正規化並核驗最終任務歸屬 Agent ID (DIP)."""
        if target_agent_id and target_agent_id != identity.agent_id:
            if target_agent_id == "DispatchAgent":
                if "can_supervise_dispatch" in identity.capabilities:
                    return "DispatchAgent"
                raise PermissionError(
                    "Permission denied: Unauthorized access to DispatchAgent board"
                )
            raise PermissionError(f"Permission denied: Cannot access board of {target_agent_id}")
        return identity.agent_id

    def _get_mission_path(
        self, identity: AgentIdentity, target_agent_id: str | None = None
    ) -> Path:
        """獲取該代理人任務看板的位置（``mission_board_path``，ADR-0325）.

        C2 修復：不再獨立校驗權限，由呼叫方的 _resolve_target_agent_id 集中負責。
        路徑只看目標編號，和存取者的角色無關（跨身分讀不再需要查註冊身分）。
        """
        return mission_board_path(target_agent_id or identity.agent_id)

    async def _get_current_mission_unlocked(
        self, identity: AgentIdentity, target_agent_id: str | None = None
    ) -> MissionState | None:
        """載入目前代理人進行中的任務看板（無鎖內部方法）.

        路上有連結時往外丟 ``LinkRefusedError``（ADR-0293），其他讀取失敗當作沒有看板。
        """
        path = self._get_mission_path(identity, target_agent_id)
        try:
            content = get_resource_proxy().read_file_text_no_follow(path)
            if not content:
                return None
            data = json.loads(content)
            return MissionState.model_validate(data)
        except FileNotFoundError:
            return None
        except LinkRefusedError:
            raise
        except Exception:
            resolved_agent_id = target_agent_id if target_agent_id else identity.agent_id
            logger.exception("❌ [MissionManager] Failed to load mission for %s", resolved_agent_id)
            return None

    async def _save_mission_unlocked(
        self, identity: AgentIdentity, state: MissionState, target_agent_id: str | None = None
    ) -> None:
        """將任務狀態持久化至檔案庫（無鎖內部方法）.

        Phase 3 互審（ADR-0137 §4.4）：gate 做成**落盤不變式**、放在唯一落盤閘口——
        ``save_mission`` 直呼路徑零 FSM 檢查（後門），只有在這裡擋才堵得住。
        """
        # --- 互審落盤不變式（先驗證再落盤；raise 不得被下方 try 吞掉）---
        _check_board_rules(state)

        pending = state.model_copy(deep=True)
        pending.updated_at = get_current_epoch()

        path = self._get_mission_path(identity, target_agent_id)
        try:
            get_resource_proxy().write_file_text_no_follow(path, pending.model_dump_json(indent=4))
        except LinkRefusedError:  # 訊息固定、沒有寫入，不包成「結果未知」（ADR-0293）
            raise
        except Exception as exc:
            logger.exception("❌ [MissionManager] Failed to save mission for %s", state.agent_id)
            raise MissionPersistenceError from exc
        state.updated_at = pending.updated_at
        logger.info("✅ [MissionManager] Mission saved for %s", state.agent_id)

    async def initialize_mission(
        self,
        identity: AgentIdentity,
        goal: str,
        initial_tasks: list[dict[str, Any]] | None = None,
        target_agent_id: str | None = None,
    ) -> MissionState:
        """建立全新任務看板並初始化目標與子任務."""
        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            tasks = []
            now = get_current_epoch()

            if initial_tasks:
                processed_tasks = []
                for t in initial_tasks:
                    processed_tasks.append({**t, "id": t.get("id") or str(uuid.uuid4())[:8]})

                task_ids = {t["id"] for t in processed_tasks}
                for t in processed_tasks:
                    deps = t.get("dependencies", [])
                    for dep in deps:
                        if dep not in task_ids:
                            raise ValueError(f"Dependency '{dep}' does not exist")  # noqa: RAISE_STRING_BAN

                adj = {t["id"]: t.get("dependencies", []) for t in processed_tasks}
                visited = {}

                def has_cycle(u: str) -> bool:
                    """利用深度優先搜尋 (DFS) 偵測任務依賴圖中是否存在環。"""
                    visited[u] = 1
                    for v in adj.get(u, []):
                        if visited.get(v, 0) == 1:
                            return True
                        if visited.get(v, 0) == 0 and has_cycle(v):
                            return True
                    visited[u] = 2
                    return False

                for node in adj:
                    if visited.get(node, 0) == 0 and has_cycle(node):
                        raise ValueError("Circular dependency detected")  # noqa: RAISE_STRING_BAN

                for t in processed_tasks:
                    tasks.append(
                        Task(
                            id=t["id"],
                            description=t.get("description", ""),
                            dependencies=t.get("dependencies", []),
                            status=TaskStatus.TODO,
                            verification_cmd=t.get("verification_cmd"),
                            boundary_rules=t.get("boundary_rules", []),
                            test_log=t.get("test_log"),
                            artifact_ref=t.get("artifact_ref"),
                            artifact_refs=t.get("artifact_refs") or [],
                            # Phase 3（ADR-0137）：init 帶 cmd 即上鎖（producer 不可覆寫考卷）；
                            # review_required/review_risk 為 init 顯式標記（決策 2 顯式來源）。
                            verification_cmd_locked=bool(t.get("verification_cmd")),
                            review_required=bool(t.get("review_required", False)),
                            review_risk=("high" if t.get("review_risk") == "high" else "normal"),
                            created_at=now,
                            updated_at=now,
                        )
                    )

            resolved_agent_id = target_agent_id if target_agent_id else identity.agent_id
            state = MissionState(
                mission_id=str(uuid.uuid4()),
                goal=goal,
                agent_id=resolved_agent_id,
                tasks=tasks,
                created_at=now,
                updated_at=now,
            )

            await self._save_mission_unlocked(identity, state, target_agent_id)
            return state

    async def get_current_mission(
        self, identity: AgentIdentity, target_agent_id: str | None = None
    ) -> MissionState | None:
        """載入目前代理人進行中的任務看板."""
        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            return await self._get_current_mission_unlocked(identity, target_agent_id)

    async def save_mission(
        self, identity: AgentIdentity, state: MissionState, target_agent_id: str | None = None
    ) -> None:
        """將任務狀態持久化至檔案庫 (SSOT)."""
        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            await self._save_mission_unlocked(identity, state, target_agent_id)

    # --- 看板跟著代理人走（刪除、改名、建立、重置；ADR-0325）：Proxy 一律經這些方法動看板 ---
    # 權限由呼叫端（管理員路由、重置原語）決定；這裡只拿鎖、不跟隨連結地讀寫。

    async def delete_board(self, owner: AgentIdentity) -> bool:
        """刪掉這個編號的看板；回有沒有刪到（不存在不算錯）."""
        async with self._get_lock(owner.agent_id):
            try:
                get_resource_proxy().remove_file_no_follow(mission_board_path(owner.agent_id))
            except FileNotFoundError:
                return False
            return True

    async def has_board(self, owner: AgentIdentity) -> bool:
        """這個編號有沒有看板."""
        async with self._get_lock(owner.agent_id):
            return bool(get_resource_proxy().exists(mission_board_path(owner.agent_id)))

    async def move_board(self, source: AgentIdentity, target: AgentIdentity) -> bool:
        """看板從舊編號搬到新編號；回有沒有搬（舊的沒有看板就什麼都不做）.

        新編號已有看板丟 ``MissionBoardExistsError``、不覆蓋。看板裡的 ``agent_id`` 改成新編號
        （讀不懂的看板原樣搬，讀的時候照舊報壞）。刪舊檔失敗時把新檔刪掉再往外丟：不然呼叫端把
        名字改回去之後，新編號會多一份看板。兩把鎖依檔名順序拿（反向改名不互卡）。
        """
        if source.agent_id == target.agent_id:
            return False  # 同一把鎖不能拿兩次
        first, second = sorted((source.agent_id, target.agent_id), key=_board_key)
        async with self._get_lock(first), self._get_lock(second):
            proxy = get_resource_proxy()
            old_path = mission_board_path(source.agent_id)
            new_path = mission_board_path(target.agent_id)
            try:
                content = proxy.read_file_text_no_follow(old_path)
            except FileNotFoundError:
                return False
            if proxy.exists(new_path):
                raise MissionBoardExistsError
            with contextlib.suppress(ValueError):
                state = MissionState.model_validate_json(content)
                state.agent_id = target.agent_id
                content = state.model_dump_json(indent=4)
            proxy.write_file_text_no_follow(new_path, content)
            try:
                proxy.remove_file_no_follow(old_path)
            except Exception:
                with contextlib.suppress(OSError):
                    proxy.remove_file_no_follow(new_path)
                raise
            return True

    def legacy_import_done(self) -> bool:
        """升級後的舊看板搬過了沒（記號在 ``data/missions`` 裡，跟著備份走）."""
        return bool(get_resource_proxy().exists(missions_dir() / LEGACY_IMPORT_MARKER))

    def mark_legacy_import_done(self) -> None:
        """寫下搬過了的記號；之後舊位置的看板不再被搬、也不會被讀."""
        marker = missions_dir() / LEGACY_IMPORT_MARKER
        get_resource_proxy().write_file_text_no_follow(marker, str(get_current_epoch()))

    async def import_legacy_board(self, identity: AgentIdentity) -> LegacyImport:
        """把升級前個人空間裡的看板搬到新位置（啟動時一次，ADR-0325）.

        舊檔用現在讀看板的同一個解析找（``agent_drive``），區內方法讀（不跟隨連結）。內容要是
        合規則的看板，``agent_id`` 改成這個編號；用和平常存看板相同的序列化寫到新位置、讀回比對
        一致後才刪舊檔。略過的只記編號與原因，不記內容。
        """
        agent_id = identity.agent_id
        async with self._get_lock(agent_id):
            proxy = get_resource_proxy()
            drive = proxy.agent_drive(identity)
            if drive is None:
                raise ZoneSegmentError
            legacy = drive.child(MISSIONS_DIR_NAME, LEGACY_BOARD_FILE)
            try:
                content = proxy.zone_read_text(legacy)
            except FileNotFoundError:
                return LegacyImport.ABSENT
            except Exception as exc:
                return _skip_legacy(agent_id, "unreadable", exc)
            target = mission_board_path(agent_id)
            if proxy.exists(target):
                logger.info("Mission board of %s already moved; the old file stays", agent_id)
                return LegacyImport.KEPT
            try:
                state = MissionState.model_validate(json.loads(content))
                _check_board_rules(state)
            except Exception as exc:
                return _skip_legacy(agent_id, "not a valid board", exc)
            if state.agent_id != agent_id:
                # 舊版改名只搬資料夾、只差大小寫時用另一種寫法存過：舊檔在這個代理人的個人空間，
                # 一律改成這個編號（擋不了偽造；互審的自簽檢查要的是現在的編號）
                logger.info("Mission board of %s carried another agent id; rewritten", agent_id)
                state.agent_id = agent_id
            text = state.model_dump_json(indent=4)
            proxy.write_file_text_no_follow(target, text)
            if proxy.read_file_text_no_follow(target) != text:
                proxy.remove_file_no_follow(target)
                return _skip_legacy(agent_id, "read-back mismatch", None)
            proxy.zone_remove_file(legacy)
            logger.info("Mission board of %s moved to the system folder", agent_id)
            return LegacyImport.IMPORTED

    async def finalize_mission(
        self,
        identity: AgentIdentity,
        verdict: str,
        target_agent_id: str | None = None,
    ) -> MissionState:
        """全 task DONE 後記錄 mission 級最終驗收結論 (Phase 2 finalize 原語).

        僅在總體狀態 DONE（全 task 完成）時可記錄；重複 finalize 以最新結論覆寫
        （時間戳同步更新）。鎖內載入→校驗→寫入，與 update_task 同層原子性。
        """
        if not verdict or not verdict.strip():
            raise ValueError("finalize requires a non-empty verdict")  # noqa: RAISE_STRING_BAN
        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            state = await self._get_current_mission_unlocked(identity, target_agent_id)
            if state is None:
                raise ValueError("No active mission to finalize")  # noqa: RAISE_STRING_BAN
            if state.status != TaskStatus.DONE:
                incomplete = [t.id for t in state.tasks if t.status != TaskStatus.DONE]
                raise ValueError(  # noqa: RAISE_STRING_BAN
                    f"Mission not complete; incomplete tasks: {', '.join(incomplete)}"
                )
            now = get_current_epoch()
            state.final_verdict = verdict.strip()
            state.finalized_at = now
            state.updated_at = now
            await self._save_mission_unlocked(identity, state, target_agent_id)
            return state

    async def record_review(
        self,
        identity: AgentIdentity,
        task_id: str,
        *,
        review_status: str,
        reviewer_id: str,
        critic_verdict: str | None = None,
        evidence_digest: str | None = None,
        attempts_increment: int = 0,
        review_locked: bool | None = None,
        stash_fixes: list[str] | None = None,
        target_agent_id: str | None = None,
    ) -> MissionState:
        """審查結論的**唯一**合法寫路徑（ADR-0137 §4.4 第二層：隔離寫入堵自簽）.

        - 需 ``can_review`` capability（或 Owner/Debugger/admin 角色）——呼叫者通常
          是 review_loop（跑在主 agent 的 request context 內）；模型工具面**永不**
          直接暴露本方法，模型面的自簽防線＝下一條的標籤獨立性；
        - ``reviewer_id`` 不得等於看板擁有者 agent_id（reviewer≠producer，堵自簽）；
        - ``review_status`` 僅限審查 FSM 狀態集合（REVIEW_STATES）；
        - 路徑/跨身分沿用既有 ``_resolve_target_agent_id`` 權限牆（不另開洞）。
        """
        can_review = (
            "can_review" in identity.capabilities
            or identity.is_privileged()  # ADR-0141：三元組 {Owner,Debugger,admin} 謂詞
        )
        if not can_review:
            raise PermissionError(  # noqa: RAISE_STRING_BAN
                "record_review requires the 'can_review' capability"
            )
        if review_status not in REVIEW_STATES:
            raise ValueError(f"invalid review_status '{review_status}'")  # noqa: RAISE_STRING_BAN

        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            state = await self._get_current_mission_unlocked(identity, target_agent_id)
            if state is None:
                raise ValueError("No active mission to review")  # noqa: RAISE_STRING_BAN
            if reviewer_id == state.agent_id:
                raise ValueError(  # noqa: RAISE_STRING_BAN
                    "self-signing rejected: reviewer_id equals the board owner"
                )
            task = next((t for t in state.tasks if t.id == task_id), None)
            if task is None:
                raise ValueError(f"Task ID {task_id} not found in mission")  # noqa: RAISE_STRING_BAN

            task.review_status = review_status
            task.reviewer_id = reviewer_id
            if critic_verdict is not None:
                task.critic_verdict = critic_verdict
            if evidence_digest is not None:
                task.evidence_digest = evidence_digest
            if attempts_increment:
                task.review_attempts_total += attempts_increment
            if review_locked is not None:
                task.review_locked = review_locked
            if stash_fixes is not None:
                # 停滯偵測暫存：上一輪成功解析的 blocking fixes 正規化集合
                state.metadata[f"review_fixes:{task_id}"] = stash_fixes

            await self._save_mission_unlocked(identity, state, target_agent_id)
            return state

    async def update_task(
        self,
        identity: AgentIdentity,
        task_id: str,
        status: TaskStatus,
        artifact_ref: str | None = None,
        artifact_refs: list[str] | None = None,
        test_log: str | None = None,
        blocker_detail: str | None = None,
        target_agent_id: str | None = None,
    ) -> MissionState:
        """更新單一任務狀態與關聯產出物，並自動重新判定 Mission 總進度."""
        agent_id = self._resolve_target_agent_id(identity, target_agent_id)
        async with self._get_lock(agent_id):
            state = await self._get_current_mission_unlocked(identity, target_agent_id)
            if not state:
                resolved_agent_id = target_agent_id if target_agent_id else identity.agent_id
                msg = f"No active mission found for agent {resolved_agent_id}"
                raise ValueError(msg)

            found = False
            now = get_current_epoch()

            for task in state.tasks:
                if task.id == task_id:
                    current_status = task.status
                    if current_status != status:
                        VALID_TRANSITIONS = {
                            TaskStatus.TODO: {TaskStatus.DOING, TaskStatus.BLOCKED},
                            TaskStatus.DOING: {
                                TaskStatus.TODO,
                                TaskStatus.DONE,
                                TaskStatus.FAILED,
                                TaskStatus.BLOCKED,
                            },
                            TaskStatus.BLOCKED: {TaskStatus.TODO, TaskStatus.DOING},
                            TaskStatus.DONE: set(),
                            TaskStatus.FAILED: {TaskStatus.TODO},
                        }
                        if status not in VALID_TRANSITIONS.get(current_status, set()):
                            raise ValueError(  # noqa: RAISE_STRING_BAN
                                f"Invalid status transition from {current_status} to {status}"
                            )

                        if current_status == TaskStatus.FAILED and status == TaskStatus.TODO:
                            is_privileged = (
                                "can_supervise_dispatch" in identity.capabilities
                                or identity.is_privileged()  # ADR-0141：三元組謂詞
                            )
                            if not is_privileged:
                                raise ValueError("Only authorized agents can reset a FAILED task")  # noqa: RAISE_STRING_BAN

                    # H1 修復：轉入 BLOCKED 必須提供非空的 blocker_detail
                    if status == TaskStatus.BLOCKED and not blocker_detail:
                        raise ValueError(  # noqa: RAISE_STRING_BAN
                            "blocker_detail is required when transitioning to BLOCKED status"
                        )

                    if status in (TaskStatus.DOING, TaskStatus.DONE):
                        for dep in task.dependencies:
                            dep_task = next((t for t in state.tasks if t.id == dep), None)
                            if not dep_task or dep_task.status != TaskStatus.DONE:
                                raise ValueError(f"Dependency '{dep}' is not completed")  # noqa: RAISE_STRING_BAN

                    # Phase 3 TOCTOU（ADR-0137 §4.4）：producer 內容重寫（產出/測試紀錄）
                    # → 既有「已放行」複審立即失效（stale 化），防「先蓋 PASS 再改產出」。
                    # 純狀態轉移（無內容參數）不重設——DOING→DONE 是正常收案路徑。
                    content_changed = any(
                        v is not None for v in (artifact_ref, artifact_refs, test_log)
                    )
                    if content_changed and task.review_status in REVIEW_PASSING_STATES:
                        task.review_status = None

                    task.status = status
                    if artifact_refs is not None:
                        task.artifact_refs = artifact_refs
                    if artifact_ref is not None:
                        task.artifact_ref = artifact_ref

                    if test_log is not None:
                        task.test_log = test_log
                    if blocker_detail is not None:
                        task.blocker_detail = blocker_detail
                    task.updated_at = now
                    found = True
                    break

            if not found:
                msg = f"Task ID {task_id} not found in mission"
                raise ValueError(msg)

            # 總體狀態自動判定（Phase 3：DONE 聚合同步要求審查放行——防後門直寫聚合）
            if all(t.status == TaskStatus.DONE and review_passing(t) for t in state.tasks):
                state.status = TaskStatus.DONE
            elif any(t.status == TaskStatus.DOING for t in state.tasks):
                state.status = TaskStatus.DOING
            elif any(t.status == TaskStatus.FAILED for t in state.tasks):
                state.status = TaskStatus.FAILED

            await self._save_mission_unlocked(identity, state, target_agent_id)
            return state
