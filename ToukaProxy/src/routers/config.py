"""系統設定路由器，提供對環境變數檔案的讀寫接口。"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.config import apply_runtime_env, resolve_env_file_path
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.authorization_gateway import require_admin_authority
from src.services.model_manager import model_manager
from src.services.providers import registry
from src.utils.secrets import MASK as _MASK  # 遮罩 SSOT（向後相容別名）
from src.utils.secrets import mask_value as _mask_value

router = APIRouter(prefix="/config", tags=["config"])
logger = logging.getLogger(__name__)


# DA1: reading/writing .env (incl. TOUKA_AUTH_SECRET) requires admin.
_require_config_role = require_admin_authority


def _env_file_paths() -> tuple[Path, Path]:
    """回傳 (live .env, .env.example) 路徑（SSOT：一律經 ``resolve_env_file_path``）。

    root-cause-A：舊版硬編碼 cwd 相對 ``".env"`` → docker 下寫入容器暫存層
    ``/app/ToukaProxy/.env``（非 compose ``env_file`` 讀的宿主 .env、重建即失）。改走與
    ``Settings`` / ``SetupService`` 相同的解析器，讀寫同源、且可經 ``TOUKA_ENV_FILE`` 指向
    持久化路徑。範本為 live .env 的同目錄 sibling。
    """
    env = resolve_env_file_path()
    return env, env.with_name(".env.example")


class EnvUpdate(BaseModel):
    """環境變數更新請求模型。"""

    settings: dict[str, str]

@router.get("/env")
async def get_env_settings(_identity: AgentIdentity = Depends(_require_config_role)):
    """讀取 .env.example 作為欄位定義，並從 .env 獲取目前值"""
    env_path, example_path = _env_file_paths()
    definitions = []
    current_values = {}

    # 讀取現有 .env
    if env_path.exists():
        with env_path.open(encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    key, val = line.strip().split("=", 1)
                    current_values[key] = val

    # 讀取 .env.example
    if not example_path.exists():
        return {"error": "Missing .env.example template"}

    with example_path.open(encoding="utf-8") as f:
        group = "General"
        for line in f:
            line = line.strip()
            if line.startswith("# ---"):
                group = line.replace("#", "").replace("-", "").strip()
            elif "=" in line and not line.startswith("#"):
                key = line.split("=", 1)[0]
                definitions.append({
                    "key": key,
                    "value": _mask_value(key, current_values.get(key, "")),
                    "group": group
                })
                
    return {"definitions": definitions}

@router.post("/env")
async def update_env(update: EnvUpdate, _identity: AgentIdentity = Depends(_require_config_role)):
    """更新 .env 檔案內容。注意：這不會立即重啟服務。"""
    env_path, _ = _env_file_paths()
    try:
        lines = []
        if env_path.exists():
            with env_path.open(encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        updated_keys = set()
        
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0]
                if key in update.settings:
                    # 縱深防禦：遮罩值（GET 回傳的佔位）被原樣送回時，保留既有真值不覆寫。
                    if update.settings[key] == _MASK:
                        new_lines.append(line)
                        updated_keys.add(key)
                        continue
                    new_lines.append(f"{key}={update.settings[key]}\n")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        # 加入原本 .env 裡面沒有的 key（遮罩值不落盤）
        for key, val in update.settings.items():
            if key not in updated_keys and val != _MASK:
                new_lines.append(f"{key}={val}\n")
                
        with env_path.open("w", encoding="utf-8") as f:
            f.writelines(new_lines)

        logger.info(
            f"⚙️ .env file updated via UI ({env_path}). {len(update.settings)} keys modified."
        )
    except Exception as e:
        # E#21：原始例外（含檔案系統路徑等內部細節）只進 server log（exception 附完整 traceback），
        # 不回傳給呼叫端。
        logger.exception("Failed to update .env")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from e

    # .env 寫入成功 → provider 金鑰即時生效＋探測（免重啟）；非 provider 設定仍需重啟。
    # best-effort：即時套用/探測失敗不影響「已存檔」結果。值為空＝清除（探測回 "cleared"，
    # 其模型由 get_models 的 health 邏輯自動標離線——反向生命週期）。
    provider_keys = {
        k: v
        for k, v in update.settings.items()
        if v != _MASK and k in registry.known_provider_key_names()
    }
    if not provider_keys:
        return {
            "status": "success",
            "message": "Settings saved. Please restart the container manually.",
        }
    try:
        apply_runtime_env(provider_keys)
        key_probe = await model_manager.probe_provider_keys(set(provider_keys))
    except Exception:
        logger.exception("Provider key live-apply/probe failed")
        return {
            "status": "success",
            "message": "金鑰已儲存，但即時套用/探測失敗；重啟後仍會生效。",
        }
    return {
        "status": "success",
        "message": "金鑰已即時生效（免重啟）；其餘設定變更仍需重啟容器。",
        "key_probe": key_probe,
    }
