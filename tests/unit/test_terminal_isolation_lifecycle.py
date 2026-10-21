"""終端指令經 Landlock 啟動器時，背景／停止／逾時／取消與舊審批通道的語意不變（iso-c2 U1e）.

啟動器 exec 之後 PID 不變，所以殺的仍是 spawn 回來的那個行程（連坐殺整個群組）。spawn 換成
記錄替身、殺行程換成記錄函式；argv 必須是 ``python -I -S <kernel>/launcher.py --spec-fd N --``
開頭。不真的啟動任何程式、不打網路。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from typing import Any

import pytest
import src.routers.terminal as terminal_router
from src.models.identity import AgentIdentity
from src.sandbox_kernel.spec import kernel_digest
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import agent_command
from src.services.isolation.agent_command import CommandScope
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.executor_client import ExecutorReady
from src.tools import terminal

from tests.support.isolation import fake_kernel_layout, install_real_broker, pinned_snapshot

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
LANDLOCK = CommandScope(ALICE, ExecutionGrant("landlock"))
EXECUTOR = CommandScope(ALICE, ExecutionGrant("executor"))
REAL_COMMAND_PROFILE = agent_command.build_command_profile
READY = ExecutorReady(
    version=1, abi=7, enforced=True, net=None, features=("fs", "tcp"), roots=(),
    state_root="/x/executor_state", kernel_digest="k" * 64, server_digest="s" * 64,
    boot_id="b" * 32, block_ports_digest="d" * 64, uid=1001,
)


class _HangingProc:
    """讀取永遠卡住的行程替身；記錄 kill 與 wait。"""

    def __init__(self, argv: tuple[Any, ...]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.events: list[str] = []
        self.reading = asyncio.Event()
        self.stdout = self

    async def readline(self) -> bytes:
        self.reading.set()
        await asyncio.Event().wait()
        return b""

    async def wait(self) -> int:
        self.events.append("wait")
        self.returncode = -9
        return -9


@pytest.fixture
def harness(monkeypatch, tmp_path) -> dict[str, Any]:
    layout = fake_kernel_layout(tmp_path)
    install_real_broker(monkeypatch, pinned_snapshot("landlock"), layout)
    state: dict[str, Any] = {"procs": [], "killed": [], "launcher": str(layout.kernel_dir)}

    async def _spawn(*argv: Any, **_kwargs: Any) -> _HangingProc:
        proc = _HangingProc(argv)
        state["procs"].append(proc)
        return proc

    def _kill(proc: _HangingProc) -> None:
        state["killed"].append(proc)
        proc.events.append("kill")

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill)
    terminal.reset_async_state_for_test()
    yield state
    terminal.reset_async_state_for_test()


def _launched(proc: _HangingProc, launcher_dir: str) -> bool:
    argv = [str(arg) for arg in proc.argv]
    return argv[1:3] == ["-I", "-S"] and argv[3].startswith(launcher_dir) and "--" in argv


async def _first_proc(state: dict[str, Any]) -> _HangingProc:
    async with asyncio.timeout(5):
        while not state["procs"]:
            await asyncio.sleep(0.005)
    proc = state["procs"][0]
    await asyncio.wait_for(proc.reading.wait(), 5)
    return proc


async def test_background_command_is_stopped_through_the_launcher_process(harness):
    handle = json.loads(
        await terminal.execute_command(
            "sleep 100", ALICE, auth_level="Manual", _hitl_preapproved=True, background=True,
            session_id="s1", _call_id="c1", _ctx={"identity": ALICE},
        )
    )
    proc = await _first_proc(harness)
    record = terminal._ASYNC_RESULTS[handle["execution_id"]]
    assert record["_proc"] is proc and _launched(proc, harness["launcher"])
    stopped = json.loads(await terminal.stop_command(handle["execution_id"], identity=ALICE))
    assert stopped["ok"] is True and record["status"] == "stopped"
    assert harness["killed"] == [proc]


async def test_timeout_kills_the_launcher_process_group(harness):
    exit_code, output, _ms = await terminal._execute_core(
        "sleep 100", "powershell", ".", "s1", timeout=0.05, scope=LANDLOCK
    )
    (proc,) = harness["procs"]
    assert exit_code == -1 and "timed out" in output
    assert _launched(proc, harness["launcher"]) and harness["killed"] == [proc]


async def test_cancel_kills_and_reaps_the_launcher_process(harness):
    task = asyncio.create_task(
        terminal._execute_core("sleep 100", "powershell", ".", "s1", scope=LANDLOCK)
    )
    proc = await _first_proc(harness)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _launched(proc, harness["launcher"])
    assert proc.events == ["kill", "wait"]


def _open_fds() -> set[int]:
    found = set()
    for fd in range(3, 256):
        try:
            os.fstat(fd)
        except OSError:
            continue
        found.add(fd)
    return found


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_status_fd_is_closed_when_the_command_times_out_or_is_cancelled(harness, ending):
    """逾時與取消時，啟動器的狀態 fd（讀端與寫端）也不留在 proxy."""
    before = _open_fds()
    if ending == "timeout":
        await terminal._execute_core(
            "sleep 100", "powershell", ".", "s1", timeout=0.05, scope=LANDLOCK
        )
    else:
        task = asyncio.create_task(
            terminal._execute_core("sleep 100", "powershell", ".", "s1", scope=LANDLOCK)
        )
        await _first_proc(harness)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _open_fds() <= before


async def test_legacy_channel_runs_the_approved_command_through_the_launcher(
    monkeypatch, harness
):
    async def _approve(*_args: Any) -> tuple[bool, None]:
        return True, None

    monkeypatch.setattr(terminal, "_is_touka_ui_available", lambda: True)
    monkeypatch.setattr(terminal_router, "request_approval", _approve)
    pending = json.loads(
        await terminal.execute_command(
            "sleep 100", ALICE, auth_level="Manual", session_id="s1", _call_id="c1",
            _ctx={"identity": ALICE},
        )
    )
    assert pending["status"] == "approval_pending"
    proc = await _first_proc(harness)
    assert _launched(proc, harness["launcher"])
    await terminal.stop_command(pending["execution_id"], identity=ALICE)



# --- 執行器層：殺的是本機 relay（relay 斷線時執行器收掉遠端的整個群組） ---------------


@pytest.fixture
def relay_harness(monkeypatch, tmp_path) -> dict[str, Any]:
    layout = replace(
        fake_kernel_layout(tmp_path), executor_dir=tmp_path / "ipc", in_container=False
    )
    (layout.kernel_dir / "relay.py").write_text("# kernel\n", encoding="utf-8")
    drive = layout.data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    snap = replace(
        pinned_snapshot("executor"), executor=READY, features=frozenset({"fs", "tcp"})
    )
    monitor = install_real_broker(monkeypatch, snap, layout)
    monitor.checked_kernel_digest = kernel_digest(layout.kernel_dir)
    monkeypatch.setattr(agent_command, "build_command_profile", REAL_COMMAND_PROFILE)
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    monkeypatch.setattr(terminal, "_get_agent_drive_path", lambda _identity: drive)
    monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: {"PATH": "/usr/bin"})
    state: dict[str, Any] = {"procs": [], "killed": [], "drive": drive}

    async def _spawn(*argv: Any, **_kwargs: Any) -> _HangingProc:
        proc = _HangingProc(argv)
        state["procs"].append(proc)
        return proc

    def _kill(proc: _HangingProc) -> None:
        state["killed"].append(proc)
        proc.events.append("kill")

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill)
    terminal.reset_async_state_for_test()
    yield state
    terminal.reset_async_state_for_test()


def _relay(proc: _HangingProc) -> bool:
    argv = [str(arg) for arg in proc.argv]
    return argv[1:3] == ["-I", "-S"] and argv[3].endswith("relay.py") and "--request-fd" in argv


async def test_timeout_kills_the_relay_process_group(relay_harness):
    exit_code, output, _ms = await terminal._execute_core(
        "sleep 100", "powershell", relay_harness["drive"], "s1", timeout=0.05, scope=EXECUTOR
    )
    (proc,) = relay_harness["procs"]
    assert exit_code == -1 and "timed out" in output
    assert _relay(proc) and relay_harness["killed"] == [proc]


async def test_background_command_is_stopped_through_the_relay_process(relay_harness):
    handle = json.loads(
        await terminal.execute_command(
            "sleep 100", ALICE, auth_level="Manual", _hitl_preapproved=True, background=True,
            session_id="s1", _call_id="c1", _ctx={"identity": ALICE},
        )
    )
    proc = await _first_proc(relay_harness)
    assert _relay(proc)
    stopped = json.loads(await terminal.stop_command(handle["execution_id"], identity=ALICE))
    assert stopped["ok"] is True and relay_harness["killed"] == [proc]
