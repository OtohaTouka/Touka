"""Model Management Router.

用於查詢、驗證和管理 AI 模型。
"""

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.config import ANTHROPIC_API_BASE_URL, get_config_dir, get_full_env, resolve_env_file_path
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.routers import custom_models
from src.services.agent_service import agent_service
from src.services.authorization_gateway import require_privileged
from src.services.models import model_manager as _model_manager
from src.services.providers import registry
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

router = APIRouter(prefix="/models", tags=["models"])

# Require Owner/Debugger/admin to refresh, modify, or delete models.
_require_models_mgmt = require_privileged
logger = logging.getLogger(__name__)


# ── Pydantic Schemas ──────────────────────────────────────────────────────────


class ModelInfo(BaseModel):
    """模型資訊模型。"""

    id: str
    name: str | None = None
    owned_by: str = "unknown"
    object: str = "model"
    created: int | None = None
    description: str | None = None
    # 新增持久化與狀態欄位
    enabled: bool | None = True
    visibility: str | None = "visible"
    status: str | None = "online" # "online" | "offline"
    is_agent: bool | None = False
    # ADR-0111：原生模型 per-model 預設取樣參數（None＝未設定→回退 schema/請求預設）。
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    # ADR-0111 Phase 2：取樣參數能力（自 Provider Registry SSOT 衍生，供 ModelsTab
    # provider-aware UI——非原生模型不可編、不支援 top_k 的供應商隱藏該滑桿、Anthropic 夾溫度）。
    params_editable: bool = False
    supports_top_k: bool = False
    temperature_max: float = 2.0


class ModelsListResponse(BaseModel):
    """模型列表響應。"""

    object: str = "list"
    data: list[ModelInfo]


class ModelHealthRequest(BaseModel):
    """用於驗證模型可用性的請求"""

    test_prompt: str | None = "test"
    timeout: int | None = 10


class ModelHealthResponse(BaseModel):
    """模型健康狀態響應。"""

    model_id: str  # noqa: IDENTITY_SSOT
    status: str  # "healthy" | "degraded" | "unavailable"
    latency_ms: float | None = None
    error: str | None = None

class ModelSettingsUpdate(BaseModel):
    """模型設定更新。"""

    enabled: bool | None = None
    visibility: str | None = None
    # ADR-0111：原生模型取樣參數（顯式送 null＝清除回退預設；未送＝不變，由 exclude_unset 過濾）。
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=1, le=500)


# ── 輔助函數 ──────────────────────────────────────────────────────────────────


def get_models_config() -> dict[str, Any]:
    """讀取 ToukaProxy/config/models.json"""
    config_path = Path(get_config_dir()) / "models.json"
    data = resource_manager.read_json(config_path)
    return data if data else {"models": []}


def get_model_metadata(model_id: str) -> dict[str, Any]: # noqa: IDENTITY_SSOT
    """獲取特定模型的元數據 (SSOT: 支援 ID 糾偏)"""
    # 1. 透過 AgentService 獲取正規化身分 (SSOT)
    agent_info = agent_service.identify_agent(model_id)
    normalized_id = agent_info.get("agent_id", model_id) if agent_info else model_id

    # 2. 從配置文件中查找模型信息
    config = get_models_config()
    for model in config.get("models", []):
        curr_id = model.get("id")
        # 支援精確匹配或正規化後的匹配
        if curr_id in (model_id, normalized_id):
            return {**model, "id": normalized_id}  # 強制回傳正式 ID

    # 3. 如果未找到，嘗試推斷供應商
    owner_map = {
        "claude": "anthropic",
        "gpt": "openai",
        "gemini": "google",
        "toukasoul": "touka",
        "xai": "xai",
        "command": "cohere",
    }

    for prefix, owner in owner_map.items():
        if prefix in normalized_id.lower():
            return {
                "id": normalized_id,
                "owned_by": owner,
                "object": "model",
                "description": f"Model: {normalized_id}",
            }

    return {
        "id": normalized_id,
        "owned_by": "unknown",
        "object": "model",
        "description": f"Model: {normalized_id}",
    }


