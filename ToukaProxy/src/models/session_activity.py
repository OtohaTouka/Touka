"""GET /v1/ui/activity 的公開合約（ADR-0300）：只有對話編號與進度，不帶標題、內容與靈魂。"""

from pydantic import BaseModel, ConfigDict


class ActivityModel(BaseModel):
    """白名單輸出：多的欄位一律擋下，不會順手帶出去。"""

    model_config = ConfigDict(extra="forbid")


class BackgroundActivity(ActivityModel):
    """正在跑的背景工作（心跳第幾輪）。"""

    session_id: str
    kind: str
    current: int
    total: int


class FinishedActivity(ActivityModel):
    """``since`` 之後結束的心跳。"""

    session_id: str
    finished_at: int


class SessionActivityResponse(ActivityModel):
    """側欄的工作狀態；``cursor`` 是下一次查詢帶的 ``since``。"""

    streams: list[str]
    background: list[BackgroundActivity]
    finished: list[FinishedActivity]
    cursor: int
