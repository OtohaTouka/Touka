"""統一工具審批 broker（hitl-unification H2）。

泛化 ``routers/terminal.py`` 的審批機殼（asyncio.Event＋單發解算＋逾時自動拒）成
Gate-2 全家族（原生／memory／plugin／MCP／終端）共用的同步 HITL 通道：

- **請求面**：``tool_executor`` 對 CONFIRM 決策先 ``register`` 再把
  ``tool_approval_request`` 事件（tool_events 封套）推進**當前聊天 SSE 佇列**——
  審批卡直接出現在對話裡，不需要獨立的 /terminal/stream 訂閱（ToukaChat 從未訂閱
  該通道＝Sumi 診斷的 ui_unavailable 根因）。
- **解算面**：人類以 ``POST /v1/tools/approve|reject/{exec_id}`` 解算（is_human 硬性
  要求——agent token 不可自批；admin 或 agent/session 擁有者）。
- **逾時**：``wait`` 逾時自動視同拒絕（fail-closed）。
- **作用域**：行程內、重啟即清（與 terminal 審批同語意）；headless（無互動串流）
  由各 broker fail-closed 拒絕（H1 錨「未決不執行」永遠成立）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# 與 terminal 審批同量級：總等待 180s（terminal 為 60s×3 重推；聊天卡片常駐不需重推）
DEFAULT_TIMEOUT_S = 180.0
MAX_PENDING = 50  # M2 同款：防審批洪水撐爆記憶體

ApprovalDecision = Literal["accepted", "declined", "cancelled"]
# 解算結果 → 呼叫端看到的審批結果
APPROVAL_VERDICTS = {
    "accepted": "approved",
    "declined": "rejected",
    "cancelled": "cancelled",
    "timeout": "timeout",
}


@dataclass
class PendingApproval:
    """一筆待決審批（行程內狀態）。"""

    exec_id: str
    tool_name: str
    # 發起調用的 agent 識別字串（顯示＋owned_agents 所有權比對用的輕量快照；
    # 授權判定的主體是解算端點的已驗證 AgentIdentity，非此欄位）
    agent_id: str  # noqa: IDENTITY_SSOT
    session_id: str | None
    arguments_preview: dict[str, Any]
    created_at: float
    interaction_kind: str = "tool_approval"
    interaction_schema: dict[str, Any] | None = None
    interaction_payload: dict[str, Any] | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: ApprovalDecision | None = None
    response_payload: dict[str, Any] | None = None
    resolver_id: str | None = None


@dataclass(frozen=True)
class ApprovalResolution:
    """一次性 interaction 解算；payload 只留在行程內 broker。"""

    decision: ApprovalDecision | Literal["timeout"]
    response_payload: dict[str, Any] | None = None


class ApprovalService:
    """行程內審批註冊表：register → wait（executor 阻塞）→ resolve（人類端點）。"""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        exec_id: str,
        *,
        tool_name: str,
        agent_id: str,
        session_id: str | None,
        arguments_preview: dict[str, Any],
        interaction_kind: str = "tool_approval",
        interaction_schema: dict[str, Any] | None = None,
        interaction_payload: dict[str, Any] | None = None,
    ) -> bool:
        """登記一筆待決審批；佇列滿或 exec_id 撞號回 False（呼叫端 fail-closed）。"""
        async with self._lock:
            if len(self._pending) >= MAX_PENDING:
                logger.warning(
                    "🚫 [Approval] Queue full (%d/%d); rejecting %s (%s).",
                    len(self._pending), MAX_PENDING, exec_id, tool_name,
                )
                return False
            if exec_id in self._pending:
                logger.warning("🚫 [Approval] Duplicate exec_id %s.", exec_id)
                return False
            self._pending[exec_id] = PendingApproval(
                exec_id=exec_id,
                tool_name=tool_name,
                agent_id=agent_id,
                session_id=session_id,
                arguments_preview=arguments_preview,
                created_at=get_current_epoch(),
                interaction_kind=interaction_kind,
                interaction_schema=interaction_schema,
                interaction_payload=interaction_payload,
            )
            return True

    def get(self, exec_id: str) -> PendingApproval | None:
        """查一筆待決審批（供端點做所有權檢查；已解算/不存在 → None）。"""
        pending = self._pending.get(exec_id)
        if pending is None or pending.decision is not None:
            return None
        return pending

    async def resolve(
        self,
        exec_id: str,
        *,
        resolver: str,
        approved: bool | None = None,
        decision: ApprovalDecision | None = None,
        response_payload: dict[str, Any] | None = None,
    ) -> bool:
        """人類解算（單發）：未知／已解算回 False。AUDIT 落 log。"""
        resolved_decision = (
            decision
            if decision is not None
            else "accepted"
            if approved
            else "declined"
        )
        async with self._lock:
            pending = self._pending.get(exec_id)
            if pending is None or pending.decision is not None:
                return False
            pending.decision = resolved_decision
            pending.response_payload = response_payload
            pending.resolver_id = resolver
            pending.event.set()
        logger.info(
            "🔏 AUDIT [Approval] %s %s by %s (tool=%s, agent=%s, session=%s)",
            exec_id, pending.decision, resolver,
            pending.tool_name, pending.agent_id, pending.session_id,
        )
        return True

    async def wait_for_resolution(
        self,
        exec_id: str,
        timeout_s: float | None = None,
    ) -> ApprovalResolution:
        """等待結構化 interaction；逾時 fail-closed，取消會清除 pending。"""
        pending = self._pending.get(exec_id)
        if pending is None:
            return ApprovalResolution(decision="declined")
        try:
            await asyncio.wait_for(
                pending.event.wait(),
                timeout=timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "⏰ [Approval] %s timed out (tool=%s) — auto-rejecting (fail-closed).",
                exec_id,
                pending.tool_name,
            )
            return ApprovalResolution(decision="timeout")
        else:
            return ApprovalResolution(
                decision=pending.decision or "declined",
                response_payload=pending.response_payload,
            )
        finally:
            async with self._lock:
                self._pending.pop(exec_id, None)

    async def wait(self, exec_id: str, timeout_s: float | None = None) -> str:
        """阻塞等待決定；回 "approved"|"rejected"|"timeout"。結束即清除登記。

        逾時＝自動拒絕（fail-closed）；等待被取消（串流中斷）亦清除登記。
        """
        resolution = await self.wait_for_resolution(exec_id, timeout_s)
        return APPROVAL_VERDICTS[resolution.decision]

    def pending_count(self) -> int:
        """目前待決筆數（觀測／測試用）。"""
        return sum(1 for p in self._pending.values() if p.decision is None)

    def reset_for_test(self) -> None:
        """測試隔離：喚醒所有等待者（視同拒絕）並清空登記表。"""
        for pending in self._pending.values():
            if pending.decision is None:
                pending.decision = "declined"
                pending.event.set()
        self._pending.clear()


approval_service = ApprovalService()
