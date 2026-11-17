"""核准關卡（ADR-0312）：一次一個視窗，時間到自動拒絕，拒絕後冷卻。

等待中的可能是搬移、刪除清單，也可能是開放請求（ADR-0320）。這裡不認得具體型別：
核准、撤回時由呼叫端傳判斷，在鎖裡、拿掉等待項目之前先比對。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from touka_local.errors import Refused

if TYPE_CHECKING:
    from collections.abc import Callable

    from touka_local.fsops import Clock

MAX_DECISION_SECONDS: Final = 540.0
APPROVE_DELAY_SECONDS: Final = 1.5
REJECT_COOLDOWN_SECONDS: Final = 5.0


@dataclass(frozen=True)
class PendingApproval:
    """正在等人決定的一份清單或一個開放請求。"""

    job_id: str
    plan: Any
    requester: str
    offered_at: float
    deadline: float
    # 照模式的清單碰到底線時，視窗上方說明為什麼要人按（ADR-0317）
    reason: str = ""


class ApprovalGate:
    """人還沒按核准之前，什麼都不會動。"""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._cooldown_until = 0.0

    @property
    def current(self) -> PendingApproval | None:
        """正在等決定的清單。"""
        with self._lock:
            return self._pending

    def offer(
        self,
        job_id: str,
        plan: Any,
        decide_within: float,
        requester: str = "",
        reason: str = "",
    ) -> None:
        """打開核准視窗；已經有一個或還在冷卻就拒絕。"""
        with self._lock:
            now = self._clock()
            if self._pending is not None:
                raise Refused("approval_busy")
            if now < self._cooldown_until:
                raise Refused("approval_cooldown")
            self._pending = PendingApproval(
                job_id=job_id,
                plan=plan,
                requester=requester,
                offered_at=now,
                deadline=now + min(decide_within, MAX_DECISION_SECONDS),
                reason=reason,
            )

    def fill(self, job_id: str, accepts: Callable[[Any], bool], plan: Any) -> bool:
        """先佔住視窗、之後才知道內容的（開放請求）：把內容放進去。

        核准要等的 1.5 秒從現在算（視窗這時才畫出來）；期限不變，總時間才不會變長。
        """
        with self._lock:
            pending = self._pending
            if pending is None or pending.job_id != job_id or not accepts(pending.plan):
                return False
            self._pending = replace(pending, plan=plan, offered_at=self._clock())
            return True

    def approve(self, job_id: str, digest: str, accepts: Callable[[Any], bool]) -> Any:
        """人按了核准。回傳內容；編號、種類、內容都對得上才算，種類不對時等待項目留著。"""
        with self._lock:
            pending = self._pending
            now = self._clock()
            if pending is None or pending.job_id != job_id:
                raise Refused("no_pending")
            if not accepts(pending.plan):
                raise Refused("wrong_window")
            if now >= pending.deadline:
                raise Refused("expired")
            if now < pending.offered_at + APPROVE_DELAY_SECONDS:
                raise Refused("too_soon")
            if pending.plan.digest != digest:
                raise Refused("changed")
            self._pending = None
            return pending.plan

    def reject(self, job_id: str) -> PendingApproval | None:
        """人按了拒絕。回傳被關掉的那一筆。"""
        with self._lock:
            pending = self._pending
            if pending is None or pending.job_id != job_id:
                return None
            self._close_rejected()
            return pending

    def expire_due(self) -> PendingApproval | None:
        """時間到就自動拒絕，回傳被拒絕的那份。"""
        with self._lock:
            pending = self._pending
            if pending is None or self._clock() < pending.deadline:
                return None
            self._close_rejected()
            return pending

    def cancel(self, job_id: str, accepts: Callable[[Any], bool]) -> PendingApproval | None:
        """Touka 那邊不等了，收掉視窗。也要冷卻，否則反覆提出又撤回就能一直跳視窗。

        ``accepts`` 不接受的不收（開放請求不理會 Touka 撤回，ADR-0320）。
        """
        with self._lock:
            pending = self._pending
            if pending is None or pending.job_id != job_id or not accepts(pending.plan):
                return None
            self._close_rejected()
            return pending

    def abandon(self) -> PendingApproval | None:
        """配對失效或小幫手被看見：不論哪一種都收掉，回傳被收掉的那一筆。"""
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            self._close_rejected()
            return pending

    def _close_rejected(self) -> None:
        self._pending = None
        self._cooldown_until = self._clock() + REJECT_COOLDOWN_SECONDS
