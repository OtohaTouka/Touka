"""最小 MCPServer stub（供 ADR-0069 SMOKE 整合測試）。

以 stdio 啟動、暴露 1 個工具 (``ping``)，用來證明 ``smoke_mcp_server`` 的真實
啟動 + MCP 握手 + tools/list 流程。檔名以 ``_`` 開頭，pytest 不會收集為測試。
"""
from mcp.server import MCPServer

mcp = MCPServer("SmokeStub", version="2.0.0")


@mcp.tool()
def ping() -> str:
    """回傳 pong（smoke 測試用）。"""
    return "pong"


if __name__ == "__main__":
    mcp.run()
