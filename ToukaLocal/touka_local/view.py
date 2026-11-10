"""畫面上要顯示的文字（ADR-0311）。邏輯都在這裡，測試不需要載入 tkinter。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from touka_local.paths import is_hidden_character

_VISIBLE_MARK: Final = "⍰"
_REASON_LIMIT: Final = 300
_SIZE_UNITS: Final = ("KB", "MB", "GB", "TB")

_ACTIONS: Final = {"move": "搬移", "delete": "丟到回收桶"}
_MARKS: Final = {
    "executable": "可執行檔",
    "hidden": "隱藏檔",
    "many_items": "項目很多",
}
_REPORT_LABELS: Final = (
    ("done", "完成"),
    ("changed", "檔案有變動跳過"),
    ("failed", "失敗"),
    ("aborted", "被中止"),
    ("not_done", "沒做"),
)
_CONNECTION: Final = {
    "starting": "連線中",
    "online": "已連線",
    "offline": "連不上 Touka",
    "unpaired": "尚未配對",
    "upgrade_needed": "版本不符，請重新安裝小幫手",
}
_REFUSALS: Final = {
    "bad_path": "這個路徑的寫法不合規則",
    "protected": "這是受保護的資料夾，不能開放或更動",
    "link": "路徑上有連結或雲端佔位檔，為了安全不能使用",
    "git_project": "這是 Git 專案（或在專案裡面），不能開放或更動",
    "drive_type": "只能開放這台電腦的固定磁碟",
    "overlap": "和已經開放的資料夾重疊",
    "blocked": "這個位置不能開放（系統資料夾、Touka 資料夾、使用者資料夾本身或磁碟根目錄）",
    "system_folder": "這是系統資料夾，不能開放",
    "not_found": "找不到這個資料夾",
    "not_a_folder": "選的不是資料夾",
    "bad_port": "連接埠要是 1 到 65535 的數字",
    "bad_token": "配對碼格式不對，請從 Touka 的設定頁重新複製",
    "approval_busy": "已經有一份清單在等你決定",
    "approval_cooldown": "剛拒絕過一份清單，請稍等幾秒",
    "too_soon": "請先看過清單，再按核准",
    "expired": "這份清單已經逾時",
    "changed": "檔案有變動，請重新提出",
    "no_pending": "這份清單已經不在等待中",
    "busy": "正在執行另一批，請等它做完",
}
_REFUSAL_FALLBACK: Final = "這個動作沒有完成"


@dataclass(frozen=True)
class ApprovalRow:
    """核准清單的一列。"""

    action: str
    source: str
    destination: str
    detail: str
    marks: str


@dataclass(frozen=True)
class ApprovalCounts:
    """核准清單的統計。"""

    moves: int
    deletes: int
    unusual: int


def display_safe(text: str) -> str:
    """把看不見或會改變顯示方向的字元換成看得見的記號，字數不變。"""
    return "".join(_VISIBLE_MARK if is_hidden_character(c) else c for c in text)


def clean_reason(text: str) -> str:
    """代理人寫的說明：併成一行，換掉看不見的字元，限制長度。"""
    return display_safe(" ".join(text.split()))[:_REASON_LIMIT]


def human_size(size: int) -> str:
    """人看得懂的大小。"""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in _SIZE_UNITS:
        value /= 1024
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{value:.1f} {unit}"
    return f"{size} B"


def _detail(item: Any) -> str:
    if item.is_folder:
        return f"資料夾，{item.contained} 項，{human_size(item.size)}"
    return human_size(item.size)


def approval_rows(plan: Any) -> list[ApprovalRow]:
    """把清單變成表格的列。"""
    return [
        ApprovalRow(
            action=_ACTIONS.get(item.op, item.op),
            source=display_safe(item.source_text),
            destination=display_safe(item.destination_text or ""),
            detail=_detail(item),
            marks="、".join(_MARKS.get(mark, mark) for mark in item.unusual),
        )
        for item in plan.items
    ]


def approval_counts(plan: Any) -> ApprovalCounts:
    """統計搬移、刪除和不尋常的項目。"""
    return ApprovalCounts(
        moves=sum(1 for item in plan.items if item.op == "move"),
        deletes=sum(1 for item in plan.items if item.op == "delete"),
        unusual=sum(1 for item in plan.items if item.unusual),
    )


def approval_title(counts: ApprovalCounts) -> str:
    """清單的標題。"""
    parts = []
    if counts.moves:
        parts.append(f"搬移 {counts.moves} 項")
    if counts.deletes:
        parts.append(f"丟到回收桶 {counts.deletes} 項")
    return "、".join(parts)


def report_text(report: dict[str, Any]) -> str:
    """執行結果的一句話。"""
    statuses = [item["status"] for item in report["items"]]
    return "，".join(
        f"{label} {statuses.count(status)} 項"
        for status, label in _REPORT_LABELS
        if statuses.count(status)
    )


def connection_text(state: str) -> str:
    """連線狀態。"""
    return _CONNECTION.get(state, state)


def refusal_text(code: str) -> str:
    """把拒絕代碼換成給人看的話。"""
    return _REFUSALS.get(code, _REFUSAL_FALLBACK)
