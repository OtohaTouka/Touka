"""從 Touka 請小幫手開放資料夾（ADR-0320）：驗章與停收規則。

驗章通過的請求，不論位置能不能開放都跳視窗，所以 Touka 在人動手前分不出位置能不能開放。
例外只有兩種由名單字串決定的回覆（ADR-0330 2b）：等於或在開放資料夾裡的直接回「已經開放」；
包住開放資料夾的上一層照樣跳視窗，但不能開放，收掉時回報包住了哪些。名單本來就給 Touka 看，
這兩種回覆探不出名單以外的位置。規則只放在這裡，``service.py`` 只負責接線。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from touka_local.errors import Refused
from touka_local.grants import SYSTEM_FOLDER_NAMES
from touka_local.paths import MAX_REQUEST_PATH_CHARS, is_same_or_inside, normalize_folder, parts_of
from touka_local.rules import is_protected_name
from touka_local.signing import open_request_signature_ok, text_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterable

    from touka_local.grants import Grant
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
# 這一層包住了已經開放的資料夾（ADR-0330 2b）：不能開放，視窗與 Touka 都說包住了哪些
REASON_CONTAINS: Final = "contains_open"
# 請求的位置等於或在某個開放資料夾裡：不跳視窗，直接回這個代碼（不和 presence.ALREADY_OPEN 撞名）
OPEN_ALREADY_OPEN: Final = "already_open"
MAX_CONTAINED_NAMES: Final = 64

OpenState = Literal["checking", "openable", "not_openable"]


@dataclass(frozen=True)
class OpenRequest:
    """一個開放請求。

    ``reason`` 只顯示在小幫手的視窗，不回給 Touka；``contains`` 是包住的開放資料夾名稱，
    視窗收掉時連同代碼回給 Touka（ADR-0330 2b）。
    """

    path: str
    digest: str
    state: OpenState = STATE_CHECKING
    folder: str | None = None
    reason: str | None = None
    contains: tuple[str, ...] = ()

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
    if any(0xD800 <= ord(c) <= 0xDFFF for c in path):
        # 單獨的代理字元算不出指紋；這種路徑 Touka 蓋不出對的章
        raise Refused("bad_signature")
    if key is None:
        raise Refused("needs_new_pairing")
    if job.decision is None or not open_request_signature_ok(key, job.id, path, job.decision):
        raise Refused("bad_signature")
    return path


def _normalized(path: str) -> str | None:
    try:
        return normalize_folder(path)
    except Refused:
        return None


def already_open(path: str, grants: Iterable[Grant]) -> Grant | None:
    """請求的位置等於或在某個開放資料夾裡（ADR-0330 2b）。只比字串，不讀磁碟。

    名單上的全部都算（含暫停中的）；只有手改名單才會重疊，那時選最深的。多出來的那幾段有受保護
    的名稱或系統資料夾時不算，照舊跳視窗（視窗會說不能開放）。路徑格式不對也不算。
    """
    normalized = _normalized(path)
    if normalized is None:
        return None
    inside = [grant for grant in grants if is_same_or_inside(normalized, grant.path)]
    if not inside:
        return None
    deepest = max(inside, key=lambda grant: len(parts_of(grant.path)))
    extra = parts_of(normalized)[len(parts_of(deepest.path)) :]
    if any(is_protected_name(part) or part.casefold() in SYSTEM_FOLDER_NAMES for part in extra):
        return None
    return deepest


def contained_by(path: str, grants: Iterable[Grant]) -> tuple[str, ...]:
    """請求的位置包住了哪些開放資料夾（名稱照名單順序，最多 64 個）。只比字串，不讀磁碟。"""
    normalized = _normalized(path)
    if normalized is None:
        return ()
    names = [grant.name for grant in grants if is_same_or_inside(grant.path, normalized)]
    return tuple(names[:MAX_CONTAINED_NAMES])


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
