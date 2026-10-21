"""自主權管理路由器 (Autonomy Router).

提供 API 端點用於查詢與更新代理人的自主權配置 (SSOT)。
"""

import logging

from fastapi import APIRouter, Depends

from src.core.exceptions import ErrorCode, ToukaException
from src.models.autonomy import (
    AutonomyConfigModel,  # noqa: TC001
    ReviewerModelOption,
    ReviewerModelsResponse,
)
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_admin_human, require_privileged
from src.services.autonomy import autonomy_service
from src.services.models import model_manager
from src.services.providers import registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/autonomy", tags=["autonomy"])

# Require Owner/Debugger/admin to manage autonomy configuration.
_require_autonomy_role = require_privileged
# 審查員設定（auto_review）只給人類 admin（R-2c ③ #1）：model 決定 Auto 的資料送到哪家廠商，
# headless_terminal 會讓所有靈魂無人在場的終端指令改成送審（決定 8 丙，要人類決定才開）。
_require_reviewer_admin = require_admin_human


@router.get("", response_model=AutonomyConfigModel)
async def get_autonomy_config(
    _identity: AgentIdentity = Depends(_require_autonomy_role),  # noqa: B008
) -> AutonomyConfigModel:
    """獲取目前的自主行為設定 (SSOT 來源).

    Returns:
        AutonomyConfigModel: 目前生效的自主權配置。
    """
    return AutonomyConfigModel.model_validate(autonomy_service.config)


def _has_provider_key(owned_by: str) -> bool:
    """供應商有登錄金鑰才算；靈魂、自訂模型、不用金鑰的端點型供應商都不算。"""
    spec = registry.spec_by_owned_by(owned_by)
    return bool(spec and spec.key_attrs and registry.get_key(spec))


def _reviewer_options() -> list[ReviewerModelOption]:
    """審查員選單與 POST 驗證共用同一份清單（R-2c ③ #1）。"""
    options = [
        ReviewerModelOption(
            id=m["id"], name=m.get("name") or m["id"], provider=m.get("owned_by", "")
        )
        for m in model_manager.get_models()
        if m.get("id")
        and not m.get("is_agent")
        and m.get("enabled", True) is not False
        and _has_provider_key(m.get("owned_by", ""))
    ]
    return sorted(options, key=lambda option: option.id)


@router.get("/reviewer-models", response_model=ReviewerModelsResponse)
async def list_reviewer_models(
    _identity: AgentIdentity = Depends(_require_autonomy_role),  # noqa: B008
) -> ReviewerModelsResponse:
    """Auto 審查員可選的模型：供應商金鑰已設定、啟用中的一般模型（決定 5）。"""
    return ReviewerModelsResponse(models=_reviewer_options())


@router.post("")
async def update_autonomy_config(
    new_config: AutonomyConfigModel,
    _identity: AgentIdentity = Depends(_require_autonomy_role),  # noqa: B008
) -> dict:
    """更新全域自主行為設定.

    Args:
        new_config (AutonomyConfigModel): 符合 Pydantic 模型驗證的新配置。

    Raises:
        HTTPException: 若持久化儲存失敗則拋出 500 錯誤。

    Returns:
        dict[str, str]: 成功狀態訊息。
    """
    # Pydantic 會自動驗證 new_config；auto_review 有變更時另外驗（前端整份送回原值不算變更）
    current = AutonomyConfigModel.model_validate(autonomy_service.config).auto_review
    if new_config.auto_review != current:
        _require_reviewer_admin(_identity)
        model = new_config.auto_review.model
        if model != current.model and model not in {"", *(o.id for o in _reviewer_options())}:
            raise ToukaException(ErrorCode.VALIDATION_ERROR, field="auto_review.model")
    success = autonomy_service.update_config(new_config.model_dump())
    if success:
        return {"status": "success", "message": "Autonomy configuration updated and saved."}
    
    raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED)