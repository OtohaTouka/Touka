"""life1 stdio peer：真 MCPServer，用來測連線 owner 的關閉與失聯。

- argv[1]：啟動時寫入自己 PID 的檔案；
- argv[2] 為 ``ignore-eof``：stdin 關閉後不結束，再睡 120 秒（模擬關不掉的伺服器）。
工具：``ping``；``die`` 讓行程立刻結束（模擬伺服器途中崩潰）。
"""

import os
import sys
import time
from pathlib import Path

from mcp.server import MCPServer

mcp = MCPServer("life1-lifecycle-stdio", version="1.0.0")


@mcp.tool()
def ping() -> str:
    """Return a deterministic marker."""
    return "pong-stdio"


@mcp.tool()
def die() -> str:
    """Exit the server process immediately."""
    os._exit(3)


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
    mcp.run(transport="stdio")
    if len(sys.argv) > 2 and sys.argv[2] == "ignore-eof":
        time.sleep(120)
