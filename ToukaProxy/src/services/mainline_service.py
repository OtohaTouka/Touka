"""主線怎麼組（3d PR 1，ADR-0305）：一個靈魂只有一條主線＝前情＋現任家.

網頁、IDE、SNS 家模式（SoulLayer）、群聊、心跳都呼叫 ``inject_mainline``，只有這一個地方決定
帶不帶前情、帶不帶家的原文：

| 誰送出 | 前情 | 家原文 |
|---|---|---|
| 網頁版家本身（送完整紀錄） | 帶 | 不帶 |
| 家，但 IDE、SNS 家模式（只送最新一句） | 帶 | 帶 |
| 子對話（網頁、IDE） | 帶 | 帶，每則內容前標時間 |
| 群聊 | 帶 | 照舊帶 |
| 心跳（在家或子對話） | 帶 | 不帶（在家時自己的歷史就是家；子對話時全文太貴） |
| 無痕 | 不帶 | 不帶 |

前情讀不到時照樣送出這一輪（只記 warning），家原文的規則不受影響。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.sliding_window_config import load_sliding_window_config
from src.services.soul_prelude_service import build_prelude_block
from src.services.soul_service import append_system_block, soul_service
from src.utils.async_db import SessionPersistenceError, session_service

logger = logging.getLogger(__name__)

type MainlineMode = Literal["chat", "group", "heartbeat", "incognito"]


async def _home_session(identity: AgentIdentity) -> dict[str, Any] | None:
    """現任家；讀不到或矛盾（兩個家）時當作沒有家（家原文那邊會照舊記 warning）."""
    try:
        snapshot = await session_service.read_home_snapshot(identity)
    except (ToukaException, SessionPersistenceError):
        logger.warning("Mainline: home conversation unavailable for %s", identity.agent_id)
        return None
    return snapshot.session if snapshot else None


async def _inject_prelude(
    request: Any, identity: AgentIdentity, home: dict[str, Any] | None
) -> None:
    try:
        block = await build_prelude_block(
            identity,
            home_id=home["id"] if home else None,
            config=load_sliding_window_config(),
            home_session=home,
        )
    except Exception:
        logger.warning(
            "Mainline: prelude unavailable for %s; sending without it", identity.agent_id
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
) -> None:
    """依上表把前情（system 尾端）與家原文（對話開頭）放進請求.

    Args:
        request: 已套上人格的模型請求。
        identity: 這一輪的靈魂。
        mode: ``chat``（網頁、IDE、SNS）、``group``、``heartbeat``、``incognito``。
        session_id: 這一輪所在的對話；判斷它是不是家（子對話要標時間）。
        sends_own_history: 請求已帶著這個對話的完整紀錄（網頁版）；它就是家時不再插家原文。
    """
    if mode == "incognito":
        return
    home = await _home_session(identity)
    await _inject_prelude(request, identity, home)
    if mode == "heartbeat":
        return
    home_id = home["id"] if home else None
    await soul_service.inject_home_messages(
        request,
        identity,
        current_session_id=session_id if sends_own_history else None,
        stamp_times=mode == "chat" and session_id != home_id,
    )
