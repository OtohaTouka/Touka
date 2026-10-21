"""可觀測性端點的結構化回應模型。"""

from typing import Literal

from pydantic import BaseModel

IsolationLevelName = Literal["none", "landlock", "executor"]


class SystemNotice(BaseModel):
    """畫面提示（``/health`` 的 ``system_notices``）：只有代碼與等級，文字由前端翻譯。"""

    code: str
    level: Literal["info", "warning"]


class HealthResponse(BaseModel):
    """基礎存活狀態、權限設定熱重載健康度與通用提示。"""

    status: Literal["healthy", "degraded"]
    timestamp: str
    permission_config_degraded: bool
    permission_config_degraded_reason: str
    system_notices: list[SystemNotice]


class SystemHealthResponse(HealthResponse):
    """系統資源狀態。"""

    cpu_usage: float
    memory_usage: float
    disk_usage: float


class IsolationLocalStatus(BaseModel):
    """proxy 本機 Landlock：自測狀態、ABI、原因代碼與網路設定問題（只有代碼）。"""

    status: Literal["pending", "available", "unavailable"]
    abi: int
    reason: str | None
    net: str | None


class IsolationExecutorStatus(BaseModel):
    """可用的執行器：只有 ABI 與網路設定問題；roots、state_root、digest、boot_id 一律不帶。"""

    abi: int
    net: str | None


class IsolationRepairs(BaseModel):
    """自癒計數（U2n ``PermissionRepairService.snapshot()``）：只有計數與旗標，沒有路徑與代理人 ID。

    ``permission_relock``：這一分鐘的修復額度用完（有代理人反覆鎖權限）；
    ``denied_burst``：短時間大量被拒（只記 log、不上橫幅）；
    ``window_resets_at``：時間窗結束的 epoch 秒。
    """

    attempted: int
    repaired: int
    refused_outside: int
    refused_protected: int
    refused_not_owner: int
    nothing_changed: int
    throttled: int
    retried: int
    retry_failed: int
    permission_relock: bool
    denied_burst: bool
    window_resets_at: float


class IsolationMcpStatus(BaseModel):
    """MCP 隔離計數與設定檔讀取狀態，不帶伺服器名稱或路徑。"""

    unisolated: int
    waiting: int
    suspended: int
    store_issue: str | None


class IsolationStatusResponse(BaseModel):
    """``GET /isolation``：monitor 的判定結果，只有層級與原因代碼（給側欄橫幅用）。

    ``local_issue``：讓降級只能「未隔離」的本機原因；``block_ports_issue``：封鎖埠擋不全；
    ``repairs``：自癒沒有在跑（不是 Linux）或 snapshot 還沒接上時是 null。
    """

    level: IsolationLevelName
    expected: IsolationLevelName
    reason: str | None
    detail: str | None
    features: list[str]
    local: IsolationLocalStatus
    local_issue: str | None
    executor: IsolationExecutorStatus | None
    block_ports_issue: str | None
    repairs: IsolationRepairs | None
    mcp: IsolationMcpStatus
