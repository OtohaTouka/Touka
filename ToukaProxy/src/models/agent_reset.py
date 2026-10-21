"""每靈魂資料重置的回應模型（`POST /admin/agents/{target}/reset`）."""

from pydantic import BaseModel, Field
from src.models.memory_scrub import MemoryScrubReport


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
