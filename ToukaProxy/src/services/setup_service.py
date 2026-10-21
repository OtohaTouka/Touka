"""Setup Service — First-Run Setup Wizard backend logic.

Reads system configuration state and writes values to .env via python-dotenv.
Follows SOLID SRP: this service owns only setup-status detection and .env mutation.
"""

import os
from pathlib import Path

from dotenv import set_key
from pydantic import BaseModel

from src.config import CONFIG_DIR, is_public_secret, resolve_env_file_path
from src.core.exceptions import ToukaException
from src.services.auth_service import AuthService
from src.services.providers import registry


def _is_running_in_docker() -> bool:
    """偵測是否於容器內執行（`/.dockerenv` 為 Docker 標準標記）。

    容器內時，使用者輸入的宿主路徑對容器檔案系統不可見，故不可用 `is_dir()` 驗證；
    真正的存在性由 `docker compose up` 依 `.env` 重新 bind-mount 時把關（bug②）。
    """
    return Path("/.dockerenv").exists()


def _validate_host_dir(value: str, field: str) -> None:
    """驗證「將寫入 .env、供 compose 掛載」的宿主目錄路徑（SSOT）。

    一律拒絕空字串；原生模式（非容器）額外檢查其為既有目錄；容器模式跳過存在性檢查
    （宿主路徑對容器不可見，改由掛載時把關）。
    """
    if not value.strip():
        msg = f"{field} must not be empty or whitespace"
        raise ValueError(msg)  # noqa: TRY003
    if not _is_running_in_docker() and not Path(value).is_dir():
        msg = f"{field} must be an existing directory"
        raise ValueError(msg)  # noqa: TRY003


def _reject_public_secret(key: str, value: str, field: str) -> None:
    """歷來公開過的金鑰值不准經精靈寫進 .env（禁令 15；對照表 SSOT 在 config）。

    訊息只寫欄位名、不寫值（會經 422 回給前端）。
    """
    if is_public_secret(key, value):
        msg = f"{field} must not be a publicly known value; use a newly generated one"
        raise ValueError(msg)


class SetupStatus(BaseModel):
    """Response model for GET /v1/admin/setup/status."""

    dispatch_token_is_default: bool
    auth_secret_persisted: bool
    api_keys_configured: list[str]
    is_admin_temp_password: bool
    data_path_is_default: bool
    workspace_root_is_default: bool
    embedding_model: str


