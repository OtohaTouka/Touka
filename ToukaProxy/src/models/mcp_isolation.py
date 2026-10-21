"""MCP 隔離設定的 API 合約（ADR-0275）：來源（origin）與額外允許的資料夾。

這兩樣不放在 ``server_config.json``（格式不變），另存在 MCP 隔離設定檔
（``services/mcp_isolation_store``）；設定頁經 ``GET``／``PUT /mcp/{name}/isolation``
讀寫，手動啟動經重啟端點帶同意。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SANDBOX_PATHS = 16
"""每台最多幾筆額外允許的資料夾（禁令 18 的資源上限）。"""
MAX_RESTART_FEATURES = 16

BOUND_PATTERN = r"^[0-9a-f]{64}$"
"""``bound``：條目程式身分（command／args／url，不含 env）的 sha256。"""

McpOrigin = Literal["hub", "skill", "manual", "creation", "scaffold", "evolution", "agent"]
McpRecordState = Literal["ok", "none", "stale", "unreadable"]
McpIsolationClass = Literal["self", "third_party", "scaffold", "remote"]
McpFallback = Literal["executor", "landlock", "direct"]


class SandboxPathGrant(BaseModel):
    """人給第三方 MCP 的額外允許資料夾；存檔與啟動都要再過一次永不授權。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)
    access: Literal["ro", "rw"]

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        """只收絕對路徑；相對路徑會隨工作目錄改變指到的地方。"""
        if "\x00" in value or not Path(value).is_absolute():
            msg = "sandbox path must be an absolute path"
            raise ValueError(msg)
        return value


class McpIsolationView(BaseModel):
    """``GET``／``PUT /mcp/{name}/isolation`` 的回應：只有代碼，沒有原因原文。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    server: str
    isolation_class: McpIsolationClass = Field(alias="class")
    origin: McpOrigin | None
    record_state: McpRecordState
    sandbox_paths: list[SandboxPathGrant]
    bound: str
    can_mark_agent: bool


class McpIsolationUpdateResponse(McpIsolationView):
    """``PUT`` 的回應：已存檔，背景重新判定並連線。"""

    applies_on: Literal["background_relaunch"] = "background_relaunch"


class McpIsolationUpdateRequest(BaseModel):
    """``PUT /mcp/{name}/isolation``：``bound`` 必須等於目前條目的，來源只能改成 ``agent``。"""

    model_config = ConfigDict(extra="forbid")

    bound: str = Field(pattern=BOUND_PATTERN)
    origin: Literal["agent"] | None = None
    sandbox_paths: list[SandboxPathGrant] | None = Field(default=None, max_length=MAX_SANDBOX_PATHS)


class McpConfigSaveResponse(BaseModel):
    """``POST /mcp/ui/config`` 的回應；``isolation_cleared``：程式變了、資料夾被清掉的伺服器。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    isolation_cleared: list[str] = Field(default_factory=list)


class McpRestartRequest(BaseModel):
    """``POST /mcp/{name}/restart`` 的選填 body。

    帶 ``isolation_ack`` 時，``features``、``generation``、``bound`` 都必填，代表這次同意。
    """

    model_config = ConfigDict(extra="forbid")

    isolation_ack: McpFallback | None = None
    features: list[str] | None = Field(default=None, max_length=MAX_RESTART_FEATURES)
    generation: int | None = Field(default=None, ge=0)
    bound: str | None = Field(default=None, pattern=BOUND_PATTERN)

    @model_validator(mode="after")
    def ack_needs_generation_and_bound(self) -> McpRestartRequest:
        """同意要綁定設定版本與條目內容，缺一不可。"""
        if self.isolation_ack is not None and (
            self.generation is None or self.bound is None or self.features is None
        ):
            msg = "isolation_ack requires generation, bound and features"
            raise ValueError(msg)
        return self
