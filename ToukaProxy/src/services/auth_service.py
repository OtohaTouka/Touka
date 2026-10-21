"""Authentication and User Management Service.

Handles user CRUD against the users.json SSOT, password hashing, and token generation.
"""

import contextlib
import hashlib
import json
import logging
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any

import bcrypt
import jwt

from src.config import (
    AUTH_REFRESH_GRACE_PERIOD_SEC,
    CONFIG_DIR,
    get_initial_admin_password_file,
)
from src.core.exceptions import ErrorCode, ToukaException, load_error_definitions
from src.core.security import AuthSecretManager
from src.models.user import User
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

load_error_definitions()  # Trigger loading of errors.json in case it was cached

logger = logging.getLogger(__name__)

# N4 fix: module-level lock so every AuthService instance shares the same
# serialization guard for the users.json file. threading.Lock is used (not
# asyncio.Lock) because _read_users/_write_users are synchronous and may be
# called from a thread-pool worker in FastAPI's sync-to-async bridge.
_users_file_lock: threading.Lock = threading.Lock()

# 修復 0（2026-07-11 空密碼事故）：伺服器端密碼策略 SSOT。與前端表單／自助改密碼
# 端點的 pydantic min_length=8 同值；服務層為最終防線，涵蓋所有寫入呼叫者
# （create_user／admin_update_user／change_own_password），前端驗證只是 UX。
MIN_PASSWORD_LENGTH = 8