class SetupService:
    """Reads setup state and writes configuration values to .env."""

    def __init__(
        self,
        users_file_path: str | None = None,
        env_file_path: str | None = None,
    ) -> None:
        """初始化設定服務，指定使用者檔案路徑與環境變數檔案路徑。"""
        self._users_file_path = users_file_path
        self._env_file_path = Path(env_file_path) if env_file_path else resolve_env_file_path()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> SetupStatus:
        """Return current setup status by inspecting environment and users.json."""
        return SetupStatus(
            dispatch_token_is_default=self._is_dispatch_token_default(),
            auth_secret_persisted=self._is_auth_secret_persisted(),
            api_keys_configured=self._get_configured_api_keys(),
            is_admin_temp_password=self._is_admin_temp_password(),
            data_path_is_default=self._is_data_path_default(),
            workspace_root_is_default=self._is_workspace_root_default(),
            embedding_model=self._get_embedding_model(),
        )

    def _is_dispatch_token_default(self) -> bool:
        token = os.environ.get("DISPATCH_TOKEN", "").strip()  # noqa: ENV_SSOT_VIOLATION
        return not token or is_public_secret("DISPATCH_TOKEN", token)

    def _is_auth_secret_persisted(self) -> bool:
        return bool(os.environ.get("TOUKA_AUTH_SECRET", "").strip())  # noqa: ENV_SSOT_VIOLATION

    def _get_configured_api_keys(self) -> list[str]:
        # provider 清單一律由 registry SSOT 衍生（杜絕硬編碼漂移——先前漏 deepseek 即一例，
        # ADR-0099 建立 registry 正為消除此類 bug）；值仍讀 live os.environ 以反映真實生效狀態。
        # 每個可發現 provider 的任一 key_attr 有值即視為已設定。
        configured = [
            spec.id
            for spec in registry.discoverable_specs()
            if any(os.environ.get(attr, "").strip() for attr in spec.key_attrs)  # noqa: ENV_SSOT_VIOLATION
        ]
        # Tavily 為搜尋金鑰（非模型 provider，無 ProviderSpec），但精靈/設定頁仍顯示，另行納入。
        if os.environ.get("TAVILY_API_KEY", "").strip():  # noqa: ENV_SSOT_VIOLATION
            configured.append("tavily")
        return configured

    def _is_admin_temp_password(self) -> bool:
        users_file = self._users_file_path or str(Path(CONFIG_DIR) / "users.json")
        auth = AuthService(users_file_path=users_file)
        try:
            users = auth.get_all_users()
        except ToukaException:
            return True
        admin = next((u for u in users if u.role == "admin"), None)
        if admin is None:
            return True
        return admin.is_temp_password

    def _is_data_path_default(self) -> bool:
        return not os.environ.get("SANCTUARY_DATA_PATH", "").strip()  # noqa: ENV_SSOT_VIOLATION

    def _is_workspace_root_default(self) -> bool:
        return not os.environ.get("WORKSPACE_ROOT", "").strip()  # noqa: ENV_SSOT_VIOLATION

    def _get_embedding_model(self) -> str:
        raw = os.environ.get("EMBEDDING_MODEL", "").strip()  # noqa: ENV_SSOT_VIOLATION
        return raw or "all-MiniLM-L6-v2"

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(
        self,
        dispatch_token: str | None = None,
        auth_secret: str | None = None,
        data_path: str | None = None,
        workspace_root: str | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Write provided values to the .env file.

        Validates all fields before writing any to prevent partial writes.
        ``data_path`` (SANCTUARY_DATA_PATH) 與 ``workspace_root`` (WORKSPACE_ROOT) 皆為
        供 docker compose 掛載的宿主路徑：原生模式驗證其為既有目錄，容器模式跳過存在性
        檢查（宿主路徑對容器不可見，由掛載時把關）。Raises ValueError for blank values, a
        publicly known DISPATCH_TOKEN／TOUKA_AUTH_SECRET value or, in native mode, a path
        that is not an existing directory.
        """
        writes: list[tuple[str, str]] = []

        if dispatch_token is not None:
            if not dispatch_token.strip():
                msg = "dispatch_token must not be empty or whitespace"
                raise ValueError(msg)  # noqa: TRY003
            _reject_public_secret("DISPATCH_TOKEN", dispatch_token, "dispatch_token")
            writes.append(("DISPATCH_TOKEN", dispatch_token))

        if auth_secret is not None:
            if not auth_secret.strip():
                msg = "auth_secret must not be empty or whitespace"
                raise ValueError(msg)  # noqa: TRY003
            _reject_public_secret("TOUKA_AUTH_SECRET", auth_secret, "auth_secret")
            writes.append(("TOUKA_AUTH_SECRET", auth_secret))

        if data_path is not None:
            _validate_host_dir(data_path, "data_path")
            writes.append(("SANCTUARY_DATA_PATH", data_path))

        if workspace_root is not None:
            _validate_host_dir(workspace_root, "workspace_root")
            writes.append(("WORKSPACE_ROOT", workspace_root))

        if embedding_model is not None:
            if not embedding_model.strip():
                msg = "embedding_model must not be empty or whitespace"
                raise ValueError(msg)  # noqa: TRY003
            writes.append(("EMBEDDING_MODEL", embedding_model))

        for env_var, value in writes:
            set_key(str(self._env_file_path), env_var, value, quote_mode="never")
