"""隔離路徑的 MCP stdio 傳輸（iso-c2 C2-3 U3e-1；差異設計 §3.1、§4.1，設計審查 M1～M3、M7）.

全部用假行程（``asyncio.StreamReader`` 當 stdout／stderr、記錄寫入的 stdin、記錄信號的
killpg），Windows 與 CI 都會跑；真核心（Landlock、執行器、SDK ``Client``）的案例在
``test_isolation_linux.py``。這個模組在 Windows 能匯入 ``isolated_stdio`` 本身就是 M2 的證明
（``os.killpg`` 不能在匯入時碰）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from memory_server.utils.zone_paths import Anchor, HandoffKind, ZonePath, is_handed_off
from src.config import settings
from src.models.identity import AgentIdentity
from src.protocols import manager as manager_mod
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp import environment as env_module
from src.protocols.mcp import isolated_stdio as ist
from src.protocols.mcp import owner as owner_mod
from src.protocols.mcp.client import McpIsolatedLaunch
from src.protocols.mcp.isolated_stdio import (
    McpServerExited,
    McpStdoutLineInvalid,
    McpStdoutLineTooLong,
    isolated_stdio_client,
)
from src.protocols.mcp.owner import McpConnectionOwner, root_cause
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import observability
from src.services.approval_flow import approval_args_preview, card_isolation
from src.services.isolation.consent import ConsentKey
from src.services.isolation.mcp_launch import McpLaunch, McpLaunchBook, McpLauncher, McpLaunchInfo
from src.services.isolation.runners import (
    Acquired,
    ExecRequest,
    IsolationUnavailableError,
    SandboxFailure,
)
from src.services.resource_manager import resource_manager

from tests.helpers import mcp_asgi_peers as peers
from tests.support.isolation import PinnedMonitor, fake_kernel_layout, match_state_root

_WAIT = 5.0
_PID = 4242
_PASS_FDS = (7, 8)
CANARY = "Zq7Kp-SECRET-x81Wm"  # 條目 env 的值（exec_env）：不能出現在任何記錄
PEM = "-----BEGIN KEY-----\nQ0FOQVJZLWxpbmUx\n-----END KEY-----"
_REQUEST = ExecRequest(
    argv=("srv",),
    env={"PATH": "/usr/bin"},
    profile=None,  # 傳輸不看 profile（runner 的 plan 才看）
    io="stdio",
    exec_env={"TOKEN": CANARY, "PEM_KEY": PEM, "FLAG": "1"},
)
_RESPONSE = b'{"jsonrpc":"2.0","id":1,"result":{}}\n'


class _Reader(asyncio.StreamReader):
    """假 pipe 的讀端；``readuntil`` 被取消時記一筆（看取消時是不是先殺群組）."""

    def __init__(self, proc: _Proc, name: str, limit: int) -> None:
        super().__init__(limit=limit)
        self._proc, self._name = proc, name

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        try:
            return await super().readuntil(separator)
        except asyncio.CancelledError:
            self._proc.events.append(f"{self._name}_cancelled")
            raise


class _BoundedPipe:
    """容量一塊的 stderr：沒有人讀，寫的那一方就卡住（像寫滿的 pipe）."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)

    async def write(self, chunk: bytes) -> None:
        await self._queue.put(chunk)

    async def read(self, _size: int) -> bytes:
        return await self._queue.get()

    def feed_eof(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(b"")


class _Stdin:
    def __init__(self, proc: _Proc) -> None:
        self._proc = proc
        self.data = bytearray()
        self.broken = False
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError
        self.data += data

    async def drain(self) -> None:
        if self.broken:
            raise BrokenPipeError
        await asyncio.sleep(0)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._proc.events.append("stdin_closed")
        if self._proc.exits_on_stdin_close:
            self._proc.exit(0)


class _SubprocessTransport:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Proc:
    """假伺服器：pid 是群組長（＝pgid）；SIGKILL 一定死，SIGTERM 與 stdin 關閉看設定."""

    def __init__(
        self,
        *,
        limit: int,
        exits_on_stdin_close: bool = True,
        exits_on_term: bool = True,
        bounded_stderr: bool = False,
    ) -> None:
        self.pid = _PID
        self.events: list[Any] = []
        self.returncode: int | None = None
        self.exits_on_stdin_close = exits_on_stdin_close
        self.exits_on_term = exits_on_term
        self.stdout = _Reader(self, "stdout", limit)
        self.stderr: Any = _BoundedPipe() if bounded_stderr else _Reader(self, "stderr", limit)
        self.stdin = _Stdin(self)
        self._transport = _SubprocessTransport()

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.events.append(("exit", code))
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def killpg(self, pgid: int, sig: int) -> None:
        self.events.append(("killpg", pgid, sig))
        if sig == ist.SIGKILL or (sig == ist.SIGTERM and self.exits_on_term):
            self.exit(-sig)

    def signals(self) -> list[int]:
        kills = [e for e in self.events if isinstance(e, tuple) and e[0] == "killpg"]
        return [event[2] for event in kills]


class _Spawner:
    def __init__(self, **proc_options: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.proc_options = proc_options
        self.proc: _Proc | None = None
        self.error: BaseException | None = None

    async def __call__(self, *argv: Any, **kwargs: Any) -> _Proc:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        self.proc = _Proc(limit=kwargs.get("limit", 2**16), **self.proc_options)
        return self.proc

    def killpg(self, pgid: int, sig: int) -> None:
        assert self.proc is not None
        self.proc.killpg(pgid, sig)


@dataclass
class _Plan:
    argv: tuple[str, ...] = ("python3", "-I", "-S", "launcher.py", "--spec-fd", "7", "--", "srv")
    cwd: Path = Path("st") / "home"
    env: dict[str, str] = field(default_factory=lambda: {"PATH": "/usr/bin", "HOME": "st/home"})
    preexec_fn: Any = None
    pass_fds: tuple[int, ...] = _PASS_FDS
    status: bytes = b""
    events: list[str] = field(default_factory=list)

    def release(self) -> None:
        self.events.append("release")

    def read_status(self) -> bytes:
        self.events.append("read_status")
        return self.status


class _Runner:
    level = "landlock"

    def __init__(self, plan: _Plan | None = None) -> None:
        self.plan_obj = plan or _Plan()
        self.requests: list[ExecRequest] = []

    def plan(self, request: ExecRequest) -> _Plan:
        self.requests.append(request)
        return self.plan_obj

    def failure(self, status: bytes) -> SandboxFailure | None:
        return SandboxFailure(status.decode()) if status else None


def _client(
    runner: _Runner,
    spawner: _Spawner,
    *,
    on_eof: str = "raise",
    reports: list[str] | None = None,
    tails: list[str] | None = None,
    sanitize: Any = None,
) -> Any:
    extra = {} if sanitize is None else {"sanitize": sanitize}
    return isolated_stdio_client(
        runner,
        _REQUEST,
        label="demo",
        report_failure=(reports if reports is not None else []).append,
        on_eof=on_eof,
        stderr_tail=None if tails is None else tails.append,
        spawn=spawner,
        killpg=spawner.killpg,
        **extra,
    )


async def _until(predicate: Any, limit: float = _WAIT) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "condition not reached"
    raise AssertionError(msg)


@pytest.fixture
def no_unretrieved(caplog: pytest.LogCaptureFixture) -> Any:
    yield
    assert peers.task_leak_messages(caplog) == []


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> float:
    """關閉每一段的等待縮短（不真的等 1.5 秒）."""
    for name in ("MCP_STDIO_STDIN_GRACE_SECONDS", "MCP_STDIO_TERM_GRACE_SECONDS"):
        monkeypatch.setattr(ist, name, 0.2)
    monkeypatch.setattr(ist, "MCP_STDIO_REAP_SECONDS", 0.2)
    return 0.6


# ── 啟動：plan 在進入時、spawn 參數、release、狀態代碼 ────────────────────────────────


async def test_spawn_gets_only_the_plans_fds_a_new_group_pipes_and_the_line_limit():
    spawner, runner = _Spawner(), _Runner()
    async with _client(runner, spawner):
        pass
    ((argv, kwargs),) = spawner.calls
    plan = runner.plan_obj
    assert argv == plan.argv
    assert kwargs == {
        "new_process_group": True,
        "cwd": str(plan.cwd),
        "env": plan.env,
        "pass_fds": _PASS_FDS,
        "close_fds": True,
        "preexec_fn": None,
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "limit": 16 * 1024 * 1024,
    }
    assert CANARY not in json.dumps(kwargs["env"])  # exec_env 只在 spec，不在啟動器的環境


async def test_a_cwd_in_the_sandbox_state_is_handed_off_while_spawning(zone_data_root):
    """cwd 在 ST 裡（自建 MCP 的 home）：spawn 當下以子行程的工作資料夾交接、結束後收回（P6）."""
    home = ZonePath(Anchor.SANDBOX_STATE, ("proxy", "mcp_self", "tool", "home"))
    (zone_data_root.parent / "zone_state" / "proxy" / "mcp_self" / "tool" / "home").mkdir(
        parents=True
    )
    text = resource_manager.zone_text(home)
    handed: list[bool] = []

    class _Recording(_Spawner):
        async def __call__(self, *argv: Any, **kwargs: Any) -> _Proc:
            handed.append(is_handed_off(kwargs["cwd"], HandoffKind.SUBPROCESS_CWD))
            return await super().__call__(*argv, **kwargs)

    spawner, runner = _Recording(), _Runner(_Plan(cwd=Path(text)))
    async with _client(runner, spawner):
        pass
    assert handed == [True]
    assert not is_handed_off(text, HandoffKind.SUBPROCESS_CWD)


async def test_plan_happens_on_enter_once_per_entry():
    spawner, runner = _Spawner(), _Runner()
    transport = _client(runner, spawner)
    assert runner.requests == []  # 建好傳輸物件還沒 plan（spawn 前才取 fd）
    async with transport:
        assert runner.requests == [_REQUEST]
    async with _client(runner, spawner):
        pass
    assert len(runner.requests) == len(spawner.calls) == 2


@pytest.mark.parametrize(
    ("status", "reported"), [(b"", []), (b"landlock_failed", ["landlock_failed"])]
)
async def test_release_after_spawn_and_report_only_a_status_code(status, reported):
    runner, spawner, reports = _Runner(_Plan(status=status)), _Spawner(), []
    async with _client(runner, spawner, reports=reports):
        assert runner.plan_obj.events == ["release"]
    assert runner.plan_obj.events == ["release", "read_status"]
    assert reports == reported


async def test_spawn_failure_still_releases_and_reports():
    runner, spawner, reports = _Runner(_Plan(status=b"spec_invalid")), _Spawner(), []
    spawner.error = FileNotFoundError(2, "missing")
    with pytest.raises(FileNotFoundError):
        async with _client(runner, spawner, reports=reports):
            pass
    assert runner.plan_obj.events == ["release", "read_status"]
    assert reports == ["spec_invalid"]


def test_killpg_is_looked_up_when_called_not_at_import():
    """M2：``os.killpg`` 只有 POSIX 有；預設參數若是它，Windows 匯入 client.py 就會失敗."""
    default = inspect.signature(isolated_stdio_client).parameters["killpg"].default
    assert default is ist._killpg


def test_graceful_close_fits_inside_the_owner_deadline():
    total = (
        ist.MCP_STDIO_STDIN_GRACE_SECONDS
        + ist.MCP_STDIO_TERM_GRACE_SECONDS
        + ist.MCP_STDIO_REAP_SECONDS
    )
    assert total <= 4.0 < owner_mod.OWNER_CLOSE_SECONDS


# ── stdout：一行一則、上限、壞行 ─────────────────────────────────────────────────


async def test_a_200_kib_line_round_trips():
    spawner = _Spawner()
    blob = "x" * (200 * 1024)
    async with _client(_Runner(), spawner) as (read, _write):
        line = {"jsonrpc": "2.0", "id": 1, "result": {"blob": blob}}
        spawner.proc.stdout.feed_data(json.dumps(line).encode() + b"\n")
        item = await asyncio.wait_for(read.receive(), _WAIT)
    assert isinstance(item, SessionMessage)
    assert item.message.result == {"blob": blob}


async def test_a_line_over_the_limit_fails_the_connection_and_kills_the_group(monkeypatch):
    monkeypatch.setattr(ist, "MCP_STDIO_LINE_LIMIT", 1024)
    spawner = _Spawner(exits_on_stdin_close=False)
    with pytest.raises(BaseExceptionGroup) as caught:
        async with _client(_Runner(), spawner):
            spawner.proc.stdout.feed_data(b"x" * 4096 + b"\n")
            await asyncio.sleep(_WAIT)
    assert type(root_cause(caught.value)) is McpStdoutLineTooLong
    assert ist.SIGKILL in spawner.proc.signals()


async def test_messages_are_written_one_json_line_each():
    spawner = _Spawner()
    request = types.JSONRPCRequest(jsonrpc="2.0", id=7, method="ping")
    async with _client(_Runner(), spawner) as (_read, write):
        await write.send(SessionMessage(request))
        await _until(lambda: spawner.proc.stdin.data)
    expected = request.model_dump_json(by_alias=True, exclude_unset=True).encode() + b"\n"
    assert bytes(spawner.proc.stdin.data) == expected


async def test_a_bad_line_becomes_a_typed_value_without_its_text(caplog):
    """M3①：pydantic 的錯誤字串帶 ``input_value``（伺服器把環境印到 stdout 時，值就在裡面）；
    交給 session 的是只帶型別的例外，不記原文，之後的行照常讀."""
    spawner = _Spawner()
    async with _client(_Runner(), spawner) as (read, _write):
        spawner.proc.stdout.feed_data(f"TOKEN={CANARY}\n".encode())
        bad = await asyncio.wait_for(read.receive(), _WAIT)
        spawner.proc.stdout.feed_data(_RESPONSE)
        good = await asyncio.wait_for(read.receive(), _WAIT)
    assert isinstance(bad, McpStdoutLineInvalid)
    assert "ValidationError" in str(bad)
    assert CANARY not in str(bad) and bad.__cause__ is None and bad.__context__ is None
    assert isinstance(good, SessionMessage)
    assert CANARY not in caplog.text


# ── 伺服器結束、寫不進去、超限：owner 失聯恰好一次（life1 的 on_lost）────────────────────


async def _owned(spawner: _Spawner, lost: list[BaseException], **client: Any) -> Any:
    async def on_lost(_owner: McpConnectionOwner, error: BaseException) -> None:
        lost.append(error)

    async def connect() -> tuple[Any, AsyncExitStack]:
        stack = AsyncExitStack()
        streams = await stack.enter_async_context(_client(_Runner(), spawner, **client))
        return SimpleNamespace(streams=streams), stack

    owner = McpConnectionOwner("demo", connect, on_lost=on_lost, registry=set())
    return owner, await owner.start()


@pytest.mark.parametrize("trigger", ["eof", "stdin", "overrun"])
async def test_server_end_makes_the_owner_report_lost_exactly_once(
    monkeypatch, no_unretrieved, trigger
):
    monkeypatch.setattr(ist, "MCP_STDIO_LINE_LIMIT", 1024)
    spawner, lost = _Spawner(), []
    owner, client = await _owned(spawner, lost)
    proc = spawner.proc
    if trigger == "eof":
        proc.exit(1)
    elif trigger == "stdin":
        proc.stdin.broken = True
        ping = SessionMessage(types.JSONRPCRequest(jsonrpc="2.0", id=1, method="ping"))
        await client.streams[1].send(ping)
    else:
        proc.stdout.feed_data(b"y" * 4096 + b"\n")
    await _until(lambda: owner.task.done())
    await owner.aclose()
    expected = McpStdoutLineTooLong if trigger == "overrun" else McpServerExited
    assert [type(error) for error in lost] == [expected]
    assert not owner.alive
    assert asyncio.current_task().cancelling() == 0  # 建立者沒被取消
    await asyncio.sleep(0)


async def test_requested_close_is_quiet(no_unretrieved, caplog):
    """要求關閉：伺服器收到 stdin 關閉就結束，EOF 在收尾中，不拋、不通知、不殺、不記 WARNING."""
    spawner, lost = _Spawner(), []
    owner, _ = await _owned(spawner, lost)
    await owner.aclose()
    assert lost == []
    assert spawner.proc.signals() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_launch_transport_loss_and_public_diagnostics_keep_environment_private(
    monkeypatch, tmp_path, caplog, no_unretrieved
):
    layout = fake_kernel_layout(tmp_path)
    match_state_root(monkeypatch, layout.state_root)
    monitor, book = PinnedMonitor(), McpLaunchBook()
    runner, spawner = _Runner(), _Spawner()
    broker = SimpleNamespace(acquire=AsyncMock(return_value=Acquired(runner, None)))
    launcher = McpLauncher(
        monitor=monitor, broker=broker, layout_provider=lambda: layout,
        base_dir=lambda: tmp_path, book=book,
    )
    config = ProtocolServerConfig(command="fake", env={"TOKEN": CANARY})
    reader = SimpleNamespace(record_for=Mock(return_value=SimpleNamespace(
        effective_origin="creation", records_unreadable=False, state="ok",
        usable_sandbox_paths=(),
    )))
    launch = await launcher.plan_server_launch(
        "s", config, reader, tool_base_url="http://localhost:8000"
    )
    registry = ProtocolRegistry("__no_secret_config__.json")
    registry.servers = {"s": config}
    manager = manager_mod.ProtocolManager(registry, launch_book=book, monitor=monitor)
    monkeypatch.setattr(manager, "_start_subscription", Mock())
    monkeypatch.setattr(manager, "_connection_lost_hook", Mock())
    monkeypatch.setattr(observability, "mcp_launch_book", book)

    async def connect(_config, *, isolated, **_kwargs):
        stack = AsyncExitStack()
        streams = await stack.enter_async_context(isolated_stdio_client(
            isolated.launch.acquired.runner,
            ExecRequest(argv=isolated.launch.argv, env={}, profile=None,
                        exec_env=isolated.launch.exec_env),
            label=isolated.label, report_failure=isolated.report_failure,
            on_eof=isolated.on_eof, sanitize=isolated.sanitize,
            spawn=spawner, killpg=spawner.killpg,
        ))
        session = await stack.enter_async_context(ClientSession(*streams))
        original_write = spawner.proc.stdin.write

        def reply(data):
            original_write(data)
            request = json.loads(data)
            if request.get("method") == "tools/list":
                response = {"jsonrpc": "2.0", "id": request["id"],
                            "error": {"code": -32603, "message": CANARY}}
                spawner.proc.stdout.feed_data(json.dumps(response).encode() + b"\n")

        spawner.proc.stdin.write = reply
        spawner.proc.stderr.feed_data((CANARY + "\n").encode())
        return session, stack

    monkeypatch.setattr(manager_mod, "connect_mcp_server", connect)
    try:
        await manager.connect_server("s", config, launch=launch)
        owner = manager.handle_pool.get(manager.handle_key_for("s", None)).stack
        before = await manager.get_active_server_info()
        assert before[0]["status"] == "error"
        assert "<redacted>" in before[0]["last_error"]
        assert runner.requests[0].exec_env["TOKEN"] == CANARY
        broker.acquire.assert_awaited_once()
        spawner.proc.exit(1)
        await _until(lambda: owner.task.done())
        after = await manager.get_active_server_info()
        assert after[0]["status"] == "error"
        assert "MCP connection lost" in after[0]["last_error"]
        isolation = await observability.get_isolation_status(
            AgentIdentity.for_role("Probe", "Debugger")
        )
        preview = approval_args_preview({"query": "example"})
        card = await card_isolation(ConsentKey("Probe", "session", "call"), "s__ping")
        public = json.dumps([before, after, isolation.model_dump(), preview]) + repr(card)
        assert CANARY not in public + caplog.text
    finally:
        await manager.cleanup()


async def test_a_line_after_the_session_left_is_drained_not_raised(no_unretrieved, caplog):
    """M1：session 先關掉讀端、自己再收尾一下，這時伺服器送來一行：只排空，不拋、不殺、
    owner 不記「closed with error」."""
    spawner, lost = _Spawner(), []

    async def on_lost(_owner: McpConnectionOwner, error: BaseException) -> None:
        lost.append(error)

    async def connect() -> tuple[Any, AsyncExitStack]:
        stack = AsyncExitStack()
        read, _write = await stack.enter_async_context(_client(_Runner(), spawner))

        async def session_exit() -> None:  # 像 SDK：dispatcher 先關讀端，session 的收尾還要時間
            read.close()
            await asyncio.sleep(0.1)

        stack.push_async_callback(session_exit)
        return SimpleNamespace(), stack

    owner = McpConnectionOwner("demo", connect, on_lost=on_lost, registry=set())
    await owner.start()
    spawner.proc.stdout.feed_data(b'{"jsonrpc":"2.0","method":"notifications/progress"}\n')
    await asyncio.sleep(0.05)  # 讀端卡在送給 session
    await owner.aclose()
    assert lost == []
    assert spawner.proc.signals() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_close_mode_ends_the_session_without_cancelling_the_host():
    """M7：不在 owner 裡用（演化驗證、創建試跑）要選 ``close``：伺服器結束時只關讀端，等著的請求
    拿 CONNECTION_CLOSED，呼叫端自己的 task 不被取消."""
    spawner = _Spawner()
    async with (
        _client(_Runner(), spawner, on_eof="close") as (read, write),
        ClientSession(read, write) as session,
    ):
        ping = asyncio.create_task(session.send_ping())
        await _until(lambda: spawner.proc.stdin.data)
        spawner.proc.exit(1)
        with pytest.raises(MCPError) as caught:
            await asyncio.wait_for(ping, _WAIT)
    assert caught.value.code == types.CONNECTION_CLOSED
    assert asyncio.current_task().cancelling() == 0


async def test_close_mode_also_ends_the_session_when_stdin_breaks():
    spawner = _Spawner()
    async with _client(_Runner(), spawner, on_eof="close") as (read, write):
        spawner.proc.stdin.broken = True
        await write.send(SessionMessage(types.JSONRPCRequest(jsonrpc="2.0", id=1, method="ping")))
        with pytest.raises(anyio.EndOfStream):
            await asyncio.wait_for(read.receive(), _WAIT)
    assert asyncio.current_task().cancelling() == 0


# ── 關閉與取消 ─────────────────────────────────────────────────────────────────────


async def test_close_escalates_stdin_then_sigterm_then_sigkill(quick):
    spawner = _Spawner(exits_on_stdin_close=False, exits_on_term=False)
    started = time.monotonic()
    async with _client(_Runner(), spawner):
        pass
    elapsed = time.monotonic() - started
    order = [e for e in spawner.proc.events if e == "stdin_closed" or e[0] == "killpg"]
    assert order == ["stdin_closed", ("killpg", _PID, ist.SIGTERM), ("killpg", _PID, ist.SIGKILL)]
    assert elapsed < quick + 1.0


async def test_close_stops_at_sigterm_when_that_is_enough(quick):
    spawner = _Spawner(exits_on_stdin_close=False)
    async with _client(_Runner(), spawner):
        pass
    assert spawner.proc.signals() == [ist.SIGTERM]


async def test_cancel_kills_the_group_before_waiting_on_anything():
    spawner = _Spawner(exits_on_stdin_close=False)
    entered = asyncio.Event()

    async def body() -> None:
        async with _client(_Runner(), spawner):
            entered.set()
            await asyncio.sleep(_WAIT)

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = spawner.proc.events
    kill = events.index(("killpg", _PID, ist.SIGKILL))
    assert "stdout_cancelled" not in events[:kill]  # 先殺，才等讀的工作結束
    assert "stdin_closed" not in events[:kill]  # 不是優雅關閉
    assert spawner.proc._transport.closed == 1


async def test_no_killpg_once_the_leader_is_reaped():
    """群組長已被回收（孫行程還拿著 stdout）：pgid 可能被重用，不 killpg；
    仍關掉子行程的 transport."""
    spawner = _Spawner(exits_on_stdin_close=False)
    entered = asyncio.Event()

    async def body() -> None:
        async with _client(_Runner(), spawner):
            spawner.proc.returncode = 0
            entered.set()
            await asyncio.sleep(_WAIT)

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT)
    assert spawner.proc.signals() == []
    assert spawner.proc._transport.closed == 1


# ── stderr：一直讀、遮罩後截斷、有上限 ───────────────────────────────────────────────


async def test_stderr_is_read_while_stdout_is_quiet():
    """伺服器先寫 1 MiB stderr 才回應：傳輸一直讀 stderr，伺服器不會卡在寫滿的 pipe."""
    spawner = _Spawner(bounded_stderr=True)
    async with _client(_Runner(), spawner) as (read, _write):
        proc = spawner.proc

        async def server() -> None:
            for _ in range(16):
                await proc.stderr.write(b"e" * 65536)
            proc.stdout.feed_data(_RESPONSE)

        writer = asyncio.create_task(server())
        item = await asyncio.wait_for(read.receive(), _WAIT)
        await writer
    assert isinstance(item, SessionMessage)


def _stderr_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ist.__name__]


