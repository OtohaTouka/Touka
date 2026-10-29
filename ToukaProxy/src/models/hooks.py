"""Hook 設定模型（ADR-0292）.

唯一物理來源為 ``config/hooks.json``，只有管理員能經 ``/hooks`` 改；代理人的工具碰不到。
格式照 Claude Code 的 ``hooks`` 區塊：``{事件: [{matcher, hooks: [handler]}]}``，差別：

- 只收已接線的事件（``WIRED_EVENTS``），其他名稱或拼錯一律拒收，不會「寫了卻不觸發」。
- handler 只有 ``command`` 與 ``http``；``command`` 是程式＋參數清單，不經 shell
  （沒有 ``shell``、``if``、``async`` 欄位）。
- ``http`` 的標頭照字面送出；寫成 ``$VAR``／``${VAR}`` 會被拒收
  （不替換環境變數，寫了會誤以為有效）。
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.models.permissions import BasePolicyModel

# 丙-1 接線的四個事件；丙-2 接其餘事件時往這裡加（模型只收已接線的事件）。
WIRED_EVENTS: Final[tuple[str, ...]] = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
# 照 Claude Code：這兩個事件看工具名稱；UserPromptSubmit 與 Stop 不看 matcher。
MATCHER_EVENTS: Final[frozenset[str]] = frozenset({"PreToolUse", "PostToolUse"})

MAX_HANDLERS_PER_EVENT: Final[int] = 20
DEFAULT_HOOK_TIMEOUT_SECONDS: Final[int] = 30
MAX_HOOK_TIMEOUT_SECONDS: Final[int] = 300
MAX_HOOK_ARGS: Final[int] = 64

HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]

_ENV_REFERENCE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
# HTTP 標頭名稱（RFC 9110 的 token）；Touka 自己送的標頭不讓設定改（比對不分大小寫）
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_RESERVED_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-type", "content-length", "host", "transfer-encoding", "accept-encoding"}
)


class HookConfigError(ValueError):
    """hooks.json 內容不合規則（Pydantic 會轉成 ValidationError）。"""


class _HandlerBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=120, description="設定頁顯示的名稱")
    enabled: bool = True
    timeout: int = Field(
        DEFAULT_HOOK_TIMEOUT_SECONDS, ge=1, le=MAX_HOOK_TIMEOUT_SECONDS, description="秒"
    )


class CommandHook(_HandlerBase):
    """執行一個程式：``command`` 是絕對路徑，``args`` 原樣交給作業系統，事件內容從標準輸入給。"""

    type: Literal["command"]
    command: str = Field(..., min_length=1, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=MAX_HOOK_ARGS)

    @field_validator("command", "args")
    @classmethod
    def _no_nul(cls, value: str | list[str]) -> str | list[str]:
        items = [value] if isinstance(value, str) else value
        if any("\x00" in item for item in items):
            msg = "hook command and args must not contain NUL"
            raise HookConfigError(msg)
        return value


class HttpHook(_HandlerBase):
    """POST 事件內容到 ``url``；不跟隨轉址，標頭照字面送出。"""

    type: Literal["http"]
    url: str = Field(..., min_length=1, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        parts = urlsplit(value)
        # 埠號不是 1–65535 時，讀 parts.port 會丟 ValueError（存檔時回驗證錯誤，不會留到執行時）
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.port == 0:
            msg = "hook url must be http or https with a host"
            raise HookConfigError(msg)
        return value

    @field_validator("headers")
    @classmethod
    def _literal_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header in value.items():
            if any(ch in name + header for ch in "\r\n\x00") or not name.strip():
                msg = "hook header names and values must be single-line text"
                raise HookConfigError(msg)
            if not _HEADER_NAME.fullmatch(name):
                msg = "hook header names may not contain spaces, colons or other separators"
                raise HookConfigError(msg)
            if name.lower() in _RESERVED_HEADERS:
                msg = f"hook header {name} is set by Touka and can not be changed"
                raise HookConfigError(msg)
            if _ENV_REFERENCE.search(header):
                msg = "hook headers are sent literally; environment references are not supported"
                raise HookConfigError(msg)
        return value


HookHandler = Annotated[CommandHook | HttpHook, Field(discriminator="type")]


class HookMatcherGroup(BaseModel):
    """一組比對規則與它的 handler；比對規則見 ``services/hooks/matcher``。"""

    model_config = ConfigDict(extra="forbid")

    matcher: str = Field("", max_length=1024)
    hooks: list[HookHandler] = Field(..., min_length=1)

    @field_validator("matcher")
    @classmethod
    def _regex_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"hook matcher is not a valid regular expression: {exc}"
            raise HookConfigError(msg) from exc
        return value


class HooksConfig(BasePolicyModel):
    """整份 hook 設定；預設關閉，打開後依事件執行。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    hooks: dict[HookEvent, list[HookMatcherGroup]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _limit_handlers(self) -> HooksConfig:
        for event, groups in self.hooks.items():
            if sum(len(group.hooks) for group in groups) > MAX_HANDLERS_PER_EVENT:
                msg = f"at most {MAX_HANDLERS_PER_EVENT} hooks per event ({event})"
                raise HookConfigError(msg)
        return self

    def groups_for(self, event: str) -> list[HookMatcherGroup]:
        """該事件的設定（關閉時回空清單）。"""
        if not self.enabled:
            return []
        return list(self.hooks.get(event, []))


# --- 管理 API（/hooks）的請求與回應 -------------------------------------------------------


class HookHealthOut(BaseModel):
    """設定檔狀態：``revision`` 是目前檔案內容的 sha256，存檔時原樣送回（沒有檔案時 null）。"""

    status: Literal["ok", "missing", "error"]
    revision: str | None = None
    detail: str | None = None


class HooksConfigResponse(BaseModel):
    """目前生效的設定（設定檔壞掉時是上一份好的）與設定檔狀態。"""

    config: HooksConfig
    health: HookHealthOut
    wired_events: list[HookEvent]
    matcher_events: list[HookEvent]


class HooksSaveRequest(BaseModel):
    """整份設定與載入時的 ``health.revision``；revision 必填（沒有檔案時明確送 null）。"""

    model_config = ConfigDict(extra="forbid")

    config: HooksConfig
    revision: str | None = Field(..., max_length=64)


class HooksSaveResponse(BaseModel):
    """存檔後的新 revision。"""

    revision: str


class HookRunOut(BaseModel):
    """一筆執行紀錄；無痕對話只有時間、事件與結果。"""

    at: float
    event: str
    hook: str
    hook_type: str
    status: str
    outcome: Literal["blocked", "ask", "context", "none", "failed"]
    duration_ms: int
    exit_code: int | None = None
    http_status: int | None = None
    detail: str | None = None
    incognito: bool = False


class HookRunsResponse(BaseModel):
    """最近的執行紀錄，新的在前（只在記憶體，重啟後清空）。"""

    runs: list[HookRunOut]
