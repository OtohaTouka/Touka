"""不等結果的 hook 事件共用的背景入口（ADR-0292）.

Stop、PermissionDenied、Notification 都走這裡：呼叫端不等 hook，也不會因為 hook 出錯而受影響。
同時執行的數量有上限（慢的 hook 碰上大量事件時，背景工作不能一直累積）；超過就跳過這次，並在
執行紀錄記一筆，設定頁看得到。沒有符合的 hook 時不組事件內容、也不佔名額。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from src.services.hooks.service import hook_service
from src.utils.task_utils import spawn_logged

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

MAX_BACKGROUND_HOOK_RUNS: Final[int] = 32

_in_flight: set[asyncio.Task[Any]] = set()


def in_flight_count() -> int:
    """正在背景執行的事件數。"""
    return len(_in_flight)


def schedule_background_hook(
    event: str,
    build_payload: Callable[[], dict[str, Any]],
    *,
    match: Iterable[str] = (),
    incognito: bool = False,
) -> None:
    """有符合的 hook 時在背景執行；``build_payload`` 只在要執行時才呼叫。出錯只記 log。"""
    try:
        names = tuple(match)
        if not hook_service.has_matching_hooks(event, names):
            return
        if len(_in_flight) >= MAX_BACKGROUND_HOOK_RUNS:
            hook_service.record_skipped(event, incognito=incognito)
            logger.warning("Too many background hooks are running; skipped one %s event", event)
            return
        task = spawn_logged(
            hook_service.dispatch(event, build_payload(), match=names, incognito=incognito),
            name=f"hook:{event}",
        )
    except Exception:
        logger.exception("%s hooks could not be started; nothing else is affected", event)
        return
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)