# ── API Endpoints ─────────────────────────────────────────────────────────────


@router.get("/search")
async def search_models(q: str) -> dict:
    """搜索模型（支援部分匹配）。

    Query param: q - 搜索關鍵字

    example: GET /models/search?q=claude
    """
    try:
        from src.services.model_manager import model_manager

        all_models = model_manager.get_models()
    except (ImportError, AttributeError):
        config = get_models_config()
        all_models = config.get("models", [])

    query_lower = q.lower()
    matching = [m for m in all_models if query_lower in m.get("id", "").lower()]

    return {
        "query": q,
        "count": len(matching),
        "models": [
            ModelInfo(**m)
            if all(k in m for k in ["id", "owned_by"])
            else ModelInfo(id=m.get("id"), owned_by=m.get("owned_by", "unknown"))
            for m in matching
        ],
    }


@router.get("", response_model=ModelsListResponse)
async def list_all_models() -> ModelsListResponse:
    """列出所有可用的 AI 模型。"""
    # 嘗試從 ModelManager 實例獲取最新的模型列表
    try:
        from src.services.model_manager import model_manager

        models_data = model_manager.get_models()
    except (ImportError, AttributeError):
        # 回退到文件讀取
        config = get_models_config()
        models_data = config.get("models", [])

    # 轉換為 ModelInfo 格式
    models = []
    for m in models_data:
        try:
            model_id = m.get("id")
            owned_by = m.get("owned_by", "unknown")
            # ADR-0111 Phase 2：取樣參數能力自 Provider Registry SSOT 衍生（不於前端硬編供應商
            # 知識——延續 ADR-0099 消除多處硬編碼的精神）。
            params_editable, supports_top_k, temperature_max = registry.model_param_caps(owned_by)
            model_info = ModelInfo(
                id=model_id,
                name=m.get("name", model_id),
                owned_by=owned_by,
                object=m.get("object", "model"),
                created=m.get("created"),
                enabled=m.get("enabled", True),
                visibility=m.get("visibility", "visible"),
                status=m.get("status", "online"),
                is_agent=m.get("is_agent", False),
                # ADR-0111：帶出持久化取樣參數，供 ModelsTab 顯示目前值（persist→read 往返）。
                temperature=m.get("temperature"),
                top_p=m.get("top_p"),
                top_k=m.get("top_k"),
                params_editable=params_editable,
                supports_top_k=supports_top_k,
                temperature_max=temperature_max,
            )
            models.append(model_info)
        except Exception as e:
            logger.warning(f"Failed to parse model {m}: {e}")

    return ModelsListResponse(data=models)


def _collect_env_api_key_names() -> tuple[set[str], str | None]:
    """收集目前環境中所有 *_API_KEY 鍵名（環境變數 ＋ .env 檔；**僅鍵名、不含值**）。

    回傳 (鍵名集合, 讀取錯誤)。.env 讀取失敗時回 (環境變數鍵名, 錯誤類別字串)——稽核結果
    可能不完整，由端點據此回報 env_read_error，不靜默忽略。
    """
    names = {k for k in get_full_env() if k.endswith("_API_KEY")}
    env_path = resolve_env_file_path()  # .env 路徑 SSOT（整合/獨立模式皆正確）
    try:
        if resource_manager.exists(env_path):  # IO 主權：經 ResourceManager 讀取
            for raw in resource_manager.read_file_text(env_path).splitlines():
                line = raw.strip()
                if "=" in line and not line.startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if key.endswith("_API_KEY"):
                        names.add(key)
    except Exception as e:
        logger.warning("env audit: 無法讀取 .env（稽核可能不完整）：%s", type(e).__name__)
        return names, type(e).__name__
    return names, None


