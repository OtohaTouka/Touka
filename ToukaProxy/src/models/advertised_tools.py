"""這一輪實際列給模型的工具：ToolLayer 寫入，模型迴圈的執行器據此核對。

純資料、不做 I/O。只由系統寫入（ToolLayer、子代理組裝），模型的參數碰不到。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


@dataclass(frozen=True)
class AdvertisedTools:
    """去重後真的放進請求的伺服器端工具：原生工具名，和 MCP 的 (伺服器, 裸工具名)。"""

    native: frozenset[str] = frozenset()
    mcp: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        """一律轉成不可變集合：呼叫端事後改自己的 list 影響不到清單。"""
        object.__setattr__(self, "native", frozenset(self.native))
        object.__setattr__(self, "mcp", frozenset(tuple(pair) for pair in self.mcp))

    @classmethod
    def from_schemas(cls, schemas: Iterable[Any]) -> AdvertisedTools:
        """由注入的 OpenAI 形狀 schema 算清單；帶 server 的是 MCP（沒有裸名時用廣告名）。"""
        native: set[str] = set()
        mcp: set[tuple[str, str]] = set()
        for schema in schemas or ():
            name = (schema.get("function") or {}).get("name") if isinstance(schema, dict) else None
            if not name:
                continue
            if schema.get("server"):
                mcp.add((schema["server"], schema.get("tool") or name))
            else:
                native.add(name)
        return cls(native=frozenset(native), mcp=frozenset(mcp))

    def permits_native(self, name: str, successors: Mapping[str, frozenset[str]]) -> bool:
        """有列出；或是舊名，而且它的後繼工具全部有列出。"""
        required = successors.get(name)
        return name in self.native or (bool(required) and required <= self.native)

    def permits_mcp(self, server: str, tool: str) -> bool:
        """(伺服器, 裸工具名) 這一對有列出。"""
        return (server, tool) in self.mcp

    def has_server(self, server: str) -> bool:
        """清單裡有任何一對用這台伺服器。"""
        return any(listed == server for listed, _ in self.mcp)
