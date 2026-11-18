"""插話標註（ADR-0249 裁定 7）：伺服器從 DB 組歷史給模型時，把插話放回那一則 AI 回覆。

對帶 ``interjections[]`` 的助理節點，在 content 前面加固定格式的標註（只取已送達、有文字的，
照步數排、每則一行，與原文以空行分隔）；角色仍是 assistant，回合數不變。
格式與前端 ``ToukaChat/src/lib/chat/interjections.ts`` 的 ``INTERJECTION_NOTE_TEMPLATE``
完全相同（跨端一致性測試以純文字比對）。純函式、不改傳入的節點。

被使用者停止（``generationStopped``）或出錯（``generationFailed``）的那一則，最前面再加一行
固定記號（ADR-0318）；SNS 送不出去（``deliveryFailed``，ADR-0322）的再加一行「沒有送到」。
和前端 ``ToukaChat/src/lib/chat/turnNotes.ts`` 同名常數相同。
"""

from __future__ import annotations

from typing import Any

# 給模型看的固定格式（不是畫面文字）；改動時前端同名常數要一起改
INTERJECTION_NOTE_TEMPLATE = "〔本回合中使用者補充（第 {step} 步）：{text}〕"
# 這一輪沒做完（ADR-0318）；同樣是給模型看的固定文字，前端同名常數要一起改
TURN_STOPPED_NOTE = "〔本回合被使用者停止，沒有做完〕"
TURN_FAILED_NOTE = "〔本回合出錯，沒有完成〕"
# SNS 回覆送不出去（ADR-0322 PR 2）：平台只用固定名稱；前端同名常數要一起改
TURN_UNDELIVERED_NOTE_TEMPLATE = "〔這則回覆沒有送到 {platform}〕"
DELIVERY_PLATFORM_NAMES = {"discord": "Discord", "telegram": "Telegram"}
OTHER_PLATFORM_NAME = "外部平台"


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


def _turn_end_note(node: dict[str, Any]) -> str | None:
    """被停止優先於出錯；只認 ``True``。"""
    if node.get("generationStopped") is True:
        return TURN_STOPPED_NOTE
    return TURN_FAILED_NOTE if node.get("generationFailed") is True else None


def undelivered_note(node: dict[str, Any]) -> str | None:
    """SNS 回覆送不出去（``deliveryFailed`` 只認 ``True``）的記號；平台名稱只用固定值。"""
    if node.get("deliveryFailed") is not True:
        return None
    platform = node.get("deliveryPlatform")
    name = DELIVERY_PLATFORM_NAMES.get(platform.lower()) if isinstance(platform, str) else None
    return TURN_UNDELIVERED_NOTE_TEMPLATE.replace("{platform}", name or OTHER_PLATFORM_NAME)


def _turn_notes(node: dict[str, Any]) -> str | None:
    """停止／出錯在前、沒有送到在後，一行一個。"""
    notes = [note for note in (_turn_end_note(node), undelivered_note(node)) if note]
    return "\n".join(notes) if notes else None


def annotate_node(node: dict[str, Any], *, tool_record: str = "") -> dict[str, Any]:
    """助理節點帶插話、沒做完、沒送到或有工具紀錄、且 content 是文字時，回傳加上標註的副本。

    其他節點原樣返回。順序：停止／出錯記號、沒有送到的記號、插話標註、工具紀錄
    （``tool_record``，ADR-0319，由 ``utils/tool_records.py`` 組好傳進來；預設不加）、原本的文字。
    """
    content = node.get("content")
    note = _turn_notes(node)
    if (
        node.get("role") != "assistant"
        or (note is None and not tool_record and not _delivered(node.get("interjections")))
        or not (content is None or isinstance(content, str))
    ):
        return node
    body = content or ""
    if tool_record:
        body = f"{tool_record}\n\n{body}" if body else tool_record
    text = with_interjection_notes(body, node.get("interjections"))
    if note is not None:
        text = f"{note}\n\n{text}" if text else note
    return {**node, "content": text}
