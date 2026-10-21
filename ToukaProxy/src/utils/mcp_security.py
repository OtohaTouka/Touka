"""MCP 命令安全 — shell 注入防護的單一事實來源 (SSOT)。

純函式模組：零 IO、零環境變數存取 (SRP)，供 ``routers/mcp.py`` 的
``MCPServerItem`` 與 ``services/creation`` 的 ``McpCreationKind`` 共用同一份
命令驗證，消除重複正則。對齊 ADR-0033 (InfoSec — 結構化邊界校驗 M4)：
MCP server 命令不得含 shell 注入元字元，防止可寫入 server_config.json 者
注入任意指令。
"""
from __future__ import annotations

import re

# M4 (ADR-0033)：shell 注入元字元黑名單，集中於此作為唯一事實來源。
_SHELL_INJECTION_RE: re.Pattern[str] = re.compile(r"[;&|`$<>()\n\r]")


def is_command_safe(command: str) -> bool:
    """回傳 True 表示命令不含 shell 注入元字元，可安全作為 MCP server 命令。"""
    return _SHELL_INJECTION_RE.search(command) is None
