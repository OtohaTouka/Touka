"""系統設定路由器，提供對環境變數檔案的讀寫接口。"""

import io
import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from memory_server.utils.memory_worker import run_memory_job
from pydantic import BaseModel

from src.config import ENV_FILE_LOCK, apply_runtime_env, resolve_env_file_path
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services import embedding_service
from src.services.authorization_gateway import require_admin_authority
from src.services.hooks.lifecycle import notify_config_change
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

def _read_env_text(path: Path) -> str:
    """設定檔目前的內容；還沒有設定檔回空字串。"""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _merge_env_text(text: str, settings: dict[str, str]) -> str:
    """把 ``settings`` 寫進設定檔內容：已有的鍵就地改值，沒有的接在最後（遮罩值不落盤）。"""
    new_lines = []
    updated_keys = set()

    for line in io.StringIO(text).readlines():
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0]
            if key in settings:
                # 縱深防禦：遮罩值（GET 回傳的佔位）被原樣送回時，保留既有真值不覆寫。
                if settings[key] == _MASK:
                    new_lines.append(line)
                    updated_keys.add(key)
                    continue
                new_lines.append(f"{key}={settings[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # 加入原本 .env 裡面沒有的 key（遮罩值不落盤）
    for key, val in settings.items():
        if key not in updated_keys and val != _MASK:
            new_lines.append(f"{key}={val}\n")
    return "".join(new_lines)


def _unchecked_model_change(text: str, settings: dict[str, str], checked: str | None) -> bool:
    """照現在的設定檔寫入會換模型，卻不是先前評估過的那個換法。"""
    change = embedding_service.env_model_change(text, _merge_env_text(text, settings))
    if change is None:
        return False
    return checked is None or not embedding_service.same_model(change, checked)


@router.post("/env")
async def update_env(update: EnvUpdate, _identity: AgentIdentity = Depends(_require_config_role)):
    """更新 .env 檔案內容。注意：這不會立即重啟服務。

    寫入會換掉設定檔裡的嵌入模型時，先在記憶工作者上用和精靈一樣的規則評估（ADR-0315）：要確認
    的整筆拒絕、什麼都不寫。評估在拿鎖之前（持鎖不能等，ADR-0288）；鎖內重讀，出現沒評估過的
    換法也拒絕。沒改到模型的寫入（例如只存金鑰）不看記憶。
    """
    env_path, _ = _env_file_paths()
    before = _read_env_text(env_path)
    checked = embedding_service.env_model_change(before, _merge_env_text(before, update.settings))
    if checked is not None and await run_memory_job(
        embedding_service.model_change_requires_confirmation, checked
    ):
        raise ToukaException(ErrorCode.EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION)
    refused = False
    try:
        # 讀改寫整段持共用鎖：切換嵌入模型在記憶工作者上同時寫 .env（ADR-0288）
        with ENV_FILE_LOCK:
            current = _read_env_text(env_path)
            refused = _unchecked_model_change(current, update.settings, checked)
            if not refused:
                with env_path.open("w", encoding="utf-8") as f:
                    f.write(_merge_env_text(current, update.settings))
    except Exception as e:
        # E#21：原始例外（含檔案系統路徑等內部細節）只進 server log（exception 附完整 traceback），
        # 不回傳給呼叫端。
        logger.exception("Failed to update .env")
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from e
    if refused:
        raise ToukaException(ErrorCode.EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION)
    logger.info(f"⚙️ .env file updated via UI ({env_path}). {len(update.settings)} keys modified.")

    notify_config_change("env")
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
