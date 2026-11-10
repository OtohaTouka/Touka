"""模型快下架提醒的對外合約（ADR-0310）。"""

from typing import Literal

from pydantic import BaseModel, Field

# retiring＝30 天內；overdue＝官方排定的日期已過（模型可能還能用）；
# date_unknown＝已標停用、沒有日期；retired＝官方標示已下架；
# not_found＝實際呼叫時廠商回「找不到模型」。
ModelLifecycleStatus = Literal["retiring", "overdue", "date_unknown", "retired", "not_found"]


class ModelLifecycleAlert(BaseModel):
    """一個在用的模型要提醒的事。"""

    model: str
    status: ModelLifecycleStatus
    retires_at: int | None = Field(default=None, description="官方的下架時間（UNIX 秒）")
    souls: list[str] = Field(default_factory=list, description="呼叫者看得到、用這個模型的靈魂")
    reviewer: bool = Field(default=False, description="Auto 審查員在用（只回給人類管理員）")
    summarizer: bool = Field(default=False, description="摘要員在用（只回給人類管理員）")


class ModelLifecycleAlertsResponse(BaseModel):
    """GET /v1/models/lifecycle-alerts 的回應。"""

    alerts: list[ModelLifecycleAlert]
    checking: bool = Field(default=False, description="背景正在查廠商清單，等一下再問會有新結果")
