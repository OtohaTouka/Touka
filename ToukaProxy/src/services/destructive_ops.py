"""刪除、重置、記憶清除期間擋住這個靈魂的心跳（舊審查報告丙-4，ADR-0287）.

破壞性操作把目標登記在這裡（``hold``／``release``）；心跳迴圈開跑前與每個脈衝前看
``is_held``，看到就停。心跳迴圈也把自己登記在這裡（``start_heartbeat``／``end_heartbeat``），
刪除靠它判斷這個靈魂還有沒有心跳在跑。看標記和登記在同一把鎖裡，中間不會被插隊。

記憶清除在工作執行緒裡結束，所以用執行緒鎖。ID 不分大小寫比對（寧可多擋）。
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import TYPE_CHECKING

from src.services.rhythm_service import rhythm_service

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

_lock = threading.Lock()
_held: Counter[str] = Counter()
_running: dict[str, Counter[str]] = {}


def _key(identity: AgentIdentity) -> str:
    return identity.agent_id.casefold()


def hold(identity: AgentIdentity) -> None:
    """登記一個進行中的破壞性操作（可重疊：重置和它後面的清除各算一次）."""
    with _lock:
        _held[_key(identity)] += 1


def release(identity: AgentIdentity) -> None:
    """結束一個破壞性操作；同一個靈魂的全部結束才放行."""
    with _lock:
        key = _key(identity)
        _held[key] -= 1
        if _held[key] <= 0:
            del _held[key]


def is_held(identity: AgentIdentity) -> bool:
    """這個靈魂是否正在刪除、重置或清除."""
    with _lock:
        return _held[_key(identity)] > 0


def start_heartbeat(identity: AgentIdentity, session_id: str) -> bool:
    """心跳開跑：沒被擋就登記並回 True；被擋就回 False、什麼都不登記."""
    with _lock:
        key = _key(identity)
        if _held[key] > 0:
            return False
        _running.setdefault(key, Counter())[session_id] += 1
        return True


def end_heartbeat(identity: AgentIdentity, session_id: str) -> None:
    """心跳結束：拿掉這一次的登記（同一個對話的另一次照舊算在跑）."""
    with _lock:
        key = _key(identity)
        sessions = _running.get(key)
        if sessions is None:
            return
        sessions[session_id] -= 1
        if sessions[session_id] <= 0:
            del sessions[session_id]
        if not sessions:
            del _running[key]


def running_heartbeats(identity: AgentIdentity) -> list[str]:
    """這個靈魂正在跑的心跳的對話 ID（排序過）."""
    with _lock:
        return sorted(_running.get(_key(identity), ()))


def stop_heartbeats(identity: AgentIdentity) -> list[str]:
    """叫這個靈魂正在跑的心跳停下，回傳它們的對話 ID（沒有就是空的）."""
    sessions = running_heartbeats(identity)
    for session_id in sessions:
        rhythm_service.interrupt_session(session_id)
    return sessions