async def test_stderr_log_is_masked_before_it_is_cut_and_is_rate_limited(monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr(ist, "_monotonic_seconds", lambda: clock[0])
    caplog.set_level(logging.INFO, logger=ist.__name__)
    spawner, tails = _Spawner(), []
    secret = CANARY.encode()
    pem_line = PEM.splitlines()[1].encode()

    def sanitize(text: str) -> str:
        return text.replace("/srv/private", "<path>")

    async with _client(_Runner(), spawner, tails=tails, sanitize=sanitize):
        err = spawner.proc.stderr
        err.feed_data(b"A" * 2040 + secret + b"\n")  # 值跨過 2 KiB 的截斷處
        err.feed_data(b"split " + secret[:6])  # 值跨兩塊
        await asyncio.sleep(0.01)
        err.feed_data(secret[6:] + b" at /srv/private/x\n")
        err.feed_data(b"pem " + pem_line + b"\n")  # 多行的值逐行遮罩
        err.feed_data(b"B" * 3000 + secret[:9])  # 一直不換行：這段丟掉，到換行為止
        await asyncio.sleep(0.01)
        err.feed_data(secret[9:] + b" tail-of-long\n")
        for index in range(60):
            err.feed_data(f"line {index} {CANARY}\n".encode())
        await asyncio.sleep(0.05)
        clock[0] += ist.MCP_STDERR_LOG_WINDOW_SECONDS + 1
        err.feed_data(b"after the window\n")
        await asyncio.sleep(0.05)
    messages = _stderr_messages(caplog)
    text = "\n".join(messages)
    for fragment in (CANARY, CANARY[:6], CANARY[6:], CANARY[:9], CANARY[9:], pem_line.decode()):
        assert fragment not in text
        assert fragment not in tails[0]
    assert "tail-of-long" not in text and "BBBB" not in text  # 過長未換行的整段丟掉（只計數）
    assert "<redacted>" in text and "split <redacted> at <path>/x" in text
    lines = [m for m in messages if "stderr:" in m]
    assert all(len(m.encode()) <= ist.MCP_STDERR_LOG_LINE_BYTES + 64 for m in lines)
    first_window = lines[: lines.index(next(m for m in lines if "after the window" in m))]
    assert len(first_window) == ist.MCP_STDERR_LOG_LINES
    assert any("not logged" in m for m in messages)
    assert len(tails) == 1 and len(tails[0].encode()) <= ist.MCP_STDERR_TAIL_BYTES
    assert "after the window" in tails[0]


# ── client.py 的分流：只有拿到 launch（有 acquired）才走自寫傳輸，direct 照今天 ────────────────


class _FakeSdkClient:
    """``Client(target, ...)``：進入時進 target、離開時出 target；discover 回最小的合法結果."""

    def __init__(self, target: Any, **_kwargs: Any) -> None:
        self._target = target

        async def discover(_version: str) -> dict[str, Any]:
            return {
                "cacheScope": "public",
                "capabilities": {},
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "ttlMs": 0,
            }

        self.session = SimpleNamespace(send_discover=discover, adopt=lambda _result: None)

    async def __aenter__(self) -> _FakeSdkClient:
        await self._target.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return bool(await self._target.__aexit__(*exc))


@pytest.fixture
def transports(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """攔下兩種傳輸：SDK 的 ``stdio_client`` 與自寫的 ``isolated_stdio_client``."""
    seen: dict[str, list[Any]] = {"sdk": [], "isolated": []}

    @contextlib.asynccontextmanager
    async def sdk(params: Any) -> Any:
        seen["sdk"].append(params)
        yield ("read", "write")

    @contextlib.asynccontextmanager
    async def isolated(runner: Any, request: Any, **kwargs: Any) -> Any:
        seen["isolated"].append((runner, request, kwargs))
        yield ("read", "write")

    monkeypatch.setattr(mcp_client, "stdio_client", sdk)
    monkeypatch.setattr(mcp_client, "isolated_stdio_client", isolated)
    monkeypatch.setattr(mcp_client, "Client", _FakeSdkClient)
    monkeypatch.setattr(mcp_client, "ISOLATED_STDIO_SUPPORTED", True)
    return seen


def _launch(*, acquired: bool = True) -> McpLaunch:
    info = McpLaunchInfo("self", "evolution", "ok", "launched", "landlock")
    if not acquired:
        return McpLaunch(info)
    profile = SimpleNamespace(name="mcp_self")
    return McpLaunch(
        info, Acquired(_Runner(), profile), ("python3", "server.py"), {"TOKEN": CANARY}
    )


def _isolated(launch: McpLaunch, **extra: Any) -> McpIsolatedLaunch:
    return McpIsolatedLaunch(
        launch, label="demo", report_failure=lambda _code: None, on_eof="raise", **extra
    )


async def _connect(**kwargs: Any) -> None:
    config = ProtocolServerConfig(command="python3", args=["server.py"], env={"TOKEN": CANARY})
    _client_, stack = await mcp_client.connect_mcp_server(config, **kwargs)
    await stack.aclose()


async def test_an_isolated_launch_goes_through_the_isolated_transport(monkeypatch, transports):
    monkeypatch.setattr(env_module, "get_default_environment", lambda: {"PATH": "sdk-path"})
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: {"LANG": "C", "K": CANARY})
    launch = _launch()
    tails: list[str] = []
    isolated = _isolated(launch, sanitize=str.upper, stderr_tail=tails.append)
    await _connect(isolated=isolated)
    assert transports["sdk"] == []
    ((runner, request, kwargs),) = transports["isolated"]
    assert runner is launch.acquired.runner
    assert request.env == {"PATH": "sdk-path", "LANG": "C"}  # 啟動器環境：SDK 預設＋允許清單
    assert (request.argv, request.io, dict(request.exec_env)) == (
        ("python3", "server.py"),
        "stdio",
        {"TOKEN": CANARY},
    )
    assert request.profile is launch.acquired.profile
    assert kwargs == {
        "label": "demo",
        "report_failure": isolated.report_failure,
        "on_eof": "raise",
        "sanitize": str.upper,
        "stderr_tail": tails.append,
    }


async def test_without_a_launch_the_sdk_transport_is_used_like_today(transports):
    await _connect()
    assert transports["isolated"] == []
    (params,) = transports["sdk"]
    assert params.command == "python3"
    assert params.env["TOKEN"] == CANARY  # direct：條目 env 照今天進 SDK 的環境
    assert params.env["TOOL_BASE_URL"] == mcp_client.tool_base_url()


@pytest.mark.parametrize("case", ["windows", "no_runner"])
async def test_an_isolated_launch_never_falls_back_to_the_sdk_transport(
    transports, monkeypatch, case
):
    """Windows（判定本來就不會給 acquired，這是縱深）或沒有 runner：拒絕，不改走未隔離."""
    if case == "windows":
        monkeypatch.setattr(mcp_client, "ISOLATED_STDIO_SUPPORTED", False)
    with pytest.raises(IsolationUnavailableError):
        await _connect(isolated=_isolated(_launch(acquired=case == "windows")))
    assert transports == {"sdk": [], "isolated": []}


def test_isolated_stdio_is_supported_only_off_windows():
    assert mcp_client.ISOLATED_STDIO_SUPPORTED is (sys.platform != "win32")


_UNSPECIFIED = str(ipaddress.IPv4Address(0))


@pytest.mark.parametrize(
    ("host", "expected"), [(_UNSPECIFIED, "localhost"), ("10.1.2.3", "10.1.2.3")]
)
def test_tool_base_url_maps_the_unspecified_host_to_localhost(monkeypatch, host, expected):
    monkeypatch.setattr(settings, "HOST", host)
    monkeypatch.setattr(settings, "PORT", 8123)
    assert mcp_client.tool_base_url() == f"http://{expected}:8123"


def test_stderr_escapes_terminal_and_direction_controls(caplog):
    sink = ist.StderrLog('server', ('secret-token',), None)
    with caplog.at_level(logging.INFO, logger=ist.__name__):
        sink.feed('secret-token\rAUDIT:\x1b[2J\x85\u202e\u2066\tend\n'.encode())
        sink.close()
    for character in ('\r', '\x1b', '\x85', '\u202e', '\u2066'):
        assert character not in caplog.text + sink.tail()
    assert 'secret-token' not in caplog.text + sink.tail()
    assert '\\x1b' in sink.tail() and '\\u202e' in sink.tail()
    assert '\tend' in sink.tail()
