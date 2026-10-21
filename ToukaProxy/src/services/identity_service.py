"""身分服務 (IdentityService) 模組.

負責 Touka 系統中代理人身分的發行與驗證。
採用 JWT (JSON Web Token) 作為核心認證機制，並與權限系統 (PermissionService) 深度連動，
確保所有入站請求具備合法且經驗證的身分主權.
"""

import logging
from typing import Protocol

import jwt

from src.core.security import AuthSecretManager
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


class IIdentityService(Protocol):
    """身分提供者介面 (Dependency Inversion).

    定義身分驗證與 Token 核發的行為標準，確保系統可以抽換不同的認證供應商.
    """

    def verify_token(self, token: str) -> AgentIdentity | None:
        """驗證 Token 並回傳身分物件."""
        ...

    def issue_token(self, identity: AgentIdentity, expires_delta: int | None = None) -> str:
        """為指定代理人核發新的 Token."""
        ...


class JWTAuthenticationProvider(IIdentityService):
    """JWT 身分提供者 (SOLID - Implementation).

    負責 Token 簽署、過期驗證與 AgentIdentity 轉換。
    此實作與權限系統連動，在驗證身分的同時會載入該 Agent 的角色與能力標籤 (Capabilities).
    """

    ALGORITHM = "HS256"
    DEFAULT_EXPIRY = 3600 * 24 * 7  # 預設一週 (7天)

    @property
    def secret(self) -> str:
        """動態獲取驗證金鑰，避免快取 stall。"""
        return AuthSecretManager.get_secret()

    def verify_token(self, token: str) -> AgentIdentity | None:
        """驗證並將 JWT 酬載轉譯為實體 AgentIdentity (SSOT).

        此過程包括：
        1. JWT 簽章與過期校驗.
        2. 提取 'sub' 宣告作為核心 ID.
        3. 向 PermissionService 查詢並綁定角色與能力.

        Args:
            token (str): 待驗證的 JWT 字串.

        Returns:
            AgentIdentity | None: 經驗證的身分物件，若驗證失敗則回傳 None.
        """
        try:
            payload = jwt.decode(token, self.secret, algorithms=[self.ALGORITHM])
            agent_id = payload.get("sub")

            if not agent_id:
                logger.warning("JWT missing 'sub' claim.")
                return None

            # #1/#3 fix: reject non-access typed tokens (refresh/service) presented
            # as a Bearer. Only access tokens (type='access') and untyped agent
            # tokens (issue_token) may authenticate here; refresh tokens stay usable
            # ONLY at /auth/refresh, where jti rotation/revocation runs.
            token_type = payload.get("type")
            if token_type not in (None, "access"):
                logger.warning("Rejected non-access JWT type %r at Bearer gate.", token_type)
                return None

            # 1. 優先檢查是否為人類使用者 (Human RBAC SSOT)
            from src.services.auth_service import AuthService, access_token_is_stale
            auth_service = AuthService()
            user = auth_service.get_user_by_id(agent_id)
            if user:
                # DA23: a suspended user's existing access tokens stop working
                # immediately at the central Bearer gate.
                if not user.is_active:
                    logger.warning("Rejected token for suspended user %s.", agent_id)
                    return None
                # 修復 1：改密碼即撤——與 routers/auth.get_current_user 同判準
                # （SSOT），兩道 Bearer 閘一致拒絕改密碼前簽發的 token。
                if access_token_is_stale(payload, user):
                    logger.warning(
                        "Rejected pre-password-change token for user %s.", agent_id
                    )
                    return None
                return AgentIdentity(
                    agent_id=agent_id,
                    human_role=user.role,  # User.role ∈ {admin, normal}＝人類面
                    is_human=True,
                    owned_agents=user.owned_agents,
                    capabilities=["heartbeat_awakening"] if user.role == "admin" else []
                )

            # 2. 若非人類，則獲取與身分綁定的代理人角色與能力標籤 (連動 PermissionService SSOT)
            # ADR-0147：經 agent_service 帶類型旗標（is_toukasoul/is_custom）套 tag_rules，
            # 靈魂才會解析成 Owner（而非全塌 default Base）。
            role, caps = agent_service.resolve_role_and_caps(agent_id)

            return AgentIdentity.for_role(agent_id, role, is_human=False, capabilities=caps)

        except jwt.ExpiredSignatureError:
            logger.warning("JWT Token has expired.")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT Token: {e}")
            return None
        except Exception:
            logger.exception("Unexpected error in verify_token")
            return None

    def issue_token(self, identity: AgentIdentity, expires_delta: int | None = None) -> str:
        """為 Agent 核發 JWT (Identity Issue SSOT).

        Args:
            identity (AgentIdentity): 經驗證的身分對象.
            expires_delta (int | None): 過期秒數，若未提供則使用預設值.

        Returns:
            str: 簽署完成的 JWT 字串.
        """
        agent_id = identity.agent_id
        now = int(get_current_epoch())
        expiry = now + (expires_delta or self.DEFAULT_EXPIRY)
        payload = {"sub": agent_id, "exp": expiry, "iat": now, "iss": "ToukaProxy"}
        return jwt.encode(payload, self.secret, algorithm=self.ALGORITHM)

    def get_identity(self, identity_id: str) -> AgentIdentity:
        """透過 Agent ID 快速解析並組裝身分物件 (不經過 Token 驗證).
        
        此方法主要用於內部服務 (如 Heartbeat 或系統排程) 在已知 ID 的情況下
        獲取該 ID 綁定的角色與能力標籤。
        """
        # ADR-0147：同 verify_token，經 agent_service 帶類型旗標套 tag_rules
        # （靈魂→Owner）；heartbeat/排程喚醒的靈魂才不會全塌 Base。
        role, caps = agent_service.resolve_role_and_caps(identity_id)
        return AgentIdentity.for_role(identity_id, role, capabilities=caps)


# 單例實例 (SSOT Identity Provider)
identity_service = JWTAuthenticationProvider()
