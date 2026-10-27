"""刪除、重置、記憶清除期間擋住這個靈魂的心跳（舊審查報告丙-4，ADR-0287）.

破壞性操作把目標登記在這裡（``hold``／``release``），每次 ``hold`` 也讓這個靈魂的代數加一。
心跳迴圈開跑時登記自己並記下代數（``start_heartbeat``），每個脈衝前與結束時看 ``disturbed``：
正在進行、或開跑之後發生過（代數變了）就停。重置先結束、或中斷旗標被別的流程清掉，都照樣停。
刪除靠登記判斷這個靈魂還有沒有心跳在跑（``stop_heartbeats``）。看標記和登記在同一把鎖裡。

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
_generation: Counter[str] = Counter()
_running: dict[str, Counter[str]] = {}


def _key(identity: AgentIdentity) -> str:
    return identity.agent_id.casefold()


def hold(identity: AgentIdentity) -> None:
    """登記一個進行中的破壞性操作（可重疊：重置和它後面的清除各算一次）."""
    with _lock:
        key = _key(identity)
        _held[key] += 1
        _generation[key] += 1


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


def generation(identity: AgentIdentity) -> int | None:
    """目前的代數；正在刪除、重置或清除時回 None（入口據此回 409）."""
    with _lock:
        key = _key(identity)
        return None if _held[key] > 0 else _generation[key]


def disturbed(identity: AgentIdentity, since: int) -> bool:
    """從代數 ``since`` 之後，這個靈魂是否正在或曾經刪除、重置、清除."""
    with _lock:
        key = _key(identity)
        return _held[key] > 0 or _generation[key] != since


def start_heartbeat(
    identity: AgentIdentity, session_id: str, *, since: int | None = None
) -> int | None:
    """心跳開跑：沒被擋就登記並回開跑當下的代數；被擋就回 None、什麼都不登記.

    ``since`` 是入口檢查時的代數：之後發生過破壞性操作也不開跑。
    """
    with _lock:
        key = _key(identity)
        if _held[key] > 0 or (since is not None and _generation[key] != since):
            return None
        _running.setdefault(key, Counter())[session_id] += 1
        return _generation[key]


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


def reload_all() -> None:
    """清掉全部標記、代數與登記（供測試環境使用）."""
    with _lock:
        _held.clear()
        _generation.clear()
        _running.clear()
