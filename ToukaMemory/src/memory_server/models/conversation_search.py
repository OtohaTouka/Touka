"""跨來源對話搜尋的內部資料與固定錯誤；不攜帶儲存路徑。"""

from dataclasses import dataclass
from typing import Literal


class ConversationSearchError(Exception):
    """來源失敗不可退回部分結果，也不可回顯原始例外。"""

    def __init__(self, reason: Literal["permission_denied", "internal_error"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ConversationRow:
    """只保留可呈現的文字欄位。"""

    source: Literal["legacy", "ui"]
    message_id: str
    session_id: str
    timestamp_ms: int | None
    role: str
    content: str
    title: str | None


@dataclass(frozen=True)
class ConversationPage:
    """總數在切頁前計算；未知時間數只計符合角色及文字的訊息。"""

    total: int
    rows: list[ConversationRow]
    unknown_time_count: int = 0


def sort_conversations(rows: list[ConversationRow], oldest_first: bool) -> list[ConversationRow]:
    """穩定排序依次覆蓋次要鍵；未知時間在兩種方向都排最後。"""
    ordered = sorted(rows, key=lambda row: (row.source != "legacy", row.session_id))
    ordered.sort(key=lambda row: (row.timestamp_ms or 0, row.message_id), reverse=not oldest_first)
    ordered.sort(key=lambda row: row.timestamp_ms is None)
    return ordered
