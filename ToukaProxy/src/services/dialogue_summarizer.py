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

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from memory_server.utils.legacy_time import format_taipei, to_epoch_ms

from src.services.completion import complete_once
from src.utils.interjection_notes import annotate_node
from src.utils.time_utils import CONVERSATION_RECORD_TIME_NOTE, get_current_epoch
from src.utils.token_estimator import estimate_tokens

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
# 每次摘要請求（指示＋前情最後一段＋對話）不超過這麼多 token：Haiku 5.5 在 10 萬以內是便宜那一檔，
# 本機估算留兩成（ADR-0305）
SUMMARY_REQUEST_TOKEN_CAP = 80_000
# 長對話切段的大小；只摘要最新的 SUMMARY_TOTAL_TOKEN_CAP，更早的留來源註記
CHUNK_TOKENS = 60_000
SUMMARY_TOTAL_TOKEN_CAP = 500_000
# 最多同時送幾段
MAX_PARALLEL_CHUNKS = 3
_SYSTEM = (
    "你是對話歸檔摘要員。下面是一段『即將移出 AI 即時脈絡的較早對話』。"
    "請壓成第三人稱要點，保留：人名、做過的決定、承諾與待辦、未解問題、"
    "情緒或關係的轉折；丟棄寒暄、重複與無關閒聊。"
    "用繁體中文輸出精簡條列，不要開場白或結語，只要要點本身。"
    "每行開頭方括號是該則訊息在對話紀錄裡的台北時間（可能來自使用者裝置的時鐘；"
    "unknown time 表示沒有紀錄）。有可靠時間戳時，把「明天」「昨天」「下週」這類相對時間"
    "換成絕對日期；時間不明或不確定時保留原話。"
    "如果有『前情最後一段』，那是已經摘要過的內容，只當背景，不要重複寫進要點。"
)
_MERGE_SYSTEM = (
    "你是前情整理員。下面是幾段較早的前情，每段開頭方括號標它從哪段對話來、涵蓋什麼時間。"
    "請合併成一份精簡條列，保留：人名、做過的決定、承諾與待辦、未解問題、情緒或關係的轉折，"
    "以及每件事從哪段對話來；丟棄重複。用繁體中文，不要開場白或結語，只要要點本身。"
)
_PREVIOUS_LABEL = "[前情最後一段（已摘要過，只當背景）]"
_DIALOGUE_LABEL = "[要摘要的對話]"
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


def _dialogue_lines(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        text = _flatten_content(annotate_node(m).get("content")).strip()  # 含插話（ADR-0249）
        if text and role in _DIALOGUE_ROLES:
            lines.append(f"[{format_taipei(m.get('timestamp'))}] {role}: {text}")
    return lines


def _fit_line(line: str, cost: int, limit: int) -> str:
    """單一則就超過一段的上限時只留開頭（依估算比例截短）."""
    if cost <= limit:
        return line
    return line[: max(1, len(line) * limit // cost)]


def _chunk_lines(lines: list[str], model: str, limit: int) -> tuple[list[str], int]:
    """從最新往前取到總上限，再依每段上限切段（舊到新）；回傳各段文字與沒取到的則數."""
    taken: list[tuple[str, int]] = []
    total = 0
    for line in reversed(lines):
        cost = estimate_tokens(line, model) + 1  # 加上換行與逐則四捨五入的誤差
        line = _fit_line(line, cost, limit)
        cost = min(cost, limit)
        if taken and total + cost > SUMMARY_TOTAL_TOKEN_CAP:
            break
        taken.append((line, cost))
        total += cost
    taken.reverse()
    chunks: list[list[str]] = [[]]
    used = 0
    for line, cost in taken:
        if chunks[-1] and used + cost > limit:
            chunks.append([])
            used = 0
        chunks[-1].append(line)
        used += cost
    return ["\n".join(chunk) for chunk in chunks], len(lines) - len(taken)


async def _complete_text(
    identity: AgentIdentity, model: str, system: str, user: str, max_tokens: int
) -> str | None:
    """送一次摘要；任何失敗（含例外與空回應）回 None."""
    try:
        result = await complete_once(
            identity, "summarizer", model, system, user, max_tokens, _TIMEOUT_S
        )
    except Exception as e:  # complete_once 不應拋出；摘要仍守住 fail-safe
        logger.warning("DialogueSummarizer 失敗，降級為純無損剪裁：%s", e)
        return None
    if not result.ok:
        logger.warning("DialogueSummarizer 未成功（%s），降級為純無損剪裁", result.error.reason)
        return None
    return result.text.strip() or None


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


def _skipped_note(count: int) -> str:
    return (
        f"[{count} earlier messages were not summarized; the originals are in the legacy "
        "conversation archive]"
    )


async def summarize_dialogue(
    identity: AgentIdentity,
    messages: list[dict[str, Any]],
    budget_tokens: int,
    model: str,
    *,
    previous: str | None = None,
) -> str | None:
    """把 messages 壓成 ≤ budget_tokens 的摘要文字；任何失敗回 None（降級）。

    每次請求不超過 ``SUMMARY_REQUEST_TOKEN_CAP``：長對話切段（最多同時 ``MAX_PARALLEL_CHUNKS``
    段），只摘要最新的 ``SUMMARY_TOTAL_TOKEN_CAP``，更早的留一行來源註記。任何一段失敗整份回 None。
    ``previous``（前情最後一段）只附在最舊那一段，讓摘要員不重複寫已經摘要過的事。
    """
    if not messages or budget_tokens <= 0 or not model:
        return None
    lines = _dialogue_lines(messages)
    if not lines:
        return None
    prefix = f"{_PREVIOUS_LABEL}\n{previous}\n\n{_DIALOGUE_LABEL}\n" if previous else ""
    overhead = estimate_tokens(_SYSTEM, model) + estimate_tokens(prefix, model)
    limit = max(1, min(CHUNK_TOKENS, SUMMARY_REQUEST_TOKEN_CAP - overhead))
    chunks, skipped = _chunk_lines(lines, model, limit)
    max_tokens = max(64, min(int(budget_tokens), 4096) // len(chunks))
    gate = asyncio.Semaphore(MAX_PARALLEL_CHUNKS)

    async def one(index: int, chunk: str) -> str | None:
        async with gate:
            user = f"{prefix}{chunk}" if index == 0 else chunk
            return await _complete_text(identity, model, _SYSTEM, user, max_tokens)

    texts = await asyncio.gather(*(one(i, chunk) for i, chunk in enumerate(chunks)))
    if any(text is None for text in texts):
        return None
    body = "\n".join(text for text in texts if text)
    note = f"\n{_skipped_note(skipped)}" if skipped else ""
    return f"{summary_time_range(messages, get_current_epoch())}\n{body}{note}"


async def summarize_entries(
    identity: AgentIdentity, entries: list[str], max_tokens: int, model: str
) -> str | None:
    """把幾段前情合併成一段（前情超過上限時用）；任何失敗回 None，原本的段落照留."""
    texts = [entry for entry in entries if entry.strip()]
    if not texts or not model:
        return None
    return await _complete_text(identity, model, _MERGE_SYSTEM, "\n\n".join(texts), max_tokens)
