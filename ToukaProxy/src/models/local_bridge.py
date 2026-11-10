"""本機小幫手通道的資料合約（ADR-0312）.

小幫手是另一支跑在使用者電腦上的程式，自己有一份 ``protocol.py``；測試核對兩邊相同。
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION: Final = 1
MAX_JOB_RESULT_BYTES: Final = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS: Final = 2000
MAX_REQUESTER_CHARS: Final = 128

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

LocalAction = Literal["list", "read", "create_folder", "create_file", "propose"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalPollRequest(_Strict):
    """小幫手來要下一件工作時帶的資料。"""

    protocol: int
    helper_version: str = Field(min_length=1, max_length=32)


class LocalJob(_Strict):
    """交給小幫手的一件工作。``decide_within`` 是小幫手自己要在幾秒內決定。"""

    id: str
    action: LocalAction
    args: dict[str, Any]
    requester: str = Field(max_length=MAX_REQUESTER_CHARS)
    decide_within: float


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


class LocalPairingRecord(_Strict):
    """配對檔的內容。解除配對時 ``token_hash``、``human_id`` 是空的，世代號繼續往上。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=0)
    token_hash: str | None
    human_id: str | None

    @field_validator("token_hash")
    @classmethod
    def _sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX.fullmatch(value) is None:
            msg = "token_hash must be a SHA-256 hex digest"
            raise ValueError(msg)
        return value
