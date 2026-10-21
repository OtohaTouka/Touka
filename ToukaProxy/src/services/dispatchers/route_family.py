"""路由家族判定：「這個模型名會被 factory 送到哪一家」的單一事實來源。

順序與 ``factory.get_stream_generator`` 相同：Human → 有效自訂連線 → 名稱含 claude／gemini
（``native_family``，factory 的兩個判斷也呼叫它）→ Provider Registry 關鍵字。備援只在主要
模型與備援模型的家族相同、且都判定得出來時才允許（跨廠商備援關閉）。

``resolve_model_route``＝「模型名→走哪一條路＋連線資訊」，factory 與 ``complete_once`` 共用
（ADR-0244）。官方 OpenAI（非自訂連線）且規則表標 ``uses_responses_api`` 的模型走 Responses，
目的地是固定常數（ADR-0248 裁定 2、附註 37）；自訂連線一律留在 Chat。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.config import OPENAI_RESPONSES_BASE_URL
from src.core.exceptions import ErrorCode, ToukaException
from src.services.model_manager import model_manager
from src.services.models.model_rules import rules_for
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.services.providers import registry

ANTHROPIC_FAMILY = "anthropic"
GEMINI_FAMILY = "gemini"
CUSTOM_FAMILY_PREFIX = "custom:"
HUMAN_ROUTE = "human"
OPENAI_ROUTE = "openai"  # 有效自訂連線＋Provider Registry 的 OpenAI 相容廠商
RESPONSES_ROUTE = "responses"  # 官方 OpenAI 的 Responses API（3b 裁定 2）
_OFFICIAL_OPENAI = "OpenAI"  # Provider Registry 的官方 OpenAI label
_NOT_BRAIN_OWNERS = frozenset({HUMAN_ROUTE, "touka"})  # human 與舊靈魂路由（ToukaSoul<id>）


class RouteCatalog(Protocol):
    """resolve_model_route 需要的模型清單介面（生產為 model_manager）。"""

    def get_models(self) -> list[dict[str, Any]]:
        """全部模型描述（含 owned_by）。"""
        ...

    def resolve_custom_route(self, model_name: str) -> CustomRouteResolution:
        """自訂連線的路由判定（有效／停用／未知）。"""
        ...


@dataclass(frozen=True)
class ModelRoute:
    """一個模型名的路由：kind ∈ human／anthropic／gemini／openai／responses，加上連線資訊。

    openai 帶 url、api_key、provider（自訂連線的空 key 照原樣保留，不改用官方 key）；
    anthropic／gemini 只帶 Provider Registry 的即時金鑰（可能是空字串）。
    """

    kind: str
    api_key: str | None = None
    url: str | None = None
    provider: str | None = None
    custom: bool = False  # 有效自訂連線（3b 裁定 5：主迴圈不串流，不看模型名）


def resolve_model_route(
    model_name: str,
    *,
    manager: RouteCatalog,
    provider_info: Callable[[str], tuple[str, str, str]],
) -> ModelRoute:
    """模型名 → ModelRoute；停用的自訂連線與無法識別的廠商拋 MODEL_NOT_FOUND。

    依賴由呼叫端注入：factory 傳自己模組的 ``model_manager``／``get_provider_info``，
    原有的 patch 點維持有效。不改任何狀態、不建連線。
    """
    model_info = next((m for m in manager.get_models() if m.get("id") == model_name), None)
    if model_info and model_info.get("owned_by") == "human":
        return ModelRoute(HUMAN_ROUTE)
    # 明確自訂路由優先於模型名稱推測。endpoint + api_key 是不可拆分的安全單位；
    # 空 key 亦為連線的明確設定，絕不可回退成官方 provider key。
    resolution = manager.resolve_custom_route(model_name)
    if resolution.disposition is CustomRouteDisposition.ACTIVE_CUSTOM:
        route = resolution.route
        if route is None:
            raise ToukaException(ErrorCode.MODEL_NOT_FOUND)
        return ModelRoute(OPENAI_ROUTE, api_key=route.api_key, url=route.endpoint,
                          provider=route.provider, custom=True)
    if resolution.disposition is CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE:
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND)
    family = native_family(model_name)
    if family:
        # 家族名＝Provider Registry 的 provider id（anthropic／gemini）；即時金鑰（root-cause-B）
        return ModelRoute(family, api_key=registry.get_key_by_id(family))
    url, api_key, provider = provider_info(model_name)
    if provider == _OFFICIAL_OPENAI and rules_for(model_name).uses_responses_api:
        return ModelRoute(RESPONSES_ROUTE, api_key=api_key, url=OPENAI_RESPONSES_BASE_URL,
                          provider=provider)
    return ModelRoute(OPENAI_ROUTE, api_key=api_key, url=url, provider=provider)


def native_family(model_name: str) -> str | None:
    """以名稱推測原生協定家族：含 claude → anthropic；含 gemini → gemini；其他 → None。"""
    lowered = model_name.lower()
    if "claude" in lowered:
        return ANTHROPIC_FAMILY
    if "gemini" in lowered:
        return GEMINI_FAMILY
    return None


def route_family(model_name: str | None) -> str | None:
    """回傳模型的路由家族；Human、停用的自訂模型或無法判定時回 None。"""
    if not model_name:
        return None
    if any(
        m.get("id") == model_name and m.get("owned_by") == "human"
        for m in model_manager.get_models()
    ):
        return None
    resolution = model_manager.resolve_custom_route(model_name)
    if resolution.disposition is CustomRouteDisposition.ACTIVE_CUSTOM:
        route = resolution.route
        return f"{CUSTOM_FAMILY_PREFIX}{route.connection_id}" if route else None
    if resolution.disposition is CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE:
        return None
    family = native_family(model_name)
    if family:
        return family
    spec = registry.resolve_provider(model_name)
    return spec.id if spec else None


def is_selectable_brain(model_name: str, *, manager: RouteCatalog) -> bool:
    """這個名字能不能當靈魂的主要模型（ADR-0250）：比只看 ``route_family`` 嚴。

    啟用中的自訂連線照原名可選；其他名字轉小寫後（同 SoulLayer）必須是模型清單裡的一般
    模型。停用的自訂連線、靈魂與自訂模型（is_agent）、human、舊靈魂路由 ID 與清單外的名字
    （例如 3a 拿掉的舊簡稱）都不可選。
    """
    resolution = manager.resolve_custom_route(model_name)
    if resolution.disposition is CustomRouteDisposition.ACTIVE_CUSTOM:
        return resolution.route is not None
    if resolution.disposition is CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE:
        return False
    lowered = model_name.lower()
    return any(
        m.get("id") == lowered
        and not m.get("is_agent")
        and m.get("owned_by") not in _NOT_BRAIN_OWNERS
        for m in manager.get_models()
    )


def same_route_family(primary: str | None, fallback: str | None) -> bool:
    """兩個模型是否屬於同一條、可判定的路由家族（備援只限同一家）。"""
    family = route_family(primary)
    return family is not None and family == route_family(fallback)
