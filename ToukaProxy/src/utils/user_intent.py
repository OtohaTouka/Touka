"""使用者原意（ADR-0246 U2c-2）：給 Auto 審查員當證據的資料，不是指令。

純函式＋常數（禁令 18）。只產生、不送出、不落地：結果只放在記憶體的 ctx（無痕也一樣）。
只收人類原話（禁令 64 ③）：SNS 外人訊息、前端內嵌的附件、子代理的派工目標都不算（R-2c）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

INTENT_MAX_MESSAGES = 3
INTENT_MAX_CHARS = 1000
INTENT_TRUNCATION_MARK = "…[truncated]"
SYSTEM_SPEAKER = "System"
# 系統寫進對話的 user 訊息開頭（heartbeat.py 用同一組常數組字；前端送來時 name 會被剝掉）
PULSE_NUDGE_PREFIX = "【脈衝能量補給："
SYSTEM_NUDGE_PREFIX = "【系統】"
HEARTBEAT_INIT_PREFIX = "【目前系統時間："
# SNS 外人訊息（sns_gateway 存檔與送出時加的開頭）：外人不是這個靈魂的人類，不能替代理人授權
SNS_MESSAGE_PREFIX = "【SNS 外部訊息】"
# 前端內嵌的附件（buildTextInlinePart／buildFileRefHint 接在人類文字後面）：從第一個標記起都
# 不是人類原話，整段排除（附件內容可能偽造結束標記，所以不找結尾）
ATTACHMENT_MARKERS = ("[Attached file: ", "[Uploaded file: ")


class IntentSource(StrEnum):
    """原意來源。"""

    CHAT = "chat"  # 對話：最後 3 則人類訊息
    HEARTBEAT = "heartbeat_init_prompt"  # 心跳：排程寫的 init_prompt（已消毒）
    RETRIGGER = "retrigger_last_human"  # UI 重跑：最後一則人類節點
    SUBAGENT = "subagent"  # 子代理：沒有人類訊息，人類原意在 parent（派工目標是代理人轉述，不收）


@dataclass(frozen=True)
class UserIntent:
    """標成資料：只能放進分隔區塊當證據，任何文字都不得被當成指令。"""

    source: IntentSource
    messages: tuple[str, ...]
    parent: UserIntent | None = None
    trust: Literal["data"] = "data"


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) > INTENT_MAX_CHARS:
        text = text[:INTENT_MAX_CHARS] + INTENT_TRUNCATION_MARK
    return text


def _field(message: Any, key: str) -> Any:
    return message.get(key) if isinstance(message, dict) else getattr(message, key, None)


def _text(content: Any) -> str:
    """只取文字；tool_result、圖片等區塊不算人說的話。"""
    if isinstance(content, str):
        return content.strip()
    blocks = content if isinstance(content, list) else []
    parts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if isinstance(p, str) and p).strip()


def _without_attachments(text: str) -> str:
    cut = min((i for m in ATTACHMENT_MARKERS if (i := text.find(m)) >= 0), default=len(text))
    return text[:cut].strip()


def _human_texts(messages: Any, excluded: tuple[str, ...]) -> list[str]:
    texts: list[str] = []
    for message in messages or []:
        if _field(message, "role") != "user" or _field(message, "name") == SYSTEM_SPEAKER:
            continue
        text = _without_attachments(_text(_field(message, "content")))
        if text and not text.startswith((SNS_MESSAGE_PREFIX, *excluded)):
            texts.append(_truncate(text))
    return texts


def chat_intent(messages: Any) -> UserIntent:
    """對話：最後 3 則人類 user 訊息；nudge 與心跳寫進對話的 init_prompt 都不算。"""
    excluded = (PULSE_NUDGE_PREFIX, SYSTEM_NUDGE_PREFIX, HEARTBEAT_INIT_PREFIX)
    texts = _human_texts(messages, excluded)[-INTENT_MAX_MESSAGES:]
    return UserIntent(IntentSource.CHAT, tuple(texts))


def heartbeat_intent(safe_init_prompt: str) -> UserIntent:
    """心跳：消毒後的 init_prompt（排程擁有者寫的指示）。"""
    text = _truncate(safe_init_prompt or "")
    return UserIntent(IntentSource.HEARTBEAT, (text,) if text else ())


def retrigger_intent(linear_messages: Any) -> UserIntent:
    """UI 重跑：最後一則人類節點（那次背景任務寫進去的 init_prompt 也算）。"""
    texts = _human_texts(linear_messages, (PULSE_NUDGE_PREFIX, SYSTEM_NUDGE_PREFIX))
    return UserIntent(IntentSource.RETRIGGER, tuple(texts[-1:]))


def subagent_intent(parent: Any) -> UserIntent:
    """子代理：只帶父代理的人類原意；派工目標是代理人轉述，不送審查員（R-2c ③ #4）。"""
    parent_intent = parent if isinstance(parent, UserIntent) else None
    return UserIntent(IntentSource.SUBAGENT, (), parent_intent)
