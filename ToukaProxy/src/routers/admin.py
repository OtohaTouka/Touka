"""Admin Router for User Management.

Handles administrative tasks like user CRUD and role management.
Enforces Admin RBAC.
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, EmailStr, StrictBool

from src.core.exceptions import ErrorCode, ToukaException
from src.models.agent_reset import AgentResetResponse, LastResetStatus
from src.models.identity import AgentIdentity
from src.models.user import User, UserCreate, UserOut
from src.routers.auth import get_auth_service, get_current_user
from src.routers.system import restore_in_progress
from src.services.agent_reset.last_result import last_reset
from src.services.agent_reset.orchestrator import reset_agent_data, scrub_rejected
from src.services.auth_service import AuthService
from src.services.authorization_gateway import require_admin_authority, require_admin_human
from src.services.user_service import UserService
from src.utils.async_db import session_service
from src.utils.csrf_guard import verify_origin

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


# Dependency to enforce admin role（公開名稱，setup.py 亦匯入；ADR-0239 共用實作）。
require_admin = require_admin_authority


def get_user_service() -> UserService:
    """Dependency for UserService."""
    return UserService()


class AdminUpdateUserRequest(BaseModel):
    """Request model for admin to update a user.

    P3（認證縫）：role/status 收斂為 Literal 枚舉——過去接受任意字串，typo（如
    role="Admin" 大小寫錯、status="disabled"）會悄悄失權/等同停權（is_active 判準是
    status=="active"）。省略欄位＝不更新（exclude_unset）；顯式 null 的關鍵欄位由
    服務層收斂為 no-op（見 admin_update_user）。
    """
    display_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    role: Literal["admin", "normal"] | None = None
    status: Literal["active", "suspended"] | None = None
    owned_agents: list[str] | None = None
    email: EmailStr | None = None
    password: str | None = None


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _admin: AgentIdentity = Depends(require_admin),
    user_service: UserService = Depends(get_user_service)
) -> list[User]:
    """List all users (Admin only)."""
    return user_service.get_users()


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user_detail(
    user_id: str,
    _admin: AgentIdentity = Depends(require_admin),
    user_service: UserService = Depends(get_user_service)
) -> User:
    """Get user details (Admin only)."""
    return user_service.get_user(user_id)


@router.post("/users", response_model=UserOut)
async def create_user(
    data: UserCreate,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
    user_service: UserService = Depends(get_user_service),  # noqa: B008
) -> User:
    """Create a new user (Admin only)."""
    new_user = user_service.create_user(
        email=data.email,
        password=data.password,
        role=data.role,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
        bio=data.bio,
    )
    logger.warning(  # P27 fix: audit trail for admin user creation
        "AUDIT: admin %s created user %s with role %s",
        _admin.agent_id,
        new_user.email,
        new_user.role,
    )
    return new_user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    data: AdminUpdateUserRequest,
    _admin: AgentIdentity = Depends(require_admin),  # noqa: B008
    user_service: UserService = Depends(get_user_service),  # noqa: B008
) -> User:
    """Update a user (Admin only)."""
    updated = user_service.admin_update_user(user_id, data.model_dump(exclude_unset=True))
    logger.warning(  # P27 fix: audit trail for admin user updates
        "AUDIT: admin %s updated user %s with role %s",
        _admin.agent_id,
        updated.email,
        updated.role,
    )
    return updated


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    _admin: AgentIdentity = Depends(require_admin),
    user_service: UserService = Depends(get_user_service)
) -> dict:
    """Delete a user (Admin only)."""
    user_service.delete_user(user_id)
    # F-fix: also remove the deleted user's owned project rows (un-link their sessions,
    # keep physical root_path folders) — mirrors clear_my_data (auth.py). Without this
    # the rows keep a dangling owner_user_id and become inaccessible to non-admins.
    # Best-effort: the account is already removed (and last-admin guard has passed), so
    # a cleanup hiccup must not fail the request (mirrors delete_user's avatar cleanup);
    # failures are logged, not silent.
    try:
        cleared = await session_service.clear_projects_owned_by(user_id)
        logger.info("Cleared %d owned project(s) for deleted user %s", cleared, user_id)
    except Exception:
        logger.exception("Failed to clear owned projects for deleted user %s", user_id)
    return {"message": "User deleted successfully"}


class AgentResetRequest(BaseModel):
    """Per-agent 資料重置請求（ADR-0113）：雙重確認＋密碼重輸，防誤觸。

    `confirm_agent_id` 為字面字串確認（須與目標代理人相符），非身分傳遞 → 不適用 IDENTITY_SSOT。
    ``scrub_backups``：一併清除備份裡這個靈魂的記憶（預設不勾）；``scrub_acknowledged``：第二段確認。
    多送或打錯的欄位一律 422（打錯會變成「以為清了其實沒清」）。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    confirm_agent_id: str = ""
    current_password: str = ""
    scrub_backups: StrictBool = False
    scrub_acknowledged: StrictBool = False


