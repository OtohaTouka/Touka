"""本機小幫手通道的資料合約（ADR-0312）.

小幫手是另一支跑在使用者電腦上的程式，自己有一份 ``protocol.py``；測試核對兩邊相同。
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION: Final = 2
MAX_JOB_RESULT_BYTES: Final = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS: Final = 2000
MAX_REQUESTER_CHARS: Final = 128
# 開放請求（ADR-0320）：路徑長度和小幫手的請求上限相同（parity 測試核對）
MAX_OPEN_PATH_CHARS: Final = 1024
# 資料夾名稱最多 255 字，重名時小幫手會在後面加「 2」之類
MAX_OPEN_NAME_CHARS: Final = 300
MAX_RESULT_CODE_CHARS: Final = 64

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_HEX64_PATTERN: Final = r"^[0-9a-f]{64}$"
_NONCE_PATTERN: Final = r"^[A-Za-z0-9_-]{22}$"
_NO_CONTROL_CHARS: Final = r"^[^\x00-\x1f\x7f]+$"

# request_open 只從設定頁送（ADR-0320），模型的工具合約沒有這個動作
LocalAction = Literal[
    "list", "read", "create_folder", "create_file", "propose", "preview", "request_open"
]
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
    code: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=MAX_ERROR_MESSAGE_CHARS)
    data: dict[str, Any] | None = None


class LocalJobAck(_Strict):
    """確認收到。"""

    ok: bool = True


class LocalPairingCreated(_Strict):
    """配對碼，只回這一次，Touka 不留明文。"""

    code: str


class LocalHelperStatus(_Strict):
    """設定頁顯示的狀態。"""

    paired: bool
    online: bool
    helper_version: str | None = None


class LocalOpenRequest(_Strict):
    """設定頁請小幫手開放的資料夾路徑（ADR-0320）。路徑規則都由小幫手判斷，這裡只擋控制字元與長度。"""

    path: str = Field(min_length=1, max_length=MAX_OPEN_PATH_CHARS, pattern=_NO_CONTROL_CHARS)


class LocalOpenResult(_Strict):
    """開放請求的結果。``opened`` 才有名稱與權限；其他只有代碼，前端照代碼顯示。"""

    status: Literal["opened", "refused", "error"]
    code: str | None = Field(default=None, max_length=MAX_RESULT_CODE_CHARS)
    name: str | None = Field(default=None, max_length=MAX_OPEN_NAME_CHARS)
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