@router.post("/refresh")
async def refresh_models(
    _identity: AgentIdentity = Depends(_require_models_mgmt),  # noqa: B008
) -> dict:
    """手動觸發所有動態模型的刷新。

    不靜默失敗：回應含 `errors`（逐供應商發現失敗的分類碼，已沙化）與 `env_audit`
    （各供應商金鑰是否設定、以及偵測到的未支援 `*_API_KEY` 鍵名）。
    """
    try:
        new_count = await _model_manager.refresh_all_dynamic_models()
    except Exception as e:
        logger.exception("Failed to refresh models")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e

    total = len(_model_manager.get_models())
    discovery_errors = _model_manager.get_discovery_errors()
    env_names, env_read_error = _collect_env_api_key_names()
    unknown_keys = registry.unknown_provider_key_names(env_names)
    if unknown_keys:
        logger.warning(
            "偵測到未支援的供應商金鑰（已設定但無對應供應商，將被忽略）：%s",
            ", ".join(unknown_keys),
        )
    return {
        "status": "partial" if discovery_errors else "success",
        "new_count": new_count,
        "total": total,
        "errors": discovery_errors,
        "env_audit": {
            "providers": registry.provider_key_status(),
            "unknown_keys": unknown_keys,
            "env_read_error": env_read_error,  # None＝.env 讀取正常；否則稽核可能不完整
        },
    }


@router.patch("/{model_id}/settings")
async def update_model_settings(
    model_id: str,  # noqa: IDENTITY_SSOT
    settings: ModelSettingsUpdate,
    _identity: AgentIdentity = Depends(_require_models_mgmt),  # noqa: B008
) -> dict:
    """更新模型的持久化設定（開啟/關閉、可見性）。"""
    try:
        identity = AgentIdentity(agent_id=model_id)
        success = _model_manager.patch_model_settings(
            identity, settings.model_dump(exclude_unset=True)
        )
        if not success:
            raise ToukaException(ErrorCode.MODEL_NOT_FOUND, model_id=model_id)  # noqa: TRY301
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update model settings")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return {"status": "success"}


@router.delete("/{model_id}")
async def delete_model(
    model_id: str,  # noqa: IDENTITY_SSOT
    _identity: AgentIdentity = Depends(_require_models_mgmt),  # noqa: B008
) -> dict:
    """從名冊中刪除特定的模型紀錄。"""
    # 自訂模型的資料夾走自訂模型的刪除（擁有權、R11，ADR-0293）
    identity = AgentIdentity(agent_id=model_id)
    removed_dir = False
    if custom_models.is_custom_model(identity):
        await custom_models.delete_custom_model(model_id, _identity)
        removed_dir = True
    try:
        success = _model_manager.delete_model(identity) or removed_dir
        if not success:
            raise ToukaException(ErrorCode.MODEL_NOT_FOUND, model_id=model_id)  # noqa: TRY301
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete model")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return {"status": "success"}


@router.get("/{model_id}", response_model=ModelInfo)
async def get_model(model_id: str) -> ModelInfo:  # noqa: IDENTITY_SSOT
    """獲取特定模型的詳細信息。

    path: model_id - 模型 ID

    example: GET /models/claude-3.5-sonnet
    """
    metadata = get_model_metadata(model_id)

    if not metadata:
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND, model_id=model_id)

    # ADR-0111 Phase 2：能力旗標自 registry SSOT 衍生，與 list_all_models 一致（避免單一模型
    # 端點回退 schema 預設 False/False/2.0 → 與列表矛盾的潛在 API 契約漂移）。
    params_editable, supports_top_k, temperature_max = registry.model_param_caps(
        metadata.get("owned_by", "unknown")
    )
    try:
        return ModelInfo(
            **metadata,
            params_editable=params_editable,
            supports_top_k=supports_top_k,
            temperature_max=temperature_max,
        )
    except Exception as e:
        logger.exception("Error creating ModelInfo for %s", model_id)
        raise ToukaException(ErrorCode.MODEL_METADATA_ERROR) from e


