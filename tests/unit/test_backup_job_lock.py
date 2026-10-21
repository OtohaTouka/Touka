"""備份執行鎖一直持有到執行緒裡的備份真的結束，呼叫端被取消也一樣。

`system_backup.run_backup_job` 在自己的 task 裡取鎖、以執行緒跑備份，呼叫端用 shield 等；
重置、手動備份、設定匯入、排程四個呼叫點都改用它。備份一律是替身，不跑真的備份腳本。
"""

from __future__ import annotations

import asyncio
import gc
import threading

import pytest
from src.models.backup_schedule import BackupScheduleConfig
from src.services import backup_scheduler_service as sched_module
from src.services import system_backup
from src.services.agent_reset import orchestrator

_LOCK = system_backup.backup_execution_lock


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_cancelled_reset_keeps_lock_until_backup_finishes(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def _backup() -> dict:
        started.set()
        release.wait(5)
        return {"status": "success"}

    monkeypatch.setattr(system_backup, "create_backup", _backup)
    monkeypatch.setattr(orchestrator, "ALL_STORES", ())
    task = asyncio.create_task(orchestrator.reset_debug_agent_data())
    try:
        assert await _wait_until(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _LOCK.locked()  # 備份還在執行緒裡跑，鎖不能先放
    finally:
        release.set()
    assert await _wait_until(lambda: not _LOCK.locked())


async def test_run_backup_job_runs_in_thread_under_lock():
    loop_thread, seen = threading.get_ident(), {}

    def _job() -> str:
        seen["locked"], seen["thread"] = _LOCK.locked(), threading.get_ident()
        return "ok"

    assert await system_backup.run_backup_job(_job) == "ok"
    assert seen["locked"] is True and seen["thread"] != loop_thread
    assert not _LOCK.locked()


async def test_run_backup_job_failure_propagates_and_releases_lock():
    def _boom() -> None:
        raise RuntimeError("disk-full")

    with pytest.raises(RuntimeError, match="disk-full"):
        await system_backup.run_backup_job(_boom)
    assert not _LOCK.locked()


async def test_orphaned_job_failure_is_retrieved_and_logged(caplog):
    """呼叫端已取消、job 之後失敗：例外被取回（沒有 never retrieved）並留下 warning。"""
    loop, messages = asyncio.get_running_loop(), []
    started, release, previous = threading.Event(), threading.Event(), loop.get_exception_handler()

    def _job() -> None:
        started.set()
        release.wait(5)
        raise RuntimeError("orphan-failed")

    loop.set_exception_handler(lambda _loop, ctx: messages.append(ctx["message"]))
    try:
        caller = asyncio.create_task(system_backup.run_backup_job(_job))
        assert await _wait_until(started.is_set)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        del caller
        release.set()
        assert await _wait_until(lambda: not _LOCK.locked())
        await asyncio.sleep(0)
        gc.collect()
    finally:
        release.set()
        loop.set_exception_handler(previous)
    assert not any("never retrieved" in m for m in messages)
    assert "orphan-failed" in caplog.text


async def test_scheduled_backup_and_prune_run_under_lock(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        sched_module, "create_backup", lambda **_k: seen.setdefault("backup", _LOCK.locked())
    )
    monkeypatch.setattr(
        sched_module,
        "prune_old_auto_backups",
        lambda *_a: seen.setdefault("prune", _LOCK.locked()) and [],
    )
    monkeypatch.setattr(sched_module, "backups_dir", lambda: None)

    async def _record(_epoch) -> None:
        seen["recorded"] = True

    monkeypatch.setattr(sched_module, "_record_last_run", _record)
    await sched_module.backup_scheduler._run_once(BackupScheduleConfig(enabled=True))
    assert seen == {"backup": True, "prune": True, "recorded": True}
