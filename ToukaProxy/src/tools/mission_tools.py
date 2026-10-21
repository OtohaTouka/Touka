"""自主任務看板工具 (Autonomous Mission Board Tools).

作為代理人的「外部執行記憶 (External Executive Memory)」，
負責任務樹的分解、狀態追蹤與產出物連結。
此工具確保 Agent 在長期任務中具備進度感知能力，並能解決多輪對話中的專注力流失問題。

ADR-0051 擴充：
  - initialize + notify_on_complete=True → 為每個 task 生成 callback_token
  - await_dispatch_result → 在同一個 ReAct loop 內等待 DispatchAgent 回呼結果
"""

import logging
from pathlib import Path
from typing import Any

from memory_server.services.mission_manager import MissionPersistenceError
from src.core.exceptions import ErrorCode
from src.models.identity import AgentIdentity
from src.services import continuity_events
from src.services.approval_flow import CommandApprover
from src.services.auto_reviewer import auto_reviewer
from src.services.autonomy import autonomy_service
from src.services.dispatch_callback_store import get_callback_store
from src.services.isolation.policy import session_project_root
from src.services.resource_manager import resource_manager
from src.services.tool_errors import INTERNAL_ERROR, PERMISSION_DENIED, build_tool_error
from src.tools.contracts import native_parameters

logger = logging.getLogger(__name__)


def _agent_artifact_root(identity: AgentIdentity) -> Path | None:
    """互審蒐證的 artifact 檢查根＝看板擁有者的 personal vault（相對路徑 base）.

    與檔案動詞（read/write scope=personal）同一個沙盒根，也是 verification_cmd 的 cwd。
    解析不到時回 None（呼叫端 fail-closed），**不得**退回 Proxy 行程的 cwd。
    v1：以呼叫者身分的 vault 為準（主 agent 擁看板＋產出）。
    """
    base = resource_manager.get_vault_path("personal", identity)
    return Path(base) if base else None


def _normalize_task_keys(task: dict[str, Any]) -> dict[str, Any]:
    """Schema 扁平鍵（task_id/dependency_task_ids）→ 引擎鍵（id/dependencies）正規化.

    Tier 2 壓平：schema 廣告自我說明的鍵名（與 update_status 的 task_id 一致）；
    引擎（MissionManager）維持既有鍵。舊鍵直通（向後相容既有 prompt／錄製案例）；
    兩者並存時以既有引擎鍵優先。
    """
    out = {k: v for k, v in task.items() if k not in ("task_id", "dependency_task_ids")}
    if "id" not in out and task.get("task_id"):
        out["id"] = task["task_id"]
    if "dependencies" not in out and task.get("dependency_task_ids") is not None:
        out["dependencies"] = task["dependency_task_ids"]
    return out


