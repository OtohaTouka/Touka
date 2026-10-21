"""已授權對話的純投影；只查目前祖先鏈的 user／assistant 文字。"""

import json
from typing import Any

from memory_server.models.conversation_search import (
    ConversationPage,
    ConversationRow,
    ConversationSearchError,
    sort_conversations,
)
from memory_server.utils.legacy_time import to_epoch_ms

from src.models.history import TreeHistory

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def project_conversation(row: dict[str, Any]) -> list[ConversationRow]:
    """先驗樹型別，再驗目前鏈；斷鏈、循環及節點 ID 矛盾都不能當成空。"""
    tree = TreeHistory.model_validate(json.loads(row["messages"]))
    current = tree.currentId
    seen: set[str] = set()
    messages = []
    while current is not None:
        if current in seen or current not in tree.messages:
            raise ConversationSearchError("internal_error")
        seen.add(current)
        node = tree.messages[current]
        if node.id != current:
            raise ConversationSearchError("internal_error")
        if node.role in {"user", "assistant"} and isinstance(node.content, str):
            messages.append(
                ConversationRow(
                    source="ui",
                    message_id=node.id,
                    session_id=row["id"],
                    timestamp_ms=to_epoch_ms(node.timestamp),
                    role=node.role,
                    content=node.content,
                    title=row["title"],
                )
            )
        current = node.parentId
    return list(reversed(messages))


def conversation_page(
    rows: list[ConversationRow],
    query: str,
    role: str | None,
    date_start: int | None,
    date_end: int | None,
    oldest_first: bool,
    limit: int,
    offset: int,
) -> ConversationPage:
    """文字比對沿 SQLite LIKE 的 ASCII 大小寫語意，先篩完整文字再切頁。"""
    needle = query.translate(_ASCII_LOWER)
    matching = []
    unknown = 0
    for row in rows:
        if (role is not None and row.role != role) or needle not in row.content.translate(
            _ASCII_LOWER
        ):
            continue
        timestamp = row.timestamp_ms
        if timestamp is None:
            unknown += 1
            if date_start is not None or date_end is not None:
                continue
        elif (date_start is not None and timestamp < date_start) or (
            date_end is not None and timestamp > date_end
        ):
            continue
        matching.append(row)
    ordered = sort_conversations(matching, oldest_first)
    return ConversationPage(len(ordered), ordered[offset : offset + limit], unknown)
