"""請求分發器 (Dispatcher) 模組.

作為系統請求的頂層路由中樞 (SOLID - SRP)，負責將進入的請求分發至
單模型執行器或群聊執行器，並統一處理「串流 (Streaming)」與「非串流 (Buffered)」的響應格式轉譯.
"""

import asyncio
import copy
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import (
    FailedResult,
    PersistedStreamAdmission,
    SavedResult,
    SaveResult,
    SessionChanges,
    SkippedResult,
    UiGenerationStopped,
    UiPersistence,
    UnknownResult,
)
from src.services.agent_service import agent_service
from src.services.authorization_gateway import can_access_session
from src.services.dispatchers import dispatch_group_stream, get_stream_generator
from src.services.hooks import events as hook_events
from src.services.identity_service import identity_service
from src.services.interjection_queue import InterjectionQueue, interjection_queue
from src.services.sliding_window_service import sliding_window_service
from src.services.stream_buffer_service import (
    PendingRequest,
    StreamOwnershipError,
    stream_buffer_service,
)
from src.utils.async_db import SessionSnapshot, session_service
from src.utils.brain_persistence import record_working_brain
from src.utils.history_manager import TreeMessageManager
from src.utils.session_scope import is_ui_turn_base
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


def _unknown_ui_save() -> UnknownResult:
    return UnknownResult(
        status="unknown", error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id
    )


def _reject_ui_save() -> FailedResult:
    return FailedResult(
        status="failed", error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id
    )


def _valid_ui_base(snapshot: SessionSnapshot, admission: PersistedStreamAdmission) -> bool:
    if snapshot.session is None or snapshot.revision != admission.revision:
        return False
    return is_ui_turn_base(
        snapshot.session, admission.user_parent_id, admission.assistant_node_id
    )


async def _persist_ui_assistant_turn(
    session_id: str,
    content: str,
    model_name: str,
    requester: AgentIdentity,
    snapshot: SessionSnapshot,
    admission: PersistedStreamAdmission,
    *,
    stream_id: str,
    interjections: InterjectionQueue,
    execution: AgentIdentity | None = None,
) -> SaveResult:
    tree = TreeMessageManager.append_node(
        copy.deepcopy(snapshot.session["messages"]),
        "assistant",
        content,
        name=model_name,
        parent_id=admission.user_parent_id,
        node_id=admission.assistant_node_id,
    )
    # ADR-0249：這個串流已送達的插話存進同一個助理節點（不算回合）；沒有就不加欄位。
    # 串流結束才清空佇列（StreamBufferService._complete），這裡在 finish 之前讀。
    delivered = [
        item.model_dump(mode="json")
        for item in interjections.list_items(session_id, stream_id)
        if item.status == "delivered"
    ]
    if delivered:
        tree["messages"][admission.assistant_node_id]["interjections"] = delivered
    changes = SessionChanges(messages=tree, updatedAt=int(get_current_epoch() * 1000))
    if execution:
        session_service.require_scope(snapshot.session, execution)
        changes = changes.model_copy(update={"agent_id": execution})
    result = await session_service.conditional_save_session(
        session_id,
        admission.revision,
        changes,
        requester,
        representation="text",
    )
    # bool False 及其他未驗證回覆不能表示已保存。
    return (
        result
        if isinstance(result, (SavedResult, FailedResult, UnknownResult))
        else _unknown_ui_save()
    )


