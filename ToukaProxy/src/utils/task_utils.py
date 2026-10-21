"""背景任務工具 (Task Utils).

fire-and-forget 的 ``asyncio.create_task`` 若協程內拋例外，例外會被靜默吞掉
（除非有人 await 或讀 ``.result()``）——背景失敗無聲、難以察覺。``spawn_logged``
以 ``add_done_callback`` 確保例外一定被記錄，並持有強參考避免任務被 GC 提前回收。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# 持有 task 強參考，避免 fire-and-forget task 被 GC 提前回收（CPython 已知陷阱）。
_background_tasks: set[asyncio.Task[Any]] = set()


def _log_task_exception(task: asyncio.Task[Any], name: str) -> None:
    """任務完成回呼：取消不記、有例外則記錄 ERROR（避免靜默吞例外）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("🔥 背景任務 '%s' 失敗：%s", name, exc, exc_info=exc)


def spawn_logged(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
    """以 create_task 啟動 fire-and-forget 協程，並確保例外被記錄 (SSOT).

    取代裸 ``asyncio.create_task(coro)``：背景任務拋例外時不再無聲消失，而是
    以任務名稱記錄 ERROR，便於追蹤失敗來源。

    Args:
        coro: 要在背景執行的協程。
        name: 任務名稱（記錄用，便於辨識失敗來源）。

    Returns:
        asyncio.Task: 已排程的任務（呼叫端通常不需保留）。
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(lambda t: _log_task_exception(t, name))
    return task


async def cancel_all_background_tasks(timeout: float = 10.0) -> int:
    """取消所有經 ``spawn_logged`` 啟動、仍在執行的背景任務並等待收尾（優雅關機 SSOT）。

    解決「fire-and-forget 任務在 lifespan/程序關閉時仍懸掛」的問題：未取消的背景任務
    （如啟動時的動態模型發現）會讓事件圈無法乾淨關閉——在 ``TestClient`` (anyio
    BlockingPortal) 下會 hang、在生產 SIGTERM 下則留下半途的連線。取消父任務會經由
    ``asyncio.gather`` 將取消級聯至其子任務。

    僅作用於「當前 running loop」的任務，避免測試環境跨 event loop 的殘留任務造成
    ``RuntimeError``（生產為單一 loop，故無過濾副作用）。

    Args:
        timeout: 等待取消收尾的秒數上限；逾時僅記錄警告，不再阻塞關閉流程。

    Returns:
        int: 本次實際送出取消的任務數。
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    tasks = [
        t
        for t in list(_background_tasks)
        if not t.done() and (running is None or t.get_loop() is running)
    ]
    if not tasks:
        return 0

    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout
        )
    except TimeoutError:
        logger.warning("⚠️ 背景任務取消逾時（%.1fs），仍有任務未收尾", timeout)
    return len(tasks)
