"""Touka 和使用者電腦上小幫手之間的工作佇列（ADR-0312）.

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
    MAX_OPEN_NAME_CHARS,
    MAX_REQUESTER_CHARS,
    MAX_RESULT_CODE_CHARS,
    PROTOCOL_VERSION,
    UNSAFE_TEXT_PATTERN,
    LocalDecision,
    LocalFolderList,
    LocalJob,
    LocalJobResult,
    LocalOpenResult,
    LocalPollResponse,
)
from src.utils.local_bridge_signing import (
    DECIDED_BY_OPEN_REQUEST,
    sign_open_request,
    text_fingerprint,
)
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.local_bridge import LocalFolderEntry, LocalFolderReport

MAX_PENDING_JOBS: Final = 8
MAX_CANCELLED_IDS: Final = 32

READ_WAIT_SECONDS: Final = 30.0
# 預覽要掃整份清單，資料夾很大時比讀檔久；要在小幫手記得預覽號碼的 540 秒內（ADR-0317）
PREVIEW_WAIT_SECONDS: Final = 120.0
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
# 開放名單（ADR-0330）：小幫手名單沒變也每 30 秒送一次，超過這麼久沒有新的就當作沒有
FOLDER_REPORT_MAX_AGE_SECONDS: Final = 75.0
# 記下被拒絕的輪詢帶的版本號，只收這個範圍
_MIN_NOTED_PROTOCOL: Final = 1
_MAX_NOTED_PROTOCOL: Final = 99

CODE_OFFLINE: Final = "helper_offline"
CODE_BUSY: Final = "helper_busy"
CODE_NO_DECISION: Final = "no_decision"
CODE_TIMED_OUT: Final = "timed_out"
CODE_RESULT_UNKNOWN: Final = "result_unknown"
CODE_PAIRING_CHANGED: Final = "pairing_changed"

ACTION_NEEDING_CONFIRM: Final = "propose"
ACTION_PREVIEW: Final = "preview"
ACTION_REQUEST_OPEN: Final = "request_open"
# 交給小幫手之後就可能已經做了（開放請求是人在小幫手視窗按的，不經 confirm，ADR-0320）
_MUTATING_WITHOUT_CONFIRM: Final = frozenset({"create_folder", "create_file", ACTION_REQUEST_OPEN})

# 開放請求（ADR-0320）：小幫手視窗開這麼久（小幫手自己也定同一個值，parity 測試核對），
# Touka 多等 30 秒給加名單與回報
OPEN_DECISION_SECONDS: Final = 120.0
OPEN_WAIT_SECONDS: Final = 150.0
CODE_NEEDS_NEW_PAIRING: Final = "needs_new_pairing"
CODE_BAD_RESULT: Final = "bad_result"
CODE_UNKNOWN: Final = "unknown"
_OPENED_FIELDS: Final = frozenset({"name", "writable"})
# 小幫手回的代碼與名稱會顯示在設定頁：代碼只收小寫英數與底線，名稱不收控制字元與改變顯示方向的字元
_RESULT_CODE: Final = re.compile(rf"[a-z0-9_]{{1,{MAX_RESULT_CODE_CHARS}}}")
_UNSAFE_NAME_CHAR: Final = re.compile(UNSAFE_TEXT_PATTERN)


# Windows 磁碟在 Git Bash、WSL、Docker Desktop 裡的寫法
_POSIX_DRIVE: Final = re.compile(
    r"//?(?:mnt/(?:host/)?|run/desktop/mnt/host/|host_mnt/)?([A-Za-z]):?(/.*)?"
)


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


_TOUKA_WAITS: Final = {
    ACTION_NEEDING_CONFIRM: DECISION_WAIT_SECONDS,
    ACTION_PREVIEW: PREVIEW_WAIT_SECONDS,
    ACTION_REQUEST_OPEN: OPEN_WAIT_SECONDS,
}
_HELPER_WAITS: Final = {
    ACTION_NEEDING_CONFIRM: DECISION_WAIT_SECONDS - HELPER_DECISION_MARGIN_SECONDS,
    ACTION_PREVIEW: PREVIEW_WAIT_SECONDS,
    ACTION_REQUEST_OPEN: OPEN_DECISION_SECONDS,
}


def _touka_wait(action: str) -> float:
    """Touka 等這件工作多久。"""
    return _TOUKA_WAITS.get(action, READ_WAIT_SECONDS)


def _helper_wait(action: str) -> float:
    """小幫手要在幾秒內決定；比 Touka 早放棄，才不會 Touka 不等了小幫手還在做。"""
    return _HELPER_WAITS.get(action, READ_WAIT_SECONDS)


def _failure(code: str) -> LocalJobResult:
    return LocalJobResult(status="error", code=code)


@dataclass
class _Job:
    id: str
    action: str
    args: dict[str, Any]
    requester: str
    deadline: float
    decision: LocalDecision | None = None
    sent: bool = False
    confirmed: bool = False
    result: LocalJobResult | None = field(default=None)

    @property
    def may_have_run(self) -> bool:
        """小幫手可能已經動手了，失敗時不能說沒事。"""
        return self.confirmed or (self.sent and self.action in _MUTATING_WITHOUT_CONFIRM)


@dataclass(frozen=True)
class _FolderReport:
    """小幫手最近一次回報的開放名單，只在記憶體。"""

    generation: int
    instance: str
    revision: int
    exposed: bool
    folders: tuple[LocalFolderEntry, ...]
    omitted: int
    received_at: float


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
        # 開放名單與版本不符（ADR-0330），都只在記憶體；換配對時清掉
        self._report: _FolderReport | None = None
        self._mismatch: tuple[float, int] | None = None

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

    @property
    def version_problem(self) -> str | None:
        """最近被拒絕的輪詢是哪一邊版本舊；連線中、或超過上線期限就不算。"""
        mismatch = self._mismatch
        if self.online or mismatch is None:
            return None
        noted_at, protocol = mismatch
        if self._clock() - noted_at > ONLINE_WINDOW_SECONDS:
            return None
        return "helper_older" if protocol < PROTOCOL_VERSION else "touka_older"

    def note_mismatch(self, protocol: int) -> None:
        """權杖對、版本不同的輪詢。呼叫端要先驗過權杖，別人留不下紀錄。"""
        if protocol != PROTOCOL_VERSION and _MIN_NOTED_PROTOCOL <= protocol <= _MAX_NOTED_PROTOCOL:
            self._mismatch = (self._clock(), protocol)

    def accept_report(self, generation: int, report: LocalFolderReport) -> None:
        """收下小幫手回報的開放名單。別的配對的、同一次啟動裡較舊的都不收。"""
        if generation != self._generation:
            return
        current = self._report
        if (
            current is not None
            and current.instance == report.instance
            and report.revision < current.revision
        ):
            return
        self._report = _FolderReport(
            generation=generation,
            instance=report.instance,
            revision=report.revision,
            exposed=report.exposed,
            folders=tuple(report.folders),
            omitted=report.omitted,
            received_at=self._clock(),
        )

    def folder_list(self) -> LocalFolderList:
        """設定頁看的開放名單：離線、還沒回報、太久沒回報都不給清單。"""
        if not self.online:
            return LocalFolderList(state="offline")
        report = self._report
        if report is None or report.generation != self._generation:
            return LocalFolderList(state="waiting")
        if self._clock() - report.received_at > FOLDER_REPORT_MAX_AGE_SECONDS:
            return LocalFolderList(state="stale")
        return LocalFolderList(
            state="ok", exposed=report.exposed, folders=list(report.folders), omitted=report.omitted
        )

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
        self._report = None
        self._mismatch = None
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
        return LocalJob(
            id=job.id,
            action=job.action,
            args=job.args,
            requester=job.requester,
            decide_within=_helper_wait(job.action),
            decision=job.decision,
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

    async def submit(
        self,
        action: str,
        args: dict[str, Any],
        requester: str,
        *,
        signer: Callable[[str], LocalDecision] | None = None,
    ) -> LocalJobResult:
        """交一件工作給小幫手，等結果。呼叫端被取消時，小幫手下次輪詢會知道。

        ``signer`` 拿到這件工作的編號後蓋章（ADR-0317）；鑰匙不進通道。
        """
        if not self.online:
            return _failure(CODE_OFFLINE)
        if len(self._jobs) >= MAX_PENDING_JOBS:
            return _failure(CODE_BUSY)
        job_id = secrets.token_urlsafe(16)
        job = _Job(
            id=job_id,
            action=action,
            args=args,
            requester=requester[:MAX_REQUESTER_CHARS],
            deadline=self._clock() + _touka_wait(action),
            decision=signer(job_id) if signer is not None else None,
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


def open_request_signer(key: bytes, path: str) -> Callable[[str], LocalDecision]:
    """開放請求的章（ADR-0320）：拿到工作編號後才蓋，每次一個新的一次性號碼。鑰匙不進通道。"""
    fingerprint = text_fingerprint(path)

    def sign(job_id: str) -> LocalDecision:
        nonce = secrets.token_urlsafe(16)
        return LocalDecision(
            nonce=nonce,
            digest=fingerprint,
            shown=fingerprint,
            decided_by=DECIDED_BY_OPEN_REQUEST,
            signature=sign_open_request(key, job_id=job_id, nonce=nonce, path=path),
        )

    return sign


def open_result_from(result: LocalJobResult) -> LocalOpenResult:
    """小幫手的回報換成設定頁看的結果。格式不對的當成錯誤，不回給模型看的英文說明。"""
    if result.status != "ok":
        code = result.code or CODE_UNKNOWN
        if _RESULT_CODE.fullmatch(code) is None:
            return LocalOpenResult(status="error", code=CODE_BAD_RESULT)
        return LocalOpenResult(status=result.status, code=code)
    data = result.data
    if (
        isinstance(data, dict)
        and set(data) == _OPENED_FIELDS
        and isinstance(data["name"], str)
        and 1 <= len(data["name"]) <= MAX_OPEN_NAME_CHARS
        and _UNSAFE_NAME_CHAR.search(data["name"]) is None
        and isinstance(data["writable"], bool)
    ):
        return LocalOpenResult(status="opened", name=data["name"], writable=data["writable"])
    return LocalOpenResult(status="error", code=CODE_BAD_RESULT)


@lru_cache(maxsize=1)
def get_local_bridge() -> LocalBridge:
    """全行程共用的通道。"""
    return LocalBridge()
