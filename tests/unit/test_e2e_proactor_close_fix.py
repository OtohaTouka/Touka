"""實際驗證後端的 Windows 連線收尾修正（ADR-0295）.

CI 只跑 Linux，所以用真的 asyncio 類別模擬 Windows 上失敗的那一步：
``_ProactorBasePipeTransport`` 關連線時只用到事件迴圈的 ``call_soon``，在哪個平台都建得起來。
真的 Windows 連線只在本機驗證過，結果見 ADR-0295。
"""

import asyncio
import asyncio.proactor_events
import importlib.util
import logging
import socket
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "e2e" / "proactor_close_fix.py"
_spec = importlib.util.spec_from_file_location("proactor_close_fix", MODULE_PATH)
proactor_close_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proactor_close_fix)

Transport = asyncio.proactor_events._ProactorBasePipeTransport
WSAECONNRESET = 10054
WSAECONNABORTED = 10053
WSAENOTCONN = 10057
FINISHES_WITHIN_S = 1.0
STILL_WAITING_AFTER_S = 0.1
ONE_LOOP_TURN_S = 0.01
REPAIR_LOG = "finished closing a connection"


class ScriptedSocket(socket.socket):
    """真的 socket，只有 ``shutdown`` 照測試的安排."""

    def __init__(self, shutdown_error: BaseException | None = None) -> None:
        super().__init__()
        self.shutdown_error = shutdown_error
        self.shutdown_calls = 0

    def shutdown(self, how: int) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


class FakePipe:
    """子行程管線的形狀：沒有 ``shutdown``."""

    def __init__(self) -> None:
        self.closed = False

    def fileno(self) -> int:
        return 7

    def close(self) -> None:
        self.closed = True


class RecordingProtocol(asyncio.Protocol):
    def __init__(self, error: BaseException | None = None) -> None:
        self.lost: list[BaseException | None] = []
        self.error = error

    def connection_lost(self, exc: BaseException | None) -> None:
        self.lost.append(exc)
        if self.error is not None:
            raise self.error


@pytest.fixture
def fix_installed(monkeypatch):
    # install() 直接改類別；先登記原值，測試結束 monkeypatch 會放回去
    monkeypatch.setattr(Transport, "_call_connection_lost", Transport._call_connection_lost)
    proactor_close_fix.install()


@pytest.fixture
async def server():
    listening = await asyncio.get_running_loop().create_server(asyncio.Protocol, "127.0.0.1", 0)
    yield listening
    listening.close()


@pytest.fixture
async def loop_errors():
    errors: list[BaseException] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: errors.append(context["exception"])
    )
    return errors


async def _close(transport: Transport) -> None:
    transport.close()
    await asyncio.sleep(ONE_LOOP_TURN_S)


def _transport(sock, protocol: asyncio.Protocol, server=None) -> Transport:
    return Transport(asyncio.get_running_loop(), sock, protocol, server=server)


async def test_without_the_fix_the_server_never_finishes_waiting(server, loop_errors):
    # 這條釘住要修的缺陷真的存在；Python 修好時會變紅，那時可以拿掉 proactor_close_fix
    sock = ScriptedSocket(ConnectionResetError(WSAECONNRESET, "reset"))
    try:
        await _close(_transport(sock, RecordingProtocol(), server))
        server.close()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), STILL_WAITING_AFTER_S)
        assert [type(error) for error in loop_errors] == [ConnectionResetError]
    finally:
        sock.close()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionResetError(WSAECONNRESET, "reset"), id="reset"),
        pytest.param(ConnectionAbortedError(WSAECONNABORTED, "aborted"), id="aborted"),
        pytest.param(OSError(WSAENOTCONN, "not connected"), id="not-connected"),
    ],
)
async def test_server_finishes_waiting_after_the_peer_reset(
    error, fix_installed, server, loop_errors, caplog
):
    sock = ScriptedSocket(error)
    protocol = RecordingProtocol()
    with caplog.at_level(logging.WARNING):
        await _close(_transport(sock, protocol, server))
    server.close()
    await asyncio.wait_for(server.wait_closed(), FINISHES_WITHIN_S)
    assert sock.fileno() == -1
    assert protocol.lost == [None]
    assert loop_errors == []
    assert sum(REPAIR_LOG in message for message in caplog.messages) == 1


async def test_normal_close_still_calls_shutdown(fix_installed, server, loop_errors, caplog):
    sock = ScriptedSocket()
    protocol = RecordingProtocol()
    with caplog.at_level(logging.WARNING):
        await _close(_transport(sock, protocol, server))
    assert sock.shutdown_calls == 1
    assert sock.fileno() == -1
    assert protocol.lost == [None]
    assert loop_errors == []
    assert not any(REPAIR_LOG in message for message in caplog.messages)


async def test_protocol_errors_are_not_swallowed(fix_installed, server, loop_errors):
    boom = RuntimeError("protocol failed")
    sock = ScriptedSocket()
    await _close(_transport(sock, RecordingProtocol(boom), server))
    server.close()
    await asyncio.wait_for(server.wait_closed(), FINISHES_WITHIN_S)
    assert loop_errors == [boom]
    assert sock.fileno() == -1


async def test_errors_that_are_not_network_errors_still_surface(fix_installed, server, loop_errors):
    boom = ValueError("not a network error")
    sock = ScriptedSocket(boom)
    try:
        await _close(_transport(sock, RecordingProtocol(), server))
        assert loop_errors == [boom]
    finally:
        sock.close()


async def test_a_connection_that_is_still_open_keeps_the_server_waiting(
    fix_installed, server, loop_errors
):
    # 真的卡住要判失敗（ADR-0295 決定 3、9）：修正不能讓伺服器提早放行
    sock = ScriptedSocket()
    transport = _transport(sock, RecordingProtocol(), server)
    server.close()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.wait_closed(), STILL_WAITING_AFTER_S)
    assert server._active_count == 1
    await _close(transport)
    await asyncio.wait_for(server.wait_closed(), FINISHES_WITHIN_S)
    assert loop_errors == []


async def test_subprocess_pipes_are_left_alone(fix_installed, loop_errors):
    pipe = FakePipe()
    transport = _transport(pipe, RecordingProtocol())
    await _close(transport)
    assert pipe.closed
    assert transport._sock is None
    assert loop_errors == []


async def test_a_second_call_does_nothing(fix_installed, loop_errors):
    sock = ScriptedSocket()
    protocol = RecordingProtocol()
    transport = _transport(sock, protocol)
    await _close(transport)
    transport._call_connection_lost(None)
    assert transport._sock is None
    assert protocol.lost == [None]
    assert sock.shutdown_calls == 1
    assert loop_errors == []