async def _pump_ui_stream(
    gen: AsyncGenerator[str],
    session_id: str,
    stream_id: str,
    model_name: str,
    requester: AgentIdentity,
    admission: UiPersistence,
    execution: AgentIdentity | None = None,
) -> None:
    """明示 UI 契約：固定基底、獨立生成/保存終態；不自動保存生成失敗的部分。"""
    result: SaveResult = _unknown_ui_save()
    snapshot: SessionSnapshot | None = None
    full_content = ""
    try:
        if admission.mode == "persistent":
            snapshot = await session_service.read_persistence_snapshot(session_id, requester)
            if not _valid_ui_base(snapshot, admission):
                await stream_buffer_service.finish(
                    session_id, "error", stream_id=stream_id, persistence=_reject_ui_save()
                )
                return
            if execution:
                session_service.require_scope(snapshot.session, execution)
        async for chunk in gen:
            if not await stream_buffer_service.append(session_id, chunk, stream_id=stream_id):
                return
            full_content += _extract_delta_text(chunk)
    except asyncio.CancelledError:
        await stream_buffer_service.finish(session_id, "cancelled", stream_id=stream_id)
        raise
    except UiGenerationStopped as stopped:
        await stream_buffer_service.finish(session_id, stopped.generation, stream_id=stream_id)
        return
    except Exception:
        await stream_buffer_service.finish(session_id, "error", stream_id=stream_id)
        return
    else:
        if admission.mode == "incognito":
            result = SkippedResult(status="skipped", reason="incognito")
        elif not full_content.strip():
            result = SkippedResult(status="skipped", reason="no_text")
        else:
            try:
                result = await _persist_ui_assistant_turn(
                    session_id,
                    full_content,
                    model_name,
                    requester,
                    snapshot,
                    admission,
                    stream_id=stream_id,
                    interjections=interjection_queue,
                    execution=execution,
                )
            except asyncio.CancelledError:
                await stream_buffer_service.finish(session_id, "cancelled", stream_id=stream_id)
                raise
            except Exception:
                result = _unknown_ui_save()
        if isinstance(result, SavedResult):
            spawn_logged(
                _maybe_trim_after_chat(session_id), f"sliding_window_chat:{session_id}"
            )
        await stream_buffer_service.finish(
            session_id, "done", stream_id=stream_id, persistence=result
        )
    finally:
        await gen.aclose()


def _extract_delta_text(line: str) -> str:
    """從單一 SSE 行擷取增量文字（OpenAI delta / Anthropic content_block_delta）。

    非 data 行、[DONE]、解析失敗或無文字 → 回空字串（純函式，供 producer 累積
    最終回覆內容以便伺服器端持久化，ADR-0081）。
    """
    if not line.startswith("data: "):
        return ""
    data_str = line[6:].strip()
    if not data_str or data_str == "[DONE]":
        return ""
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return ""
    choices = data.get("choices") if isinstance(data, dict) else None
    if choices:
        try:
            return choices[0].get("delta", {}).get("content") or ""
        except (IndexError, AttributeError):
            return ""
    if isinstance(data, dict) and data.get("type") == "content_block_delta":
        return data.get("delta", {}).get("text") or ""
    return ""


async def _persist_assistant_turn(
    session_id: str, content: str, model_name: str, identity: AgentIdentity,
    *, check_access: bool = True,
) -> bool:
    """伺服器端把完成的 assistant 回覆接進 session 樹（照 heartbeat 既有範式）。

    前端若全程連線，onDone 會再以富樹（含 steps）整棵覆寫——順序為本函式先寫、
    前端後寫，最終以前端富版為準；前端從未接回時本寫入即最終結果（ADR-0081）。

    人類身分只供授權與 CAS，不作為 agent_id；既有 owner 不能被回覆保存改寫。
    """
    snapshot = await session_service.read_persistence_snapshot(session_id, identity)
    session = snapshot.session
    if not session:
        logger.warning("⚠️ [Resumable] Session %s not found; skip persistence.", session_id)
        return False
    # 舊串流路徑寫回前驗存取：照路由的 session 存取規則（擁有者或 admin，ADR-0245 修補）
    owner = session.get("owner_user_id")
    if check_access and not can_access_session(identity, session.get("agent_id"), owner):
        logger.warning("⚠️ [Resumable] %s cannot write %s; skip.", identity.agent_id, session_id)
        return False
    tree = TreeMessageManager.append_node(
        session["messages"], "assistant", content, name=model_name
    )
    result = await session_service.conditional_save_session(
        session_id, snapshot.revision, SessionChanges(messages=tree), identity,
    )
    return isinstance(result, SavedResult)


