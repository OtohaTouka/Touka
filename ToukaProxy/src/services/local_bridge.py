"""Touka 和使用者電腦上小幫手之間的工作佇列（ADR-0311）.

小幫手主動來拿工作（長輪詢），電腦上不開連線入口。佇列只在記憶體，和 ``ask_user_store``
一樣假設單一行程；Touka 重啟後舊工作全部作廢，小幫手的 confirm 會失敗，不會執行。

搬移、刪除要人在小幫手的視窗核准。按下核准後小幫手要先 ``confirm``，Touka 回成功才動手，
所以工作被取消、逾時、Touka 重啟時都不會執行。
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from src.models.local_bridge import (
    MAX_REQUESTER_CHARS,
    LocalJob,
    LocalJobResult,
    LocalPollResponse,
)
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_PENDING_JOBS: Final = 8
MAX_CANCELLED_IDS: Final = 32

READ_WAIT_SECONDS: Final = 30.0
DECISION_WAIT_SECONDS: Final = 600.0
# 小幫手比 Touka 早放棄，才不會 Touka 回「沒核准」之後小幫手還按得下去
HELPER_DECISION_MARGIN_SECONDS: Final = 60.0
HELPER_EXECUTION_LIMIT_SECONDS: Final = 300.0
# 比小幫手的執行上限寬，結果才送得回來
EXECUTION_WAIT_SECONDS: Final = 330.0
# Docker 預設 10 秒就強殺，輪詢結束後還要留時間給關機清理
POLL_HOLD_SECONDS: Final = 5.0
ONLINE_WINDOW_SECONDS: Final = 20.0
CHECK_INTERVAL_SECONDS: Final = 1.0

CODE_OFFLINE: Final = "helper_offline"
CODE_BUSY: Final = "helper_busy"
CODE_NO_DECISION: Final = "no_decision"
CODE_TIMED_OUT: Final = "timed_out"
CODE_RESULT_UNKNOWN: Final = "result_unknown"
CODE_PAIRING_CHANGED: Final = "pairing_changed"

ACTION_NEEDING_CONFIRM: Final = "propose"
# 不用人核准，交給小幫手之後就可能已經做了
_MUTATING_WITHOUT_CONFIRM: Final = frozenset({"create_folder", "create_file"})


# Windows 磁碟在 Git Bash、WSL、Docker Desktop 裡的寫法
_POSIX_DRIVE: Final = re.compile(r"/(?:mnt/|run/desktop/mnt/host/|host_mnt/)?([A-Za-z])(/.*)?")


def _as_host_path(value: str) -> str | None:
    """POSIX 寫法的 Windows 磁碟換成磁碟路徑；其他 POSIX 絕對路徑是容器裡的預設值，當作取不到。"""
    if not PurePosixPath(value).is_absolute():
        return value
    drive = _POSIX_DRIVE.fullmatch(value)
    if drive is None:
        return None
    return f"{drive.group(1).upper()}:{drive.group(2) or '/'}"


def host_mount_roots(*sources: str) -> list[str]:
    """主機上已經掛進容器的路徑。小幫手只拿來多擋，不會因此多開放。"""
    converted = (_as_host_path(source.strip()) for source in sources if source.strip())
    return [path for path in converted if path is not None]


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


def _failure(code: str) -> LocalJobResult:
    return LocalJobResult(status="error", code=code)


@dataclass
class _Job:
    id: str
    action: str
    args: dict[str, Any]
    requester: str
    deadline: float
    sent: bool = False
    confirmed: bool = False
    result: LocalJobResult | None = field(default=None)

    @property
    def may_have_run(self) -> bool:
        """小幫手可能已經動手了，失敗時不能說沒事。"""
        return self.confirmed or (self.sent and self.action in _MUTATING_WITHOUT_CONFIRM)


class LocalBridge:
    """一次只服務一個小幫手；後來的輪詢取代先前的。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _monotonic_seconds,
        check_interval: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._clock = clock
        self._check_interval = check_interval
        self._generation: int | None = None
        self._jobs: dict[str, _Job] = {}
        self._queue: deque[_Job] = deque()
        self._cancelled: deque[str] = deque(maxlen=MAX_CANCELLED_IDS)
        self._waiters: set[asyncio.Event] = set()
        self._poll_seq = 0
        self._last_poll: float | None = None
        self._helper_version: str | None = None

    @property
    def online(self) -> bool:
        """最近有沒有來輪詢。"""
        return (
            self._last_poll is not None and self._clock() - self._last_poll <= ONLINE_WINDOW_SECONDS
        )

    @property
    def helper_version(self) -> str | None:
        """上線中的小幫手版本。"""
        return self._helper_version if self.online else None

    def _notify(self) -> None:
        for waiter in self._waiters:
            waiter.set()

    async def _wait(self, timeout: float) -> None:
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._waiters.discard(waiter)

    def invalidate(self, generation: int) -> None:
        """換權杖或解除配對：掛著的輪詢作廢、工作全部失敗、上線狀態清掉。

        配對檔不見重建時世代號會變小，所以這裡不比大小；呼叫端要傳當下最新的世代號。
        """
        self._generation = generation
        self._poll_seq += 1
        self._last_poll = None
        self._helper_version = None
        for job in self._jobs.values():
            job.result = _failure(CODE_RESULT_UNKNOWN if job.may_have_run else CODE_PAIRING_CHANGED)
        self._jobs.clear()
        self._queue.clear()
        self._cancelled.clear()
        self._notify()

    def _next_job(self) -> _Job | None:
        while self._queue:
            job = self._queue.popleft()
            if job.id in self._jobs:
                job.sent = True
                return job
        return None

    def _drain_cancelled(self) -> list[str]:
        cancelled = list(self._cancelled)
        self._cancelled.clear()
        return cancelled

    @staticmethod
    def _describe(job: _Job) -> LocalJob:
        decide_within = (
            DECISION_WAIT_SECONDS - HELPER_DECISION_MARGIN_SECONDS
            if job.action == ACTION_NEEDING_CONFIRM
            else READ_WAIT_SECONDS
        )
        return LocalJob(
            id=job.id,
            action=job.action,
            args=job.args,
            requester=job.requester,
            decide_within=decide_within,
        )

    async def poll(
        self, generation: int, helper_version: str, *, hold: float = POLL_HOLD_SECONDS
    ) -> LocalPollResponse | None:
        """等下一件工作。回 None 表示這個權杖世代已經作廢。"""
        if self._generation is None:
            self._generation = generation
        self._poll_seq += 1
        mine = self._poll_seq
        self._notify()
        deadline = self._clock() + hold
        started = _monotonic_seconds()
        while True:
            if generation != self._generation:
                return None
            if mine != self._poll_seq:
                return LocalPollResponse()
            self._last_poll = self._clock()
            self._helper_version = helper_version
            job = self._next_job()
            cancelled = self._drain_cancelled()
            if job is not None or cancelled:
                return LocalPollResponse(
                    job=self._describe(job) if job is not None else None, cancelled=cancelled
                )
            # 假時鐘不會自己走，實際經過時間也算，測試和實際都不會卡住
            elapsed = _monotonic_seconds() - started
            remaining = min(deadline - self._clock(), hold - elapsed)
            if remaining <= 0:
                return LocalPollResponse()
            await self._wait(remaining)

    async def submit(self, action: str, args: dict[str, Any], requester: str) -> LocalJobResult:
        """交一件工作給小幫手，等結果。呼叫端被取消時，小幫手下次輪詢會知道。"""
        if not self.online:
            return _failure(CODE_OFFLINE)
        if len(self._jobs) >= MAX_PENDING_JOBS:
            return _failure(CODE_BUSY)
        wait = DECISION_WAIT_SECONDS if action == ACTION_NEEDING_CONFIRM else READ_WAIT_SECONDS
        job = _Job(
            id=secrets.token_urlsafe(16),
            action=action,
            args=args,
            requester=requester[:MAX_REQUESTER_CHARS],
            deadline=self._clock() + wait,
        )
        self._jobs[job.id] = job
        self._queue.append(job)
        self._notify()
        try:
            return await self._await_outcome(job)
        except asyncio.CancelledError:
            self._abandon(job)
            raise

    def _abandon(self, job: _Job) -> None:
        if self._jobs.pop(job.id, None) is not None and job.sent:
            self._cancelled.append(job.id)
            self._notify()

    def _stalled_outcome(self, job: _Job) -> LocalJobResult | None:
        if self._clock() >= job.deadline:
            if job.may_have_run:
                return _failure(CODE_RESULT_UNKNOWN)
            return _failure(
                CODE_NO_DECISION if job.action == ACTION_NEEDING_CONFIRM else CODE_TIMED_OUT
            )
        if not self.online:
            return _failure(CODE_RESULT_UNKNOWN if job.may_have_run else CODE_OFFLINE)
        return None

    async def _await_outcome(self, job: _Job) -> LocalJobResult:
        while job.result is None:
            outcome = self._stalled_outcome(job)
            if outcome is not None:
                self._abandon(job)
                return outcome
            await self._wait(self._check_interval)
        return job.result

    def confirm(self, job_id: str, generation: int) -> bool:
        """人按了核准。只有還等得到結果的搬移、刪除清單才回 True，回 False 就不能動手。"""
        job = self._jobs.get(job_id)
        if (
            job is None
            or generation != self._generation
            or job.action != ACTION_NEEDING_CONFIRM
            or not job.sent
            or job.confirmed
            or self._clock() >= job.deadline
        ):
            return False
        job.confirmed = True
        job.deadline = self._clock() + EXECUTION_WAIT_SECONDS
        self._notify()
        return True

    def deliver(self, job_id: str, generation: int, result: LocalJobResult) -> bool:
        """小幫手回報結果。清單沒有經過 confirm 就不能回報成功。"""
        job = self._jobs.get(job_id)
        if job is None or generation != self._generation or not job.sent:
            return False
        if job.action == ACTION_NEEDING_CONFIRM and result.status == "ok" and not job.confirmed:
            return False
        job.result = result
        del self._jobs[job_id]
        self._notify()
        return True


@lru_cache(maxsize=1)
def get_local_bridge() -> LocalBridge:
    """全行程共用的通道。"""
    return LocalBridge()
