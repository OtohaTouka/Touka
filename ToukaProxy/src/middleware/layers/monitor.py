"""Monitor middleware layer for request identification and logging."""

import logging
import uuid
from typing import Any

from src.middleware.base import ProxyLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.services.authorization_gateway import require_soul_access
from src.services.hooks.stopping import stop_baseline
from src.services.rhythm_service import rhythm_service
from src.utils.time_utils import get_current_epoch
from src.utils.user_intent import chat_intent

logger = logging.getLogger(__name__)

class MonitorLayer(ProxyLayer):
    """負責請求生成 ID、計時與日誌紀錄 (SOLID - SRP)。"""

    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """生成請求 ID、記錄時間戳並識別代理人身分。"""
        # 1. 生成基礎請求資訊
        req_id = str(uuid.uuid4())
        ctx["request_id"] = req_id
        ctx["t0"] = get_current_epoch()
        
        # 2. 獲取請求的模型名稱
        model_name = "Unknown"
        if hasattr(request, "model"):
            model_name = request.model
        elif isinstance(request, dict):
            model_name = request.get("model", "Unknown")
        
        # 3. 身份識別與 SSOT 驗證 (改用 AgentService 作為唯一入口)
        is_toukasoul = False
        is_custom = False
        target_agent_id = None

        # 3.1 優先權 1: 顯性身份標頭 (X-Touka-Soul-ID)
        soul_header_id = ctx.get("headers", {}).get("x-touka-soul-id")
        if soul_header_id:
            passport = agent_service.identify_agent(soul_header_id)
            if passport:
                is_toukasoul = passport["is_toukasoul"]
                is_custom = passport["is_custom"]
                target_agent_id = passport["agent_id"]
                logger.info(
                    f"✨ Agent verified via Header: {target_agent_id} "
                    f"(soul={is_toukasoul}, custom={is_custom})"
                )

        # 3.2 優先權 2: 從模型名稱解析 Agent ID
        if not target_agent_id and model_name:
            passport = agent_service.identify_model_target(model_name)
            if passport:
                is_toukasoul = passport["is_toukasoul"]
                is_custom = passport["is_custom"]
                target_agent_id = passport["agent_id"]
                logger.info(
                    f"✨ Agent verified via Model Name: {target_agent_id} "
                    f"(soul={is_toukasoul}, custom={is_custom})"
                )

        # 3.3 優先權 3: Session 中記錄的 agent_id (最後手段)
        headers = ctx.get("headers", {})
        session_id = (
            getattr(request, "session_id", None) 
            or headers.get("x-touka-session-id") 
            or headers.get("x-toukachat-session-id")
        )
        ctx["session_id"] = session_id

        # 3.4 模式（auth_level）不在本層決定：請求參數不是模式來源。ToolLayer 讀 session，
        # 誰能把對話設成 Auto／YOLO 在 session 寫入時授權（ui_sessions，ADR-0246）。
        # 使用者原意（U2c-2）：給審查員的資料、只留在記憶體；子代理的原意在派工時設好。
        if not ctx.get("subagent_run"):
            messages = request.get("messages") if isinstance(request, dict) else None
            ctx["user_intent"] = chat_intent(messages or getattr(request, "messages", None))

        if session_id and not target_agent_id:
            try:
                from src.utils.async_db import get_async_db
                async with get_async_db() as db, db.execute(
                    "SELECT agent_id FROM user_sessions WHERE id = ?", (session_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row["agent_id"]:
                        potential_id = row["agent_id"]
                        passport = agent_service.identify_agent(potential_id)
                        if passport:
                            is_toukasoul = passport["is_toukasoul"]
                            is_custom = passport["is_custom"]
                            target_agent_id = passport["agent_id"]
                            logger.info(f"🛡️ Agent confirmed via Session link: {target_agent_id}")
            except Exception:
                logger.exception(f"⚠️ Error querying session link for {session_id}")

        # 3.5 優先權 4: 降級使用已驗證的身分 ID (Identity SSOT Fallback)
        if not target_agent_id and ctx.get("agent_id"):
            potential_id = ctx.get("agent_id")
            from memory_server import config_utils
            current_app_data = config_utils.get_app_data_dir()
            logger.info(f"🔍 Identity Fallback: ID={potential_id}, DataDir={current_app_data}")
            
            passport = agent_service.identify_agent(potential_id)
            if passport:
                is_toukasoul = passport["is_toukasoul"]
                is_custom = passport["is_custom"]
                target_agent_id = passport["agent_id"]
                logger.info(
                    f"✨ Agent identity inherited from Auth: {target_agent_id} "
                    f"(soul={is_toukasoul})"
                )
            else:
                logger.info(
                    f"❌ Identity Fallback failed: agent_service doesn't recognize {potential_id}"
                )

        # 3.6 目標是靈魂時先驗呼叫者（ADR-0245 修補），不行就在套人設、家對話之前 403。
        # 沒有身分只會是不經 HTTP 閘的程式內部路徑（SNS）；HTTP 聊天端點已由
        # require_authenticated 保證有身分。
        caller = ctx.get("identity")
        if is_toukasoul and caller is not None:
            require_soul_access(caller, AgentIdentity(agent_id=target_agent_id))

        # 中斷旗標：foreground 每則新訊息起點清除殘留（ADR-0245：移到授權之後，被 403
        # 的請求不再清旗標；heartbeat 自設 session_id、不經此層）。
        if session_id:
            rhythm_service.clear_interrupt(session_id)
            # 這次執行的停止起點：之後的請求清掉旗標，hook 跑完時仍認得這次被停止過（ADR-0292）
            ctx.update(stop_baseline(session_id))

        # 4. 寫入護照 (Context)
        ctx["is_toukasoul"] = is_toukasoul
        ctx["is_custom"] = is_custom
        if target_agent_id:
            ctx["agent_id"] = target_agent_id
            self._maybe_assume_soul_identity(ctx, target_agent_id, is_toukasoul)

        logger.info(
            f"🆕 Proxy Request: {req_id} | Model: {model_name} | "
            f"Soul: {is_toukasoul} | Custom: {is_custom} | Agent: {target_agent_id or 'None'}"
        )

    def _maybe_assume_soul_identity(
        self, ctx: PipelineContext, target_agent_id: str, is_toukasoul: bool
    ) -> None:
        """④ 身分裂腦修復：前景對話解析出「人類擁有的靈魂」為 target 時，把 acting
        identity 從人類請求者切換為該靈魂——與心跳（``identity_service.get_identity``）
        及 X-Agent-ID 代打（``identity_router`` :98）一致。

        未修前本層只把 ``ctx["agent_id"]`` 字串改成靈魂 id，``ctx["identity"]`` 仍是人類
        → 下游全走人類身分：``get_allowed_tools`` 讀人類的 ``agent_role``（None）回受限集
        （靈魂「看不到工具」的真因）、``get_vault_path`` 把記憶/檔案落到
        ``custom/{human_uuid}/drive``（與心跳的 ``personal_drive/{soul}`` 分家）、payload
        logger 記人類 UUID。人類請求者的角色僅作 Gate-1 授權（擁有該靈魂或人類 admin）。

        身分建構鏡像 ``identity_service.get_identity``（``resolve_role_and_caps`` SSOT +
        ``for_role`` 依值路由），此處以已匯入的 ``agent_service`` 直建、避免對 service 層的
        額外模組相依。session ``owner_user_id`` 不受影響——僅 INSERT 寫入（前端以人類 token
        建立 session 時記錄），server-side 續寫恆為 UPDATE、``owner_user_id`` 不進 ON CONFLICT
        set，歸屬永不改。
        """
        if not is_toukasoul:
            return
        caller = ctx.get("identity")
        # 僅「人類請求者」代打——心跳/代打的 caller 已是靈魂身分（is_human=False），不重複
        # 處理；非人類 / 未驗證（None）不代打。
        if caller is None or not caller.is_human:
            return
        if caller.agent_id == target_agent_id:
            return
        # Gate-1 授權：僅擁有該靈魂或人類 admin 可以其身分驅動（防越權代打他人靈魂）。
        if target_agent_id not in caller.owned_agents and not caller.is_admin_authority():
            return
        role, caps = agent_service.resolve_role_and_caps(target_agent_id)
        soul_identity = AgentIdentity.for_role(target_agent_id, role, capabilities=caps)
        ctx["identity"] = soul_identity
        ctx["role"] = soul_identity.display_role
        ctx["capabilities"] = soul_identity.capabilities
        logger.info(
            "🎭 前景代打：人類 %s 以其擁有的靈魂 %s 身分執行（工具/記憶/vault/歸屬歸靈魂）",
            caller.agent_id,
            target_agent_id,
        )

