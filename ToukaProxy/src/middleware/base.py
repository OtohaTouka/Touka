"""代理管道中介軟體層的基礎介面定義。"""

from typing import Any, Protocol, runtime_checkable

from ..models.context import PipelineContext


@runtime_checkable
class ProxyLayer(Protocol):
    """Proxy 中介軟體層的基礎協定 (SOLID - DIP)。
    每層負責處理請求的一個特定面向（如：上下文注入、身分識別、工具裝配）。
    """
    async def process(self, request: Any, ctx: PipelineContext) -> None:
        """處理請求。修改 request 或 ctx 物件。
        """
        ...
