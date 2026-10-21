"""ADR-0111：原生模型 per-model 預設取樣參數注入層。

僅作用於**原生供應商模型**（``owned_by`` 非 custom/proxy/touka）；自定義模型(L2)與靈魂(L1)
的參數由各自的 ``CustomModelLayer``/``SoulLayer`` 處理。本層在它們**之前**執行，且**僅填補
請求未顯式帶的參數**（Pydantic ``model_fields_set``），因此：
  ① 不覆蓋客戶端明確送出的值（API 消費者的 per-request 設定優先）；
  ② custom/soul 層在本層之後執行，其設定會覆蓋本層（精準度：custom > soul > 原生預設 > 請求預設）。
"""

import logging
from typing import Any

from src.middleware.base import ProxyLayer
from src.models.context import PipelineContext
from src.services.models import model_manager
from src.services.providers import registry

logger = logging.getLogger(__name__)

# 取樣參數白名單（與 ModelSettingsUpdate 一致）。custom/proxy/touka 由其他層處理。
_PARAMS = ("temperature", "top_p", "top_k")


def apply_native_defaults(request: Any) -> None:
    """為原生模型套用 models.json 的預設取樣參數（fill-when-absent；不覆蓋客戶端明確值）。

    供 ``NativeModelLayer`` 與群聊 per-agent 派發（bypass pipeline）共用，確保兩條路徑一致。
    """
    model_name = getattr(request, "model", "")
    if not model_name:
        return

    meta = next((m for m in model_manager.get_models() if m.get("id") == model_name), None)
    # 原生判準走 registry SSOT（與 ModelsTab `params_editable`、`patch_model_settings` 同源）。
    if meta is None or not registry.is_native_owner(meta.get("owned_by", "")):
        return  # 非原生（custom/soul/agent/未知）→ 交由 CustomModelLayer / SoulLayer 處理

    fields_set = getattr(request, "model_fields_set", set())
    for param in _PARAMS:
        stored = meta.get(param)
        if stored is not None and param not in fields_set and hasattr(request, param):
            setattr(request, param, stored)
            logger.debug("native default: %s.%s -> %s", model_name, param, stored)


class NativeModelLayer(ProxyLayer):
    """為原生模型套用其儲存的預設取樣參數（fill-when-absent；不覆蓋客戶端明確值）。"""

    async def process(self, request: Any, _ctx: PipelineContext) -> None:
        """Apply a native model's stored sampling-param defaults for absent fields."""
        apply_native_defaults(request)
