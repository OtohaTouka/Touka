"""插話 API 合約（ADR-0249 第 4a 段）。

請求只有文字與 urgent：沒有任何核准、模式或工具欄位，多送的欄位一律拒絕。
插話不能核准動作、不能改模式，由這個結構保證。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InterjectionStatus = Literal["queued", "pending", "delivered", "undelivered", "retracted"]


class InterjectionCreateRequest(BaseModel):
    """加入插話；長度上限由佇列檢查（``INTERJECTION_MAX_CHARS``）。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description="使用者的補充文字")
    urgent: bool = Field(default=False, description="Ctrl+Enter：這一步做完就先處理")


class Interjection(BaseModel):
    """一則插話。``step`` 是交給模型的工具步數，還沒交出時為 None。"""

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    urgent: bool = False
    status: InterjectionStatus
    step: int | None = None
    created_at: float


class InterjectionListResponse(BaseModel):
    """目前串流的插話（照加入順序）。"""

    interjections: list[Interjection]
