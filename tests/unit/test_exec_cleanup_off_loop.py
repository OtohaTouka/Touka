"""取消／逾時／停止時的子程序清理不卡事件迴圈，且被殺的程序有被回收。

- 殺行程樹（Windows 為同步 taskkill）一律在執行緒裡跑，不在事件迴圈的執行緒上；
  涵蓋 terminal 取消／逾時、stop_command、互審蒐證取消／逾時、`/stop` 端點。
- 取消分支殺完後有界等待 `proc.wait()`，並以 shield 保護：第二次取消不會中斷清理。
- 殺行程函式都 patch 成記錄替身（沿用各模組 import 的名稱），只有最後一個整合測試
  用真的 `sys.executable` 子程序。全程不讀寫 data/、不打網路。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from unittest.mock import AsyncMock

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.routers import ui_sessions as ui_mod
from src.services import process_registry as registry_mod
from src.services.review import evidence
from src.services.review.evidence import collect_evidence
from src.tools import terminal

_OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])


class _Proc:
    """asyncio subprocess 替身：讀取永遠卡住，記錄 kill／wait 的順序；刻意不設 pid。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.events: list[str] = []
        self.reading = asyncio.Event()
        self.stdout = self

    async def readline(self) -> bytes:
        self.reading.set()
        await asyncio.Event().wait()
        return b""

    async def communicate(self):
        self.reading.set()
        await asyncio.Event().wait()

    async def wait(self) -> int:
        self.events.append("wait")
        self.returncode = -9
        return -9

    def kill(self) -> None:
        self.returncode = -9


def _kill_recorder(threads: list[int], gate: threading.Event | None = None):
    def _kill(proc) -> None:
        if gate is not None:
            gate.wait(5)
        threads.append(threading.get_ident())
        proc.events.append("kill")

    return _kill


async def _allow_all(_cmd: str) -> str | None:
    return None


def _evidence_task(tmp_path, timeout: float = 60):
    return asyncio.create_task(
        collect_evidence(
            artifact_refs=[],
            verification_cmd="pytest -q",
            test_log=None,
            artifact_root=tmp_path,
            timeout_seconds=timeout,
            command_gate=_allow_all,
        )
    )


def _spawner(proc: _Proc):
    async def _spawn(*_args, **_kwargs):
        return proc

    return _spawn


async def test_terminal_cancel_kills_off_loop_then_reaps(tmp_path, monkeypatch):
    loop_thread, threads, proc = threading.get_ident(), [], _Proc()
    monkeypatch.setattr(terminal, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill_recorder(threads))
    task = asyncio.create_task(
        terminal._execute_core("sleep 100", "powershell", tmp_path, "b6-terminal")
    )
    await asyncio.wait_for(proc.reading.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert threads and threads[0] != loop_thread
    assert proc.events == ["kill", "wait"]


async def test_terminal_timeout_kills_off_loop(tmp_path, monkeypatch):
    loop_thread, threads, proc = threading.get_ident(), [], _Proc()
    monkeypatch.setattr(terminal, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill_recorder(threads))
    exit_code, output, _ = await terminal._execute_core(
        "sleep 100", "powershell", tmp_path, "b5-timeout", timeout=0.05
    )
    assert exit_code == -1 and "timed out" in output
    assert threads and threads[0] != loop_thread


async def test_stop_command_kills_off_loop(monkeypatch):
    loop_thread, threads, proc = threading.get_ident(), [], _Proc()
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill_recorder(threads))
    monkeypatch.setitem(
        terminal._ASYNC_RESULTS,
        "b5-exec",
        {"agent_id": _OWNER.agent_id, "status": "running", "command": "x", "_proc": proc},
    )
    await terminal.stop_command("b5-exec", identity=_OWNER)
    assert threads and threads[0] != loop_thread


async def test_stop_command_result_not_overwritten_while_killing(monkeypatch):
    """殺行程移到執行緒後，背景任務不能趁等待時把已定案的 stopped 覆寫成 done。"""
    loop, proc, exited = asyncio.get_running_loop(), _Proc(), asyncio.Event()
    record = {"agent_id": _OWNER.agent_id, "status": "running", "command": "x", "_proc": proc}

    async def _background() -> None:
        await exited.wait()
        record["status"] = "done"

    record["_task"] = asyncio.create_task(_background())
    monkeypatch.setattr(
        terminal, "terminate_process_tree", lambda _p: loop.call_soon_threadsafe(exited.set)
    )
    monkeypatch.setitem(terminal._ASYNC_RESULTS, "b5-race", record)
    await terminal.stop_command("b5-race", identity=_OWNER)
    await asyncio.sleep(0.05)
    assert record["status"] == "stopped"


