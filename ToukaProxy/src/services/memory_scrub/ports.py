"""清除連備份需要的事實庫介面（依賴反轉：協調器只依賴這個合約，不依賴具體實作）."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.identity import AgentIdentity


class ScrubFactStore(Protocol):
    """FactualStore 在清除時用到的部分."""

    base_dir: Path  # 記憶庫根目錄（向量庫的 _qdrant_db 也在這裡）

    @property
    def upgrade_backup_dir(self) -> Path:
        """升級備份資料夾."""
        ...

    def compact_after_reset(self, identity: AgentIdentity) -> bool:
        """整理線上庫；不存在回 False."""
        ...

    def list_agent_ids(self) -> set[str]:
        """事實庫裡出現過的 agent_id."""
        ...
