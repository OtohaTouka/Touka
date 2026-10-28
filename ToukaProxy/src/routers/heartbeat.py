"""心跳路由 (Heartbeat Router).

處理心跳喚醒與連通性測試。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from src.services.continuity_events import HeartbeatEnd

import httpx
import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response  # noqa: TC002
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from memory_server.utils.content_guard import sanitize_memory_content
from pydantic import BaseModel, Field

from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.config import HEARTBEAT_EXTERNAL_URL, HEARTBEAT_TOKEN
from src.core.exceptions import ErrorCode, ToukaException
from src.core.security import AuthSecretManager
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.heartbeat_schedule import HeartbeatSchedule
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.session_persistence import SavedResult, SessionChanges
from src.models.user import User  # noqa: TC001
from src.routers.auth import get_auth_service, get_current_user
from src.routers.ui_sessions import _can_access_session
from src.services import destructive_ops
from src.services.activity_registry import activity_registry
from src.services.auth_service import AuthService  # noqa: TC001
from src.services.autonomy import autonomy_service
from src.services.continuity_events import (
    heartbeat_run_key,
    record_heartbeat_finished,
    record_heartbeat_started,
)
from src.services.continuity_service import inject_continuity
from src.services.dispatcher import dispatch_request
from src.services.heartbeat_schedule_bridge import HeartbeatScheduleBridge
from src.services.hooks.stopping import request_stopped, stop_baseline
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.model_manager import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.rhythm_service import rhythm_service
from src.services.sliding_window_service import sliding_window_service
from src.services.soul_service import soul_service
from src.utils import user_intent
from src.utils.async_db import SessionPersistenceError, session_service
from src.utils.auth_mode import AuthMode, authorize_auth_mode  # noqa: TC001
from src.utils.brain_persistence import get_active_brain
from src.utils.history_manager import TreeMessageManager
from src.utils.interjection_notes import annotate_node
from src.utils.session_scope import channel_session_id
from src.utils.time_utils import format_epoch_to_taipei, get_current_epoch


def verify_heartbeat_token(
    request: Request,  # noqa: TC002  — for X-Heartbeat-Token header
    req: HeartbeatAwakenRequest,  # body token as fallback (SSOT: single source)
) -> str:
    """Validate and return the heartbeat service token (SOLID-SRP SSOT).

    Owns ALL token-validation logic so awaken_agent body stays clean.
    Supports two token sources (header wins over body) and two validation
    strategies (plain secret comparison, then JWT):

    1. Extract: X-Heartbeat-Token header  OR  req.token body field
    2. Validate:
       a. If HEARTBEAT_TOKEN is set and matches → accepted
       b. Otherwise → decode as service JWT (sub="heartbeat", type="service")
    3. Raise ToukaException(HEARTBEAT_INVALID_TOKEN) on any failure
    """
    service_token: str = request.headers.get("X-Heartbeat-Token") or req.token

    if not service_token:
        raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN)

    # Strategy A: plain secret comparison (fast path for scripts / Heartbeat service)
    # #2/#5 fix: constant-time comparison (secrets.compare_digest) to remove the
    # token-length/prefix timing side-channel, matching the project's compare_digest
    # standard for shared secrets. bytes operands avoid a non-ascii TypeError.
    if HEARTBEAT_TOKEN and secrets.compare_digest(service_token.encode(), HEARTBEAT_TOKEN.encode()):
        return service_token

    # Strategy B: JWT validation (standard inter-service auth)
    try:
        payload = jwt.decode(service_token, AuthSecretManager.get_secret(), algorithms=["HS256"])
        if payload.get("type") != "service" or payload.get("sub") != "heartbeat":
            raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN)  # noqa: TRY301
    except ToukaException:
        raise
    except Exception:
        raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN) from None

    return service_token


router = APIRouter(prefix="/heartbeat", tags=["heartbeat"])
logger = logging.getLogger(__name__)


class HeartbeatScheduleRoute(APIRoute):
    """Sanitize input errors only for human schedule management routes."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """Keep authentication while replacing raw Pydantic diagnostics."""
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ToukaException(ErrorCode.HEARTBEAT_SCHEDULE_INVALID) from None

        return handle


schedule_router = APIRouter(
    prefix="/schedules", tags=["heartbeat"], route_class=HeartbeatScheduleRoute
)


def _human_schedule_bridge(user: User) -> HeartbeatScheduleBridge:
    identity = AgentIdentity(
        agent_id=user.id,
        is_human=True,
        human_role="admin" if user.role == "admin" else "normal",
        owned_agents=user.owned_agents,
    )

    def can_manage(agent_id: str) -> bool:  # noqa: IDENTITY_SSOT
        return resource_manager.is_owner(AgentIdentity(agent_id=agent_id), identity)

    return HeartbeatScheduleBridge(HEARTBEAT_EXTERNAL_URL, HEARTBEAT_TOKEN, can_manage)


@schedule_router.get("", response_model=list[HeartbeatSchedule])
async def list_human_heartbeat_schedules(
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
) -> list[HeartbeatSchedule]:
    """List schedules visible to the freshly authenticated human owner."""
    schedules, revision = await _human_schedule_bridge(user).list()
    response.headers["ETag"] = revision
    return schedules


@schedule_router.post("", response_model=HeartbeatSchedule)
async def upsert_human_heartbeat_schedule(
    schedule: HeartbeatSchedule,
    if_match: str | None = Header(default=None),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
) -> HeartbeatSchedule:
    """Upsert an owned schedule through the revision-checked service bridge."""
    return await _human_schedule_bridge(user).upsert(schedule, if_match)


@schedule_router.delete("/{schedule_id:path}")
async def delete_human_heartbeat_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, str]:
    """Delete an owned schedule without revealing inaccessible identifiers."""
    await _human_schedule_bridge(user).delete(schedule_id, if_match)
    return {"status": "deleted"}


router.include_router(schedule_router)


# DA24 + ADR-0246 U2c-5 (decision 8): heartbeat security_level → mode. Only the explicit
# "full" value may escalate to YOLO; safe → Auto (headless: no terminal, no reviewer call);
# any unknown/garbage value fails safe to the strictest mode, Manual.
_SECURITY_LEVEL_TO_AUTH = {"safe": "Auto", "full": "YOLO"}
# 心跳只跑 ToolLayer（KF-4）；不跑 MonitorLayer：它會清掉中斷旗標、改寫 session 與原意
_HEARTBEAT_TOOL_LAYER = ToolLayer()


def _resolve_auth_level(security_level: str) -> str:
    """Map a heartbeat security_level to a mode, failing safe to Manual (DA24, U2c-5)."""
    return _SECURITY_LEVEL_TO_AUTH.get(security_level, "Manual")


def _resolve_request_auth_level(req: HeartbeatAwakenRequest) -> str:
    """設定時決定的會話權限（hitl-unification H1，決策 3）。

    明確 ``auth_level``（三模式，Literal 已驗證）優先；未帶則沿用 security_level
    舊映射（DA24 fail-safe）。回傳值即整個 heartbeat 會話的 ctx.auth_level。
    """
    if req.auth_level:
        return req.auth_level
    resolved = _resolve_auth_level(req.security_level)
    if req.security_level not in _SECURITY_LEVEL_TO_AUTH:
        logger.warning(
            "⚠️ [Heartbeat] Unknown security_level %r — failing safe to Manual.",
            req.security_level,
        )
    return resolved


class HeartbeatAwakenRequest(BaseModel):
    """心跳喚醒請求模型。"""

    token: str
    # Identity SSOT — override via request body。validate_default=True 確保字串預設值
    # 也經過 AgentIdentity 的 before-validator 轉為物件（Pydantic v2 預設不驗證 default）。
    agent_id: AgentIdentity = Field(default="DebugAgent", validate_default=True)
    session_id: str = "pulse_channel_default"
    session_mode: str = "independent"  # "independent" | "home"
    security_level: str = "safe"  # "safe" | "full"
    # hitl-unification H1（決策 3）：無人 session 的權限在「設定時」就明確決定——
    # 本欄位優先於 security_level 映射，只收三模式新名稱；None＝沿用 safe/full 映射。
    # 整個 heartbeat 會話（全部 pulse）都以此 ctx.auth_level 運行（ToolLayer 保留這個值、
    # 不採 session row 的模式；也不回寫 session row——home 模式共用人類的家會話，不得
    # 踩掉人類 AuthSwitcher 的選擇）。
    auth_level: AuthMode | None = None
    pulse_count: int = 1  # 總共要發動幾次 Pulse (叫醒次數)
    # 每次 Pulse 的工具輪上限 (None＝全域保險絲，全域 0 時退無人保底 50)
    pulse_slots: int | None = None
    init_prompt: str = (  # default prompt for heartbeat awakening
        "妳剛從沉睡中醒來。這是妳的「自主心跳時間」，請檢查記憶與檔案，看看是否有需要整理的事項或新的發現？"
    )
    model_override: str | None = None


class HeartbeatRetriggerRequest(BaseModel):
    """UI 一鍵重新觸發請求（重跑上次中斷的背景會話）。"""

    agent_id: AgentIdentity  # Identity SSOT
    session_id: str
    pulse_count: int = 1


def _pulse_model_name(identity: AgentIdentity, model_override: str | None) -> str | None:
    """脈衝送出用的模型名，和聊天同一個入口（ADR-0250 A5）；沒設或名字模糊記 log 回 None（A3）。"""
    brain = model_override or get_active_brain(identity)
    code = ErrorCode.AGENT_BRAIN_NOT_SET
    if brain:
        try:
            return model_manager.request_model_name(brain)
        except ToukaException as exc:
            code = exc.code
    logger.warning("💓 [Pulse] %s has no usable model (%s); run ended.", identity.agent_id,
                   code.name)
    return None


async def _execute_single_pulse(
    final_session_id: str,
    identity: AgentIdentity,
    model_override: str | None,
    ctx: PipelineContext,
    pulse_slots: int | None,
    t0: float,
    init_prompt: str,  # noqa: ARG001
) -> int | None:
    """執行單次脈衝任務：載入最新歷史 -> 確保以 User 結尾 -> 調度請求 -> 解析並持久化。

    沒有可用的模型時回 None（已記 log、沒有任何寫入），由背景迴圈結束這次 run（ADR-0250 A3）。
    """
    request_id = str(uuid.uuid4())
    model_name = _pulse_model_name(identity, model_override)
    if model_name is None:
        return None

    # 0. 載入最新會話狀態
    snapshot = await session_service.read_execution_snapshot(final_session_id, identity)
    session_data = snapshot.session
    if not session_data:
        logger.error(f"❌ [Pulse] Session {final_session_id} not found.")
        return 0

    tree_history = session_data["messages"]
    revision = snapshot.revision

    async def save_pulse() -> None:
        nonlocal revision
        result = await session_service.conditional_save_session(
            final_session_id,
            revision,
            SessionChanges(messages=tree_history, updatedAt=int(get_current_epoch() * 1000)),
            identity,
        )
        if not isinstance(result, SavedResult):
            # CAS 失敗也可能是綁定在串流期間改變，先保留精確的歸屬拒絕。
            await session_service.read_execution_snapshot(final_session_id, identity)
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)
        revision = result.revision

    # nudge 前綴與 <continuity> 的現在時間用同一個值（同一時鐘、同為台北時間）
    now_s = get_current_epoch()

    # 0.1 角色結構檢查 (修正 Claude 400 Error)
    linear_before = TreeMessageManager.get_linear_path(tree_history)
    if linear_before:
        last_msg = linear_before[-1]
        if last_msg["role"] == "assistant":
            # 自動補上一條微小的脈衝觸發訊息，維持 User-Assistant 交替
            t_now, _ = format_epoch_to_taipei(now_s)
            nudge_content = (
                f"{user_intent.PULSE_NUDGE_PREFIX}{t_now}】\n\n請繼續執行您的任務或檢查進度。"
            )

            # 重要：更新 tree_history 物件本身
            tree_history = TreeMessageManager.append_node(
                tree_history, "user", nudge_content, name="System"
            )
            # 同步到 DB 以便前端觀察
            await save_pulse()
            logger.info(
                f"⚡ [Pulse] Injected user nudge to fix role sequence for {identity.agent_id}"
            )

    # 0.2 準備線性歷史作為 Context (現在 tree_history 已經是更新過的了)
    linear = TreeMessageManager.get_linear_path(tree_history)
    messages = [  # 插話放回那一則 AI 回覆（ADR-0249）
        OpenAIMessage(role=m["role"], content=annotate_node(m)["content"])
        for m in linear
        if m.get("role") != "system"
    ]
    # 記憶來源：觸發這次脈衝的 user 節點（已保存；存記憶時讀它算指紋）
    trigger = linear[-1] if linear and linear[-1].get("role") == "user" else {}

    # 0.5 決定大腦（model_name 已在開頭經 _pulse_model_name 決定）
    agent_id = identity.agent_id
    chat_req = OpenAIChatRequest(model=f"toukasoul-{agent_id}", messages=messages, stream=True)
    soul_service.apply_soul_to_request(chat_req, identity)
    chat_req.model = model_name
    _soul_text, soul_config = soul_service.get_soul_data(identity)
    if soul_config and "enabled_tools" in soul_config:  # 與 SoulLayer 同：靈魂設定收斂工具
        ctx["desired_tools"] = soul_config["enabled_tools"]

    logger.info(f"💓 [Pulse] Starting pulse for {agent_id} using {model_name} (REQ: {request_id})")

    # 1. 解析本次 pulse 的工具輪上限（ADR-0146：無人 run 一律有限——排程值＞
    #    全域保險絲＞保底 50）並扣一次每日配額（決策 2：配額只計無人 pulse；
    #    工具輪預算的初始化由 handler 統一負責，透過 ctx.pulse_rounds 傳遞——
    #    舊制 heartbeat/handler 雙重初始化時，handler 以全域預設覆寫排程值，
    #    排程的 Slots 旋鈕實質失效）。
    effective_rounds = rhythm_service.get_effective_rounds(
        autonomy_service.config, pulse_slots, headless=True
    )
    autonomy_service.consume_quota(identity, request_id, rounds=effective_rounds)

    # 2. 調度請求
    # 更新 Context 以授權本次心跳的自主行動權
    # 身分、無人在場標記與靈魂旗標由脈衝保證；模式由 ToolLayer 保留設定時決定的值
    # （heartbeat_run，缺值或看不懂一律 Manual）。
    ctx.update(
        {
            "session_id": final_session_id,
            "identity": identity,
            "agent_id": agent_id,
            "is_model_overridden": bool(model_override),
            "enable_autonomy": True,
            "pulse_rounds": pulse_slots,
            "heartbeat_run": True,
            "is_toukasoul": soul_service.is_toukasoul(identity),
            "source_message_id": trigger.get("id"),
        }
    )
    # KF-4（ADR-0246 心跳）：dispatch 前組裝工具；仍受地板、HUMAN_ONLY 與無人在場規則約束
    await _HEARTBEAT_TOOL_LAYER.process(chat_req, ctx)
    # continuity v1（心跳模式）：只附在這次送出的請求，不寫回對話樹；只給靈魂
    if ctx.get("is_toukasoul"):
        await inject_continuity(chat_req, ctx, identity, now_s=now_s)

    response = await dispatch_request(
        model_name=model_name,
        request=chat_req,
        identity=identity,
        request_id=request_id,
        start_time=t0,
        context=ctx,  # PipelineContext 支援 dict-like 存取
    )

    # 3. 解析串流並分段持久化 (原子化樹更新)
    current_turn_text = ""
    turn_count = 0

    TURN_SPLIT_PATTERN = re.compile(r"--- [^\n]*? 自主(?:行動|回合)")

    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")

        lines = chunk.split("\n\n")
        for line in lines:
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue

            data_str = line[6:]
            if data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
                delta_content = ""

                if data.get("type") == "log":
                    delta_content = data.get("content", "")
                elif "choices" in data and len(data["choices"]) > 0:
                    choice = data["choices"][0]
                    delta = choice.get("delta", {})
                    if "content" in delta:
                        delta_content = delta["content"]
                elif data.get("type") == "content_block_delta":
                    delta_content = data.get("delta", {}).get("text", "")

                if delta_content:
                    current_turn_text += delta_content
                    match = TURN_SPLIT_PATTERN.search(current_turn_text)
                    if match:
                        end_idx = match.end()
                        turn_content = current_turn_text[:end_idx].strip()
                        if turn_content:
                            turn_count += 1
                            tree_history = TreeMessageManager.append_node(
                                tree_history, "assistant", turn_content, name=agent_id
                            )
                            # 為了滿足 Claude API 嚴格的 User-Assistant 交替規則
                            # 在同一次 API 串流的分段持久化之間，插入一條系統過渡訊息
                            tree_history = TreeMessageManager.append_node(
                                tree_history,
                                "user",
                                f"{user_intent.SYSTEM_NUDGE_PREFIX}自主行動階段完成，請繼續。",
                                name="System",
                            )
                            await save_pulse()
                            current_turn_text = current_turn_text[end_idx:].strip()
            except (ToukaException, SessionPersistenceError):
                raise
            except Exception:  # noqa: S112
                continue

    if current_turn_text.strip():
        turn_count += 1
        tree_history = TreeMessageManager.append_node(
            tree_history, "assistant", current_turn_text.strip(), name=agent_id
        )
        await save_pulse()

    return turn_count


async def _process_heartbeat_loop(
    final_session_id: str,
    identity: AgentIdentity,
    model_override: str | None,
    ctx: PipelineContext,
    pulse_count: int,
    pulse_slots: int | None,
    t0: float,
    init_prompt: str,
    is_home_mode: bool = False,
    not_disturbed_since: int | None = None,
) -> None:
    """真正的總控背景任務：管理多個脈衝循環。

    ``not_disturbed_since``：入口檢查時的破壞性操作代數（丙-4）；之後這個靈魂被刪除、重置或
    清除過就不開跑。
    """
    agent_id = identity.agent_id
    logger.info(
        f"⚡ [Heartbeat-BG] Master loop started for {agent_id} "
        f"(Pulses: {pulse_count}, Slots/Pulse: {pulse_slots})"
    )
    # 可信活動事件：in-flight 標記寫入後記開始、finally 記結束（被取消時維持 cancelled）
    run_key = heartbeat_run_key(final_session_id, t0)
    run_started = False
    end: HeartbeatEnd = "cancelled"
    pulses_done = total_turns = 0

    # 丙-4（ADR-0287）：這個靈魂正在（或入口檢查之後曾經）刪除、重置、清除就不開跑、什麼都
    # 不寫；看標記和登記之間沒有 await。登記到 finally 剪裁做完才拿掉，刪除據此判斷「還有心跳
    # 在跑」。
    started_at = destructive_ops.start_heartbeat(
        identity, final_session_id, since=not_disturbed_since
    )
    if started_at is None:
        logger.warning(
            "[Heartbeat-BG] %s is being reset, scrubbed or deleted; heartbeat not started.",
            agent_id,
        )
        return
    # 中斷旗標：每個新背景 run 起點清除殘留（只影響本次 run 期間按下的 stop）。登記成功之後才清
    # （中間沒有 await）：被擋下的不會清掉同一個對話裡另一次心跳收到的停止，登記之後才叫停的也
    # 不會被這裡清掉。
    rhythm_service.clear_interrupt(final_session_id)
    # 整次心跳的停止起點（ADR-0292）：之後同一個對話的請求會清掉旗標，脈衝之間與工具 hook 跑完時
    # 仍認得這次心跳被停止過（ctx 由每個脈衝共用）
    baseline = stop_baseline(final_session_id)
    ctx.update(baseline)
    run_stop = {"session_id": final_session_id, **baseline}

    try:
        # P5c：落盤 in-flight 標記——當機/被強制關閉時殘留，供啟動掃描偵測未跑完的背景迴圈
        autonomy_service.mark_heartbeat_in_flight(
            identity,
            {
                "session_id": final_session_id,
                "agent": agent_id,
                "total": pulse_count,
                "started_at": t0,
            },
        )
        record_heartbeat_started(
            identity, final_session_id, run_key, pulses_total=pulse_count, home=is_home_mode
        )
        run_started = True
        # 0. Pre-flight: 更新模型健康狀態 (只需做一次)
        try:
            await model_manager.check_models_health()
        except Exception as e:
            logger.warning(f"⚠️ [Heartbeat-BG] Health check failed: {e}")

        for i in range(pulse_count):
            if destructive_ops.disturbed(identity, started_at):
                logger.info(
                    "🛑 [Heartbeat-BG] %s is being reset or deleted before pulse %d; stopping.",
                    agent_id,
                    i + 1,
                )
                end = "interrupted"
                break
            if request_stopped(run_stop):
                logger.info(
                    f"🛑 [Heartbeat-BG] Interrupted before pulse {i + 1}; "
                    "stopping remaining pulses."
                )
                end = "interrupted"
                break
            # ADR-0146：每日自主配額（決策 2：只計無人 pulse）與成本上限閘的
            # 前置執法點——耗盡即停剩餘 pulses（check_quota 舊制從無任何執法點）。
            if not autonomy_service.check_quota(identity):
                logger.warning(
                    "⛔ [Heartbeat-BG] Daily autonomy quota exhausted for %s; "
                    "skipping remaining pulses (%d/%d done).",
                    agent_id,
                    i,
                    pulse_count,
                )
                end = "quota"
                break
            if autonomy_service.is_over_budget(identity):
                logger.warning(
                    "⛔ [Heartbeat-BG] Cost cap reached for %s; "
                    "skipping remaining pulses (%d/%d done).",
                    agent_id,
                    i,
                    pulse_count,
                )
                end = "budget"
                break
            # P4：背景活動登記（→ /stats background_activity → UI「背景跑中」脈衝 i/N）
            activity_registry.set_activity(
                final_session_id,
                {
                    "kind": "heartbeat",
                    "current": i + 1,
                    "total": pulse_count,
                    "agent": agent_id,
                },
            )
            logger.info(f"🌈 [Heartbeat-BG] Executing Pulse {i + 1}/{pulse_count}...")

            # 執行單次脈衝
            turns = await _execute_single_pulse(
                final_session_id,
                identity,
                model_override,
                ctx,
                pulse_slots,
                t0,
                init_prompt=init_prompt if i == 0 else "",
            )
            if turns is None:  # 沒有可用的模型（ADR-0250 A3）：已記 log，結束這次迴圈
                end = "no_model"
                break
            pulses_done += 1
            total_turns += turns

            # 如果不是最後一個 Pulse，休息一下防止 DB 鎖定或 API 過熱
            if i < pulse_count - 1:
                await asyncio.sleep(3)
        else:
            end = "completed"

        logger.info(
            f"✅ [Heartbeat-BG] All pulses completed for {agent_id}. "
            f"Total pulses: {pulse_count}, Total turns: {total_turns}"
        )

    except (ToukaException, SessionPersistenceError) as exc:
        end = "error"
        code = exc.code.name if isinstance(exc, ToukaException) else "SESSION_SAVE_FAILED"
        logger.warning("%s", code)
    except Exception as e:
        end = "error"
        error_msg = f"❌ [Heartbeat-BG] 多脈衝任務執行失敗: {str(e)}"
        logger.exception(error_msg)
        try:
            # 丙-4：重置或刪除中不寫進對話（可能把清掉的對話重新建出來）
            quiet = destructive_ops.disturbed(identity, started_at)
            if not quiet and not await session_service.append_message_to_session(
                final_session_id, "assistant", error_msg, name="System", identity=identity
            ):
                logger.warning("SESSION_SAVE_FAILED")
        except ToukaException as exc:
            logger.warning("%s", exc.code.name)

    finally:
        # 丙-4：正在或開跑之後曾經重置、刪除，結束事件不寫、也不剪裁（剪裁會寫舊對話庫）。
        blocked = destructive_ops.disturbed(identity, started_at)
        try:
            if run_started and not blocked:  # 同步排程寫入、不 await（被取消時也安全）
                record_heartbeat_finished(
                    identity,
                    final_session_id,
                    run_key,
                    pulses_total=pulse_count,
                    pulses_done=pulses_done,
                    turns=total_turns,
                    end=end,
                )
            # P5c：背景迴圈結束（正常或例外），清除 in-flight 標記（不再算「未完成」）
            autonomy_service.clear_heartbeat_in_flight(identity)
            # PostHeartbeat hook：僅家對話觸發滑動視窗（子對話排除）
            # hook 失敗不影響 heartbeat 整體結果
            if is_home_mode and end != "error" and not blocked:
                try:
                    await sliding_window_service.check_and_trim(identity, final_session_id)
                except Exception:
                    logger.exception(
                        "[SlidingWindow] PostHeartbeat hook 失敗，略過。 Session: %s",
                        final_session_id,
                    )
        finally:
            # 剪裁做完才拿掉（丙-4）：剪裁期間畫面仍顯示「背景跑中」，刪除也看得到這次心跳在跑。
            # P4：背景活動結束，清除登記（UI 不再顯示「背景跑中」）
            activity_registry.clear(final_session_id)
            destructive_ops.end_heartbeat(identity, final_session_id)


@router.post("/awaken", status_code=202)
async def awaken_agent(
    req: HeartbeatAwakenRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
    _svc_token: str = Depends(verify_heartbeat_token),  # noqa: B008
) -> dict:
    """接收喚醒請求，立即回傳 202 並在背景執行多脈衝任務。

    SOLID-SRP: Service token validation is fully delegated to
    verify_heartbeat_token() via Depends(_svc_token). This body only
    handles human-owner authorization (守衛 B) and pulse execution.
    """
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "💓 [Heartbeat] Received awaken request from %s for Agent: %s (Pulse Count: %s)",
        client_host,
        req.agent_id,
        req.pulse_count,
    )
    logger.info("🛡️ [Heartbeat] Service token validated from %s", client_host)

    # 2. 驗證人類授權與 Refresh Token (守衛 B)
    users = auth_service.get_all_users()

    agent_id = req.agent_id.agent_id
    owner = next((u for u in users if agent_id in u.owned_agents), None)
    if not owner:
        logger.warning(f"🚨 [Heartbeat] Agent {agent_id} is orphaned. Denying pulse.")
        raise ToukaException(ErrorCode.HEARTBEAT_ORPHAN_AGENT)

    now_sec = int(get_current_epoch())
    if not owner.refresh_token_exp or now_sec >= (owner.refresh_token_exp + 30):  # P26: 30s grace
        logger.warning(
            f"🚨 [Heartbeat] Owner {owner.email} refresh token expired. "
            f"Denying pulse for {agent_id}."
        )
        raise ToukaException(ErrorCode.HEARTBEAT_ORPHAN_AGENT)

    # 1.5 獲取身分並檢查權限 (Identity & Capability SSOT)
    identity = identity_service.get_identity(agent_id)
    if not permission_service.can_awaken(identity):
        logger.warning(
            f"🚫 [Heartbeat] Agent '{agent_id}' 未獲得心跳喚醒授權 (Capability Denied)。"
        )
        raise ToukaException(ErrorCode.HEARTBEAT_UNAUTHORIZED_AGENT, agent_id=agent_id)
    # 丙-4：正在重置、清除或刪除這個靈魂時不喚醒，也不寫初始提示；讓排程端知道（409）
    since = destructive_ops.generation(identity)
    if since is None:
        logger.warning("💓 [Heartbeat] %s is being reset or deleted; not awakened.", agent_id)
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)

    logger.debug("Heartbeat checks passed for %s", req.agent_id)

    agent_id = identity.agent_id
    is_probe = agent_id == "heartbeat_probe"
    t0 = get_current_epoch()

    # 2. 準備上下文
    ctx_dict = request.app.state.context.copy()
    ctx = PipelineContext(**ctx_dict)

    # 3. 安全過濾 (SSOT: 指派授權等級)
    # 設定時決定（H1 決策 3）：明確 auth_level 三模式優先；否則 security_level 映射
    # （DA24／U2c-5：僅 "full" 給 YOLO，看不懂的值一律 Manual）。
    auth_level = _resolve_request_auth_level(req)
    ctx.auth_level = auth_level
    ctx.identity = identity
    ctx.t0 = t0
    ctx.agent_id = agent_id
    logger.info(f"💓 [Heartbeat] Context Security Level mapped to: {auth_level}")

    # 4. 尋找或建立會話
    is_home_mode = req.session_mode == "home" or (
        "session_id" in req.model_fields_set and req.session_id == "home"
    )
    if is_home_mode:
        home = await session_service.read_home_snapshot(identity)
        final_session_id = home.session["id"] if home else None
        if final_session_id is None:
            final_session_id = f"home_{agent_id}_{str(uuid.uuid4())[:8]}"
            snapshot = await session_service.read_execution_snapshot(final_session_id, identity)
            if snapshot.session is not None:
                raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
            result = await session_service.conditional_save_session(
                final_session_id, snapshot.revision,
                SessionChanges(
                    title=f"🏠 {agent_id} 的家",
                    messages=TreeMessageManager.create_empty_tree(),
                    agent_id=identity, is_home=True, home_for_agent=identity, project_id="default",
                ),
                identity,
            )
            if not isinstance(result, SavedResult):
                raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)
    else:
        default_id = channel_session_id("heartbeat", agent_id)
        final_session_id = req.session_id
        if "session_id" not in req.model_fields_set or final_session_id in {
            "", "pulse_channel_default", default_id
        }:
            final_session_id = await session_service.open_channel_session(
                "heartbeat", identity, title=f"Pulse: {agent_id}"
            )
        else:
            if final_session_id.startswith("touka."):
                raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
            snapshot = await session_service.read_execution_snapshot(final_session_id, identity)
            if snapshot.session is None:
                result = await session_service.conditional_save_session(
                    final_session_id,
                    snapshot.revision,
                    SessionChanges(
                        title=f"Pulse: {agent_id}",
                        messages=TreeMessageManager.create_empty_tree(),
                        agent_id=identity,
                        project_id="default",
                    ),
                    identity,
                )
                if not isinstance(result, SavedResult):
                    raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

    # 5. 寫入初始提示詞 (立即持久化)
    # DA24: the caller-controlled init_prompt enters the agent's privileged LLM context,
    # so pass it through the H3 content guard first (truncate + injection detection) —
    # the same boundary the memory/dispatch paths use.
    safe_init_prompt, _flagged = sanitize_memory_content(req.init_prompt)
    if _flagged:
        logger.warning("⚠️ [Heartbeat] init_prompt sanitized by ContentGuard for %s.", agent_id)
    # 無人在場的排程 run（U2c-2）：原意＝消毒後的 init_prompt（給審查員的資料）
    ctx["heartbeat_run"] = True
    ctx["user_intent"] = user_intent.heartbeat_intent(safe_init_prompt)
    epoch_now = get_current_epoch()
    time_str, weekday = format_epoch_to_taipei(epoch_now)
    init_head = f"{user_intent.HEARTBEAT_INIT_PREFIX}{time_str} ({weekday})】"
    init_content = f"{init_head}\n\n{safe_init_prompt}"
    # 丙-4：上面找或建對話時有 await，這段期間開始的重置或刪除也擋（寫初始提示之前再看一次）
    if destructive_ops.disturbed(identity, since):
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)
    if not await session_service.append_message_to_session(
        final_session_id, "user", init_content, identity=identity
    ):
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

    # 6. 如果是探針則直接結束
    if is_probe:
        return {"status": "accepted", "message": "Probe persisted.", "session_id": final_session_id}

    # 7. 驗證大腦可用性 (Fail Fast)：排程沒指定模型、靈魂也沒有主要模型 → 409（ADR-0250 A3）
    if not req.model_override and get_active_brain(identity) is None:
        logger.warning("💓 [Heartbeat] %s has no primary model; not awakened.", agent_id)
        raise ToukaException(ErrorCode.AGENT_BRAIN_NOT_SET)

    # 8. 啟動多脈衝背景任務
    logger.info(
        f"💓 [Heartbeat] Dispatching background loop for {agent_id} "
        f"(Identity Type: {type(identity)})"
    )
    background_tasks.add_task(
        _process_heartbeat_loop,
        final_session_id,
        identity,
        req.model_override,
        ctx,
        req.pulse_count,
        req.pulse_slots,
        t0,
        safe_init_prompt,  # DA24: forward the sanitized prompt, not the raw input
        is_home_mode,
        not_disturbed_since=since,
    )

    return {
        "status": "accepted",
        "message": f"Multi-pulse task ({req.pulse_count}) dispatched to background.",
        "session_id": final_session_id,
    }


@router.get("/test")
async def test_heartbeat_connectivity() -> dict:
    """測試心跳端點的對外連通性。"""
    webhook_url = HEARTBEAT_EXTERNAL_URL
    if not webhook_url:
        return {"status": "error", "message": "HEARTBEAT_EXTERNAL_URL not configured."}

    if not HEARTBEAT_TOKEN:
        return {"status": "error", "message": "HEARTBEAT_TOKEN is not configured. Set the env var."}

    # H5 fix: use only the configured token; no insecure hardcoded fallback.
    payload = {
        "token": HEARTBEAT_TOKEN,
        "agent_id": "heartbeat_probe",
        "session_mode": "test",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.post(webhook_url, json=payload, timeout=5.0)
            return {"status": "ok", "http_code": resp.status_code, "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/retrigger", status_code=202)
async def retrigger_heartbeat(
    req: HeartbeatRetriggerRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict:
    """UI 一鍵重新觸發：對既有（上次中斷的）會話派發延續性背景脈衝 (P5c bonus)。

    與 /awaken 不同：以「人類擁有者」身分驗證（非 service token），強制 Auto、
    不新建會話／不重寫 init prompt——延續既有會話歷史（_execute_single_pulse 會在
    上一則為 assistant 時自動補 user nudge 以續跑）。供「上次有 N 個未完成」通知的
    一鍵重跑按鈕使用。
    """
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    agent_id = req.agent_id.agent_id

    # 擁有權守衛：呼叫者須擁有該 agent（重用 resource_manager.is_owner SSOT）。重跑一律以 Auto
    # 執行，所以再過 session 寫入同一個模式授權：只有 admin 或人類擁有者，Debugger 的 is_owner
    # 旁路與代理身分都不算（R-2c ③ #2，只在這條路徑收緊）。
    may_run_auto = authorize_auth_mode(
        "Auto",
        is_admin=identity.is_admin_authority(),
        owns_agent=agent_id in identity.owned_agents,
    )
    if not resource_manager.is_owner(req.agent_id, identity) or may_run_auto != "Auto":
        logger.warning("🚫 [Retrigger] %s 無權重新觸發 %s。", identity.agent_id, agent_id)
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 能力守衛：與 /awaken 一致，未獲心跳喚醒授權者不可重跑
    target_identity = identity_service.get_identity(agent_id)
    if not permission_service.can_awaken(target_identity):
        raise ToukaException(ErrorCode.HEARTBEAT_UNAUTHORIZED_AGENT, agent_id=agent_id)

    # 會話須存在（重新觸發的是「既有」中斷會話，非新建）
    snapshot = await session_service.read_execution_snapshot(req.session_id, target_identity)
    session_data = snapshot.session
    if not session_data:
        raise ToukaException(ErrorCode.SESSION_NOT_FOUND)
    # 對話必須屬於這個靈魂、呼叫者也要能存取它：否則知道別人 session id 就能讓自己的靈魂在
    # 那個對話跑（讀到別人的對話、拿別人最後一則訊息當原意、存檔時把對話改掛到自己的靈魂）。
    session_agent_id = session_data.get("agent_id")
    if not _can_access_session(
        identity, session_agent_id, session_data.get("owner_user_id")
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # 丙-4：正在重置、清除或刪除這個靈魂時不重跑，「上次未完成」照留。放在最後一個 await 之後，
    # 從這裡到派發、清「上次未完成」之間沒有 await。
    since = destructive_ops.generation(target_identity)
    if since is None:
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)

    t0 = get_current_epoch()
    ctx_dict = request.app.state.context.copy()
    ctx = PipelineContext(**ctx_dict)
    ctx.auth_level = _SECURITY_LEVEL_TO_AUTH["safe"]  # UI 重跑＝safe（Auto），不升級自主權
    ctx.identity = target_identity
    ctx["heartbeat_run"] = True  # 背景脈衝無人在場；原意＝最後一則人類節點（U2c-2）
    linear = TreeMessageManager.get_linear_path(session_data.get("messages") or {})
    ctx["user_intent"] = user_intent.retrigger_intent(linear)
    ctx.t0 = t0
    ctx.agent_id = agent_id

    pulse_count = max(1, min(req.pulse_count, 5))  # 夾在 1..5 防濫用
    is_home_mode = bool(session_data.get("is_home"))

    logger.info(
        "🔁 [Retrigger] %s 重新觸發 %s 背景脈衝（session=%s, pulses=%d）。",
        identity.agent_id,
        agent_id,
        req.session_id,
        pulse_count,
    )
    background_tasks.add_task(
        _process_heartbeat_loop,
        req.session_id,
        target_identity,
        None,  # model_override：沿用持久化大腦
        ctx,
        pulse_count,
        None,  # pulse_slots：使用全域預算
        t0,
        "繼續上次未完成的背景任務。",  # init_prompt（_execute_single_pulse 忽略，留作語意）
        is_home_mode,
        not_disturbed_since=since,
    )
    # 已重新觸發 → 自「上次未完成」清單移除，避免下次 /stats 輪詢又冒出來
    autonomy_service.clear_interrupted_run(req.session_id)
    return {
        "status": "accepted",
        "message": f"已重新觸發 {agent_id} 的背景脈衝（{pulse_count}）。",
        "session_id": req.session_id,
    }


# 舊版白名單管理端點已移除，現在請直接修改 config/identity_map.json 的 capabilities。
