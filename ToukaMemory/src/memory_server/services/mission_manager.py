# ruff: noqa: E501, TRY003, RUF100, I001
"""任務看板管理器 (Mission Manager).

本模組負責管理代理人的自主任務看板 (Mission Board)。
提供任務樹的拆解、狀態追蹤、以及物理持久化至個人硬碟 (Personal Drive) 的功能。
支援任務狀態更新、產出物 (Artifacts) 關聯以及總體進度判定。
"""

from __future__ import annotations

import json
import logging
import uuid
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
    from memory_server.utils.zone_paths import ZonePath
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


class MissionPersistenceError(RuntimeError):
    """保存提交無法確認；安全訊息不包含受管 I/O 的原始內容或路徑。"""

    def __init__(self) -> None:
        super().__init__("Mission persistence failed")


class MissionManager:
    """自主任務看板管理員 (SOLID - SRP).

    負責代理人任務的生命週期管理，確保狀態更新與物理存儲的一致性。
    """

    def _get_lock(self, agent_id: str) -> MissionLock:  # noqa: IDENTITY_SSOT
        """獲取指定代理人任務看板的排他鎖（asyncio.Lock + FileLock，ADR-0049）.

        鎖檔是 ``<data>/temp/locks/missions/<id>.lock``，不在代理人寫得到的資料夾（ADR-0293）。
        """
        if not is_zone_segment(agent_id):
            raise ZoneSegmentError
        lock_dir = Path(_get_app_data_dir()) / "temp" / "locks" / "missions"
        get_resource_proxy().make_dir(str(lock_dir))
        return MissionLock(agent_id, lock_dir / agent_id)

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
    ) -> ZonePath:
        """獲取該代理人任務看板的區內路徑 (SSOT；ADR-0293 起不建資料夾、不跟隨連結).

        C2 修復：不再獨立校驗權限，由呼叫方的 _resolve_target_agent_id 集中負責。
        本方法僅依據 target_agent_id 解析正確的存儲路徑。

        跨身分定址修復：看板路徑必須是「目標 agent_id 的決定論函數」，與存取者身分
        無關。舊版跨身分分支以硬編 role="Base" 的最小身分呼叫 ``get_vault_path``，
        而 personal vault 依 ``is_toukasoul`` 分流（Owner/靈魂 → personal_drive/、
        Base/自定義 → custom/<id>/drive/）——於是 Owner/靈魂代理的看板會被跨讀誤解析
        到 custom/ 側，與擁有者自讀的 personal_drive/ 分裂（跨讀回 None 或寫到分裂位置）。
        改由 proxy 依 agent_id 查**真實註冊身分**定址，消弭角色臆測。
        """
        proxy = get_resource_proxy()

        if target_agent_id and target_agent_id != identity.agent_id:
            # 跨身分：依真實註冊身分（role/capabilities）決定論定址，不臆測角色。
            drive = proxy.agent_drive_for(target_agent_id)
        else:
            drive = proxy.agent_drive(identity)
        if drive is None:  # agent_id 不能當成資料夾名稱
            raise ZoneSegmentError
        return drive.child("missions", "current_mission.json")

    async def _get_current_mission_unlocked(
        self, identity: AgentIdentity, target_agent_id: str | None = None
    ) -> MissionState | None:
        """載入目前代理人進行中的任務看板（無鎖內部方法）.

        經過連結時往外丟 ``LinkRefusedError``（ADR-0293），其他讀取失敗當作沒有看板。
        """
        path = self._get_mission_path(identity, target_agent_id)
        try:
            content = get_resource_proxy().zone_read_text(path)
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

        pending = state.model_copy(deep=True)
        pending.updated_at = get_current_epoch()

        path = self._get_mission_path(identity, target_agent_id)
        try:
            get_resource_proxy().zone_write_text(path, pending.model_dump_json(indent=4))
        except LinkRefusedError:  # 訊息固定、沒有寫入，不包成「結果未知」（ADR-0293）
            raise
        except Exception as exc:
            logger.exception("❌ [MissionManager] Failed to save mission for %s", state.agent_id)
            raise MissionPersistenceError from exc
        state.updated_at = pending.updated_at
        logger.info(
            "✅ [MissionManager] Mission saved for %s at %s", state.agent_id, path.display()
        )

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
