"""ToukaHeartbeat 配置模型 (Env SSOT)."""

import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

HEARTBEAT_ROOT = Path(__file__).parent.resolve()
WORKSPACE_ROOT = HEARTBEAT_ROOT.parent.resolve()


def resolve_heartbeat_env_file_path() -> Path:
    """根據執行模式動態解析 .env 檔案路徑 (SSOT)."""
    explicit = os.environ.get("TOUKA_ENV_FILE")
    if explicit:
        return Path(explicit)

    cwd = Path.cwd().resolve()
    is_heartbeat_dir = (cwd == HEARTBEAT_ROOT) or (HEARTBEAT_ROOT in cwd.parents)

    is_integrated = (not is_heartbeat_dir and (WORKSPACE_ROOT / ".env").exists()) or (
        os.environ.get("TOUKA_INTEGRATED") == "true"
    )

    if is_integrated:
        return WORKSPACE_ROOT / ".env"
    return HEARTBEAT_ROOT / ".env"


class HeartbeatSettings(BaseSettings):
    """ToukaHeartbeat 配置模型 (Env SSOT)."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化心跳設定物件，注入環境變數檔案路徑並讀取配置值。"""
        if "_env_file" not in kwargs:
            kwargs["_env_file"] = str(resolve_heartbeat_env_file_path())
        super().__init__(*args, **kwargs)

    API_BASE_URL: str = "http://touka-proxy:8000"
    # A nonempty shared setting is required for outbound Proxy work. A local
    # config.json fallback authorizes management only. Apply changes by restarting
    # or recreating both services after the shared setting has been persisted.
    HEARTBEAT_TOKEN: str = ""
    TRIGGER_PORT: int = 8082

    # 允許從環境變數覆寫
    @property
    def awaken_url(self) -> str:
        """獲取心跳喚醒 URL (SSOT)."""
        return f"{self.API_BASE_URL}/v1/heartbeat/awaken"


settings = HeartbeatSettings()