def _scrub_rejected(actor: AgentIdentity, target: str, reason: str) -> None:
    """勾選清除連備份卻被擋下的稽核（格式只有清除協調器那一份）."""
    scrub_rejected(actor.agent_id, target, reason)


@router.post("/agents/{target}/reset", response_model=AgentResetResponse)
async def reset_agent(
    target: str,
    payload: AgentResetRequest,
    http_request: Request,
    actor: AgentIdentity = Depends(require_admin),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict:
    """ADR-0113：破壞性重置**單一 agent** 累積的執行期資料（保留身分/註冊），**admin only**。

    安全層（缺一即零刪除中止）：admin 角色 → CSRF Origin → **密碼重輸（DA25）** → 雙重確認
    （`confirm=true` 且 `confirm_agent_id` 與 target 字面相符）→ `assert_agent_resettable`
    （存在＋被擁有＋非系統保護）→ **備份優先**（失敗即中止）。身分（soul/legacy）受
    `is_protected_path` 物理保護、custom config 由清除原語範圍排除——永不被觸及。
    """
    scrub = payload.scrub_backups
    try:
        verify_origin(http_request)
    except ToukaException:
        if scrub:
            _scrub_rejected(actor, target, "bad_origin")
        raise
    if not payload.confirm or payload.confirm_agent_id != target:
        if scrub:
            _scrub_rejected(actor, target, "not_confirmed")
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override="重置需 confirm=true 且 confirm_agent_id 與目標代理人字面相符。",
        )
    if scrub:
        # 清除連備份只給人類 admin（admin 代打不算）；在驗密碼之前擋，代打拿不到密碼試錯的機會。
        try:
            require_admin_human(actor)
        except ToukaException:
            _scrub_rejected(actor, target, "not_human")
            raise
        if not payload.scrub_acknowledged:
            _scrub_rejected(actor, target, "not_acknowledged")
            raise ToukaException(ErrorCode.MEMORY_SCRUB_NOT_ACKNOWLEDGED)
    if restore_in_progress():  # 還原會整個蓋掉資料：勾不勾清除都不能同時重置
        if scrub:
            _scrub_rejected(actor, target, "restore_in_progress")
        raise ToukaException(ErrorCode.SYSTEM_RESTORE_IN_PROGRESS)
    if not auth_service.verify_password(payload.current_password, current_user.hashed_password):
        if scrub:
            _scrub_rejected(actor, target, "bad_password")
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)
    logger.warning(
        "⚠️ AUDIT: agent-reset requested actor=%r target=%r scrub=%s（備份優先）",
        actor.agent_id,
        target,
        scrub,
    )
    report = await reset_agent_data(target, actor, scrub=scrub)
    return report.to_dict()


@router.get("/agents/reset/last", response_model=LastResetStatus)
async def last_agent_reset(
    _admin: Annotated[AgentIdentity, Depends(require_admin_human)],
) -> LastResetStatus:
    """最後一次重置的結果（舊審查報告丙-5，ADR-0289）：「結果不明」時畫面據此查詢.

    只給人類 admin（admin 代打不算，和清除連備份同一道閘）。只存在記憶體，伺服器重啟後回 idle。
    """
    return last_reset()
