"""中介軟體執行管道，負責組合並依序執行各個處理層。"""

import logging
from typing import Any

from ..models.context import PipelineContext
from .base import ProxyLayer

logger = logging.getLogger(__name__)

class ProxyPipeline:
    """Proxy 處理管道，控管多個 Middleware Layer 的執行順序 (SOLID - SRP)。
    """
    def __init__(self, layers: list[ProxyLayer]):
        """初始化代理執行管道，設定要依序執行的中介軟體層列表。"""
        self.layers = layers

    async def execute(self, request: Any, ctx: PipelineContext) -> None:
        """依序執行所有的中介軟體層。
        """
        for layer in self.layers:
            layer_name = layer.__class__.__name__
            try:
                logger.info(f"🏃 Executing layer: {layer_name}")
                await layer.process(request, ctx)
            except Exception as e:
                logger.error(f"❌ Error in middleware layer {layer_name}: {e}", exc_info=True)
                raise e
