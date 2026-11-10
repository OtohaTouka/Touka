"""核准關卡（ADR-0311）：一次一個視窗，時間到自動拒絕，拒絕後冷卻。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from touka_local.errors import Refused

if TYPE_CHECKING:
    from touka_local.fsops import Clock

MAX_DECISION_SECONDS: Final = 540.0
APPROVE_DELAY_SECONDS: Final = 1.5
REJECT_COOLDOWN_SECONDS: Final = 5.0


@dataclass(frozen=True)
class PendingApproval:
    """正在等人決定的一份清單。"""

    job_id: str
    plan: Any
    requester: str
    offered_at: float
    deadline: float


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

    def offer(self, job_id: str, plan: Any, decide_within: float, requester: str = "") -> None:
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
            )

    def approve(self, job_id: str, digest: str) -> Any:
        """人按了核准。回傳清單；只有編號和內容都對得上才算。"""
        with self._lock:
            pending = self._pending
            now = self._clock()
            if pending is None or pending.job_id != job_id:
                raise Refused("no_pending")
            if now >= pending.deadline:
                raise Refused("expired")
            if now < pending.offered_at + APPROVE_DELAY_SECONDS:
                raise Refused("too_soon")
            if pending.plan.digest != digest:
                raise Refused("changed")
            self._pending = None
            return pending.plan

    def reject(self, job_id: str) -> bool:
        """人按了拒絕。"""
        with self._lock:
            if self._pending is None or self._pending.job_id != job_id:
                return False
            self._close_rejected()
            return True

    def expire_due(self) -> PendingApproval | None:
        """時間到就自動拒絕，回傳被拒絕的那份。"""
        with self._lock:
            pending = self._pending
            if pending is None or self._clock() < pending.deadline:
                return None
            self._close_rejected()
            return pending

    def cancel(self, job_id: str) -> bool:
        """Touka 那邊不等了，收掉視窗。也要冷卻，否則反覆提出又撤回就能一直跳視窗。"""
        with self._lock:
            if self._pending is None or self._pending.job_id != job_id:
                return False
            self._close_rejected()
            return True

    def _close_rejected(self) -> None:
        self._pending = None
        self._cooldown_until = self._clock() + REJECT_COOLDOWN_SECONDS