@router.post("/{model_id}/health", response_model=ModelHealthResponse)
async def check_model_health(
    model_id: str,  # noqa: IDENTITY_SSOT
    req: ModelHealthRequest | None = None,
    _identity: AgentIdentity = Depends(_require_models_mgmt),  # noqa: B008
) -> ModelHealthResponse:
    """驗證特定模型的可用性和健康狀態。

    此端點會嘗試與模型服務建立連接並發送測試請求。

    path: model_id - 要檢查的模型 ID
    body: ModelHealthRequest - 可選的檢查參數

    example: POST /models/claude-3.5-sonnet/health
    """
    if req is None:
        req = ModelHealthRequest()

    metadata = get_model_metadata(model_id)
    if not metadata:
        return ModelHealthResponse(
            model_id=model_id,
            status="unavailable",
            error="not_found",
        )

    start_time = get_current_epoch()

    # 根據model_id的所有者選擇檢查方式
    owner = metadata.get("owned_by", "unknown").lower()

    try:
        if owner == "anthropic":
            # Claude 健康檢查
            from src.config import ANTHROPIC_API_KEY
            api_key = ANTHROPIC_API_KEY
            if not api_key:
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    error="auth_missing",
                )

            # 禁令 22: follow_redirects=False 防止憑證隨 30x 轉送至攻擊者主機。
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.post(
                    f"{ANTHROPIC_API_BASE_URL}/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    json={
                        "model": model_id,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "test"}],
                    },
                    timeout=req.timeout,
                )
                latency = (get_current_epoch() - start_time) * 1000

                if resp.status_code == 200:
                    return ModelHealthResponse(
                        model_id=model_id,
                        status="healthy",
                        latency_ms=round(latency, 2),
                    )
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    latency_ms=round(latency, 2),
                    error=f"api_error:{resp.status_code}",
                )

        elif owner == "google":
            # Gemini 健康檢查
            from src.config import GEMINI_API_KEY
            api_key = GEMINI_API_KEY
            if not api_key:
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    error="auth_missing",
                )

            # 禁令 23: 金鑰走 x-goog-api-key header（非 URL query），避免 httpx 例外/日誌洩漏；
            # 禁令 22: follow_redirects=False。
            async with httpx.AsyncClient(follow_redirects=False) as client:
                model_name = model_id.replace("models/", "")
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json={"contents": [{"parts": [{"text": "test"}]}]},
                    timeout=req.timeout,
                )
                latency = (get_current_epoch() - start_time) * 1000

                if resp.status_code == 200:
                    return ModelHealthResponse(
                        model_id=model_id,
                        status="healthy",
                        latency_ms=round(latency, 2),
                    )
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    latency_ms=round(latency, 2),
                    error=f"api_error:{resp.status_code}",
                )

        elif owner == "openai":
            # OpenAI 健康檢查
            from src.config import OPENAI_API_KEY
            api_key = OPENAI_API_KEY
            if not api_key:
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    error="auth_missing",
                )

            # 禁令 22: follow_redirects=False。
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "test"}],
                        "max_completion_tokens": 10,
                    },
                    timeout=req.timeout,
                )
                latency = (get_current_epoch() - start_time) * 1000

                if resp.status_code == 200:
                    return ModelHealthResponse(
                        model_id=model_id,
                        status="healthy",
                        latency_ms=round(latency, 2),
                    )
                return ModelHealthResponse(
                    model_id=model_id,
                    status="degraded",
                    latency_ms=round(latency, 2),
                    error=f"api_error:{resp.status_code}",
                )

        else:
            # 不支持的供應商
            return ModelHealthResponse(
                model_id=model_id,
                status="unavailable",
                error="not_supported",
            )

    except TimeoutError:
        latency = (get_current_epoch() - start_time) * 1000
        return ModelHealthResponse(
            model_id=model_id,
            status="unavailable",
            latency_ms=round(latency, 2),
            error="timeout",
        )
    except Exception as health_err:
        # 禁令 23: 絕不把原始例外字串（可能含 Bearer/sk-*/key=）放進回應；
        # 僅回傳穩定機器碼，完整細節只進伺服器日誌。
        latency = (get_current_epoch() - start_time) * 1000
        logger.exception("Error checking health for %s", model_id)
        if isinstance(health_err, httpx.TimeoutException):
            reason = "timeout"
        elif isinstance(health_err, httpx.ConnectError):
            reason = "connection_error"
        else:
            reason = "request_failed"
        return ModelHealthResponse(
            model_id=model_id,
            status="unavailable",
            latency_ms=round(latency, 2),
            error=reason,
        )
