"""身分路由閘道 (Identity Router Gateway).

負責 FastAPI 請求的身分驗證與優雅降級，作為 Gate 1 攔截入站請求。
"""

import ipaddress
import logging

from fastapi import Depends, Request  # noqa: TC002
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer  # noqa: TC002

from src.config import CLOUD_MODE
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service
from src.services.soul_service import soul_service
from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# P24 fix: rate-limit LDH auto-token-issuance to prevent token flooding
_ldh_limiter = RateLimiter(max_attempts=30, window_sec=60)

# 使用 FastAPI 內建的 Bearer 認證 Schema (自動處理由 Header 讀取 Authorization)
auth_scheme = HTTPBearer(auto_error=False)


def is_request_from_trusted_local(request: Request) -> bool:
    """判定請求是否來自受信任的本地或私有網路.

    CLOUD_MODE=False（本機模式）：私有 IP 或 loopback 均信任（原有行為）。
    CLOUD_MODE=True（雲端模式）：
      - 優先讀取 X-Real-IP header（nginx 設定的真實客戶端 IP）
      - 僅信任 loopback（127.0.0.1）；私有 IP 不再自動信任
      - 防止反向代理 IP 穿透導致 LDH 被觸發
    """
    client_host = request.client.host if request.client else "Unknown"

    if client_host == "testclient":  # 支援 FastAPI TestClient
        return True

    if CLOUD_MODE:
        # A4 fix: use X-Real-IP set by nginx to get real client IP
        real_ip_str = request.headers.get("X-Real-IP") or client_host
        try:
            real_ip = ipaddress.ip_address(real_ip_str)
        except ValueError:
            # Invalid header value — fall back to socket IP
            try:
                real_ip = ipaddress.ip_address(client_host)
            except ValueError:
                return False
        return real_ip.is_loopback  # Cloud: only trust true localhost

    try:
        ip_obj = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    else:
        return ip_obj.is_private or ip_obj.is_loopback


