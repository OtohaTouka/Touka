"""指令逾時後的回收有時間上限、取消不被吞掉（ADR-0250 C2）。

terminal 與互審蒐證的逾時分支殺完行程樹後要等 ``proc.wait()`` 回收：

- 被殺的行程一直不結束時，等待在 ``CANCEL_REAP_TIMEOUT_SECONDS`` 後放手，逾時分支照常返回；
- 清理途中呼叫端被取消時，``CancelledError`` 往上拋，殺行程與回收照樣做完。

行程與殺行程函式都是替身（沿用各模組 import 的名稱 patch）：讀取永遠卡住、``wait()`` 可設成
永不回傳；不啟動真的子程序、不讀寫 data/、不打網路。
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from src.services import process_registry as registry_mod
from src.services.review import evidence
from src.services.review.evidence import collect_evidence
from src.tools import terminal

_REAP_LIMIT = 0.05


class _Proc:
    """asyncio subprocess 替身：讀取永遠卡住；``wait_forever`` 時回收也永遠卡住。"""

    def __init__(self, *, wait_forever: bool) -> None:
        self.returncode: int | None = None
        self.events: list[str] = []
        self.stdout = self
        self._wait_forever = wait_forever

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self) -> int:
        self.events.append("wait")
        if self._wait_forever:
            await asyncio.Event().wait()
        self.returncode = -9
        return -9

    def kill(self) -> None:
        self.returncode = -9


def _spawner(proc: _Proc):
    async def _spawn(*_args, **_kwargs):
        return proc

    return _spawn


def _gated_kill(started: threading.Event, release: threading.Event):
    def _kill(proc: _Proc) -> None:
        started.set()
        release.wait(5)
        proc.events.append("kill")

    return _kill


async def _allow_all(_cmd: str) -> str | None:
    return None


def _run_terminal(zone_drive):
    return terminal._execute_core("sleep 100", "powershell", zone_drive, "c2-timeout", timeout=0.05)


def _run_evidence(zone_drive):
    return collect_evidence(
        artifact_refs=[],
        verification_cmd="pytest -q",
        test_log=None,
        artifact_root=zone_drive,
        timeout_seconds=0.05,
        command_gate=_allow_all,
    )


async def _wait_for_event(proc: _Proc, name: str) -> None:
    async with asyncio.timeout(5):
        while name not in proc.events:
            await asyncio.sleep(0.01)


@pytest.fixture
def short_reap(monkeypatch):
    monkeypatch.setattr(registry_mod, "CANCEL_REAP_TIMEOUT_SECONDS", _REAP_LIMIT)


async def test_terminal_timeout_reap_is_bounded(zone_drive, monkeypatch, short_reap):
    proc = _Proc(wait_forever=True)
    monkeypatch.setattr(terminal, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(terminal, "terminate_process_tree", lambda p: p.events.append("kill"))
    async with asyncio.timeout(2):
        exit_code, output, _ = await _run_terminal(zone_drive)
    assert exit_code == -1 and "timed out" in output
    assert proc.events == ["kill", "wait"]


async def test_evidence_timeout_reap_is_bounded(zone_drive, monkeypatch, short_reap):
    proc = _Proc(wait_forever=True)
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", lambda p: p.events.append("kill"))
    async with asyncio.timeout(2):
        bundle = await _run_evidence(zone_drive)
    assert "timeout" in (bundle.cmd_results[0].rejected_reason or "")
    assert proc.events == ["kill", "wait"]


async def test_terminal_cancel_during_timeout_cleanup_propagates_and_still_reaps(
    zone_drive, monkeypatch, short_reap
):
    proc, started, release = _Proc(wait_forever=False), threading.Event(), threading.Event()
    monkeypatch.setattr(terminal, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(terminal, "terminate_process_tree", _gated_kill(started, release))
    task = asyncio.create_task(_run_terminal(zone_drive))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await _wait_for_event(proc, "wait")
    assert proc.events == ["kill", "wait"]


async def test_evidence_cancel_during_timeout_cleanup_propagates_and_still_reaps(
    zone_drive, monkeypatch, short_reap
):
    proc, started, release = _Proc(wait_forever=False), threading.Event(), threading.Event()
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", _gated_kill(started, release))
    task = asyncio.create_task(_run_evidence(zone_drive))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await _wait_for_event(proc, "wait")
    assert proc.events == ["kill", "wait"]


async def test_reap_process_tree_lets_cancellation_through(short_reap):
    """共用函式本身：呼叫端被取消時取消往上拋；reap_cancelled_process 照舊吞掉取消。"""
    proc, started, release = _Proc(wait_forever=False), threading.Event(), threading.Event()
    task = asyncio.create_task(registry_mod.reap_process_tree(proc, _gated_kill(started, release)))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await _wait_for_event(proc, "wait")
    assert proc.events == ["kill", "wait"]

    swallowed = _Proc(wait_forever=False)
    started2, release2 = threading.Event(), threading.Event()
    task2 = asyncio.create_task(
        registry_mod.reap_cancelled_process(swallowed, _gated_kill(started2, release2))
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(started2.wait, 5), timeout=5)
        task2.cancel()
        await task2  # 取消被吞掉，不拋例外
    finally:
        release2.set()
    await _wait_for_event(swallowed, "wait")
