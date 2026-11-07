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

每輪只讀一次家，前情與家原文用同一份（審查 F16）；讀不到家當作沒有家，照樣送出。前情讀不到時
照樣送出這一輪（記 warning 與原因），家原文的規則不受影響。上下文預覽（``preview``）不搬舊摘要，
整個流程不寫資料（審查 F12）。

子對話自己升級前的舊摘要（``<conversation_summary>``）經 ``inject_conversation_summary``：已經搬進
前情的（例如舊家）不再插第二次（審查 F13）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.sliding_window_config import load_sliding_window_config
from src.services.soul_prelude_service import (
    build_prelude_block,
    conversation_summary_in_prelude,
)
from src.services.soul_service import append_system_block, soul_service
from src.utils.async_db import SessionPersistenceError, home_messages_of, session_service

logger = logging.getLogger(__name__)

type MainlineMode = Literal["chat", "group", "heartbeat", "incognito"]


async def _home_session(identity: AgentIdentity) -> dict[str, Any] | None:
    """現任家；讀不到或矛盾（兩個家）時當作沒有家."""
    try:
        snapshot = await session_service.read_home_snapshot(identity)
    except (ToukaException, SessionPersistenceError):
        logger.warning(
            "Mainline: home conversation unavailable for %s", identity.agent_id, exc_info=True
        )
        return None
    return snapshot.session if snapshot else None


async def _inject_prelude(
    request: Any, identity: AgentIdentity, home: dict[str, Any] | None, *, preview: bool
) -> None:
    try:
        block = await build_prelude_block(
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
        return
    if block:
        append_system_block(request, block)


async def inject_mainline(
    request: Any,
    identity: AgentIdentity,
    *,
    mode: MainlineMode,
    session_id: str | None = None,
    sends_own_history: bool = False,
    preview: bool = False,
) -> None:
    """依上表把前情（system 尾端）與家原文（對話開頭）放進請求.

    Args:
        request: 已套上人格的模型請求。
        identity: 這一輪的靈魂。
        mode: ``chat``（網頁、IDE、SNS）、``group``、``heartbeat``、``incognito``。
        session_id: 這一輪所在的對話；判斷它是不是家（子對話要標時間）。
        sends_own_history: 請求已帶著這個對話的完整紀錄（網頁版）；它就是家時不再插家原文。
        preview: 上下文預覽：不搬舊摘要，不寫任何資料。
    """
    if mode == "incognito":
        return
    home = await _home_session(identity)
    await _inject_prelude(request, identity, home, preview=preview)
    if mode == "heartbeat":
        return
    home_id = home["id"] if home else None
    current = session_id if sends_own_history else None
    await soul_service.inject_home_messages(
        request,
        identity,
        current_session_id=current,
        stamp_times=mode == "chat" and session_id is not None and session_id != home_id,
        # 用同一次讀到的家；讀不到就是空的，不再讀第二次
        home_messages=home_messages_of(home, current) if home else [],
    )


async def inject_conversation_summary(
    request: Any, identity: AgentIdentity, session_id: str | None
) -> None:
    """這段對話自己升級前的舊摘要；已經搬進前情（例如舊家）就不再插（審查 F13）."""
    if not session_id:
        return
    try:
        if await conversation_summary_in_prelude(identity, session_id):
            return
    except Exception:
        logger.warning("Mainline: prelude check failed for %s", session_id, exc_info=True)
    await soul_service.inject_context_summary(request, session_id)
