"""DialogueSummarizer — 把被歸檔的舊對話壓成摘要（ADR-0092 Phase D）。

一次 ``complete_once``（purpose=``summarizer``，ADR-0244）請便宜模型（預設 haiku）把
「即將移出即時脈絡的舊對話」壓成第三人稱要點，長度逼近 budget_tokens；記帳到滑動視窗
手上的身分，並受成本閘約束。**fail-safe**：任何失敗（缺金鑰／超預算／超時／拒答／截斷／
空回應／例外）回 ``None``，呼叫端據此降級為純無損剪裁（不卡系統）。

放於 Proxy（非 Memory）：剪裁邏輯在 Proxy、摘要為其子步驟；Memory 僅負責無損存
原文（禁令 14 模組邊界）。

時間：送摘要員的每行前面帶台北時間；回傳的摘要第一行是系統寫的時間範圍（存在
context_summary 文字裡，不改資料表與合約）。節點時間來自對話紀錄（可能是使用者裝置的時鐘），
照實標示；時間格式與舊對話、隨機回想同一個函式（``legacy_time``）。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from memory_server.utils.legacy_time import format_taipei, to_epoch_ms

from src.services.completion import complete_once
from src.utils.interjection_notes import annotate_node
from src.utils.time_utils import CONVERSATION_RECORD_TIME_NOTE, get_current_epoch

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_SYSTEM = (
    "你是對話歸檔摘要員。下面是一段『即將移出 AI 即時脈絡的較早對話』。"
    "請壓成第三人稱要點，保留：人名、做過的決定、承諾與待辦、未解問題、"
    "情緒或關係的轉折；丟棄寒暄、重複與無關閒聊。"
    "用繁體中文輸出精簡條列，不要開場白或結語，只要要點本身。"
    "每行開頭方括號是該則訊息在對話紀錄裡的台北時間（可能來自使用者裝置的時鐘；"
    "unknown time 表示沒有紀錄）。有可靠時間戳時，把「明天」「昨天」「下週」這類相對時間"
    "換成絕對日期；時間不明或不確定時保留原話。"
)
_DIALOGUE_ROLES = ("user", "assistant")


def _flatten_content(content: Any) -> str:
    """把訊息 content（str / Anthropic block list）攤平成文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text")
                parts.append(t if isinstance(t, str) else json.dumps(b, ensure_ascii=False))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _format_dialogue(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        text = _flatten_content(annotate_node(m).get("content")).strip()  # 含插話（ADR-0249）
        if text and role in _DIALOGUE_ROLES:
            lines.append(f"[{format_taipei(m.get('timestamp'))}] {role}: {text}")
    return "\n".join(lines)


def summary_time_range(messages: list[dict[str, Any]], now_s: float) -> str:
    """摘要開頭那一行：被摘要的對話從何時到何時、幾則沒有時間、何時摘要（台北時間）。"""
    dialogue = [m for m in messages if isinstance(m, dict) and m.get("role") in _DIALOGUE_ROLES]
    known = sorted(s for m in dialogue if (s := to_epoch_ms(m.get("timestamp"))) is not None)
    if not known:
        span = "with no saved message times"
    else:
        missing = len(dialogue) - len(known)
        span = (
            f"from {format_taipei(known[0])} to {format_taipei(known[-1])} "
            f"{CONVERSATION_RECORD_TIME_NOTE}"
            + (f"; {missing} of {len(dialogue)} messages have no saved time" if missing else "")
        )
    return f"[Summary of earlier messages {span}; summarized {format_taipei(now_s)}]"


async def summarize_dialogue(
    identity: AgentIdentity, messages: list[dict[str, Any]], budget_tokens: int, model: str
) -> str | None:
    """把 messages 壓成 ≤ budget_tokens 的摘要文字；任何失敗回 None（降級）。"""
    if not messages or budget_tokens <= 0 or not model:
        return None
    dialogue = _format_dialogue(messages)
    if not dialogue:
        return None
    max_tokens = max(64, min(int(budget_tokens), 4096))
    try:
        result = await complete_once(
            identity, "summarizer", model, _SYSTEM, dialogue, max_tokens, _TIMEOUT_S
        )
    except Exception as e:  # complete_once 不應拋出；摘要仍守住 fail-safe
        logger.warning("DialogueSummarizer 失敗，降級為純無損剪裁：%s", e)
        return None
    if not result.ok:
        logger.warning("DialogueSummarizer 未成功（%s），降級為純無損剪裁", result.error.reason)
        return None
    text = result.text.strip()
    if not text:
        return None
    return f"{summary_time_range(messages, get_current_epoch())}\n{text}"
