"""子代理的指令只在派它的那一輪還在回覆時跑（ADR-0333）.

派出時記下那一輪自己的串流編號（``current_stream_id``，dispatcher 建串流時寫進 ctx，不是去查
這個對話現在哪一輪在回覆）；之後每條指令都比對同一個編號（``parent_turn_active``）。新的一輪
會換新的編號，所以那一輪結束後就再也不能跑，就算你之後又送了訊息、或切了模式也一樣。

判斷是否有窄開關（``SUBAGENT_PROJECT_COMMANDS``）在 ``utils.presence``；這裡只多看串流狀態，
所以放在 services（presence 維持純函式）。
"""

from __future__ import annotations

from typing import Any

from src.services.stream_buffer_service import stream_buffer_service
from src.utils.presence import (
    PARENT_SESSION_ID,
    PARENT_STREAM_ID,
    SUBAGENT_PROJECT_COMMANDS,
    TURN_STREAM_ID,
)


class ParentTurnEndedError(Exception):
    """派子代理的那一輪已經結束：指令不開，或開著的被收掉（ADR-0333）."""


def current_stream_id(ctx: Any) -> str | None:
    """這一輪自己的串流編號，而且還在回覆（派子代理時在第一個 await 之前讀）；不是就 None."""
    stream_id = ctx.get(TURN_STREAM_ID)
    return stream_id if turn_active(ctx.get("session_id"), stream_id) else None


def turn_active(session_id: str | None, stream_id: str | None) -> bool:
    """指定對話的那一輪是否還在回覆（比對串流編號）."""
    return stream_buffer_service.is_stream_active(session_id, stream_id)


def parent_turn_active(ctx: Any) -> bool:
    """窄開關打開、而且派它的那一輪還在回覆（政策環境與開指令前共用）."""
    if ctx.get(SUBAGENT_PROJECT_COMMANDS) is not True:
        return False
    return turn_active(ctx.get(PARENT_SESSION_ID), ctx.get(PARENT_STREAM_ID))


def subagent_turn_over(ctx: Any) -> bool:
    """窄開關打開的子代理，派它的那一輪已經結束（其他代理人一律 False）."""
    return ctx.get(SUBAGENT_PROJECT_COMMANDS) is True and not parent_turn_active(ctx)
