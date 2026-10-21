"""ADR-0109：自動備份排程設定模型（持久化於 config/backup_schedule.json）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackupScheduleConfig(BaseModel):
    """自動備份排程策略 ＋ 狀態。

    使用者設定策略欄位（enabled/frequency/time_of_day/weekday/retention_count）；
    ``last_run_epoch`` 為排程器維護的狀態（上次成功自動備份的 epoch 秒），不由前端設定。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    frequency: Literal["daily", "weekly"] = "daily"
    # HH:MM（24 小時、台北時區）。
    time_of_day: str = Field(default="03:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # 0=週一 … 6=週日；僅 frequency=weekly 時生效。
    weekday: int = Field(default=0, ge=0, le=6)
    # 自動備份保留份數（只計自動備份；人工備份不受影響、不計入）。
    retention_count: int = Field(default=7, ge=1, le=100)
    # 狀態：上次成功自動備份的 epoch（秒）；None=尚未跑過。
    last_run_epoch: float | None = None
