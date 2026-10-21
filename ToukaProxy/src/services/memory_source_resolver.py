"""記憶來源：存記憶時記下來源（capture），查記憶時查回狀態（resolve）.

由 Proxy 記憶閘道持有單例（不在 main.py 注入）；記憶系統只透過 ``ISourceResolver`` 呼叫查回。

- 來源只取可信執行資訊：執行器注入的 ``session_id``／``request_id``，以及 pipeline context
  裡由路由寫入的 ``source_message_id``（UI 持久模式的已保存 user 節點、心跳的觸發節點）。
  模型帶的同名參數在公開參數驗證時就被拒絕。
- 指紋讀「已保存」的那則訊息來算；讀不到、不屬於這個身分就不記來源（存成 NULL）。
- 無痕與子代理不記來源，也不去查那段對話（零殘留）。
- 查回只回狀態（原文仍在／已修改／已不在），不回原文；先查對話樹，再查舊對話庫。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from memory_server.models.source_ref import (
    SourceKind,
    SourceRef,
    SourceStatus,
    content_fingerprint,
)
from memory_server.tools import get_manager

from src.services.authorization_gateway import can_access_session
from src.utils.async_db import session_service
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

# 這些執行情境存的記憶不記來源：無痕要零殘留；子代理沒有自己的對話
_NO_SOURCE_FLAGS = ("incognito", "subagent_run", "dispatch_run")


class MemorySourceResolver:
    """記憶來源的記下與查回（實作 memory_server.protocols.ISourceResolver）."""

    async def capture(
        self,
        identity: AgentIdentity,
        ctx: Any,
        *,
        session_id: str | None,
        request_id: str | None,
    ) -> SourceRef | None:
        """依可信執行資訊組來源；任何一項缺了或讀不到，就不記來源（存記憶照常）."""
        if not ctx or not session_id or any(ctx.get(flag) for flag in _NO_SOURCE_FLAGS):
            return None
        message_id = ctx.get("source_message_id")
        if not message_id:
            return None
        try:
            node = await self._session_node(identity, session_id, message_id)
        except Exception:
            logger.warning("[MemorySource] cannot read session %s; no source kept", session_id)
            return None
        if node is None:
            logger.warning("[MemorySource] message %s is not saved; no source kept", message_id)
            return None
        return SourceRef(
            kind=SourceKind.HEARTBEAT if ctx.get("heartbeat_run") else SourceKind.CONVERSATION,
            session_id=session_id,
            message_id=message_id,
            request_id=request_id,
            captured_at=int(get_current_epoch()),
            content_sha256=content_fingerprint(node.get("content")),
        )

    async def resolve(self, identity: AgentIdentity, source: SourceRef) -> SourceStatus:
        """先查對話樹、再查舊對話庫；讀取失敗往外丟（顯示成「無法確認」）."""
        node = await self._session_node(identity, source.session_id, source.message_id)
        if node is None:
            archived = (
                get_manager()
                .get_legacy_sync_service()
                .get_store(identity)
                .get_message(source.message_id)
            )
            node = archived if archived and archived["session_id"] == source.session_id else None
        if node is None:
            return SourceStatus.MISSING
        if content_fingerprint(node.get("content")) == source.content_sha256:
            return SourceStatus.AVAILABLE
        return SourceStatus.CHANGED

    @staticmethod
    async def _session_node(
        identity: AgentIdentity, session_id: str, message_id: str
    ) -> dict[str, Any] | None:
        """已保存的訊息節點；對話不在或不屬於這個身分回 None，讀取失敗往外丟."""
        snapshot = await session_service.read_persistence_snapshot(session_id, identity)
        session = snapshot.session
        if session is None or not can_access_session(
            identity, session.get("agent_id"), session.get("owner_user_id")
        ):
            return None
        return session["messages"]["messages"].get(message_id)
