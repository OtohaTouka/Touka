"""ADR-0069 創建引擎共用命名正規化（SSOT）。

``server_name`` 為 MCP server 名稱 → server_config key 的單一正規化來源；
``mcp_kind``（碰撞檢查／註冊）與 ``scaffold``（寫檔目錄名）共用，確保三處一致。
"""
from __future__ import annotations


def server_name(name: str) -> str:
    """正規化為 server_config key（對齊 ``MCPHubService.install`` 慣例）。"""
    return name.strip().lower().replace(" ", "-")
