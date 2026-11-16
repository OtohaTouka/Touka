"""用戶端原本送來的 system 說明（ADR-0316）。

流程入口記下每一則 system 的原內容；套用靈魂設定時，目前 system 去掉這段原字串，
剩下的就是系統自己加的說明（專案、工具、看板等），一律保留，不靠字母或標籤猜。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ctx 裡存放用戶端原 system 的鍵（pipeline 入口每次都覆寫）
CLIENT_SYSTEM_KEY = "client_system"

ClientSystem = tuple[str, ...]


def _text_of(content: Any) -> str:
    """一則 system 的文字：字串照用，list 取各段的 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def system_entries(request: Any) -> list[str]:
    """目前每一則 system 的文字（Anthropic 的 list 每段算一則）。"""
    system = getattr(request, "system", None)
    if isinstance(system, str):
        return [system] if system else []
    if isinstance(system, list):
        return [_text_of([part]) for part in system if isinstance(part, dict)]
    entries: list[str] = []
    for message in getattr(request, "messages", None) or []:
        is_dict = isinstance(message, dict)
        role = message.get("role") if is_dict else getattr(message, "role", "")
        if role == "system":
            content = message.get("content") if is_dict else getattr(message, "content", "")
            entries.append(_text_of(content))
    return entries


def capture(request: Any) -> ClientSystem:
    """記下用戶端原本的 system（複製成字串，之後原地加段也不影響）。"""
    return tuple(system_entries(request))


def split(request: Any, original: ClientSystem | None) -> tuple[list[str], list[str]]:
    """把目前的 system 分成（用戶端原本的內容, 系統自己加的說明）。

    沒有記下原內容（``None``）時全部當用戶端內容，照舊判斷。某一則已不是以原內容開頭時，
    整則當用戶端內容（往安全的方向錯），並記一筆 log。
    """
    current = system_entries(request)
    if original is None:
        return current, []
    client: list[str] = []
    added: list[str] = []
    for index, text in enumerate(current):
        if index >= len(original):
            added.append(text)
            continue
        base = original[index]
        if text.startswith(base):
            client.append(base)
            added.append(text[len(base):])
        else:
            logger.warning(
                "client system entry %d changed before the soul was applied; "
                "judging the whole entry as client text",
                index,
            )
            client.append(text)
    return client, [part for part in added if part]
