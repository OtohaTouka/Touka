"""靈魂中介層模組。"""
import logging
from typing import Any

from src.core.exceptions import ErrorCode, ToukaException
from src.middleware.base import ProxyLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.agent_detector import detect_target_agent
from src.services.agent_service import agent_service
from src.services.authorization_gateway import can_drive_soul, require_soul_access
from src.services.continuity_events import record_user_turn
from src.services.continuity_service import inject_continuity
from src.services.model_manager import model_manager
from src.services.soul_service import soul_service
from src.utils.async_db import session_service
from src.utils.brain_persistence import get_active_brain
from src.utils.history_manager import sanitize_history

logger = logging.getLogger(__name__)

class SoulLayer(ProxyLayer):
    """負責代理人身份識別與「靈魂」(Soul) Prompt 注入 (SOLID - SRP)。"""

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """執行靈魂注入流程。"""
        model_name = getattr(request, "model", "")
        # ToukaGroup 擁有獨立的調度與靈魂注入邏輯，中介層應跳過以避免衝突
        if model_name == "ToukaGroup":
            return

        is_anthropic = "anthropic" in str(type(request)).lower()
        is_toukasoul = ctx.get("is_toukasoul", False)
        
        # 優先從 Context 讀取預先辨識出的身份 (由 MonitorLayer 提取)
        agent_id = ctx.get("agent_id")
        
        if is_toukasoul:
            if not agent_id:
                agent_id = self._detect_agent(request, is_anthropic)
            
            ctx["agent_id"] = agent_id
            
            if not agent_id:
                raise ToukaException(ErrorCode.ROUTING_AGENT_REQUIRED)
            # 注入前再驗一次（ADR-0245 修補）：MonitorLayer 之後若有層改了 agent_id 也擋得住
            caller = ctx.get("identity")
            if caller is not None:
                require_soul_access(caller, AgentIdentity(agent_id=agent_id))

            active_brain = get_active_brain(AgentIdentity(agent_id=agent_id))
            if not active_brain:
                # 不偷換寫死模型：串流前回 409，前端提示使用者到靈魂設定選主要模型（model-p0 U7）
                logger.warning("Agent '%s' has no primary model set.", agent_id)
                raise ToukaException(ErrorCode.AGENT_BRAIN_NOT_SET)

            model_name = model_manager.request_model_name(active_brain)  # ADR-0250 A5
            request.model = model_name
            soul_text, config = soul_service.get_soul_data(agent_id)
            if "enabled_tools" in config:
                ctx["desired_tools"] = config["enabled_tools"]
                logger.info(f"  -> Agent (Soul) desired tools: {ctx['desired_tools']}")

            soul_service.apply_soul_to_request(request, agent_id)
            ctx["session_bound"] = await self._session_bound(
                ctx, AgentIdentity(agent_id=agent_id)
            )
            # ADR-0086：無痕模式完全失憶——不注入家對話（記憶錨點），但保留人格。
            if not ctx.get("incognito"):
                await soul_service.inject_home_messages(
                    request,
                    agent_id,
                    current_session_id=ctx.get("session_id")
                    if self._sends_own_history(request, ctx)
                    else None,
                )
                # ADR-0092 Phase D：注入被剪舊對話的摘要（固定前置；無痕不注入）；
                # 只給綁在這個靈魂的 session（ADR-0245 修補）
                if ctx["session_bound"]:
                    await soul_service.inject_context_summary(request, ctx.get("session_id"))
            # 現在時間／距上一輪多久／任務看板時間：只給綁在這個靈魂的 session 與無痕（只放時間）；
            # 附在最後一則 user 訊息尾端，不進 system、不存紀錄。心跳不經本層。
            if ctx["session_bound"] or ctx.get("incognito"):
                await inject_continuity(request, ctx, AgentIdentity(agent_id=agent_id))
            # 可信活動事件：人類送來、已保存的這一輪（在 continuity 之後記，它看不到自己這一輪）
            record_user_turn(ctx, AgentIdentity(agent_id=agent_id))
        else:
            if not is_anthropic:
                sanitize_history(request.messages)

    @staticmethod
    def _sends_own_history(request: Any, ctx: PipelineContext) -> bool:
        """網頁版一般聊天：送的是這個對話自己的完整紀錄（前端存檔時持有整棵樹、revision 已核對）。

        在家對話本身就不再插一份家對話。只看 ctx 旗標不夠：子代理會繼承父 ctx 的
        ``ui_authorized``；SNS、IDE 只送最新一句，沒有存檔核准欄位。
        """
        admission = getattr(request, "ui_persistence", None)
        return (
            admission is not None
            and admission.mode == "persistent"
            and ctx.get("ui_authorized") is True
        )

    async def _session_bound(self, ctx: PipelineContext, soul: AgentIdentity) -> bool:
        """這個 session 綁的 agent（正式 ID）是要注入的靈魂、呼叫者也能驅動它（ADR-0245 修補）。

        判斷不了（沒有 session、沒綁 agent、對不上）一律 False：不注入前情摘要，
        dispatcher 的舊串流路徑也不寫回。沒有身分只會是程式內部路徑（SNS）。
        """
        session_id = ctx.get("session_id")
        session = await session_service.get_session_by_id(session_id) if session_id else None
        bound = session.get("agent_id") if session else None
        passport = agent_service.identify_agent(bound) if bound else None
        if not passport or passport["agent_id"] != soul.agent_id:
            return False
        caller = ctx.get("identity")
        return caller is None or can_drive_soul(caller, soul)

    def _detect_agent(self, request: Any, is_anthropic: bool) -> Any:
        if is_anthropic:
            last_msg = next(
                (
                    m.content
                    for m in reversed(request.messages)
                    if m.role == "user" and isinstance(m.content, str)
                ),
                "",
            )
            if not last_msg:
                return None
            return detect_target_agent([{"role": "user", "content": last_msg}], request.model)
        agent_id = detect_target_agent(request.messages, request.model)
        sanitize_history(request.messages)
        return agent_id