def validate_new_password(password: str) -> None:
    """驗證「新密碼」符合策略（非空字串且 ≥ MIN_PASSWORD_LENGTH）；不符即拋。

    只用於**設定新密碼**的路徑——驗證既有密碼（verify_password）不在此列，
    否則歷史短密碼帳號會連登入都不行。
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            ),
        )


def access_token_is_stale(payload: dict[str, Any], user: User) -> bool:
    """修復 1：判定 access token 是否簽發於該使用者最近一次密碼變更之前（須拒絕）.

    改密碼的意圖就是「踢掉所有既有 session」——只清 refresh 會留下最長 1 小時的
    舊 access 存活窗（07-11 事故的「一小時後才鎖死」；被竊 token 同理）。判準：
    ``token.iat < user.password_changed_at``。無 ``iat`` 的舊制 token 在時間戳已
    設定時一律視為過期（fail-closed）；同秒邊界（iat == changed_at）放行，讓改完
    密碼立即重新登入不受阻。兩道 Bearer 閘（routers/auth.get_current_user 與
    identity_service.verify_token 人類分支）共用本判準（SSOT）。
    """
    # P3（認證縫）：撤銷水位＝max(改密碼戳, 登出/撤銷戳)。登出（tokens_revoked_at）與
    # 改密碼（password_changed_at）共用同一「此刻之前的 access 全失效」判準（SSOT）。
    watermark = max(user.password_changed_at or 0, user.tokens_revoked_at or 0)
    if watermark <= 0:
        return False
    iat = payload.get("iat")
    if not isinstance(iat, int | float):
        return True
    return iat < watermark


class AuthService:
    """Authentication and User Management Service.
    
    Handles user CRUD against the users.json SSOT, password hashing, and token generation.
    Enforces Error SSOT and Time SSOT mandates.
    Uses bcrypt with SHA256 pre-hashing to bypass the 72-byte password limit.
    """
    
    def __init__(self, users_file_path: str | None = None) -> None:
        """初始化認證服務.

        Args:
            users_file_path (str | None, optional): 使用者資料庫 (users.json) 的實體路徑。預設會從全域設定的 CONFIG_DIR 載入。
        """
        if users_file_path:
            self.users_file_path = Path(users_file_path)
        else:
            self.users_file_path = Path(CONFIG_DIR) / "users.json"
            
    def _read_users(self) -> list[User]:
        """Read and deserialize users from users.json.

        Distinguishes two cases:
        - File does not exist: fresh install, legitimately empty → return [].
        - File exists but unreadable/corrupt: raise ToukaException(PERSISTENCE_ERROR)
          so _atomic_users_write never proceeds to _write_users and silently
          overwrites with {"users": []}.
        """
        if not self.users_file_path.exists():
            return []
        try:
            content = self.users_file_path.read_bytes().decode("utf-8")
            data = json.loads(content)
            return [User(**u) for u in data.get("users", [])]
        except Exception:
            logger.exception("Failed to read users.json — file may be corrupted")
            raise ToukaException(ErrorCode.PERSISTENCE_ERROR) from None

    def _write_users(self, users: list[User]) -> None:
        """Persist users list to users.json using atomic temp-file rename.

        Write flow: serialize → write to .tmp (same directory = same drive) →
        Path.replace() performs an atomic MoveFileExW rename on Windows.
        If write_bytes() raises mid-way, the target file is untouched.

        N4 fix: must only be called while _users_file_lock is held to prevent
        TOCTOU race conditions in concurrent read-modify-write operations.
        Callers MUST use _atomic_users_write() rather than calling this directly
        when they need atomicity.
        """
        from src.services.resource_manager import resource_manager  # noqa: LOCAL_IMPORT_BAN
        resource_manager.make_dir(self.users_file_path.parent)
        # P3（認證縫）：寫前逐筆重驗——把「毒化落盤→下次 _read_users 全站 500」翻轉成
        # 「毒化當下該請求 fail-loud」。07-11 事故形態＝admin 編輯面把關鍵欄位（role 等）
        # 寫成無效值 → users.json 從此讀不起來、登入/refresh/認證全掛需手工修檔。此處以
        # 與讀取同一份 User schema 重驗（既有合法資料恆通過，僅新毒化被擋）。
        dumped: list[dict[str, Any]] = []
        for u in users:
            u_dump = u.model_dump()
            try:
                User.model_validate(u_dump)
            except Exception as e:
                raise ToukaException(
                    ErrorCode.VALIDATION_ERROR,
                    detail_override="Refusing to persist an invalid user record.",
                ) from e
            dumped.append(u_dump)
        data = {"users": dumped}
        json_data = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        tmp_file = self.users_file_path.parent / f"{self.users_file_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            resource_manager.write_file_binary(tmp_file, json_data)
            
            # Windows 平台併發取代重試防禦
            import time
            retries = 5
            for attempt in range(retries):
                try:
                    resource_manager.move_file(tmp_file, self.users_file_path)
                    break
                except PermissionError as e:
                    if attempt == retries - 1:
                        raise e
                    time.sleep(0.01 * (2 ** attempt))  # 10ms, 20ms, 40ms, 80ms
        finally:
            if resource_manager.exists(tmp_file):
                try:
                    resource_manager.remove_file(tmp_file)
                except OSError:
                    pass
    def _atomic_users_write(self, mutate_fn: Any) -> Any:
        """Atomically read-modify-write users.json under _users_file_lock.

        N4 fix (SOLID-SRP): Encapsulates the locking + read + mutate + write
        pattern so callers never accidentally bypass the serialization guard.

        Args:
            mutate_fn: Callable[[list[User]], Any] — receives the current user
                list, mutates it in place (or replaces it), and returns any
                extra value the caller needs.  The list passed in must be the
                same object that ends up written back.

        Returns:
            Whatever mutate_fn returns.
        """
        with _users_file_lock:
            users = self._read_users()
            result = mutate_fn(users)
            self._write_users(users)
            return result

    def hash_password(self, password: str) -> str:
        """Hash a password using SHA256 pre-hashing + bcrypt."""
        # Step 1: Pre-hash with SHA256 to handle > 72 bytes
        pre_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        # Step 2: Bcrypt
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(pre_hash.encode("utf-8"), salt)
        return hashed.decode("utf-8")

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a password using SHA256 pre-hashing + bcrypt."""
        try:
            pre_hash = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
            return bcrypt.checkpw(pre_hash.encode("utf-8"), hashed_password.encode("utf-8"))
        except Exception:
            logger.exception("❌ Password verification failed")
            return False

    def get_all_users(self) -> list[User]:
        """Get all registered users."""
        return self._read_users()

    def any_claimed_user(self) -> bool:
        """系統是否已被人類「認領」：任一使用者完成真密碼設定（非暫時密碼）.

        修復 3（LDH 首裝限定）的判準 SSOT：bootstrap 產生的暫時密碼 admin 不算
        認領（首裝精靈仍可在本機無卡環境運作）；任何 create_user 註冊或改完密碼
        的帳號都算。一旦認領，identity_router 的 LDH 永久關閉（fail-loud 401）。
        """
        return any(not u.is_temp_password for u in self._read_users())

    def get_user_by_email(self, email: str) -> User | None:
        """Fetch user by their email address."""
        users = self._read_users()
        for u in users:
            if u.email == email:
                return u
        return None

    def get_user_by_id(self, user_id: str) -> User | None:
        """Fetch user by their unique UUID (sub)."""
        users = self._read_users()
        for u in users:
            if u.id == user_id:
                return u
        return None

    def ensure_admin_user(self, existing_agent_ids: list[str]) -> None:
        """Bootstrap the admin user and migrate ownership if not already present.

        N4 fix: the check-and-create is now atomic under _users_file_lock.
        """
        def _mutate(users: list[User]) -> None:
            admin = next((u for u in users if u.role == "admin"), None)
            if not admin:
                initial_password = secrets.token_urlsafe(16)
                # DA21: write the one-time password to a file instead of logging it in
                # plaintext. The persistent log rotates/aggregates and would otherwise
                # retain a valid admin credential forever (even after it is changed).
                pw_file = get_initial_admin_password_file()
                try:
                    resource_manager.write_file_text(
                        pw_file,
                        f"email: admin@touka.io\npassword: {initial_password}\n",
                    )
                except OSError:
                    raise ToukaException(ErrorCode.PERSISTENCE_ERROR) from None
                logger.warning(
                    "🛡️ No admin user found. Created default admin 'admin@touka.io'.\n"
                    "🔑 Initial password saved to: %s\n"
                    "⚠️ Log in, change the password immediately, then delete that file.",
                    pw_file,
                )
                users.append(User(
                    id=str(uuid.uuid4()),
                    email="admin@touka.io",
                    role="admin",
                    hashed_password=self.hash_password(initial_password),
                    owned_agents=existing_agent_ids,
                    is_temp_password=True,
                ))
            elif not admin.is_temp_password:
                # DA21: admin already finished setup — clean up any stale bootstrap file.
                with contextlib.suppress(OSError):
                    resource_manager.remove_file(get_initial_admin_password_file())

        self._atomic_users_write(_mutate)
    def create_user(self, email: str, password: str, role: str = "normal") -> User:
        """Register a new user in the system.

        N4 fix: email uniqueness check and append are atomic under the lock.
        修復 0（空密碼事故）：伺服器端密碼策略前置——空/短密碼拒建。
        """
        validate_new_password(password)
        hashed = self.hash_password(password)

        def _mutate(users: list[User]) -> User:
            for u in users:
                if u.email == email:
                    msg = "Email already registered"
                    raise ToukaException(ErrorCode.VALIDATION_ERROR, message=msg)
            new_user = User(
                id=str(uuid.uuid4()),
                email=email,
                role=role,
                hashed_password=hashed,
                owned_agents=[],
            )
            users.append(new_user)
            return new_user

        return self._atomic_users_write(_mutate)

    def authenticate_user(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate user and return access and refresh tokens."""
        user = self.get_user_by_email(email)
        if not user or not self.verify_password(password, user.hashed_password):
            msg = "Invalid credentials or password"
            raise ToukaException(ErrorCode.AUTH_INVALID_CREDENTIALS, detail_override=msg)
        # DA23: a suspended account must not obtain new tokens. Checked AFTER the
        # password verify, so it cannot be used to enumerate which emails exist.
        if not user.is_active:
            raise ToukaException(
                ErrorCode.AUTH_PERMISSION_DENIED, detail_override="Account is suspended."
            )

        # N4 fix: update token expiry atomically under the lock
        now_sec = int(get_current_epoch())
        refresh_exp = now_sec + (30 * 24 * 3600)
        jti = str(uuid.uuid4())  # P13 fix: unique token ID for replay detection
        # P3（認證縫 #7）：verify 在鎖外（bcrypt CPU-bound），若合法使用者於 verify 之後、
        # 本 _mutate 之前改了密碼，舊密碼登入者不得重新武裝 refresh。鎖內比對 hashed_password
        # 快照未變＝這次登入用的密碼仍是當前密碼；不符即這次登入作廢（不寫 refresh 欄位）。
        pw_snapshot = user.hashed_password
        rearmed: list[bool] = []

        def _mutate(users: list[User]) -> None:
            for i, u in enumerate(users):
                if u.id == user.id:
                    if u.hashed_password != pw_snapshot:
                        return  # 密碼於驗證期間被改 → 這次登入作廢
                    users[i].refresh_token_exp = refresh_exp
                    users[i].refresh_token_jti = jti
                    users[i].previous_refresh_jti = None
                    users[i].refresh_jti_rotated_at = None
                    users[i].last_active_at = now_sec
                    rearmed.append(True)
                    break

        self._atomic_users_write(_mutate)
        if not rearmed:
            raise ToukaException(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                detail_override="Invalid credentials or password",
            )

        # 修復 1：iat＝access／refresh 撤銷判準（access_token_is_stale／水位閘）的載體。
        access_payload = {"sub": user.id, "type": "access", "iat": now_sec, "exp": now_sec + 3600}
        refresh_payload = {
            "sub": user.id, "type": "refresh", "iat": now_sec, "exp": refresh_exp, "jti": jti,
        }

        access_token = jwt.encode(
            access_payload, AuthSecretManager.get_secret(), algorithm="HS256"
        )
        refresh_token = jwt.encode(
            refresh_payload, AuthSecretManager.get_secret(), algorithm="HS256"
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer"
        }

    def refresh_access_token(self, refresh_token: str) -> dict[str, str]:
        """Issue new access AND refresh tokens (H6 fix: token rotation).

        Issuing a new refresh token on every use ensures that a stolen token
        becomes invalid after the legitimate owner uses it once, limiting the
        exposure window to a single rotation cycle.

        Returns:
            dict with 'access_token' and 'refresh_token' keys.
        """
        try:
            # verify_iat=False：iat 用於本函式的水位閘（業務時鐘 get_current_epoch），
            # 不走 jwt 的 UTC iat「未成熟」驗證——避免與業務時鐘偏移衝突（且時鐘微幅
            # 超前不該把合法 refresh 判為 immature）。簽章/exp 仍驗。
            payload = jwt.decode(
                refresh_token, AuthSecretManager.get_secret(), algorithms=["HS256"],
                options={"verify_iat": False},
            )
            if payload.get("type") != "refresh":
                msg = "Invalid credentials or password"
                raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN, detail_override=msg)
            user_id = payload.get("sub")
            presented_jti = payload.get("jti")
            presented_iat = payload.get("iat")
        except jwt.ExpiredSignatureError as e:
            msg = "Invalid credentials or password"
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN, detail_override=msg) from e
        except jwt.InvalidTokenError as e:
            msg = "Invalid credentials or password"
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN, detail_override=msg) from e

        # N4 + H6 + P13: verify token expiry, jti, AND rotate atomically
        now_sec = int(get_current_epoch())
        new_refresh_exp = now_sec + (30 * 24 * 3600)
        new_jti = str(uuid.uuid4())
        user_snapshot: list[User] = []
        reuse_detected: list[bool] = []

        def _mutate_refresh(users: list[User]) -> None:
            target = next((u for u in users if u.id == user_id), None)
            if not target or not target.refresh_token_exp:
                return
            if now_sec > target.refresh_token_exp:
                return
            if not target.is_active:  # DA23: suspended users cannot refresh tokens
                return
            # P3（認證縫 #7）：refresh 水位閘——簽發於改密碼／登出（max 水位）之前的
            # refresh token 一律拒輪替（否則改密碼前已持有的 refresh token 可持續換新卡、
            # 繞過踢人）。無 iat 的舊制 refresh token 在水位已設時 fail-closed。同秒放行
            # （iat == watermark）＝比照 access_token_is_stale，利改密碼後立即重登。
            watermark = max(target.password_changed_at or 0, target.tokens_revoked_at or 0)
            if watermark > 0 and (
                not isinstance(presented_iat, int | float) or presented_iat < watermark
            ):
                return

            stored_jti = target.refresh_token_jti

            # Case 1: Normal rotation — presented JTI matches current stored JTI
            # DA26: constant-time JTI comparison (gated behind the verified JWT signature).
            if presented_jti and stored_jti and secrets.compare_digest(presented_jti, stored_jti):
                for i, u in enumerate(users):
                    if u.id == user_id:
                        users[i].previous_refresh_jti = presented_jti
                        users[i].refresh_jti_rotated_at = now_sec
                        users[i].refresh_token_jti = new_jti
                        users[i].refresh_token_exp = new_refresh_exp
                        users[i].last_active_at = now_sec
                        user_snapshot.append(users[i])
                        break
                return

            # Case 2: Grace period — concurrent request with the same old JTI
            # previous_refresh_jti is preserved (not updated) so N concurrent tabs can all pass
            # refresh_jti_rotated_at is also preserved so the window does not extend
            prev_jti = target.previous_refresh_jti
            rotated_at = target.refresh_jti_rotated_at
            if (
                presented_jti
                and prev_jti
                and secrets.compare_digest(presented_jti, prev_jti)
                and rotated_at is not None
                and now_sec - rotated_at <= AUTH_REFRESH_GRACE_PERIOD_SEC
            ):
                for i, u in enumerate(users):
                    if u.id == user_id:
                        users[i].refresh_token_jti = new_jti
                        users[i].refresh_token_exp = new_refresh_exp
                        users[i].last_active_at = now_sec
                        user_snapshot.append(users[i])
                        break
                return

            # Case 3: JTI mismatch outside grace — genuine replay attack, revoke all
            if presented_jti and stored_jti and not secrets.compare_digest(presented_jti, stored_jti):
                reuse_detected.append(True)
                for i, u in enumerate(users):
                    if u.id == user_id:
                        users[i].refresh_token_exp = None
                        users[i].refresh_token_jti = None
                        users[i].previous_refresh_jti = None
                        users[i].refresh_jti_rotated_at = None
                        break

        self._atomic_users_write(_mutate_refresh)

        if reuse_detected:
            raise ToukaException(ErrorCode.AUTH_REFRESH_TOKEN_REUSE)

        if not user_snapshot:
            msg = "Invalid credentials or password"
            raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN, detail_override=msg)

        confirmed_user = user_snapshot[0]
        secret = AuthSecretManager.get_secret()
        # 修復 1：iat＝access token 撤銷判準（access_token_is_stale）的載體。
        access_payload = {
            "sub": confirmed_user.id, "type": "access", "iat": now_sec, "exp": now_sec + 3600,
        }
        refresh_payload = {
            "sub": confirmed_user.id, "type": "refresh",
            "iat": now_sec, "exp": new_refresh_exp, "jti": new_jti,
        }

        return {
            "access_token": jwt.encode(access_payload, secret, algorithm="HS256"),
            "refresh_token": jwt.encode(refresh_payload, secret, algorithm="HS256"),
        }

    def revoke_user_tokens(self, user_id: str) -> None:
        """Invalidate user session by clearing refresh token expiration.

        N4 fix: atomic under _users_file_lock.
        """
        found: list[bool] = []
        now_sec = int(get_current_epoch())

        def _mutate(users: list[User]) -> None:
            for i, u in enumerate(users):
                if u.id == user_id:
                    users[i].refresh_token_exp = None
                    users[i].refresh_token_jti = None
                    users[i].previous_refresh_jti = None
                    users[i].refresh_jti_rotated_at = None
                    # P3（認證縫）：設 access 撤銷水位——登出即撤既有 access token
                    # （不再殘活至 1h）。對齊 ADR-0144 的「改密碼即撤」語意。
                    users[i].tokens_revoked_at = now_sec
                    found.append(True)
                    return

        self._atomic_users_write(_mutate)
        if found:
            logger.info(f"🛡️ Tokens revoked for user {user_id}")
        else:
            logger.warning(f"⚠️ Attempted to revoke tokens for non-existent user {user_id}")
