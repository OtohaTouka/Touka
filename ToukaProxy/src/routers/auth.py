"""Authentication Router for ToukaProxy.

Handles registration, login, logout, and token refresh.
Enforces Identity SSOT and Error SSOT mandates.
"""

from __future__ import annotations

import contextlib
import logging

import jwt
from fastapi import (  # noqa: TC002
    APIRouter,
    Cookie,
    Depends,
    File,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, ConfigDict, EmailStr, Field  # noqa: TC002
from starlette.background import BackgroundTask

from src.config import (
    AUTH_RATE_LIMIT_MAX,
    AUTH_RATE_LIMIT_WINDOW_SEC,
    SECURE_COOKIES,
)
from src.core.exceptions import ErrorCode, ToukaException
from src.core.security import AuthSecretManager
from src.models.user import User, UserCreate, UserOut  # noqa: TC001
from src.services.auth_service import AuthService, access_token_is_stale
from src.services.avatar_service import delete_avatar, save_avatar
from src.services.gdpr_export_service import create_encrypted_export
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils.async_db import session_service
from src.utils.csrf_guard import verify_origin
from src.utils.rate_limiter import RateLimiter
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# H2 fix: shared rate limiter instance for login endpoint (IP-based sliding window)
login_rate_limiter = RateLimiter(
    max_attempts=AUTH_RATE_LIMIT_MAX,
    window_sec=AUTH_RATE_LIMIT_WINDOW_SEC,
)

# P6 fix: rate limiters for register and refresh endpoints
register_rate_limiter = RateLimiter(
    max_attempts=AUTH_RATE_LIMIT_MAX,
    window_sec=AUTH_RATE_LIMIT_WINDOW_SEC,
)
refresh_rate_limiter = RateLimiter(
    max_attempts=AUTH_RATE_LIMIT_MAX * 3,
    window_sec=AUTH_RATE_LIMIT_WINDOW_SEC,
)


def get_auth_service() -> AuthService:
    """獲取驗證服務實例。"""
    return AuthService()


def get_current_user(
    token: str = Depends(oauth2_scheme),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> User:
    """根據 Access Token 獲取目前的使用者身分物件。"""
    try:
        payload = jwt.decode(token, AuthSecretManager.get_secret(), algorithms=["HS256"])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
        # #1/#3 fix: a refresh/service token must not authenticate as a Bearer.
        if payload.get("type") not in (None, "access"):
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
    except jwt.InvalidTokenError as e:
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN) from e

    users = auth_service.get_all_users()
    user = next((u for u in users if u.id == user_id), None)
    if user is None:
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
    if not user.is_active:  # DA23: suspended users' tokens are rejected
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
    # 修復 1：改密碼即撤——簽發於最近一次密碼變更之前的 access token 立即失效
    # （不再有 1h 殘活窗）。判準 SSOT 見 auth_service.access_token_is_stale。
    if access_token_is_stale(payload, user):
        raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
    return user


class LoginRequest(BaseModel):
    """使用者登入請求模型。"""

    email: EmailStr
    password: str


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Set the refresh token HttpOnly cookie (H1 SSOT helper).

    Uses SECURE_COOKIES config value to control the secure flag:
    - False  → development (HTTP localhost)
    - True   → production (requires HTTPS / TLS at reverse proxy)
    """
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=30 * 24 * 3600,
        samesite="lax",
        path="/",
        secure=SECURE_COOKIES,  # H1 fix: configurable via SECURE_COOKIES env var
    )


@router.post("/register", response_model=UserOut)
async def register(
    request: Request,
    user_data: UserCreate,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> UserOut:
    """註冊新使用者。"""
    client_ip = request.client.host if request.client else "unknown"
    now = get_current_epoch()
    if not register_rate_limiter.is_allowed(client_ip, now):
        raise ToukaException(ErrorCode.AUTH_RATE_LIMITED)
    register_rate_limiter.record(client_ip, now)

    return auth_service.create_user(
        email=user_data.email,
        password=user_data.password,
        role="normal",
    )


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict:
    """使用者登入：核發 Access Token 並在 Cookie 設定 Refresh Token.

    H2 fix: enforces IP-based sliding-window rate limiting.
    H1 fix: sets Cookie secure flag from SECURE_COOKIES config.
    """
    client_ip = request.client.host if request.client else "unknown"
    now = get_current_epoch()

    # H2: check rate limit before attempting authentication
    if not login_rate_limiter.is_allowed(client_ip, now):
        raise ToukaException(ErrorCode.AUTH_RATE_LIMITED)

    try:
        tokens = auth_service.authenticate_user(req.email, req.password)
    except ToukaException:
        # Record failed attempt only on authentication failure
        login_rate_limiter.record(client_ip, get_current_epoch())
        raise

    # Successful login: reset counter for this IP
    login_rate_limiter.reset(client_ip)

    # H1: use helper that reads SECURE_COOKIES config
    _set_refresh_cookie(response, tokens["refresh_token"])

    return {
        "access_token": tokens["access_token"],
        "token_type": tokens["token_type"],
    }


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict:
    """重新整理 Access Token.

    H6 fix: also rotates the refresh token (issues a new one in the cookie).
    """
    client_ip = request.client.host if request.client else "unknown"
    now = get_current_epoch()
    if not refresh_rate_limiter.is_allowed(client_ip, now):
        raise ToukaException(ErrorCode.AUTH_RATE_LIMITED)
    refresh_rate_limiter.record(client_ip, now)

    if not refresh_token:
        raise ToukaException(ErrorCode.SYSTEM_ERROR, detail_override="Missing refresh token")

    # H6: refresh_access_token now returns both tokens (rotation)
    new_tokens = auth_service.refresh_access_token(refresh_token)

    # H1 + H6: rotate cookie with new refresh token
    _set_refresh_cookie(response, new_tokens["refresh_token"])

    return {
        "access_token": new_tokens["access_token"],
        "token_type": "bearer",
    }


def _user_id_from_refresh_cookie(request: Request) -> str | None:
    """從 HttpOnly refresh cookie 解出 user_id（驗簽＋type 檢查）；無效回 None。"""
    token = request.cookies.get("refresh_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, AuthSecretManager.get_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != "refresh":
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict:
    """使用者登出：撤銷後端 token 並清除 Cookie。

    P3（認證縫）：改為**憑 refresh cookie** 解出使用者撤銷——舊制依賴 get_current_user
    （需有效 Bearer），使用者閒置逾 1h（access 過期）點登出即得 401 → revoke 從未執行
    → 30 天 refresh cookie 完好 → 下次開頁 session 自動復活（「登出了其實沒登出」）。
    cookie 也失效＝session 本已死，best-effort 清 cookie 即可。
    """
    user_id = _user_id_from_refresh_cookie(request)
    if user_id:
        auth_service.revoke_user_tokens(user_id)
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}


@router.get("/me", response_model=UserOut)
async def get_me(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> UserOut:
    """獲取目前登入使用者的詳細資料。"""
    return current_user


class ChangePasswordRequest(BaseModel):
    """Self-service password change payload (DA25)."""

    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Change the authenticated user's OWN password (DA25).

    Requires the current password (a stolen access token alone cannot rotate it); clears
    any temp-password flag and revokes existing refresh tokens, so the user must re-login.
    """
    UserService().change_own_password(
        current_user.id, req.current_password, req.new_password
    )
    return {"status": "success", "message": "Password changed. Please log in again."}


class UpdateProfileRequest(BaseModel):
    """Self-service profile-update payload.

    Only the caller's own profile fields are accepted; ``extra="forbid"`` rejects
    role/email/status/owned_agents/password/etc. at the schema boundary (422) — defense
    in depth over ``UserService.update_user_profile``'s safe_fields whitelist.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None


@router.patch("/me", response_model=UserOut)
async def update_me(
    req: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> UserOut:
    """Update the authenticated user's OWN profile (display_name / avatar_url / bio).

    Identity comes from the verified JWT (``get_current_user``) — never a path param, so
    there is no IDOR surface (unlike the admin ``PATCH /users/{id}``). Only the three
    profile fields are writable; ``extra="forbid"`` and the service-layer whitelist both
    block privilege escalation. Persists via the atomic users write (禁令 21).
    """
    return UserService().update_user_profile(
        current_user.id, req.model_dump(exclude_unset=True)
    )


@router.post("/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(...),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> UserOut:
    """ADR-0106: upload the authenticated user's OWN avatar image (multipart).

    Identity comes from the verified JWT (``get_current_user``) → only the caller's
    own avatar is writable (no IDOR). ``save_avatar`` validates size + magic-byte
    format (rejects SVG/non-images) and stores it via ResourceManager. On success,
    ``avatar_url`` is set to the internal serving path with a cache-busting ``?v``.
    """
    data = await file.read()
    save_avatar(current_user.id, data)  # validates + persists; raises ToukaException on error
    epoch = get_current_epoch()
    return UserService().update_user_profile(
        current_user.id,
        {"avatar_url": f"/v1/users/{current_user.id}/avatar?v={epoch}"},
    )


@router.delete("/avatar", response_model=UserOut)
async def remove_avatar(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> UserOut:
    """ADR-0106: delete the authenticated user's OWN avatar file and clear avatar_url."""
    delete_avatar(current_user.id)
    return UserService().update_user_profile(current_user.id, {"avatar_url": None})


class ClearMyDataRequest(BaseModel):
    """ADR-0107: self-service personal-data clear payload (password re-entry)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str


@router.post("/clear-my-data")
async def clear_my_data(
    req: ClearMyDataRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> dict:
    """ADR-0107: clear the caller's OWN personal data.

    Scope: profile (display_name/bio), avatar (file + url), and the SETTINGS of
    projects the caller owns (DB rows; physical ``root_path`` folders are KEPT,
    and owned projects' sessions are un-linked via ``project_id=NULL`` — their
    conversation content is preserved). Account (email/password/role/owned_agents),
    agent conversations/memory, and audit trails are NOT touched.

    Safeguards: identity from JWT (self only, no IDOR), CSRF Origin guard, and
    password re-entry (a stolen access token alone cannot trigger it — DA25 pattern).
    No full-system backup (deliberately light per the data scope).
    """
    verify_origin(http_request)
    if not auth_service.verify_password(req.current_password, current_user.hashed_password):
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)

    # 1) Owned-project settings: delete rows + un-link sessions; keep physical folders.
    cleared_projects = await session_service.clear_projects_owned_by(current_user.id)
    # 2) Profile fields (incl. avatar_url=None) — the reliable DB clear runs first so a
    #    locked/unremovable avatar FILE cannot leave the profile half-cleared.
    UserService().update_user_profile(
        current_user.id, {"display_name": "", "bio": "", "avatar_url": None}
    )
    # 3) Avatar file — best-effort (avatar_url is already cleared); a locked file
    #    (e.g. AV scan) must not 500 the whole clear (mirrors user_service.delete_user).
    with contextlib.suppress(Exception):
        delete_avatar(current_user.id)
    logger.warning(
        "AUDIT: user %s self-cleared personal data (project_settings=%d; "
        "physical folders + agent data + account + audit retained)",
        current_user.id,
        cleared_projects,
    )
    return {
        "status": "cleared",
        "cleared": {"profile": True, "avatar": True, "project_settings": cleared_projects},
        "retained": {
            "account": True,
            "agent_data": True,
            "project_folders": True,
            "audit": True,
        },
    }


class ExportMyDataRequest(BaseModel):
    """ADR-0110：自助 GDPR 資料匯出請求（密碼再驗 + 加密密語）。"""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    # 匯出含對話與記憶（高敏個資）→ 強制加密；密語下限 12（比照 ADR-0108 下載）。
    passphrase: str = Field(min_length=12)


@router.post("/export-my-data")
async def export_my_data(
    req: ExportMyDataRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> FileResponse:
    """ADR-0110：匯出呼叫者本人的完整個資為 AES-256 加密 ZIP（GDPR 資料可攜權）。

    內含（脫敏）profile、頭像、對話、專案，以及每個自有 agent 的事實記憶與個人硬碟檔案。
    機密（密碼雜湊／token）、向量嵌入（衍生）與全域設定一律排除。

    防護：身分取自 JWT（僅本人、無 IDOR），CSRF Origin 守衛，密碼再驗（被竊 access
    token 亦無法單獨觸發——DA25 模式），即時加密（明文永不離開伺服器），串流後即刪暫存。
    **純唯讀**——完全不修改任何既有資料（含 agent / soul），符合常駐約束。
    """
    verify_origin(http_request)
    if not auth_service.verify_password(req.current_password, current_user.hashed_password):
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)
    enc_path = await create_encrypted_export(current_user, req.passphrase)
    logger.warning("AUDIT: user %s exported personal data (GDPR, encrypted)", current_user.id)
    epoch = int(get_current_epoch())
    return FileResponse(
        enc_path,
        media_type="application/zip",
        filename=f"touka_export_{current_user.id}_{epoch}.zip",
        background=BackgroundTask(resource_manager.remove_file, enc_path),
    )
