"""協定客戶端主要進入點 (Protocol Client Entrypoint).

集中匯出與管理所有第三方協定（例如 MCP 等）的執行與調度管理元件。
"""

from .manager import ProtocolManager

__all__ = ["ProtocolManager"]