async def get_optional_identity(
    request: Request,
    auth: HTTPAuthorizationCredentials | None = Depends(auth_scheme),  # noqa: B008
) -> AgentIdentity | None:
    """FastAPI 權限攔截依賴 (Gate 1 - 可選身分).

    1. 驗證 Token 合法性與過期——失效 token 一律 401（不分本地/外部；ADR-0131、禁令 60）。
    2. 驗證 Header 中的 X-Agent-ID 是否與 Token 一致 (防偽造)。
       - PaC 例外：若為本地信任請求且對象為 Debugger 角色，允許切換。
    3. 實作本機優雅降級邏輯 (LDH)——僅限「真正無卡」的本機請求。
    """
    header_agent_id = request.headers.get("X-Agent-ID")
    token = auth.credentials if auth else None
    is_trusted_local = is_request_from_trusted_local(request)
    client_host = request.client.host if request.client else "Unknown"

    # 1. 如果有 Token，執行驗證
    if token:
        identity = identity_service.verify_token(token)
        if not identity:
            # ADR-0131（LDH 收斂）：帶卡但卡失效（過期/簽章不符）→ 一律 401 fail-loud，
            # 讓合法客戶端走 /auth/refresh 續期。舊行為「本地壞卡當無卡 → LDH 降級成
            # DebugAgent＋發新卡」會把過期人類會話無聲挾持成 DebugAgent 身分（會話錯記
            # 其名下、前端整個 app 被降級卡接管）。LDH（下方第 3 步）僅保留給「真正無卡」
            # 的本機請求（首裝精靈、開發工具首次領卡）。（禁令 60）
            logger.warning(f"🛡️ [Gate 1] Invalid token from {client_host}")
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)

        if identity:
            # MCP request context 需要保留「原始人類 principal」。後續 X-Agent-ID／前景 soul
            # assumption 會把執行 identity 換成 agent，若不先錨定便無法隔離 user OAuth handle。
            request.state.human_principal_id = (
                identity.agent_id if identity.is_human else None
            )
            # 2. 驗證身分一致性 (防偽造)
            if header_agent_id and identity.agent_id != header_agent_id:
                # --- 多使用者主權：允許人類切換至其擁有的代理人 ---
                if identity.is_human:
                    if header_agent_id in identity.owned_agents or identity.human_role == "admin":
                        # ADR-0245：只能切進靈魂；自訂模型、群聊、DebugAgent／DispatchAgent
                        # 或任意 ID 都不能經 X-Agent-ID 取得代理身分。
                        if not soul_service.is_toukasoul(header_agent_id):
                            logger.warning(
                                "🚨 %s attempted to assume non-soul agent %s",
                                identity.agent_id,
                                header_agent_id,
                            )
                            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
                        logger.info(
                            "🛡️ User %s assuming identity of owned agent: %s",
                            identity.agent_id,
                            header_agent_id,
                        )
                        # --- 身分代打配對 (ADR-0141 §8-1，選項 A：僅管理權) ---
                        # 取代把 "admin" 字串貼到 is_human=False 身分上的走私：admin 駕駛員
                        # 帶 human_role="admin"（保管理權、閘讀 human_role），不繼承目標軀體
                        # 角色（agent_role=None）——與拆分前行為一致（舊 role="admin" 亦
                        # value-route 至 human_role）。非 admin 擁有者則成為目標軀體 agent。
                        # ADR-0147：帶類型旗標套 tag_rules——擁有者代打自己的靈魂時，
                        # 該靈魂解析為 Owner（而非全塌 Base）。admin 代打仍走選項 A
                        # （human_role="admin"、不繼承軀體角色），但 caps 取目標軀體。
                        assumed_role, caps = agent_service.resolve_role_and_caps(header_agent_id)
                        if identity.human_role == "admin":
                            return AgentIdentity(
                                agent_id=header_agent_id,
                                human_role="admin",
                                is_human=False,  # 身分切換為代理人模式執行
                                capabilities=caps,
                            )
                        return AgentIdentity.for_role(
                            header_agent_id,
                            assumed_role,
                            is_human=False,  # 身分切換為代理人模式執行
                            capabilities=caps,
                        )

                # --- PaC: 數據驅動的角色判定例外 (開發者特權) ---
                target_role = permission_service.get_role_key(header_agent_id)
                if is_trusted_local and target_role == "Debugger":
                    # P7 fix: only allow assumption of owned Debugger agents (or admin bypass)
                    if identity.human_role != "admin" and header_agent_id not in identity.owned_agents:
                        logger.warning(
                            "🚨 P7: %s attempted to assume unowned Debugger %s",
                            identity.agent_id,
                            header_agent_id,
                        )
                        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
                    logger.info(
                        "🛡️ Developer Access: %s → %s",
                        identity.agent_id,
                        header_agent_id,
                    )
                    caps = permission_service.get_capabilities_by_id(
                        header_agent_id
                    )
                    return AgentIdentity(
                        agent_id=header_agent_id,
                        agent_role=target_role,  # 已由 :124 閘保證為 "Debugger"
                        capabilities=caps,
                    )

                logger.warning(
                    "🚨 Identity Spoofing! Header: %s, Token: %s",
                    header_agent_id,
                    identity.agent_id,
                )
                raise ToukaException(ErrorCode.AUTH_AGENT_ID_MISMATCH)

            return identity

    # 3. 優雅降級 (Local Dynamic Handshake)
    if is_trusted_local:
        # 修復 3（ADR-0144，ADR-0131 的續篇）：LDH 僅限「系統未認領」（首裝——
        # 尚無任何完成真密碼設定的使用者；bootstrap 暫時密碼 admin 不算）。一旦
        # 認領，無卡本機請求不再降級成 DebugAgent 亦不發卡，一律 fail-loud——
        # 07-11 事故中，人類面認證死亡後 LDH 發的 agent 面降級卡被前端採納成
        # 「殭屍身分」擋住重新登入（跨面越界）。正規通道＝登入／refresh。
        if AuthService().any_claimed_user():
            logger.warning(
                "🛡️ [Gate 1] LDH refused (system claimed): cardless local request from %s",
                client_host,
            )
            return None

        # 如果前端提供了 ID (X-Agent-ID)，根據 identity_map 動態判定角色
        target_id = header_agent_id or "DebugAgent"

        # ADR-0147 審查修正：LDH（無卡首裝視窗）**刻意不帶類型旗標**——header 的
        # agent_id 是攻擊者可控。帶旗標會讓 X-Agent-ID=<既存靈魂 id> 解析成 Owner
        # 並鑄一張綁定該具名靈魂（其記憶 vault/home 錨點）的 Owner 卡＝身分冒用面
        # 擴大。不帶旗標：DebugAgent 經 agent_overrides→Debugger（首裝開發卡照舊），
        # 任意靈魂 id 落回 default Base（無害）。正規「擁有者代打自己的靈魂→Owner」
        # 走上方帶卡的 owned_agents 代打路徑（:98），不經此無卡分支。
        role = permission_service.get_role_key(target_id)
        caps = permission_service.get_capabilities_by_id(target_id)

        identity = AgentIdentity.for_role(target_id, role, capabilities=caps)
        logger.info(
            "🛡️ Local Graceful Downgrade: '%s' (Role: %s)",
            target_id,
            role,
        )

        # P24 fix: rate-limit token issuance to prevent flooding
        from src.utils.time_utils import get_current_epoch  # noqa: LOCAL_IMPORT_BAN
        now = get_current_epoch()
        if _ldh_limiter.is_allowed(client_host, now):
            _ldh_limiter.record(client_host, now)
            # 同步發放一個正式 Token，讓前端從此之後可以「帶卡說話」
            new_token = identity_service.issue_token(identity)
            request.state.new_auth_token = new_token
        else:
            logger.warning("🚨 LDH rate limit exceeded for %s, skipping token issuance", client_host)

        return identity

    # 4. 無身分且非本機私網，回傳 None
    logger.warning("🚨 Anonymous access attempt from %s", client_host)
    return None


async def get_verified_identity(
    request: Request,
    auth: HTTPAuthorizationCredentials | None = Depends(auth_scheme),  # noqa: B008
) -> AgentIdentity:
    """FastAPI 權限攔截依賴 (Gate 1 - 強制驗證身分).

    強制要求必須有已驗證的身分，否則拋出 AUTH_INVALID_TOKEN (401)。
    """
    identity = await get_optional_identity(request, auth)
    if not identity:
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
    return identity

