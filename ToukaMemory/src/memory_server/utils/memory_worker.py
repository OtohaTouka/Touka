"""記憶讀寫的專用工作執行緒（舊審查報告乙-3，ADR-0288）.

事實庫、向量庫（Qdrant 與嵌入模型）與舊對話庫的讀寫都排在同一條執行緒上，一次一個：

- 主迴圈不會被嵌入模型、Qdrant 或 SQLite 卡住；第一次載入嵌入模型時，其他記憶操作要排隊，
  但聊天與串流照常；
- 「先查重再寫入」整段是一個工作，不會和另一個寫入交錯，不需要另一把鎖。

呼叫端的 contextvars 複製一份帶進工作裡（無痕、子代理、派工不記記憶事件靠它）。工作裡設的
contextvars 不會傳回呼叫端。

三條規矩：
1. 工作裡不得同步等主迴圈（例如 ``run_coroutine_threadsafe(...).result()``）。
2. 主迴圈不得同步等工作者：主迴圈上一律 ``await run_memory_job(...)``；
   ``run_memory_job_blocking`` 在主迴圈上呼叫會丟 ``MemoryWorkerMisuseError``。
3. 持有工作者可能需要的鎖（例如事實庫的升級備份鎖）時，不得同步等工作者。

工作者自己呼叫時直接執行、不排隊（避免自己等自己）。``await`` 中途被取消時，還沒開始的
工作不會跑；已經在跑的會跑完（執行緒無法中途打斷），結果丟掉。
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

MEMORY_WORKER_THREAD_NAME = "touka-memory"


class MemoryWorkerMisuseError(Exception):
    """在主迴圈上同步等記憶工作者（會卡住所有對話，第 2 條規矩）."""

    def __init__(self) -> None:
        super().__init__("Do not block the event loop on the memory worker; await it instead")


class _Worker:
    """唯一的工作者；執行緒在第一個工作送進來時才建立."""

    thread_ident: int | None = None

    @classmethod
    def mark(cls) -> None:
        cls.thread_ident = threading.get_ident()


_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix=MEMORY_WORKER_THREAD_NAME, initializer=_Worker.mark
)


def in_memory_worker() -> bool:
    """目前是不是在記憶工作者的執行緒上."""
    return threading.get_ident() == _Worker.thread_ident


async def run_memory_job[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """主迴圈上用：把 ``fn(*args, **kwargs)`` 排給工作者，等它做完再拿結果."""
    if in_memory_worker():
        return fn(*args, **kwargs)
    context = contextvars.copy_context()
    return await asyncio.wrap_future(_executor.submit(context.run, fn, *args, **kwargs))


def run_memory_job_blocking[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """不在事件迴圈上的執行緒用（例如記憶清除的工作執行緒）：同步等工作者做完."""
    if in_memory_worker():
        return fn(*args, **kwargs)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise MemoryWorkerMisuseError
    context = contextvars.copy_context()
    return _executor.submit(context.run, fn, *args, **kwargs).result()
