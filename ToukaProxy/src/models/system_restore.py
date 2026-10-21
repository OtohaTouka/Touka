"""還原狀態模型（iso-c1 H1：還原失敗時讓畫面知道）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RestoreState = Literal["idle", "running", "failed"]


class RestoreStatus(BaseModel):
    """最後一次還原背景工作的結果，只存在記憶體。

    成功時伺服器會結束進程重啟，紀錄跟著消失（新進程回 ``idle``）；失敗時進程還活著、
    紀錄留著。刻意只有狀態與錯誤碼，不放路徑或例外原文（禁令 23）。
    """

    model_config = ConfigDict(extra="forbid")

    state: RestoreState = Field(
        ..., description="idle＝本次啟動後沒有還原紀錄；running＝還原進行中；failed＝還原失敗"
    )
    error_code: str | None = Field(default=None, description="失敗時的錯誤碼（errors.json 的鍵）")
