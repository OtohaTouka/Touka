"""從 Touka 請小幫手開放資料夾（ADR-0320）：驗章與停收規則。

驗章通過的請求，不論位置能不能開放都跳視窗，所以 Touka 在人動手前分不出位置能不能開放。
規則只放在這裡，``service.py`` 只負責接線。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from touka_local.errors import Refused
from touka_local.paths import MAX_REQUEST_PATH_CHARS
from touka_local.signing import open_request_signature_ok, text_fingerprint

if TYPE_CHECKING:
    from touka_local.protocol import Job

# 小幫手自己定的視窗時間，不用 Touka 給的；Touka 那邊等得比這久（parity 測試核對）
OPEN_DECISION_SECONDS: Final = 120.0
OPEN_REFUSALS_BEFORE_PAUSE: Final = 3
OPEN_PAUSE_SECONDS: Final = 600.0
REMEMBERED_NONCES: Final = 64

STATE_CHECKING: Final = "checking"
STATE_OPENABLE: Final = "openable"
STATE_NOT_OPENABLE: Final = "not_openable"
# 路徑檢查讀不到磁碟、或檢查本身出錯時，視窗上寫的原因
REASON_UNREADABLE: Final = "unreadable"
REASON_INTERNAL: Final = "internal"

OpenState = Literal["checking", "openable", "not_openable"]


@dataclass(frozen=True)
class OpenRequest:
    """一個開放請求。``reason`` 只顯示在小幫手的視窗，不回給 Touka。"""

    path: str
    digest: str
    state: OpenState = STATE_CHECKING
    folder: str | None = None
    reason: str | None = None

    @property
    def openable(self) -> bool:
        """可以按核准的那一種。"""
        return self.state == STATE_OPENABLE


def checking(path: str) -> OpenRequest:
    """還在檢查路徑，先佔住視窗；主視窗不畫這個狀態。"""
    return OpenRequest(path=path, digest=text_fingerprint(path))


def verify_open_request(job: Job, key: bytes | None) -> str:
    """核對章，回傳路徑。不正規化路徑：寫法不對也要跳視窗，不能先回一個不同的代碼。"""
    args = job.args
    path = args.get("path") if set(args) == {"path"} else None
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_REQUEST_PATH_CHARS:
        raise Refused("bad_signature")
    if key is None:
        raise Refused("needs_new_pairing")
    if job.decision is None or not open_request_signature_ok(key, job.id, path, job.decision):
        raise Refused("bad_signature")
    return path


class OpenPause:
    """連續 3 次沒核准就停收 10 分鐘；核准或人按「現在恢復」就歸零。

    本身不帶鎖：呼叫端要持有 ``HelperService`` 的鎖。
    """

    def __init__(self) -> None:
        self._misses = 0
        self._until: float | None = None

    def paused_until(self, now: float) -> float | None:
        """停收到什麼時候；沒有停收回 None。時間到就自動恢復。"""
        if self._until is not None and now >= self._until:
            self._until = None
        return self._until

    def missed(self, now: float) -> None:
        """一次沒核准：拒絕、按「知道了」、逾時，或被收掉。"""
        self._misses += 1
        if self._misses >= OPEN_REFUSALS_BEFORE_PAUSE:
            self._misses = 0
            self._until = now + OPEN_PAUSE_SECONDS

    def resume(self) -> None:
        """核准一次，或人在主視窗按「現在恢復」。"""
        self._misses = 0
        self._until = None
