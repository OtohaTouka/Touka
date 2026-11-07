"""主線怎麼組（3d PR 1，ADR-0305）：一個靈魂只有一條主線＝前情＋現任家.

網頁、IDE、SNS 家模式（SoulLayer）、群聊、心跳都呼叫 ``inject_mainline``，只有這一個地方決定
帶不帶前情、帶不帶家的原文：

| 誰送出 | 前情 | 家原文 |
|---|---|---|
| 網頁版家本身（送完整紀錄） | 帶 | 不帶 |
| 家，但 IDE、SNS 家模式（只送最新一句） | 帶 | 帶 |
| 子對話（網頁、IDE；帶對話編號、不是家） | 帶 | 帶，每則內容前標時間 |
| 沒有對話編號的請求 | 帶 | 帶，不標時間 |
| 群聊 | 帶 | 照舊帶 |
| 心跳（在家或子對話） | 帶 | 不帶（在家時自己的歷史就是家；子對話時全文太貴） |
| 無痕 | 不帶 | 不帶 |

每輪只讀一次家，前情與家原文用同一份。讀家碰到儲存錯誤時，和以前一樣讓這一輪失敗、不再讀第二次
（ADR-0078 失效不沉默，審查第 2 輪 G2、第 3 輪 H3）；心跳（以前不讀家）不帶家原文，照樣帶前情。
兩個家等範圍矛盾當作沒有家。
前情讀不到時照樣送出這一輪（記 warning 與原因）。上下文預覽（``preview``）不搬舊摘要，整個流程
不寫資料。

``own_summary``（SoulLayer 對綁在這個靈魂的對話開）：這段對話自己升級前的舊摘要
（``<conversation_summary>``）照插，除非這一輪前情已經原文照登同一段（家與子對話一樣；截短過、
被折掉、被合併時照插，審查第 2 輪 G1）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.sliding_window_config import load_sliding_window_config
from src.services.soul_prelude_service import prelude_view, render_prelude, summaries_shown_for
from src.services.soul_service import append_system_block, soul_service
from src.utils.async_db import SessionPersistenceError, home_messages_of, session_service

if TYPE_CHECKING:
    from src.utils.soul_prelude_db import StoredSegment

logger = logging.getLogger(__name__)

type MainlineMode = Literal["chat", "group", "heartbeat", "incognito"]


async def _home_session(identity: AgentIdentity, *, heartbeat: bool) -> dict[str, Any] | None:
    """現任家；範圍矛盾（兩個家）時當作沒有家.

    儲存錯誤照舊丟出去讓這一輪失敗；只有心跳（以前不讀家）當作沒有家，照樣帶前情。
    """
    try:
        snapshot = await session_service.read_home_snapshot(identity)
    except ToukaException:
        logger.warning(
            "Mainline: home conversation unavailable for %s", identity.agent_id, exc_info=True
        )
        return None
    except SessionPersistenceError:
        if not heartbeat:
            raise
        logger.warning(
            "Mainline: home unreadable for %s; heartbeat keeps its prelude",
            identity.agent_id,
            exc_info=True,
        )
        return None
    return snapshot.session if snapshot else None


async def _inject_prelude(
    request: Any, identity: AgentIdentity, home: dict[str, Any] | None, *, preview: bool
) -> list[StoredSegment]:
    """放前情區塊，回傳這一輪顯示的段落；讀不到就不放、回空清單."""
    try:
        shown, folded = await prelude_view(
            identity,
            home_id=home["id"] if home else None,
            config=load_sliding_window_config(),
            home_session=None if preview else home,  # 預覽不搬舊摘要
        )
    except Exception:
        logger.warning(
            "Mainline: prelude unavailable for %s; sending without it",
            identity.agent_id,
            exc_info=True,
        )
        return []
    block = render_prelude(shown, folded)
    if block:
        append_system_block(request, block)
    return shown


async def inject_mainline(
    request: Any,
    identity: AgentIdentity,
    *,
    mode: MainlineMode,
    session_id: str | None = None,
    sends_own_history: bool = False,
    preview: bool = False,
    own_summary: bool = False,
) -> None:
    """依上表把前情（system 尾端）、家原文（對話開頭）與這段對話自己的舊摘要放進請求.

    Args:
        request: 已套上人格的模型請求。
        identity: 這一輪的靈魂。
        mode: ``chat``（網頁、IDE、SNS）、``group``、``heartbeat``、``incognito``。
        session_id: 這一輪所在的對話；判斷它是不是家（子對話要標時間）。
        sends_own_history: 請求已帶著這個對話的完整紀錄（網頁版）；它就是家時不再插家原文。
        preview: 上下文預覽：不搬舊摘要，不寫任何資料。
        own_summary: 插這段對話自己升級前的舊摘要（呼叫端已確認對話綁在這個靈魂）。
    """
    if mode == "incognito":
        return
    home = await _home_session(identity, heartbeat=mode == "heartbeat")
    shown = await _inject_prelude(request, identity, home, preview=preview)
    if mode == "heartbeat":
        return
    home_id = home["id"] if home else None
    current = session_id if sends_own_history else None
    await soul_service.inject_home_messages(  # 用同一次讀到的家，沒有家就是空的
        request,
        identity,
        current_session_id=current,
        stamp_times=mode == "chat" and session_id is not None and session_id != home_id,
        home_messages=home_messages_of(home, current) if home else [],
    )
    if own_summary and session_id:
        await soul_service.inject_context_summary(
            request, session_id, summaries_shown_for(shown, session_id)
        )
