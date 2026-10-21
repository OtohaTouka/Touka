"""MCP installation state 與 principal-isolated handle pool。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from src.protocols.mcp.owner import OWNER_CLOSE_SECONDS, McpConnectionOwner

if TYPE_CHECKING:
    from src.protocols.registry import ProtocolServerConfig


@dataclass(frozen=True)
class McpHandleKey:
    """runtime handle 唯一鍵；principal、協定模式與 config generation 全入鍵。"""

    installation_id: str
    principal_key: str | None
    protocol_mode: str
    config_generation: int

    @property
    def is_shared(self) -> bool:
        """此 key 是否可跨 principal 共用。"""
        return self.principal_key is None


@dataclass
class InstallationState:
    """可公開診斷的 installation 層狀態；不得存可變的「目前使用者」。"""

    installation_id: str
    config: ProtocolServerConfig
    generation: int = 1
    enabled: bool = True
    last_error: str | None = None
    last_error_at: float | None = None
    restart_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LegacyMcpHandle:
    """SDK v2 Client＋transport exit stack 的最小封裝（類名保留 P1 相容）。"""

    session: Any
    stack: Any | None = None

    async def close(self, timeout: float = OWNER_CLOSE_SECONDS) -> None:
        """關閉 transport：owner 自己掌握關閉期限；其他 stack（測試替身）用 ``wait_for``。"""
        if isinstance(self.stack, McpConnectionOwner):
            await self.stack.aclose()
        elif self.stack is not None:
            await asyncio.wait_for(self.stack.aclose(), timeout=timeout)


class HandlePool(Protocol):
    """ProtocolManager 依賴的 handle pool 合約。"""

    def get(self, key: McpHandleKey) -> LegacyMcpHandle | None:
        """依完整隔離鍵讀取 handle。"""
        ...

    def put(self, key: McpHandleKey, handle: LegacyMcpHandle) -> None:
        """依完整隔離鍵保存 handle。"""
        ...

    def swap(self, key: McpHandleKey, handle: LegacyMcpHandle) -> LegacyMcpHandle | None:
        """換上 handle，回傳被換掉的舊 handle（由呼叫端關閉）。"""
        ...

    def pop_installation(self, installation_id: str) -> list[LegacyMcpHandle]:
        """移除同一 installation 的全部 handles。"""
        ...

    def pop_principal(self, principal_key: str) -> list[LegacyMcpHandle]:
        """移除 exact principal partition 的全部 handles。"""
        ...

    def pop_all(self) -> list[LegacyMcpHandle]:
        """移除全部 handles。"""
        ...

    def keys_for_installation(self, installation_id: str) -> list[McpHandleKey]:
        """列出同一 installation 的現存 handle keys。"""
        ...


class LegacyHandlePool:
    """包住既有 shared client dict，另以完整 key 隔離 principal handles。"""

    def __init__(
        self,
        shared_sessions: dict[str, Any] | None = None,
        shared_stacks: dict[str, Any] | None = None,
    ) -> None:
        self._shared_sessions = shared_sessions if shared_sessions is not None else {}
        self._shared_stacks = shared_stacks if shared_stacks is not None else {}
        self._principal_handles: dict[McpHandleKey, LegacyMcpHandle] = {}

    def get(self, key: McpHandleKey) -> LegacyMcpHandle | None:
        """依完整隔離鍵讀取 handle。"""
        if key.is_shared:
            session = self._shared_sessions.get(key.installation_id)
            if session is None:
                return None
            return LegacyMcpHandle(
                session=session,
                stack=self._shared_stacks.get(key.installation_id),
            )
        return self._principal_handles.get(key)

    def put(self, key: McpHandleKey, handle: LegacyMcpHandle) -> None:
        """依完整隔離鍵保存 handle。"""
        if key.is_shared:
            self._shared_sessions[key.installation_id] = handle.session
            if handle.stack is not None:
                self._shared_stacks[key.installation_id] = handle.stack
            return
        self._principal_handles[key] = handle

    def swap(self, key: McpHandleKey, handle: LegacyMcpHandle) -> LegacyMcpHandle | None:
        """換上 handle，回傳被換掉的舊 handle（由呼叫端關閉）。"""
        if not key.is_shared:
            previous = self._principal_handles.get(key)
            self._principal_handles[key] = handle
            return previous
        session = self._shared_sessions.get(key.installation_id)
        stack = self._shared_stacks.pop(key.installation_id, None)
        self.put(key, handle)
        if session is None and stack is None:
            return None
        return LegacyMcpHandle(session=session, stack=stack)

    def pop_installation(self, installation_id: str) -> list[LegacyMcpHandle]:
        """移除同一 installation 的全部 handles。"""
        handles: list[LegacyMcpHandle] = []
        session = self._shared_sessions.pop(installation_id, None)
        stack = self._shared_stacks.pop(installation_id, None)
        if session is not None or stack is not None:
            handles.append(LegacyMcpHandle(session=session, stack=stack))
        for key in list(self._principal_handles):
            if key.installation_id == installation_id:
                handles.append(self._principal_handles.pop(key))
        return handles

    def pop_principal(self, principal_key: str) -> list[LegacyMcpHandle]:
        """Remove only handles whose full key carries the exact principal。"""
        handles: list[LegacyMcpHandle] = []
        for key in list(self._principal_handles):
            if key.principal_key == principal_key:
                handles.append(self._principal_handles.pop(key))
        return handles

    def pop_all(self) -> list[LegacyMcpHandle]:
        """移除全部 handles。"""
        installation_ids = set(self._shared_sessions) | set(self._shared_stacks)
        handles: list[LegacyMcpHandle] = []
        for installation_id in installation_ids:
            handles.extend(self.pop_installation(installation_id))
        handles.extend(self._principal_handles.values())
        self._principal_handles.clear()
        return handles

    def keys_for_installation(self, installation_id: str) -> list[McpHandleKey]:
        """列出同一 installation 的現存 handle keys。"""
        keys = [key for key in self._principal_handles if key.installation_id == installation_id]
        if installation_id in self._shared_sessions:
            keys.append(
                McpHandleKey(
                    installation_id=installation_id,
                    principal_key=None,
                    protocol_mode="2026-07-28",
                    config_generation=0,
                )
            )
        return keys
