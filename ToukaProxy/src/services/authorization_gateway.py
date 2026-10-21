"""授權閘道 (Authorization Gateway) — 路由角色檢查的唯一實作（ADR-0239）。

各 router 原本各自定義的 ``_require_*`` 角色 helper 只是 ``AgentIdentity`` 判斷式的薄包裝，
歸納為 4 類相同行為加 1 個特例。本模組提供這 5 個共用函式，router 以「保留原名稱的別名」
引用（例如 ``_require_mcp_role = require_privileged``），端點簽名、依賴順序、Guard #24／#29
的名稱辨識與 dependency override 皆不變。

共同契約：同步 ``def``、唯一參數 ``identity`` 依賴 ``get_verified_identity``、不記錄日誌、
通過時回傳原 identity。拒絕的錯誤碼：

- ``require_privileged``：``is_privileged()``（Owner/Debugger/admin）→ ``AUTH_PERMISSION_DENIED``
- ``require_owner_or_admin``：``is_owner_or_admin()``（排除 Debugger）→ ``AUTH_PERMISSION_DENIED``
- ``require_admin_authority``：``is_admin_authority()`` → ``AUTH_ADMIN_REQUIRED``
- ``require_authenticated``：任何已認證身分 → ``AUTH_PERMISSION_DENIED``
- ``require_admin_human``：人類且 admin → ``AUTH_ADMIN_REQUIRED``（自 ``routers/mcp_oauth.py``
  原樣搬移，保留既有「未檢查 None」的行為，不在本次純重構修正）

在函式內直接呼叫（debug／observability）與掛在 ``Depends`` 的兩種用法各自保留：前者遇到
格式錯誤的 body 回 422（body 先驗證），後者回 403（依賴先解）。

``require_human_requester``（ADR-0249 插話）：人類或 admin 代打 → 否則 ``AUTH_PERMISSION_DENIED``。

另有 ``can_drive_soul``／``require_soul_access``（ADR-0245 修補）：不是 Depends，由 pipeline
在解析出靈魂目標後呼叫，判斷呼叫者能不能以該靈魂為對話目標。
"""

from fastapi import Depends

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager


def require_privileged(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """三元組閘：Owner／Debugger／人類 admin（``is_privileged``）。"""
    if not identity or not identity.is_privileged():
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return identity


def require_owner_or_admin(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """{Owner, admin} 閘：刻意排除 Debugger（``is_owner_or_admin``）。"""
    if not identity or not identity.is_owner_or_admin():
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return identity


def require_admin_authority(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """admin-only 閘：人類 admin 權威（``is_admin_authority``，含 admin 代打）。"""
    if not identity or not identity.is_admin_authority():
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)
    return identity


def require_authenticated(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """已認證閘：任何有效身分即放行，不看角色。"""
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return identity


def require_admin_human(
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """人類 admin 閘：須為人類且具 admin 權威（admin 代打不算）。"""
    if not identity.is_human or not identity.is_admin_authority():
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)
    return identity


def require_human_requester(
    identity: AgentIdentity | None = Depends(get_verified_identity),  # noqa: B008
) -> AgentIdentity:
    """人類呼叫者閘（ADR-0249 插話）：人類或 admin 代打；其他代理身分一律拒絕。

    非 admin 的 X-Agent-ID 代打、靈魂 token、DebugAgent 都是代理身分（``is_human=False``
    且沒有 admin 權威），不能把文字送進模型的 context。
    """
    if not identity or not (identity.is_human or identity.is_admin_authority()):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return identity


def can_drive_soul(identity: AgentIdentity, soul: AgentIdentity) -> bool:
    """靈魂目標判斷（ADR-0245 修補）：``identity`` 能不能以 ``soul`` 為對話目標（套人設、家對話）。

    admin（含 X-Agent-ID 代打，ADR-0141 §8-1）一律可以；其他人類要擁有該靈魂；
    代理只能是靈魂本身或 DebugAgent。不是 Depends：呼叫端在解析出目標後自己呼叫。
    """
    if identity.is_admin_authority():
        return True
    if identity.is_human:
        return soul.agent_id in identity.owned_agents
    return identity.agent_id in (soul.agent_id, "DebugAgent")


def require_soul_access(identity: AgentIdentity, soul: AgentIdentity) -> None:
    """``can_drive_soul`` 的拒絕版：不符回 403 ``AUTH_PERMISSION_DENIED``。"""
    if not can_drive_soul(identity, soul):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)


def can_access_session(
    caller: AgentIdentity,
    session_agent_id: str | None,
    owner_user_id: str | None = None,
) -> bool:
    """DA3 session 存取（IDOR 防線）；路由與 dispatcher 共用（ADR-0245 修補）。

    admin/Debugger 全看；人類是 ``owner_user_id`` 本人直接放行（ADR-0107 聯集）；其餘要
    擁有 session 綁的 agent（``resource_manager.is_owner``）；沒綁 agent 的舊 session 只給
    admin/Debugger。
    """
    if caller.is_admin_or_debugger():
        return True
    if owner_user_id and caller.is_human and caller.agent_id == owner_user_id:
        return True
    if not session_agent_id:
        return False
    if not caller.is_human:
        # 非人類呼叫者（代理）：只接受 session 綁的 agent 等於自己；不走 is_owner 的
        # 「無擁有者」與 30 天分支（ADR-0245 修補）。Debugger 已於上面放行。
        return caller.agent_id == session_agent_id
    return resource_manager.is_owner(AgentIdentity(agent_id=session_agent_id), caller)
