"""靈魂與自訂模型的工具清單比對（ADR-0281 T2）：唯一的比對規則（SSOT）。

``enabled_tools`` 每一條的意思：

- 內建與記憶工具：名稱，逐字比對。
- MCP：恰好一個斜線的 ``伺服器/工具``，比（伺服器, 工具）這一對；不收萬用字元、不收只寫伺服器名
  （刻意不沿用 MCP 權限設定「只寫伺服器名＝整台全開」那套比對）。
- 舊的長名 ``伺服器__工具``：照舊逐字比廣告名。
- 其他（舊短名、空的一邊、兩個以上斜線）：不生效。短名不自動對應：同名可能在好幾台，
  也可能和內建工具或伺服器同名，自動對應會打開沒勾的工具。

勾了「執行指令」就一起帶上「取結果」「停止指令」；它們照樣經過角色權限與政策。
純函式（禁令 18）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

MCP_SEPARATOR = "/"
LEGACY_MCP_SEPARATOR = "__"
_WILDCARD = "*"
_TERMINAL_COMPANIONS = ("get_command_result", "stop_command")
TOOL_COMPANIONS = MappingProxyType(
    {"execute_command": _TERMINAL_COMPANIONS, "bash": _TERMINAL_COMPANIONS}
)
_COMPANION_NAMES = frozenset(name for names in TOOL_COMPANIONS.values() for name in names)


@dataclass(frozen=True)
class ToolSelection:
    """``enabled_tools`` 解析後的結果：逐字比對的名稱，與 MCP 的（伺服器, 工具）。"""

    names: frozenset[str] = frozenset()
    mcp_pairs: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def parse(cls, entries: Iterable[object]) -> ToolSelection:
        """解析 ``enabled_tools``；不是字串、不生效的條目直接略過。"""
        names: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                continue
            if MCP_SEPARATOR not in entry:
                names.add(entry)
                names.update(TOOL_COMPANIONS.get(entry, ()))
                continue
            server, _, tool = entry.partition(MCP_SEPARATOR)
            if server and tool and MCP_SEPARATOR not in tool and _WILDCARD not in (server, tool):
                pairs.add((server, tool))
        return cls(frozenset(names), frozenset(pairs))

    def selects(self, name: str, *, server: str | None = None, tool: str | None = None) -> bool:
        """這個工具（廣告名，MCP 另帶伺服器與裸名）有沒有被勾。"""
        if name in self.names:
            return True
        return server is not None and tool is not None and (server, tool) in self.mcp_pairs

    def mentions_server(self, server: str) -> bool:
        """有沒有勾這台伺服器的任何工具（只寫伺服器名不算）。"""
        prefix = f"{server}{LEGACY_MCP_SEPARATOR}"
        return any(s == server for s, _ in self.mcp_pairs) or any(
            name.startswith(prefix) for name in self.names
        )

    def pair_servers(self) -> frozenset[str]:
        """``伺服器/工具`` 條目的伺服器（自訂模型路徑用來先連線）。"""
        return frozenset(server for server, _ in self.mcp_pairs)

    def short_names(self, known: frozenset[str]) -> frozenset[str]:
        """對不到任何已知工具的名稱（舊短名）；長名、``伺服器/工具`` 與自動帶上的配套不算。"""
        return frozenset(
            name
            for name in self.names - known - _COMPANION_NAMES
            if LEGACY_MCP_SEPARATOR not in name
        )
