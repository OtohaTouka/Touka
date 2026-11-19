"""``<continuity>`` 區塊共用的開頭、時間行與對話種類，以及群聊成員的精簡版（ADR-0323）.

``continuity_service`` 會讀家與交接（``home_handover`` → 摘要 → 分發器），群聊分發器匯入它會
繞回自己；這裡只放固定字與不讀任何紀錄的純函式，兩邊都引用同一份（禁令 18）。
"""

from __future__ import annotations

from src.services.prompt_blocks import CONTINUITY, wrap_block
from src.utils.time_utils import format_epoch_to_taipei, get_current_epoch

HEADER = (
    "Added by the Touka OS harness to this turn, not written by the user. Facts only; "
    "refreshed on every request and never saved. When you save any of this to memory, write "
    'absolute dates (for example 2026-10-15), not relative times such as "3 hours ago".'
)
# 這段對話是什麼（ADR-0322 PR 3 家-1）
CONVERSATION_LABEL = "This conversation"
KIND_INCOGNITO = "incognito; nothing here is saved or reported to your home"
KIND_GROUP = "a group chat; its content is not reported to your home"
SERVER_CLOCK = "(server clock)"


def current_time_line(now_s: float) -> str:
    """現在時間那一行（伺服器時鐘，台北時間）。"""
    time_str, weekday = format_epoch_to_taipei(now_s)
    return f"- Current time (Asia/Taipei): {time_str} ({weekday}) {SERVER_CLOCK}"


def build_group_member_continuity(*, incognito: bool, now_s: float | None = None) -> str:
    """群聊成員的精簡 ``<continuity>``：現在時間與「這是群聊」。

    不讀上一輪、不讀家、不寫事件：群聊沒有成員自己的對話，其他行對成員都不成立。
    """
    now_s = get_current_epoch() if now_s is None else now_s
    kind = KIND_INCOGNITO if incognito else KIND_GROUP
    lines = [HEADER, current_time_line(now_s), f"- {CONVERSATION_LABEL}: {kind}"]
    return wrap_block(CONTINUITY, "\n".join(lines))