async def _legacy_write_authorized(context: dict[str, Any], session_id: str) -> bool:
    """舊串流路徑該不該為這個 session 建 buffer／寫回（ADR-0245 修補，LOW 3）。

    靈魂對話沿用 SoulLayer 的綁定判定；一般模型對話照 session 存取規則。既有 session
    且呼叫者不能存取 → False（不建 buffer、不寫回、改走一般串流）；新 session（尚未建立）
    → True，維持首次建立的既有行為。
    """
    if context.get("is_toukasoul"):
        return context.get("session_bound") is True
    identity = context.get("identity")
    if not isinstance(identity, AgentIdentity):
        return True
    session = await session_service.get_session_by_id(session_id)
    if not session:
        return True
    return can_access_session(identity, session.get("agent_id"), session.get("owner_user_id"))


def _home_soul_identity(agent_id: Any) -> AgentIdentity | None:
    """家對話歸屬（home_for_agent）的靈魂身分；判定與設家相同（正式靈魂且 ID 不需正規化）。

    身分建法與心跳相同（``identity_service.get_identity``）。不是靈魂或查不到回 None。
    """
    passport = agent_service.identify_agent(agent_id) if isinstance(agent_id, str) else None
    if not passport or not passport.get("is_toukasoul") or passport.get("agent_id") != agent_id:
        return None
    return identity_service.get_identity(agent_id)


async def _maybe_trim_after_chat(session_id: str) -> None:
    """聊天回合落盤後的滑動視窗觸發（ADR-0092 Phase D / 禁令 28 第二授權觸發點）。

    僅家對話（子對話排除，對齊 heartbeat）；`check_and_trim` 內含 per-session 鎖、
    換模型冷卻與無需剪裁的早退。非阻塞背景執行（spawn_logged），失敗只記 log、
    不影響串流終態。剪裁身分是家對話所屬的靈魂（和心跳同一個），不是 UI 請求者：
    否則剪下的舊對話與摘要記帳會落到人類名下、靈魂找不到。解析不到靈魂就不剪。
    """
    session = await session_service.get_session_by_id(session_id)
    if not session or not session.get("is_home"):
        return
    soul = _home_soul_identity(session.get("home_for_agent"))
    if soul is None:
        logger.warning("SlidingWindow: home session %s has no soul owner; skip trim", session_id)
        return
    await sliding_window_service.check_and_trim(soul, session_id)


async def _pump_stream_to_buffer(
    gen: AsyncGenerator[str],
    session_id: str,
    model_name: str,
    identity: AgentIdentity | None,
    incognito: bool = False,
    persist: bool = True,
    check_access: bool = False,
) -> None:
    """producer：抽乾 handler 串流寫入緩衝；正常完成則先持久化再標記終態。

    與 HTTP 連線完全解耦——client 斷線只取消訂閱者，本協程續跑（ADR-0081）。
    """
    full_content = ""
    try:
        async for chunk in gen:
            full_content += _extract_delta_text(chunk)
            await stream_buffer_service.append(session_id, chunk)
    except asyncio.CancelledError:
        # stop 路徑：stream_buffer_service.cancel 已標記終態並喚醒訂閱者
        raise
    except Exception:
        logger.exception("🔥 [Resumable] Producer failed for %s", session_id)
        await stream_buffer_service.finish(session_id, "error")
        return

    if identity and full_content.strip() and not incognito and persist:
        persisted = False
        try:
            persisted = await _persist_assistant_turn(
                session_id, full_content, model_name, identity, check_access=check_access
            )
        except Exception:
            logger.exception("🔥 [Resumable] Persistence failed for %s", session_id)
        # ADR-0092 Phase D：聊天回合落盤後，非阻塞觸發滑動視窗（家對話、經鎖、
        # 不阻斷 finish；禁令 28 第二觸發點、禁令 57）。沒有權限寫回就不剪。
        if persisted:
            spawn_logged(
                _maybe_trim_after_chat(session_id),
                f"sliding_window_chat:{session_id}",
            )
        else:
            await stream_buffer_service.finish(session_id, "error")
            return
    elif not identity:
        logger.warning("⚠️ [Resumable] No identity for %s; skip persistence.", session_id)

    # 先持久化、後終態：訂閱者看到 done 時資料已落盤
    await stream_buffer_service.finish(session_id, "done")