async def mission_tools(action: str, **kwargs: Any) -> Any:
    """任務看板分發器：管理任務生命週期與狀態追蹤.

    SSOT: 任務資料實體儲存於 ToukaMemory (memory_server).

    Args:
        action (str): 執行的動作類型 (initialize, update_status, get_board).
        **kwargs (Any): 包含身份實體 (identity) 與具體動作參數.
            - goal (str): initialize 使用，任務大目標.
            - tasks (list[dict]): initialize 使用，初始清單.
            - task_id (str): update_status 使用.
            - status (str): TODO, DOING, DONE, FAILED, BLOCKED.
            - artifact_ref (str): 產出的檔案路徑或識別碼.
            - artifact_refs (list[str]): 變更檔案列表.
            - test_log (str): 測試結果輸出.
            - blocker_detail (str): 阻礙細節.
            - target_agent_id (str): 跨身分目標 Agent ID.

    Returns:
        str: 執行結果訊息或格式化後的 Markdown 看板.
    """
    try:
        from memory_server.models.mission import TaskStatus
        from memory_server.services.mission_manager import MissionManager
    except ImportError:
        logger.exception("❌ Mission system missing in memory_server")
        return "Error: Mission management system is not properly integrated."

    manager = MissionManager()

    # --- 1. 身分錨定與權限核驗 (Security Identity Anchoring) ---
    identity = kwargs.get("identity")
    if not isinstance(identity, AgentIdentity):
        return "Error: Missing or invalid authentication identity."

    target_agent_id = kwargs.get("target_agent_id")

    # --- 2. 邏輯分發器 ---
    try:
        if action == "initialize":
            goal = kwargs.get("goal")
            tasks = [
                _normalize_task_keys(t) if isinstance(t, dict) else t
                for t in kwargs.get("tasks", [])
            ]
            notify_on_complete: bool = bool(kwargs.get("notify_on_complete", False))
            if not goal:
                return "Error: 'goal' is required to initialize a mission."

            mission = await manager.initialize_mission(
                identity=identity,
                goal=goal,
                initial_tasks=tasks,
                target_agent_id=target_agent_id,
            )
            continuity_events.record_mission_started(identity, mission, kwargs.get("_ctx"))

            # ADR-0051：若 notify_on_complete=True，為每個 task 生成 callback_token
            if notify_on_complete and target_agent_id == "DispatchAgent":
                store = get_callback_store()
                callback_tokens: dict[str, str] = {}
                for task in mission.tasks:
                    token = store.generate_token()
                    store.register(token)
                    callback_tokens[task.id] = token

                # 持久化至 MissionState.metadata（透過 mission_manager 更新）
                mission.metadata["dispatch_callbacks"] = callback_tokens
                await manager.save_mission(identity, mission, target_agent_id)

                token_lines = "\n".join(f"  {tid}: {tok}" for tid, tok in callback_tokens.items())
                return (
                    f"✅ Mission Initialized: '{goal}' with {len(mission.tasks)} tasks.\n"
                    f"Callback tokens:\n{token_lines}"
                )

            return f"✅ Mission Initialized: '{goal}' with {len(mission.tasks)} tasks."

        if action == "await_dispatch_result":
            callback_token: str = kwargs.get("callback_token", "")
            timeout: float = float(kwargs.get("timeout", 300))
            if not callback_token:
                return "Error: 'callback_token' is required for await_dispatch_result."

            # Phase 4（需求 3 韌性）：耐久優先——結果已算完（含跨重啟：記憶體 Event
            # 沒了、但耐久落盤還在）即直接重讀、不空等到 TIMEOUT。
            from src.services.subagent_results import read_result  # noqa: LOCAL_IMPORT_BAN

            result = read_result(identity, callback_token)
            if result is None:
                result = await get_callback_store().wait_for_result(callback_token, timeout=timeout)
                # 等待期間（或剛好在等）才落盤者，超時再撈一次耐久（跨重啟窗）。
                if result.get("status") in ("TIMEOUT", "UNKNOWN_TOKEN"):
                    durable = read_result(identity, callback_token)
                    if durable is not None:
                        result = durable
            if result.get("status") == "TIMEOUT":
                return (
                    "Error: Timed out waiting for dispatch result. "
                    "The external AI may still be running — check the mission board later."
                )
            if result.get("status") == "UNKNOWN_TOKEN":
                return (
                    "Error: Unknown callback token. "
                    "Ensure the token was generated by initialize (notify_on_complete=True)."
                )
            lines = [
                "✅ Dispatch result received.",
                f"Task: {result.get('task_id', 'unknown')}",
                f"Status: {result.get('status', 'unknown')}",
            ]
            # Phase 2：結構化回報渲染（in-process 子代理 deliver 攜帶；缺欄位＝舊格式直通）
            report_status = result.get("report_status")
            if report_status:
                conf = result.get("confidence")
                conf_str = f", confidence {conf}" if conf is not None else ""
                struct_str = "structured" if result.get("structured") else "unstructured"
                lines.append(f"Report: {report_status} ({struct_str}{conf_str})")
            claims = result.get("claims") or []
            if claims:
                lines.append("Claims:")
                lines.extend(f"  - {c}" for c in claims[:8])
            artifact_refs = result.get("artifact_refs") or []
            if artifact_refs:
                lines.append(f"Artifacts: {', '.join(artifact_refs[:10])}")
            lines.append(f"Result: {result.get('result_summary', '')}")
            return "\n".join(lines)

        if action == "request_review":
            # Phase 3（ADR-0137）：對一個 task 跑一輪證據接地互審，結論落板。
            # 完整往返由主 agent 依回傳的 review_status 決策（APPROVED→可 DONE；
            # REVISING→修正後再 request_review；BLOCKED_FOR_HUMAN→human-ack）。
            task_id = kwargs.get("task_id")
            if not task_id:
                return "Error: 'task_id' is required for request_review."
            mission = await manager.get_current_mission(identity, target_agent_id)
            task = next((t for t in mission.tasks if t.id == task_id), None) if mission else None
            if task is None:
                return f"Error: task '{task_id}' not found on the mission board."
            artifact_root = _agent_artifact_root(identity)
            if artifact_root is None:
                return (
                    "Error: personal vault is unavailable; review evidence cannot be "
                    "collected safely."
                )

            # 延遲載入：破 review→services 匯入鏈於 tool 熱路徑（builtin/__init__）
            from src.services.review.review_loop import (  # noqa: LOCAL_IMPORT_BAN
                build_critic_completer,
                run_review_round,
            )

            producer_model = kwargs.get("model")
            critic_model = kwargs.get("critic_model") or producer_model
            # critic 走 complete_once：記帳到本工具的已驗證身分（ADR-0244）
            completer = build_critic_completer(identity, critic_model) if critic_model else None
            # B2：有人在場才建 approver——每條需要核准的 verification_cmd 各跳一張卡
            # （步驟 id＝<本次 id>:verify:<n>）。無人在場或子代理（卡會被子 loop 吞掉）
            # ＝None＝不跳卡、不執行，本輪等人確認（B1）。
            approver = (
                CommandApprover(
                    emit=kwargs["_emit_event"],
                    parent_call_id=kwargs["_call_id"],
                    agent_id=identity.agent_id,
                    session_id=kwargs.get("session_id"),
                    identity=identity,
                    ctx=kwargs.get("_ctx") or {},
                )
                if kwargs.get("_interactive")
                and kwargs.get("_emit_event")
                and kwargs.get("_call_id")
                and not (kwargs.get("_ctx") or {}).get("subagent_run")
                else None
            )
            # U2c-3：Auto 下 REVIEW 級驗證指令先由審查員決定（allow 才跑、escalate 再問人）
            session_id = kwargs.get("session_id")
            reviewer = auto_reviewer.command_reviewer(
                identity,
                user_intent=(kwargs.get("_ctx") or {}).get("user_intent"),
                human_present=approver is not None,
                session_id=session_id,
                conversation_key=session_id or str(kwargs.get("request_id") or task_id),
            )

            outcome = None
            try:
                outcome = await run_review_round(
                    task=task,
                    goal=mission.goal,
                    producer_claims=str(kwargs.get("producer_claims", "")),
                    artifact_root=artifact_root,
                    parent_identity=identity,
                    mission_id=mission.mission_id,
                    manager=manager,
                    completer=completer,
                    critic_model=critic_model,
                    producer_model=producer_model,
                    # 掃描缺口 E76（ADR-0175）：接上 autonomy config——cost_ceiling_exceeded
                    # （互審成本閘、無視 cap.enabled 不可關）與 review.max_attempts 收緊都
                    # 吃這個 config；漏傳＝恆 None＝成本天花板在生產永不觸發。
                    config=autonomy_service.config,
                    # verification_cmd 與 execute_command 同一套指令政策；不採用 request_review
                    # 卡的 _hitl_preapproved（卡上看不到指令，B2）。
                    auth_level=kwargs.get("auth_level", "Manual"),
                    approver=approver,
                    reviewer=reviewer,
                    # 驗證指令的專案根只來自 session（不讀 project_id kwarg）
                    project_root=await session_project_root(kwargs.get("_ctx") or {}, session_id),
                )
            finally:
                # 核准後審查若中途拋例外，已開的 verify 步驟以 internal_error 收尾
                if approver is not None:
                    await approver.finish(outcome.evidence_digest if outcome is not None else None)
            if outcome.human_gate:
                # B1：卡在人類這一關——不叫代理人自己繞去跑指令；與永鎖（一般審查結果）區分
                rejected = outcome.human_gate == "human_rejected_command"
                return build_tool_error(
                    PERMISSION_DENIED,
                    f"The verification command of task '{task_id}' was not run: {outcome.note}.",
                    suggestion=(
                        "Ask the human how this task should be verified (for example, a "
                        "different verification command) before running request_review again."
                        if rejected
                        else "Wait for the human to come back, then run request_review again "
                        "in the chat. Do not run the command another way."
                    ),
                    reason=outcome.human_gate,
                    review_status=outcome.review_status,
                    review_locked=False,
                    evidence=outcome.evidence_digest,
                )
            next_hint = {
                "APPROVED": "審查通過——可 update_status DONE。",
                "APPROVED_LOW_CONFIDENCE": "同模型審查放行但不背書——建議人工複核後再 DONE。",
                "REVISING": "退回修正——依 critic 意見修改產出後再 request_review。",
                "BLOCKED_FOR_HUMAN": "轉人工——需 human-ack 或改任務狀態。",
            }.get(outcome.review_status, "")
            return (
                f"🔎 Review [{task_id}] → {outcome.review_status}\n"
                f"{outcome.note}\n"
                f"Evidence: {outcome.evidence_digest}\n"
                f"{next_hint}"
            )

        if action == "finalize":
            # Phase 2（需求 5 閉環）：mission 級最終驗收原語。
            # 無 verdict → 驗收清單（不記錄、不執行 verification_cmd——證據執行屬 Phase 3）；
            # 帶 verdict → 經 manager.finalize_mission 記錄（僅全 DONE 可記，鎖內原子）。
            verdict = kwargs.get("verdict")
            mission = await manager.get_current_mission(identity, target_agent_id)
            if not mission:
                return "Error: no active mission to finalize."

            if verdict is not None and str(verdict).strip():
                state = await manager.finalize_mission(identity, str(verdict), target_agent_id)
                continuity_events.record_mission_finalized(identity, state, kwargs.get("_ctx"))
                return f"✅ Mission finalized: '{state.goal}'\nVerdict: {state.final_verdict}"

            if mission.status != TaskStatus.DONE:
                incomplete = [t.id for t in mission.tasks if t.status != TaskStatus.DONE]
                return (
                    "Error: mission not complete — finalize 前所有任務須 DONE。"
                    f" 未完成: {', '.join(incomplete)}"
                )

            res = [
                "## 🏁 Finalize 驗收清單",
                f"**原始目標 (goal)**: {mission.goal}",
                "",
                "**逐任務產出面（請對照原始 goal 逐項確認）：**",
            ]
            for t in mission.tasks:
                arts = ", ".join(t.artifact_refs) if t.artifact_refs else (t.artifact_ref or "—")
                t_log = "test_log ✔" if t.test_log else "test_log ✘"
                verify = (
                    f" | Verify: `{t.verification_cmd}`（系統未代跑，請自行確認）"
                    if t.verification_cmd
                    else ""
                )
                res.append(f"- **{t.id}** {t.description} | Artifacts: {arts} | {t_log}{verify}")
            res.append("")
            res.append(
                "逐項確認達成後，以 mission_tools(action='finalize', "
                "verdict='你的驗收結論') 記錄收案；未達成請如實改回任務狀態。"
            )
            return "\n".join(res)

        if action == "update_status":
            task_id = kwargs.get("task_id")
            status_str = kwargs.get("status")
            artifact = kwargs.get("artifact_ref")
            artifact_refs = kwargs.get("artifact_refs")
            test_log = kwargs.get("test_log")
            blocker_detail = kwargs.get("blocker_detail")

            if not task_id or not status_str:
                return "Error: 'task_id' and 'status' are required."

            try:
                status = TaskStatus(status_str.upper())
            except ValueError:
                return f"Error: Invalid status '{status_str}'."

            state = await manager.update_task(
                identity=identity,
                task_id=task_id,
                status=status,
                artifact_ref=artifact,
                artifact_refs=artifact_refs,
                test_log=test_log,
                blocker_detail=blocker_detail,
                target_agent_id=target_agent_id,
            )
            continuity_events.record_task_update(
                identity, state, task_id, status, kwargs.get("_ctx")
            )
            return f"✅ Task '{task_id}' updated to {status_str}."

        if action == "get_board":
            mission = await manager.get_current_mission(identity, target_agent_id)
            if not mission:
                return "No active mission board found. Use 'initialize' to start one."

            res = [f"## 📋 Mission: {mission.goal}"]
            res.append(f"**Overall Status:** {mission.status}")
            res.append("\n**Tasks:**")
            for t in mission.tasks:
                icon = "✅" if t.status == "DONE" else "⏳" if t.status == "DOING" else "⬜"
                dep = f" (Needs: {', '.join(t.dependencies)})" if t.dependencies else ""

                art_list = []
                if t.artifact_ref:
                    art_list.append(f"Art: {t.artifact_ref}")
                if getattr(t, "artifact_refs", None):
                    art_list.append(f"Arts: {', '.join(t.artifact_refs)}")
                art_str = f" [{'; '.join(art_list)}]" if art_list else ""

                verification = (
                    f" (Verify: {t.verification_cmd})"
                    if getattr(t, "verification_cmd", None)
                    else ""
                )
                t_log = f"\n   *Test Log:* {t.test_log}" if getattr(t, "test_log", None) else ""
                b_detail = (
                    f"\n   *Blocker:* {t.blocker_detail}"
                    if getattr(t, "blocker_detail", None)
                    else ""
                )

                res.append(
                    f"{icon} **{t.id}**: {t.description}"
                    f"{dep}{art_str}{verification}{t_log}{b_detail}"
                )

            return "\n".join(res)

    except MissionPersistenceError:
        return build_tool_error(
            INTERNAL_ERROR,
            "Mission persistence could not be confirmed.",
            error_code=ErrorCode.PERSISTENCE_ERROR.value,
            outcome="unknown",
            suggestion="Read the current mission before deciding whether to save again.",
        )
    except Exception as e:
        logger.exception("🔥 Error in mission_tools logic execution")
        return f"Error executing mission operation: {str(e)}"
    else:
        return f"Error: Unknown action '{action}' for mission_tools."


# --- [Schema Definition] ---

MISSION_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mission_tools",
        "description": (
            "自主任務看板：把多步驟目標拆解為子任務並追蹤進度（你的外部執行記憶）。"
            "何時使用：使用者給出需要多個步驟完成的目標時，直接以 initialize 建立"
            "看板（可依合理假設拆解，毋須先追問細節）；執行中以 update_status 回報、"
            "get_board 查看。身分由系統自動注入。"
        ),
        "parameters": native_parameters("mission_tools"),
    },
}
