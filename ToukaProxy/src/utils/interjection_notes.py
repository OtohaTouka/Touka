"""插話標註（ADR-0249 裁定 7）：伺服器從 DB 組歷史給模型時，把插話放回那一則 AI 回覆。

對帶 ``interjections[]`` 的助理節點，在 content 前面加固定格式的標註（只取已送達、有文字的，
照步數排、每則一行，與原文以空行分隔）；角色仍是 assistant，回合數不變。
格式與前端 ``ToukaChat/src/lib/chat/interjections.ts`` 的 ``INTERJECTION_NOTE_TEMPLATE``
完全相同（跨端一致性測試以純文字比對）。純函式、不改傳入的節點。
"""

from __future__ import annotations

from typing import Any

# 給模型看的固定格式（不是畫面文字）；改動時前端同名常數要一起改
INTERJECTION_NOTE_TEMPLATE = "〔本回合中使用者補充（第 {step} 步）：{text}〕"


def format_interjection_note(step: int, text: str) -> str:
    """一則插話的標註。先代入步數再代入文字，文字裡的大括號原樣保留。"""
    return INTERJECTION_NOTE_TEMPLATE.replace("{step}", str(step), 1).replace("{text}", text, 1)


def _delivered(interjections: Any) -> list[dict[str, Any]]:
    items = interjections if isinstance(interjections, list) else []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("status") == "delivered"
        and isinstance(item.get("step"), int)
        and not isinstance(item.get("step"), bool)
        and isinstance(item.get("text"), str)
        and item["text"]
    ]


def with_interjection_notes(content: str, interjections: Any) -> str:
    """已送達的插話照步數排在 content 前面；沒有就原樣返回。"""
    notes = [
        format_interjection_note(item["step"], item["text"])
        for item in sorted(_delivered(interjections), key=lambda item: item["step"])
    ]
    return "\n".join(notes) + "\n\n" + content if notes else content


def annotate_node(node: dict[str, Any]) -> dict[str, Any]:
    """助理節點帶插話且 content 是文字時，回傳加上標註的副本；其他節點原樣返回。"""
    content = node.get("content")
    if (
        node.get("role") != "assistant"
        or not _delivered(node.get("interjections"))
        or not (content is None or isinstance(content, str))
    ):
        return node
    return {**node, "content": with_interjection_notes(content or "", node["interjections"])}
