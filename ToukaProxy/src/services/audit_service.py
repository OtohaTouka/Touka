"""策略稽核服務 (PolicyAuditService) 模組.

負責將 PaC (Policy as Code) 引擎產出的所有策略決策紀錄至持久化存儲 (CSV),
以便於後續的安全審計與 AI 監督。採用非同步線程池執行寫入操作, 確保不阻塞核心邏輯.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.services.resource_manager import resource_manager
from src.utils.time_utils import format_epoch_to_taipei, get_current_epoch

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.policy import PolicyContext, PolicyDecision

    SecurityAuditSink = Callable[["SecurityAuditEvent"], None]


class SecurityAuditEvent(BaseModel):
    """Secret-free synchronous event delivered from a durable security outbox。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=256)
    authority_id: str = Field(min_length=1, max_length=256)
    authority_generation: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)
    outcome: Literal["pending", "success", "denied", "failure"]
    created_at: float
    details_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PolicyAuditService:
    """策略決議稽核服務中樞.

    負責將所有決策細節 (包含 Session ID, Actor ID, 決策理由等) 寫入實體 CSV 檔案.
    採用單執行緒線程池 (Queueing) 確保寫入順序且不影響主執行緒效能 (SOLID - SRP).
    """

    _instance: PolicyAuditService | None = None
    _executor = ThreadPoolExecutor(max_workers=1)

    def __new__(cls) -> PolicyAuditService:
        """實作單例模式."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化審計服務，配置 CSV 欄位與路徑指標."""
        # 初始化路徑（SSOT）
        self.base_dir = Path(resource_manager.get_base_path())
        self.stats_dir = self.base_dir / "stats"
        self.audit_file = self.stats_dir / "decisions.csv"
        self.security_audit_file = self.stats_dir / "security_events.csv"
        self.headers = [
            "timestamp",
            "session_id",
            "actor_id",
            "role",
            "action",
            "resource",
            "auth_level",
            "effect",
            "reason",
        ]
        self.security_headers = [
            "created_at",
            "event_id",
            "category",
            "action",
            "actor_id",
            "authority_id",
            "authority_generation",
            "request_id",
            "outcome",
            "details_digest",
        ]

    def _ensure_file(self) -> None:
        """已棄用: 由 ResourceManager 處理路徑建立."""
        pass

    def log_decision(self, context: PolicyContext, decision: PolicyDecision) -> None:
        """非同步記錄一筆策略決策，交由背景線程處理.

        Args:
            context (PolicyContext): 決策時的完整上下文.
            decision (PolicyDecision): 決策引擎產出的結果對象.
        """
        self._executor.submit(self._sync_log_write, context, decision)

    def emit_security_event(
        self,
        event: SecurityAuditEvent,
        *,
        sink: SecurityAuditSink | None = None,
    ) -> None:
        """Synchronously deliver a durable security event; sink failure is fail-loud。"""
        target = sink or self._sync_security_event_write
        target(event)

    def _sync_security_event_write(self, event: SecurityAuditEvent) -> None:
        """Write one secret-free security event without background swallowing。"""
        row = [
            event.created_at,
            event.event_id,
            event.category,
            event.action,
            event.actor_id,
            event.authority_id,
            event.authority_generation,
            event.request_id,
            event.outcome,
            event.details_digest,
        ]
        resource_manager.append_csv_row(
            self.security_audit_file,
            row,
            headers=self.security_headers,
        )

    def _sync_log_write(self, context: PolicyContext, decision: PolicyDecision) -> None:
        """執行實體的 CSV 寫入邏輯（於背景線程執行）.

        Args:
            context (PolicyContext): 上下文.
            decision (PolicyDecision): 判斷結果.
        """
        try:
            row = [
                format_epoch_to_taipei(get_current_epoch())[0],
                context.environment.get("session_id", "N/A"),
                context.actor.agent_id,
                context.actor.display_role,
                context.action,
                context.resource,
                context.environment.get("auth_level", "N/A"),
                decision.effect,
                decision.reason or "",
            ]

            resource_manager.append_csv_row(self.audit_file, row, headers=self.headers)

        except Exception:
            logger.exception("❌ Failed to write audit log to CSV")


# 全域單例
audit_service = PolicyAuditService()
