"""重置與刪除停掉代理人的全部終端指令（ADR-0293 R11）.

- 行程帶 agent_id 登記（沒有 session 也登記），cwd 經 ``zone_handoff`` 交出到 spawn 回來；
- 代理人正在重置或刪除時不啟動（進來時、spawn 前與登記後都查），回 ``permission_denied``
  （原因 ``agent_busy``）；
- ``stop_agent_commands`` 取消還沒結束的背景與待審批工作（含被擠出結果存放區的），殺掉登記的
  行程，結果存放區裡的紀錄改成 ``stopped``；別的代理人的指令不受影響；
- 開始之後個人空間才被換成連結：spawn 前的交出拒絕，不執行。
spawn 與殺行程換成替身（不啟動真的 shell），不打網路；data 根換到 ``zone_data_root``。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
import src.routers.terminal as terminal_router
from memory_server.utils.zone_paths import HandoffKind, is_handed_off
from src.models.identity import AgentIdentity
from src.services import destructive_ops
from src.services import process_registry as process_registry_module
from src.services.process_registry import process_registry
from src.tools import terminal
from src.utils.async_db import session_service

from tests.support.links import make_dir_link

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
BOB = AgentIdentity.for_role("Bob", "Owner", capabilities=[])
WAIT_STEP_SECONDS = 0.01
WAIT_LIMIT_SECONDS = 5


class _Proc:
    """替身行程：讀到 EOF 之前一直等（``finish`` 放行）；殺的時候由替身的 kill 結束它."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = self
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()

    async def readline(self) -> bytes:
        await self._done.wait()
        return b""

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code
        self._done.set()


@pytest.fixture
def procs(monkeypatch, zone_data_root) -> dict[str, Any]:
    state: dict[str, Any] = {"spawned": [], "killed": [], "after_spawn": None}

    async def _spawn(*_argv: Any, **kwargs: Any) -> _Proc:
        proc = _Proc()
        cwd = kwargs["cwd"]
        state["spawned"].append(
            {"proc": proc, "cwd": cwd, "handed": is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)}
        )
        if state["after_spawn"] is not None:
            state["after_spawn"]()
        return proc

    def _kill(proc: _Proc) -> None:  # 在執行緒裡被呼叫
        state["killed"].append(proc)
        proc._loop.call_soon_threadsafe(proc.finish, -9)

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(terminal, "terminate_process_tree", _kill)
    monkeypatch.setattr(process_registry_module, "terminate_process_tree", _kill)
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    terminal.reset_async_state_for_test()
    yield state
    terminal.reset_async_state_for_test()
    destructive_ops.reload_all()


async def _until(condition: Callable[[], Any]) -> None:
    async with asyncio.timeout(WAIT_LIMIT_SECONDS):
        while not condition():
            await asyncio.sleep(WAIT_STEP_SECONDS)


async def _execute(identity: AgentIdentity = ALICE, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "auth_level": "Manual",
        "_hitl_preapproved": True,
        "session_id": "s1",
        "_call_id": "c1",
        "_ctx": {"identity": identity},
    }
    values.update(overrides)
    return await terminal.execute_command("ls", identity, **values)


def _assert_busy(text: str) -> None:
    payload = json.loads(text)
    assert payload["error_class"] == "permission_denied", payload
    assert payload["reason"] == "agent_busy", payload


@pytest.mark.parametrize("session_id", ["s1", None])
async def test_a_foreground_command_is_handed_off_registered_and_stopped(procs, session_id):
    task = asyncio.create_task(_execute(session_id=session_id))
    await _until(lambda: procs["spawned"])
    spawned = procs["spawned"][0]
    assert spawned["handed"] is True  # spawn 當下 cwd 交出中
    assert not is_handed_off(spawned["cwd"], HandoffKind.SUBPROCESS_CWD)  # spawn 回來就取消
    assert "Alice" in spawned["cwd"]
    assert await terminal.stop_agent_commands(ALICE) == 1
    await task
    assert procs["killed"] == [spawned["proc"]]
    assert process_registry.terminate_agent("Alice") == 0  # 跑完已取消登記


async def test_background_and_pending_commands_are_cancelled_and_marked(procs, monkeypatch):
    approval_gate = asyncio.Event()

    async def _request_approval(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, Any]]:
        await approval_gate.wait()
        return True, {}

    monkeypatch.setattr(terminal, "_is_touka_ui_available", lambda: True)
    monkeypatch.setattr(terminal_router, "request_approval", _request_approval)
    monkeypatch.setattr(terminal, "_MAX_ASYNC_RESULTS", 1)  # 第二筆把待審批的擠出結果存放區
    pending = json.loads(await _execute(_hitl_preapproved=False, _call_id="c1"))
    assert pending["status"] == "approval_pending"
    running = json.loads(await _execute(background=True, _call_id="c2"))
    assert running["status"] == "running"
    await _until(lambda: procs["spawned"])
    assert pending["execution_id"] not in terminal._ASYNC_RESULTS

    await terminal.stop_agent_commands(ALICE)
    approval_gate.set()  # 已取消：核准回來也不會開始
    await asyncio.sleep(WAIT_STEP_SECONDS * 5)
    assert len(procs["spawned"]) == 1
    assert procs["killed"] == [procs["spawned"][0]["proc"]]
    record = terminal._ASYNC_RESULTS[running["execution_id"]]
    assert record["status"] == "stopped"
    _assert_busy(record["result"])


async def test_other_agents_commands_keep_running(procs):
    task = asyncio.create_task(_execute(BOB))
    await _until(lambda: procs["spawned"])
    assert await terminal.stop_agent_commands(ALICE) == 0
    assert procs["killed"] == []
    procs["spawned"][0]["proc"].finish()
    assert "[EXIT_CODE: 0]" in await task


async def test_a_held_agent_does_not_start_a_command(procs):
    destructive_ops.hold(ALICE)
    _assert_busy(await _execute())
    _assert_busy(await _execute(background=True))
    assert procs["spawned"] == []


async def test_a_reset_that_starts_after_the_command_is_queued_stops_it(procs):
    """排進背景時沒被持有、開始跑之前才開始重置：不 spawn，紀錄回 agent_busy."""
    running = json.loads(await _execute(background=True))
    destructive_ops.hold(ALICE)  # 背景工作還沒開始跑（中間沒有讓出事件迴圈）
    record = terminal._ASYNC_RESULTS[running["execution_id"]]
    await _until(lambda: record["status"] != "running")
    assert procs["spawned"] == []
    _assert_busy(record["result"])


async def test_a_reset_that_starts_during_spawn_stops_the_command(procs):
    """檢查之後、登記之前才開始的重置：登記後再看一次，停掉這個行程."""
    procs["after_spawn"] = lambda: destructive_ops.hold(ALICE)
    _assert_busy(await _execute())
    assert procs["killed"] == [procs["spawned"][0]["proc"]]
    assert process_registry.terminate_agent("Alice") == 0


async def test_a_drive_linked_after_the_start_is_not_used(procs, zone_data_root, tmp_path: Path):
    """背景指令排進去之後個人空間才被換成連結：spawn 前的交出拒絕，不執行."""
    running = json.loads(await _execute(background=True))
    folder = zone_data_root / "personal_drive" / "Alice"  # 背景工作還沒開始跑
    folder.rmdir()
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    make_dir_link(folder, fake)
    record = terminal._ASYNC_RESULTS[running["execution_id"]]
    await _until(lambda: record["status"] != "running")
    assert procs["spawned"] == []
    payload = json.loads(record["result"])
    assert payload["error_class"] == "permission_denied"
    assert str(fake) not in record["result"]
