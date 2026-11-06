"""依日期、內容與角色查自己的 UI／legacy 對話；結果包在歷史資料界線裡。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_resource_proxy
from memory_server.models.conversation_search import sort_conversations
from memory_server.utils.historical_data import wrap_historical_data
from memory_server.utils.legacy_time import format_taipei, seconds_range_to_ms
from memory_server.utils.memory_worker import run_memory_job

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.models.conversation_search import ConversationRow
    from memory_server.services.manager import MemoryManager

LEGACY_CONVERSATION_SOURCE = "legacy_conversation"
# 每則只給開頭一段；標題另外截短
PREVIEW_CHARS = 300
TITLE_CHARS = 80
# 合併查詢向每個來源要 offset＋page_size 筆；夾在 SQLite 整數上限內，極大 offset 回空頁而不是錯誤。
SQLITE_MAX_INTEGER = 2**63 - 1
_ROLE_LABELS = {"user": "使用者", "assistant": "你"}
_TIME_NOTE = "時間說明：舊對話庫裡原本沒有時間的訊息，時間記成匯入那天。"
_UI_NOTE = "UI 時間來自對話紀錄，可能是裝置時鐘。按對話歸屬整理，不代表逐則作者已驗證。"


async def search_conversations(
    identity: AgentIdentity,
    manager: MemoryManager,
    query: str = "",
    date_start: str | None = None,
    date_end: str | None = None,
    role: str | None = None,
    page_size: int = 10,
    offset: int = 0,
    source: str | None = None,
    **kwargs: Any,
) -> str:
    """每來源先取足候選，合併後才套公開 offset；任一來源失敗不回部分結果。"""
    _ = kwargs
    source = source or "all"
    start_s, end_s = manager.normalize_time_range(date_start, date_end)
    start_ms, end_ms = seconds_range_to_ms(start_s, end_s)
    oldest_first = bool(date_start or date_end)
    parameters = {
        "query": query,
        "role": role,
        "date_start": start_ms,
        "date_end": end_ms,
        "oldest_first": oldest_first,
        "limit": min(offset + page_size, SQLITE_MAX_INTEGER) if source == "all" else page_size,
        "offset": 0 if source == "all" else offset,
    }
    pages = []
    if source != "legacy":
        pages.append(await get_resource_proxy().query_ui_conversations(identity, **parameters))
    if source != "ui":
        pages.append(  # 舊對話庫在記憶工作者上讀；UI 對話要 await，留在主迴圈
            await run_memory_job(
                lambda: manager.get_legacy_sync_service()
                .get_store(identity)
                .query_messages(**parameters)
            )
        )
    total = sum(page.total for page in pages)
    unknown = sum(page.unknown_time_count for page in pages)
    rows = sort_conversations([row for page in pages for row in page.rows], oldest_first)
    if source == "all":
        rows = rows[offset : offset + page_size]
    span = f"{date_start or '最早'}～{date_end or '最新'}（Asia/Taipei）"
    order = "舊到新" if oldest_first else "新到舊"
    label = {
        "legacy": "舊對話庫（legacy）",
        "ui": "聊天對話（ui）",
        "all": "舊對話庫（legacy）與聊天對話（ui）",
    }[source]
    notes = [_TIME_NOTE] if source == "legacy" else [_UI_NOTE]
    if source == "all":
        notes.append(_TIME_NOTE)
    if oldest_first:
        notes.append(f"可見且符合 role／query，但無法按日期判定：{unknown}")
    header = f"已查：{label}｜日期：{span}｜共 {total} 則，{order}\n" + "\n".join(notes)
    if not rows:
        if total == 0:
            return f"{header}\n沒有符合的{'舊對話' if source == 'legacy' else '對話'}。"
        return f"{header}\n這一頁沒有訊息（offset={offset} 超過總數）。"
    lines = [_format_row(offset + number, row) for number, row in enumerate(rows, 1)]
    tag = {
        "legacy": LEGACY_CONVERSATION_SOURCE,
        "ui": "chat_conversation",
        "all": "conversation_search",
    }[source]
    body = wrap_historical_data(tag, "\n".join(lines))
    end = offset + len(rows)
    more = f"\n還有 {total - end} 則：用 offset={end} 看下一頁。" if total > end else ""
    return f"{header}\n這頁是第 {offset + 1}–{end} 則。\n{body}{more}"


def _format_row(number: int, row: ConversationRow) -> str:
    """一則：時間（台北）、誰說的、所屬對話標題、內容開頭."""
    when = format_taipei(row.timestamp_ms, unknown="未知時間")
    speaker = (
        "助理"
        if row.source == "ui" and row.role == "assistant"
        else _ROLE_LABELS.get(row.role, row.role)
    )
    title = str(row.title or "未命名")[:TITLE_CHARS]
    content = row.content
    preview = content[:PREVIEW_CHARS] + ("…" if len(content) > PREVIEW_CHARS else "")
    return f"{number}. ({when}) [{speaker}] [{row.source}] 對話「{title}」\n   「{preview}」"
