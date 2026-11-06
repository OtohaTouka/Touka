"""每靈魂資料重置的回應模型（重置端點與 `GET /admin/agents/reset/last`）."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from src.models.memory_scrub import MemoryScrubReport

ResetState = Literal["idle", "running", "done", "failed", "interrupted"]


class BackupResult(BaseModel):
    """重置前那份系統快照：只有狀態與檔名，不放腳本輸出（有絕對路徑）."""

    status: str
    message: str
    filename: str | None = None


class ResetStoreResult(BaseModel):
    """單一 store 的清除數與殘留."""

    name: str
    purged: int
    remaining: int
    error: str


class ResetRetained(BaseModel):
    """刻意保留、不清除的 store."""

    name: str
    reason: str


class AgentResetResponse(BaseModel):
    """重置報告；勾選清除連備份時多一個 scrub."""

    agent_id: str
    backup: BackupResult | None = None
    clean: bool
    stores: list[ResetStoreResult] = Field(default_factory=list)
    retained: list[ResetRetained] = Field(default_factory=list)
    scrub: MemoryScrubReport | None = None


class LastResetStatus(BaseModel):
    """最後一次重置的結果，只存在行程記憶體（舊審查報告丙-5，ADR-0289）.

    伺服器重啟後回 ``idle``。失敗只記錯誤碼，不放路徑或例外原文（禁令 23）。
    """

    model_config = ConfigDict(extra="forbid")

    state: ResetState = Field(
        ...,
        description=(
            "idle＝本次啟動後沒有重置紀錄；running＝進行中（請求斷線後清除還在跑也算）；"
            "done＝已結束，附報告；failed＝沒有完成，附錯誤碼；interrupted＝請求中途取消，"
            "可能只清了一部分"
        ),
    )
    agent_id: str | None = Field(default=None, description="重置的靈魂")
    started_at: float | None = Field(default=None, description="開始時間（UNIX 秒）")
    finished_at: float | None = Field(default=None, description="結束時間（UNIX 秒）")
    error_code: str | None = Field(default=None, description="失敗時的錯誤碼（errors.json 的鍵）")
    report: AgentResetResponse | None = Field(default=None, description="done 時的重置報告")
