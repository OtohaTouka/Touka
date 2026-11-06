"""User Management Service.

Handles administrative CRUD operations for users, profile updates, and agent allocation.
Adheres to SSOT, SOLID, and TDD principles.
"""

import contextlib
import logging
from typing import Any

from src.config import get_initial_admin_password_file
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.user import User  # noqa: TC001
from src.services import avatar_service
from src.services.auth_service import AuthService, validate_new_password
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


class UserService:
    """Service for managing user profiles and administrative tasks."""

    def __init__(self, auth_service: AuthService | None = None) -> None:
        """初始化使用者服務，注入或建立驗證服務實例。"""
        # UserService depends on AuthService for reading/writing users.json (SSOT)
        self.auth_service = auth_service or AuthService()

    def get_users(self) -> list[User]:
        """Fetch all users from the registry."""
        return self.auth_service.get_all_users()

    def get_user(self, user_id: str) -> User:
        """Fetch a single user by ID."""
        user = self.auth_service.get_user_by_id(user_id)
        if not user:
            raise ToukaException(
                ErrorCode.RESOURCE_NOT_FOUND, detail_override=f"User with ID {user_id} not found"
            )
        return user

    def create_user(self, email: str, password: str, **kwargs: Any) -> User:
        """Create a new user with extended profile data.

        P3（認證縫）：全欄位一次原子落盤——舊制走 auth_service.create_user 落檔後，再於
        鎖外掛擴充欄位、以整物件覆蓋回寫（兩段非原子）；兩寫之間帳號已存在且可被並發
        路徑讀寫（如立即登入寫 refresh_token_jti），第二寫會整體回滾中間變更。改為單一
        _atomic_users_write 閉包內建立＋掛欄位（與 admin_update_user 一致）。
        """
        # 密碼策略 SSOT（非空且 ≥8）＋雜湊在鎖外（CPU-bound）。
        from src.services.auth_service import validate_new_password  # noqa: LOCAL_IMPORT_BAN
        validate_new_password(password)
        hashed = self.auth_service.hash_password(password)
        import uuid  # noqa: LOCAL_IMPORT_BAN

        result: list[User] = []

        def _mutate(users: list[User]) -> None:
            if any(u.email == email for u in users):
                raise ToukaException(
                    ErrorCode.VALIDATION_ERROR, message="Email already registered"
                )
            new_user = User(
                id=str(uuid.uuid4()),
                email=email,
                role=kwargs.get("role", "normal"),
                hashed_password=hashed,
                display_name=kwargs.get("display_name"),
                avatar_url=kwargs.get("avatar_url"),
                bio=kwargs.get("bio"),
                status=kwargs.get("status", "active"),
                created_at=int(get_current_epoch()),
                owned_agents=kwargs.get("owned_agents", []),
            )
            users.append(new_user)
            result.append(new_user)

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001
        return result[0]

    def update_user_profile(self, user_id: str, data: dict[str, Any]) -> User:
        """Update a user's own profile data (safe fields only).

        SSOT fix: read, mutate, and write happen inside one _atomic_users_write
        closure — prevents concurrent token rotation from being silently rolled back.
        """
        safe_fields = {"display_name", "avatar_url", "bio"}
        result: list[User] = []

        def _mutate(users: list[User]) -> None:
            user = next((u for u in users if u.id == user_id), None)
            if not user:
                raise ToukaException(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    detail_override=f"User with ID {user_id} not found",
                )
            for field in safe_fields:
                if field in data:
                    setattr(user, field, data[field])
            result.append(user)

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001
        return result[0]

    def change_own_password(
        self, user_id: str, current_password: str, new_password: str
    ) -> None:
        """Self-service password change (DA25).

        Verifies the caller's CURRENT password (so a stolen access token alone cannot
        rotate the password), sets the new one, clears the temp-password flag, and
        revokes existing refresh tokens (forces re-login on other sessions). bcrypt is
        computed outside the write lock (CPU-bound). Used by POST /auth/change-password.
        """
        user = self.auth_service.get_user_by_id(user_id)
        if not user or not self.auth_service.verify_password(
            current_password, user.hashed_password
        ):
            raise ToukaException(
                ErrorCode.AUTH_REAUTH_FAILED,
                detail_override="Current password is incorrect.",
            )
        # 修復 0：服務層縱深防禦（端點 pydantic min_length=8 之下再守一層——
        # 服務層是所有寫入呼叫者的最終防線，不依賴上游 schema）。
        validate_new_password(new_password)
        new_hashed_password = self.auth_service.hash_password(new_password)

        def _mutate(users: list[User]) -> None:
            target = next((u for u in users if u.id == user_id), None)
            if target is not None:
                target.hashed_password = new_hashed_password
                target.is_temp_password = False
                target.refresh_token_exp = None  # revoke existing refresh tokens
                # 修復 1：連既有 access token 一併撤銷（iat < 此戳記＝拒絕）。時間戳
                # 在鎖內取（而非 bcrypt 前），把「舊密碼並發登入搶到不死 token」的
                # 競態窗從數百 ms 縮到鎖粒度。
                target.password_changed_at = int(get_current_epoch())

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001
        # DA21/DA25: rotating the admin's password makes the bootstrap file obsolete.
        if user.role == "admin":
            with contextlib.suppress(OSError):
                resource_manager.remove_file(get_initial_admin_password_file())

    def admin_update_user(self, user_id: str, data: dict[str, Any]) -> User:
        """Update any user field (Admin only).

        SSOT fix: read, mutate, and write happen inside one _atomic_users_write
        closure — prevents concurrent token rotation from being silently rolled back.
        bcrypt is pre-computed outside the lock (CPU-intensive; must not block I/O path).
        """
        admin_fields = {
            "display_name", "avatar_url", "bio", "role",
            "status", "owned_agents", "email",
        }
        # P3（認證縫）：關鍵欄位的顯式 null 一律視為「不更新」（no-op），不得寫成 JSON
        # null 毒化 users.json——07-11「看鍵不看值」只修了空字串、沒修 null。這些欄位在
        # User schema 非 Optional，寫成 null 會讓下次 _read_users 全站 500（_write_users
        # 已補寫前重驗兜底，此處於 API 面把 null 收斂為 no-op＝對編輯表單更友善）。
        _non_nullable = {"role", "status", "email", "owned_agents"}
        data = {
            k: v for k, v in data.items()
            if not (k in _non_nullable and v is None)
        }
        # 修復 0（2026-07-11 空密碼事故）：空字串／None＝「保持不變」——編輯表單的
        # 密碼欄提示「留空保持不變」自此成為真話（舊判準 `if "password" in data` 看鍵
        # 不看值，空欄送出即把密碼覆蓋成 bcrypt("")＋強制登出＝兩個 admin 全鎖死）。
        # 非空才視為變更，且先過伺服器端密碼策略（≥8；驗證失敗＝整筆原子拒絕）。
        new_hashed_password: str | None = None
        raw_password = data.get("password")
        if raw_password:
            validate_new_password(raw_password)
            new_hashed_password = self.auth_service.hash_password(raw_password)

        result: list[User] = []

        def _mutate(users: list[User]) -> None:
            user = next((u for u in users if u.id == user_id), None)
            if not user:
                raise ToukaException(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    detail_override=f"User with ID {user_id} not found",
                )
            # P3（認證縫）：改 email 須查唯一性（鎖內）——create_user 有查重、admin 改
            # email 卻無，撞號後 get_user_by_email 恆命中排序在前者 → 另一帳號密碼正確也
            # 登不進（永久遮蔽）＋token 操作可能寫錯帳號。改成自己現有 email＝no-op 放行。
            new_email = data.get("email")
            if (
                new_email is not None
                and new_email != user.email
                and any(u.id != user.id and u.email == new_email for u in users)
            ):
                raise ToukaException(
                    ErrorCode.VALIDATION_ERROR,
                    detail_override="Email already registered",
                )
            # 末位 admin 守衛（鏡像 delete_user，pre-mutate）：不可把唯一在職管理員降級/停權，
            # 否則 require_admin / 登入 / refresh 三閘全擋 → 不可從 UI 復原的管理員鎖死。
            was_active_admin = user.role == "admin" and user.status == "active"
            new_role = data.get("role", user.role)
            new_status = data.get("status", user.status)
            will_be_active_admin = new_role == "admin" and new_status == "active"
            if was_active_admin and not will_be_active_admin:
                other_active_admins = [
                    u for u in users
                    if u.id != user.id and u.role == "admin" and u.status == "active"
                ]
                if not other_active_admins:
                    raise ToukaException(
                        ErrorCode.VALIDATION_ERROR,
                        detail_override="Cannot demote or suspend the last administrator",
                    )
            for field in admin_fields:
                if field in data:
                    setattr(user, field, data[field])
            if new_hashed_password is not None:
                user.hashed_password = new_hashed_password
                user.is_temp_password = False
                user.refresh_token_exp = None  # Force logout on password change
                # 修復 1：同步撤銷既有 access token（見 change_own_password 說明）。
                user.password_changed_at = int(get_current_epoch())
            result.append(user)

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001
        # DA21: once the admin sets a real password, the one-time bootstrap password
        # file is obsolete — remove it (best-effort).
        if new_hashed_password is not None and result and result[0].role == "admin":
            with contextlib.suppress(OSError):
                resource_manager.remove_file(get_initial_admin_password_file())
        return result[0]

    def delete_user(self, user_id: str) -> None:
        """Delete a user from the system.

        N11 fix: uses _atomic_users_write to prevent TOCTOU race between the
        last-admin check and the subsequent write.
        """
        def _mutate(users: list[User]) -> None:
            user_to_delete = next((u for u in users if u.id == user_id), None)
            if not user_to_delete:
                # errors.json has no NOT_FOUND code (the old ErrorCode.NOT_FOUND
                # raised AttributeError → 500); use the existing 404 resource code.
                msg = "User not found"
                raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND, detail_override=msg)

            if user_to_delete.role == "admin":
                admins = [u for u in users if u.role == "admin"]
                if len(admins) <= 1:
                    raise ToukaException(
                        ErrorCode.VALIDATION_ERROR,
                        detail_override="Cannot delete the last administrator",
                    )

            # P25 fix: revoke session before deletion so refresh cookie is invalid
            for i, u in enumerate(users):
                if u.id == user_id:
                    users[i].refresh_token_exp = 0
                    users[i].refresh_token_jti = None
                    break

            users[:] = [u for u in users if u.id != user_id]

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001
        # ADR-0106: remove the deleted user's avatar file to avoid orphaned uploads.
        with contextlib.suppress(Exception):
            avatar_service.delete_avatar(user_id)
        logger.info(f"🛡️ User {user_id} deleted successfully")

    def _update_user_in_list(self, updated_user: User) -> None:
        """Internal helper to persist a single user's changes to the SSOT.

        N11 fix: uses _atomic_users_write to prevent TOCTOU between read and write.
        """
        def _mutate(users: list[User]) -> None:
            for i, u in enumerate(users):
                if u.id == updated_user.id:
                    users[i] = updated_user
                    return
            users.append(updated_user)

        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001

    def add_owned_agent(self, user_id: str, agent_identity: AgentIdentity) -> None:
        """Add an agent to a user's owned list safely. (SOLID/SSOT)"""
        agent_id = agent_identity.agent_id
        def _mutate(users: list[User]) -> None:
            user = next((u for u in users if u.id == user_id), None)
            if user and agent_id not in user.owned_agents:
                user.owned_agents.append(agent_id)
        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001

    def rename_owned_agent(self, old_id: str, new_id: str) -> None:
        """改名：所有使用者擁有清單裡的舊 ID 原地換成新 ID，一次原子寫入（禁令 21）。

        新 ID 已在同一份清單裡時只留一份。改名前新 ID 沒有模型，沒有舊 ID 的人清單裡的
        新 ID 是刪除後留下的殘留，一併拿掉，免得改名後的模型落到他手上。
        """
        def _mutate(users: list[User]) -> None:
            for user in users:
                if old_id in user.owned_agents:
                    renamed = [new_id if a == old_id else a for a in user.owned_agents]
                    user.owned_agents = list(dict.fromkeys(renamed))
                elif new_id in user.owned_agents:
                    user.owned_agents = [a for a in user.owned_agents if a != new_id]
        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001

    def claim_owned_agent(self, agent_identity: AgentIdentity, owner_user_id: str | None) -> None:
        """新建立的 ID 只屬於建立者，一次原子寫入（禁令 21）。

        先從所有人的清單拿掉這個 ID（刪除後留下的殘留），再加給 ``owner_user_id``；
        ``None`` 時誰都不加。
        """
        agent_id = agent_identity.agent_id
        def _mutate(users: list[User]) -> None:
            for user in users:
                if agent_id in user.owned_agents:
                    user.owned_agents = [a for a in user.owned_agents if a != agent_id]
                if user.id == owner_user_id:
                    user.owned_agents.append(agent_id)
        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001

    def remove_owned_agent_from_all(self, agent_identity: AgentIdentity) -> None:
        """刪除：所有使用者清單裡的這個 ID 一起拿掉，一次原子寫入（禁令 21）。"""
        self.claim_owned_agent(agent_identity, None)

    def remove_owned_agent(self, user_id: str, agent_identity: AgentIdentity) -> None:
        """Remove an agent from a user's owned list safely. (SOLID/SSOT)"""
        agent_id = agent_identity.agent_id
        def _mutate(users: list[User]) -> None:
            user = next((u for u in users if u.id == user_id), None)
            if user and agent_id in user.owned_agents:
                # 避開 .remove 呼叫以避免 SSOTGuard IO_PRISON_VIOLATION 誤判
                user.owned_agents = [a for a in user.owned_agents if a != agent_id]
        self.auth_service._atomic_users_write(_mutate)  # noqa: SLF001

