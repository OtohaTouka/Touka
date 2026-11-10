"""算出呼叫者該看到哪些模型下架提醒（ADR-0310）。

靈魂的範圍比照 /stats；Auto 審查員與摘要員的設定只給人類管理員。自訂連線的模型不提醒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.core.exceptions import ToukaException
from src.models.model_lifecycle import ModelLifecycleAlert, ModelLifecycleAlertsResponse
from src.services.authorization_gateway import can_access_session
from src.services.autonomy import autonomy_service
from src.services.models import model_manager
from src.services.models.model_lifecycle import model_lifecycle_service, normalize_model_name
from src.services.models.routes import CustomRouteDisposition
from src.services.sliding_window_service import sliding_window_service
from src.services.soul_service import soul_service
from src.utils.brain_persistence import list_active_brains

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity


@dataclass
class _Usage:
    souls: list[str] = field(default_factory=list)
    reviewer: bool = False
    summarizer: bool = False


def _native_name(model: object) -> str | None:
    """送去原廠的模型名；沒設、自訂連線、名字模糊都回 None。"""
    if not isinstance(model, str) or not model.strip():
        return None
    try:
        resolution = model_manager.resolve_custom_route(model)
    except ToukaException:  # 模糊的名字實際呼叫也會被擋，這裡不猜
        return None
    if resolution.disposition is not CustomRouteDisposition.UNKNOWN:
        return None
    return normalize_model_name(model)


def _usage_for(identity: AgentIdentity) -> dict[str, _Usage]:
    usage: dict[str, _Usage] = {}
    for agent_id, brain in list_active_brains().items():
        name = _native_name(brain)
        # 直聊成功也會寫進 brain_settings（人類、自訂模型），那些在設定裡沒有地方可以換
        if name and can_access_session(identity, agent_id) and soul_service.is_toukasoul(agent_id):
            usage.setdefault(name, _Usage()).souls.append(agent_id)
    if identity.is_admin_human():
        review = autonomy_service.config.get("auto_review")
        if name := _native_name(review.get("model") if isinstance(review, dict) else None):
            usage.setdefault(name, _Usage()).reviewer = True
        if name := _native_name(sliding_window_service.load_config().summary_model):
            usage.setdefault(name, _Usage()).summarizer = True
    return usage


def current_alerts(identity: AgentIdentity) -> ModelLifecycleAlertsResponse:
    """有人開畫面時呼叫：順便看要不要查清單（在背景，不等結果）。"""
    model_lifecycle_service.trigger_check()
    alerts = []
    for name, used in sorted(_usage_for(identity).items()):
        notice = model_lifecycle_service.notice_for(name)
        if notice is not None:
            alerts.append(
                ModelLifecycleAlert(
                    model=name,
                    status=notice.status,
                    retires_at=notice.retires_at,
                    souls=sorted(used.souls),
                    reviewer=used.reviewer,
                    summarizer=used.summarizer,
                )
            )
    return ModelLifecycleAlertsResponse(alerts=alerts, checking=model_lifecycle_service.checking)
