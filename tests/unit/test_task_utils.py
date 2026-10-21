"""P5d：spawn_logged — fire-and-forget 背景任務的例外不再被靜默吞掉。"""
from __future__ import annotations

import asyncio
import logging

from src.utils.task_utils import cancel_all_background_tasks, spawn_logged


async def test_spawn_logged_logs_background_exception(caplog):
    """背景協程拋例外 → 由 done callback 記錄 ERROR（含任務名稱）。"""

    async def boom() -> None:
        raise ValueError("kaboom-in-bg")

    with caplog.at_level(logging.ERROR):
        spawn_logged(boom(), name="boomer")
        await asyncio.sleep(0.05)  # 讓任務跑完、callback 觸發

    assert any("boomer" in r.getMessage() for r in caplog.records)


async def test_spawn_logged_success_no_error(caplog):
    """背景協程正常完成 → 不產生 ERROR log。"""
    result_holder: list[int] = []

    async def ok() -> None:
        result_holder.append(42)

    with caplog.at_level(logging.ERROR):
        spawn_logged(ok(), name="okjob")
        await asyncio.sleep(0.05)

    assert result_holder == [42]
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_spawn_logged_returns_task():
    """回傳 asyncio.Task，且任務確實被排程執行。"""
    ran: list[bool] = []

    async def work() -> None:
        ran.append(True)

    task = spawn_logged(work(), name="worker")
    assert isinstance(task, asyncio.Task)
    await asyncio.sleep(0.05)
    assert ran == [True]


async def test_cancel_all_cancels_pending_task():
    """優雅關機：取消仍在執行的 fire-and-forget 任務並回傳取消數。"""
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = spawn_logged(forever(), name="forever-job")
    await started.wait()  # 確保已開始排程
    assert not task.done()

    count = await cancel_all_background_tasks()
    assert count >= 1
    assert task.cancelled()


async def test_cancel_all_idempotent_when_clean():
    """重複呼叫：自身任務已收尾後，再呼叫回 0（同 loop 無殘留）。"""
    await cancel_all_background_tasks()  # 清掉本 loop 任何殘留
    again = await cancel_all_background_tasks()
    assert again == 0


async def test_cancel_all_preserves_completed_task_result():
    """已完成的任務不受影響：結果保留、且呼叫不拋例外。"""
    holder: list[int] = []

    async def quick() -> None:
        holder.append(7)

    spawn_logged(quick(), name="quick-job")
    await asyncio.sleep(0.05)  # 讓其完成 → 由 done callback 移出集合
    await cancel_all_background_tasks()
    assert holder == [7]
