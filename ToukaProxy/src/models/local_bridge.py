"""本機小幫手通道的資料合約（ADR-0312）.

小幫手是另一支跑在使用者電腦上的程式，自己有一份 ``protocol.py``；測試核對兩邊相同。
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION: Final = 4
MAX_JOB_RESULT_BYTES: Final = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS: Final = 2000
MAX_REQUESTER_CHARS: Final = 128
# 開放請求（ADR-0320）：路徑長度和小幫手的請求上限相同（parity 測試核對）
MAX_OPEN_PATH_CHARS: Final = 1024
# 資料夾名稱最多 255 字，重名時小幫手會在後面加「 2」之類
MAX_OPEN_NAME_CHARS: Final = 300
MAX_RESULT_CODE_CHARS: Final = 64
# 開放名單的回報（ADR-0330）：只給配對的人在設定頁看，只放記憶體
MAX_REPORTED_FOLDERS: Final = 64
MAX_REPORTED_PATH_CHARS: Final = 1024
# 請求的位置包住了已開放的資料夾時，小幫手回報包住了哪些（ADR-0330 2b）
MAX_CONTAINED_NAMES: Final = 64
# 小幫手回的名稱與路徑會顯示在設定頁：不收控制字元與改變顯示方向的字元（小幫手也是這一份）
UNSAFE_TEXT_PATTERN: Final = r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
_UNSAFE_TEXT: Final = re.compile(UNSAFE_TEXT_PATTERN)

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_HEX64_PATTERN: Final = r"^[0-9a-f]{64}$"
_NONCE_PATTERN: Final = r"^[A-Za-z0-9_-]{22}$"
# 小幫手每次啟動產生一次（secrets.token_urlsafe(16)），和一次性號碼同樣格式
INSTANCE_PATTERN: Final = _NONCE_PATTERN
_NO_CONTROL_CHARS: Final = r"^[^\x00-\x1f\x7f]+$"

# request_open、revoke 只從設定頁送（ADR-0320、ADR-0330），模型的工具合約沒有這兩個動作
LocalAction = Literal[
    "list", "read", "create_folder", "create_file", "propose", "preview", "request_open", "revoke"
]
# 設定頁縮小名單只有這兩種改法（ADR-0330 2b）：拿掉、改成只能讀；沒有讓名單變大的
RevokeChange = Literal["remove", "read_only"]
# 照模式的決定只有這三種；在小幫手視窗按的由小幫手自己記（ADR-0317）
DecidedByMode = Literal["user_card", "auto_review", "yolo"]
# 開放請求的章（ADR-0320）：只證明是 Touka 送的，小幫手一定跳視窗
DecidedByOpenRequest = Literal["owner_request"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalPollRequest(_Strict):
    """小幫手來要下一件工作時帶的資料。"""

    protocol: int
    helper_version: str = Field(min_length=1, max_length=32)


class LocalDecision(_Strict):
    """照模式做的決定和 Touka 蓋的章（ADR-0317），或開放請求的章（ADR-0320）。

    由通道填，模型的參數碰不到。
    """

    nonce: str = Field(pattern=_NONCE_PATTERN)
    digest: str = Field(pattern=_HEX64_PATTERN)
    shown: str = Field(pattern=_HEX64_PATTERN)
    decided_by: DecidedByMode | DecidedByOpenRequest
    signature: str = Field(pattern=_HEX64_PATTERN)


class LocalJob(_Strict):
    """交給小幫手的一件工作。``decide_within`` 是小幫手自己要在幾秒內決定。"""

    id: str
    action: LocalAction
    args: dict[str, Any]
    requester: str = Field(max_length=MAX_REQUESTER_CHARS)
    decide_within: float
    decision: LocalDecision | None = None


class LocalPollResponse(_Strict):
    """下一件工作、已經不用做的工作編號、主機上已經掛進容器的路徑。"""

    job: LocalJob | None = None
    cancelled: list[str] = Field(default_factory=list)
    host_mount_roots: list[str] = Field(default_factory=list)


class LocalJobResult(_Strict):
    """小幫手回報的結果。``refused`` 是規則不讓做或人拒絕，``error`` 是做的時候出錯。"""

    status: Literal["ok", "refused", "error"]
    code: str | None = Field(default=None, max_length=MAX_RESULT_CODE_CHARS)
    message: str | None = Field(default=None, max_length=MAX_ERROR_MESSAGE_CHARS)
    data: dict[str, Any] | None = None


class LocalJobAck(_Strict):
    """確認收到。"""

    ok: bool = True


class LocalPairingCreated(_Strict):
    """配對碼，只回這一次，Touka 不留明文。"""

    code: str


class LocalHelperStatus(_Strict):
    """設定頁顯示的狀態。

    ``version_problem``：最近被拒絕的輪詢是哪一邊版本舊（ADR-0330）；連線中一律是 None。
    """

    paired: bool
    online: bool
    helper_version: str | None = None
    version_problem: Literal["helper_older", "touka_older"] | None = None


def _displayable(value: str) -> str:
    """設定頁會顯示的文字：不收控制字元、改變顯示方向的字元與單獨的代理字元。"""
    if _UNSAFE_TEXT.search(value) is not None or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        msg = "contains characters that cannot be shown"
        raise ValueError(msg)
    return value


class LocalFolderEntry(_Strict):
    """一個開放的資料夾（ADR-0330）。完整路徑只給配對的人看，不給模型、不寫日誌。"""

    name: str = Field(min_length=1, max_length=MAX_OPEN_NAME_CHARS)
    path: str = Field(min_length=1, max_length=MAX_REPORTED_PATH_CHARS)
    writable: bool
    usable: bool

    @field_validator("name", "path")
    @classmethod
    def _shown_safely(cls, value: str) -> str:
        return _displayable(value)


class LocalFolderReport(_Strict):
    """小幫手回報的開放名單（ADR-0330）。

    ``instance`` 每次啟動不同；同一次啟動裡 ``revision`` 越大越新。
    """

    protocol: int
    instance: str = Field(pattern=INSTANCE_PATTERN)
    revision: int = Field(ge=0)
    exposed: bool
    folders: list[LocalFolderEntry] = Field(max_length=MAX_REPORTED_FOLDERS)
    omitted: int = Field(ge=0)


class LocalFolderList(_Strict):
    """設定頁看的開放名單。只有 ``ok`` 時才有 ``folders``。"""

    state: Literal["offline", "waiting", "stale", "ok"]
    exposed: bool = False
    folders: list[LocalFolderEntry] | None = None
    omitted: int = 0


class LocalOpenRequest(_Strict):
    """設定頁請小幫手開放的資料夾路徑（ADR-0320）。路徑規則都由小幫手判斷，這裡只擋控制字元與長度。"""

    path: str = Field(min_length=1, max_length=MAX_OPEN_PATH_CHARS, pattern=_NO_CONTROL_CHARS)


class LocalOpenResult(_Strict):
    """開放請求的結果。``opened`` 才有名稱與權限；其他只有代碼，前端照代碼顯示。

    ``already_open``：請求的位置已經在開放的資料夾裡（名稱、權限、``usable`` 是不是暫停中）。
    ``contains``：沒有開放、因為這一層包住了已經開放的這幾個（ADR-0330 2b）。
    """

    status: Literal["opened", "already_open", "refused", "error"]
    code: str | None = Field(default=None, max_length=MAX_RESULT_CODE_CHARS)
    name: str | None = Field(default=None, max_length=MAX_OPEN_NAME_CHARS)
    writable: bool | None = None
    usable: bool | None = None
    contains: list[str] | None = Field(default=None, max_length=MAX_CONTAINED_NAMES)


class LocalRevokeRequest(_Strict):
    """設定頁請小幫手把一個開放的資料夾拿掉或改成只能讀（ADR-0330 2b）。

    名稱與路徑照小幫手回報的那一筆送回去；小幫手兩個都對上同一筆才改。
    """

    name: str = Field(min_length=1, max_length=MAX_OPEN_NAME_CHARS)
    path: str = Field(min_length=1, max_length=MAX_REPORTED_PATH_CHARS)
    change: RevokeChange

    @field_validator("name", "path")
    @classmethod
    def _shown_safely(cls, value: str) -> str:
        return _displayable(value)


class LocalRevokeResult(_Strict):
    """縮小名單的結果。

    ``done`` 才有名稱，拿掉的帶 ``removed``、改成只能讀的帶 ``writable``；``error`` 只有代碼，
    前端照代碼說有沒有改。
    """

    status: Literal["done", "error"]
    code: str | None = Field(default=None, max_length=MAX_RESULT_CODE_CHARS)
    name: str | None = Field(default=None, max_length=MAX_OPEN_NAME_CHARS)
    removed: bool | None = None
    writable: bool | None = None


class LocalPairingRecord(_Strict):
    """配對檔的內容。解除配對時 ``token_hash``、``human_id`` 是空的，世代號繼續往上。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=0)
    token_hash: str | None
    human_id: str | None
    # 蓋章用的鑰匙（ADR-0317）。舊配對檔沒有這一欄，照樣讀得到，只是不送決定
    decision_key: str | None = None

    @field_validator("token_hash", "decision_key")
    @classmethod
    def _sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            msg = "must be 64 lowercase hex characters"
            raise ValueError(msg)
        return value