async def test_evidence_cancel_kills_off_loop_then_reaps(tmp_path, monkeypatch):
    loop_thread, threads, proc = threading.get_ident(), [], _Proc()
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", _kill_recorder(threads))
    task = _evidence_task(tmp_path)
    await asyncio.wait_for(proc.reading.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert threads and threads[0] != loop_thread
    assert proc.events == ["kill", "wait"]


async def test_evidence_timeout_kills_off_loop(tmp_path, monkeypatch):
    loop_thread, threads, proc = threading.get_ident(), [], _Proc()
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", _kill_recorder(threads))
    bundle = await _evidence_task(tmp_path, timeout=0.05)
    assert "timeout" in (bundle.cmd_results[0].rejected_reason or "")
    assert threads and threads[0] != loop_thread


async def test_second_cancel_does_not_interrupt_cleanup(tmp_path, monkeypatch):
    """清理途中再取消一次：呼叫端立即收到取消，但殺行程與回收仍會做完。"""
    threads, proc, gate = [], _Proc(), threading.Event()
    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", _kill_recorder(threads, gate))
    task = _evidence_task(tmp_path)
    await asyncio.wait_for(proc.reading.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    for _ in range(200):
        if "wait" in proc.events:
            break
        await asyncio.sleep(0.01)
    assert proc.events == ["kill", "wait"]


async def test_reap_gives_up_after_timeout(monkeypatch):
    """被殺的子程序一直不結束時，清理在 CANCEL_REAP_TIMEOUT_SECONDS 後放手返回。"""
    loop, proc = asyncio.get_running_loop(), _Proc()
    proc.wait = asyncio.Event().wait  # 永不回傳
    monkeypatch.setattr(registry_mod, "CANCEL_REAP_TIMEOUT_SECONDS", 0.05)
    start = loop.time()
    await asyncio.wait_for(registry_mod.reap_cancelled_process(proc, lambda _p: None), 2)
    assert loop.time() - start < 1


async def test_failing_kill_keeps_cancellation(tmp_path, monkeypatch):
    """殺行程函式拋錯時，清理仍等待回收，呼叫端收到的仍是取消而不是該錯誤。"""
    proc = _Proc()

    def _broken_kill(_proc) -> None:
        raise RuntimeError("kill-failed")

    monkeypatch.setattr(evidence, "spawn_exec", _spawner(proc))
    monkeypatch.setattr(evidence, "terminate_process_tree", _broken_kill)
    task = _evidence_task(tmp_path)
    await asyncio.wait_for(proc.reading.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.events == ["wait"]


async def test_stop_endpoint_kills_off_loop_and_counts(monkeypatch):
    loop_thread, threads = threading.get_ident(), []
    running, finished = [_Proc(), _Proc()], _Proc()
    finished.returncode = 0
    for proc in (*running, finished):
        registry_mod.process_registry.register("b5-stop", proc)
    monkeypatch.setattr(registry_mod, "terminate_process_tree", _kill_recorder(threads))
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", lambda _sid: None)
    monkeypatch.setattr(ui_mod, "_authorized_stream_state", AsyncMock(return_value=None))
    monkeypatch.setattr(ui_mod.stream_buffer_service, "cancel", AsyncMock())
    result = await ui_mod.stop_session_tasks("b5-stop", identity=_OWNER, stream_id=None)
    assert "terminated 2" in result["message"]
    assert len(threads) == 2 and loop_thread not in threads
    assert registry_mod.process_registry.count("b5-stop") == 0


async def test_stop_endpoint_unauthorized_leaves_processes(monkeypatch):
    """授權失敗時不取出、不殺任何登記中的子進程。"""
    threads, proc = [], _Proc()
    registry_mod.process_registry.register("b5-denied", proc)
    monkeypatch.setattr(registry_mod, "terminate_process_tree", _kill_recorder(threads))
    denied = AsyncMock(side_effect=ToukaException(ErrorCode.AUTH_PERMISSION_DENIED))
    monkeypatch.setattr(ui_mod, "_authorized_stream_state", denied)
    with pytest.raises(ToukaException):
        await ui_mod.stop_session_tasks("b5-denied", identity=_OWNER, stream_id=None)
    assert threads == [] and registry_mod.process_registry.count("b5-denied") == 1
    registry_mod.process_registry.unregister("b5-denied", proc)


async def test_cancelled_real_process_is_reaped(tmp_path, monkeypatch):
    """整合：真的子程序被取消後，回傳前已被回收（returncode 不是 None）。"""
    spawned: list = []
    real_spawn = evidence.spawn_exec

    async def _spy(*args, **kwargs):
        spawned.append(await real_spawn(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(evidence, "spawn_exec", _spy)
    task = asyncio.create_task(
        collect_evidence(
            artifact_refs=[],
            verification_cmd=f'"{sys.executable}" -c "import time; time.sleep(30)"',
            test_log=None,
            artifact_root=tmp_path,
            command_gate=_allow_all,
        )
    )
    for _ in range(200):
        if spawned:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned and spawned[0].returncode is not None
