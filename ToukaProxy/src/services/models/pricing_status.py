"""系統監控畫面要的價目表資訊（ADR-0311）。

沒有具體價格的模型只列呼叫者看得到的（範圍和模型下架提醒相同）；自訂連線的模型不列。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from src.services.models.lifecycle_alerts import models_in_use
from src.services.providers import pricing

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity


class PricingStatus(TypedDict):
    """價目表的查證日期、有沒有自己改過的價格、在用但沒有具體價格的模型。"""

    verified_at: str
    has_override: bool
    estimated_models: list[str]


def pricing_status(identity: AgentIdentity) -> PricingStatus:
    """組出呼叫者該看到的價目表資訊。"""
    return {
        "verified_at": pricing.PRICING_VERIFIED_AT,
        "has_override": pricing.has_overrides(),
        "estimated_models": sorted(
            name for name in models_in_use(identity) if pricing.is_estimated(name)
        ),
    }