def _reassemble_final_json(last_json: dict[str, Any], full_content: str) -> dict[str, Any]:
    """將串流 Delta 重組為標準 OpenAI 最終響應格式。

    部分供應商的終端 chunk 可能是 ``"choices": []``（如用量統計 chunk），
    直接索引 [0] 會 IndexError —— 以 truthy 檢查同時涵蓋缺鍵與空陣列。
    """
    if last_json.get("choices"):
        last_json["choices"][0]["message"] = {"role": "assistant", "content": full_content}
        if "delta" in last_json["choices"][0]:
            del last_json["choices"][0]["delta"]
    return last_json


async def dispatch_request(
    model_name: str,
    request: Any,
    identity: AgentIdentity,
    request_id: str,
    start_time: float,
    context: dict[str, Any],
) -> StreamingResponse | JSONResponse:
    """全系統統一請求分發入口.

    支援群聊路由 (ToukaGroup) 與單一模型路由. 自動根據 request 內容判斷
    是否執行串流輸出. 若為非串流模式，則會自動收集所有 Chunk 並重組成標準 JSON 格式.

    Args:
        model_name (str): 目標模型名稱.
        request (Any): 原始請求對象 (通常來自 FastAPI 或 Pydantic 模型).
        identity (AgentIdentity): 經驗證的代理人身分物件.
        request_id (str): 請求追蹤唯一識別碼.
        start_time (float): 請求開始的時間戳.
        context (dict[str, Any]): 執行上下文 (包含 session_id, auth_level 等).

    Returns:
        StreamingResponse | JSONResponse: 符合 OpenAI 規範的響應對象.
    """
    is_streaming = getattr(request, "stream", False)
    admission = getattr(request, "ui_persistence", None)

    async def stream_generator() -> AsyncGenerator[str]:
        """封裝後的內建串流產生器，負責執行實體路由轉向."""
        nonlocal model_name
        try:
            # 1. 群聊路由 (Group Routing)
            if model_name.lower() == "toukagroup":
                async for chunk in dispatch_group_stream(request, request_id, start_time, context):
                    yield chunk
                return

            # 2. 單一模型路由 (Single Model Routing)
            stream_gen = await get_stream_generator(
                model_name,
                request,
                identity,
                request_id,
                start_time,
                context,
                session_id=context.get("session_id"),
            )

            async for chunk in stream_gen:
                yield chunk

            # 成功執行後記錄活躍大腦 (Persistence)；guest（無身分）無腦可記
            if identity:
                record_working_brain(identity, model_name)

        except Exception:
            if admission is not None:
                # 真正 fatal 生成錯誤交給 UI producer；可恢復 tool_result 仍是普通 frame。
                raise
            logger.exception("Error occured in dispatcher stream execution")
            yield f"data: {json.dumps({'error': 'Dispatcher error during stream'})}\n\n"

    # --- [非串流模式：收集並解包 JSON] ---
    if not is_streaming:
        full_content = ""
        last_json = {}
        async for chunk in stream_generator():
            if chunk.startswith("data: "):
                data_str = chunk[6:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                    last_json = data
                    # 嘗試提取 content (相容 OpenAI/Anthropic 轉譯格式)
                    if "choices" in data:
                        delta = data["choices"][0].get("delta", {})
                        full_content += delta.get("content", "")
                    elif data.get("type") == "content_block_delta":
                        full_content += data.get("delta", {}).get("text", "")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        # 如果有完整內容，將其由 Delta 格式重組成標準 OpenAI 最終響應格式
        return JSONResponse(content=_reassemble_final_json(last_json, full_content))

    # --- [ADR-0081 可恢復串流：生成與連線解耦] ---
    # 僅 UI 對話端點（proxy.py 設 resumable_stream 旗標）且有 session_id 才走緩衝路徑；
    # heartbeat 脈衝與外部 API 呼叫不設旗標 → 走下方原路徑（防雙重持久化）。
    session_id = context.get("session_id")
    if admission is not None:
        requester = context.get("ui_requester")
        if (
            not isinstance(requester, AgentIdentity)
            or context.get("ui_authorized") is not True
            or not session_id
        ):
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        if admission.mode == "persistent":
            if not requester.is_human:
                raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
            execution = None
            if context.get("is_toukasoul"):
                passport = agent_service.identify_agent(context.get("agent_id"))
                if (
                    not passport or not passport["is_toukasoul"] or identity.is_human
                    or identity.agent_id != passport["agent_id"]
                    or context.get("identity") != identity
                ):
                    raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
                execution = identity
            snapshot = await session_service.admit_ui_stream(
                session_id, admission, requester, execution
            )
            if isinstance(snapshot, UnknownResult):
                return JSONResponse(
                    status_code=503, content={"persistence": _unknown_ui_save().model_dump()}
                )
            if not isinstance(snapshot, SessionSnapshot):
                return JSONResponse(
                    status_code=409, content={"persistence": snapshot.model_dump()}
                )
            admission = admission.model_copy(update={"revision": snapshot.revision})
        else:
            execution = None
        pending = context.get("ui_pending")
        if isinstance(pending, PendingRequest) and pending.stopped:
            # ADR-0292：還沒開始串流時按了停止（標在這一則請求上，同一對話之後的請求清不掉）
            raise ToukaException(ErrorCode.PROMPT_STOPPED)
        try:
            entry = await stream_buffer_service.create(
                session_id, identity=requester, admission=admission,
                hook_fields=hook_events.base_payload(context),
            )
        except StreamOwnershipError:
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED) from None
        producer = spawn_logged(
            _pump_ui_stream(
                stream_generator(), session_id, entry.stream_id, model_name, requester, admission,
                execution,
            ),
            name=f"ui-stream-pump:{session_id}",
        )
        stream_buffer_service.attach_task(session_id, producer, stream_id=entry.stream_id)
        return StreamingResponse(
            stream_buffer_service.subscribe(
                session_id, starting_after=-1, stream_id=entry.stream_id
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if (
        context.get("resumable_stream") is True
        and session_id
        and await _legacy_write_authorized(context, session_id)
    ):
        try:
            await stream_buffer_service.create(session_id)
        except StreamOwnershipError:
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED) from None
        producer = spawn_logged(
            _pump_stream_to_buffer(
                stream_generator(),
                session_id,
                model_name,
                context.get("identity"),
                incognito=bool(context.get("incognito")),
                # 靈魂對話只寫回綁在這個靈魂的 session（SoulLayer 判定，ADR-0245 修補）
                persist=not context.get("is_toukasoul") or context.get("session_bound") is True,
                check_access=not context.get("is_toukasoul"),  # 靈魂對話已由綁定涵蓋
            ),
            name=f"stream-pump:{session_id}",
        )
        stream_buffer_service.attach_task(session_id, producer)
        return StreamingResponse(
            stream_buffer_service.subscribe(session_id, starting_after=-1),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- [串流模式：維持 SSE 格式輸出] ---
    return StreamingResponse(stream_generator(), media_type="text/event-stream")
