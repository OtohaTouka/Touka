"""proxy 端 relay（``sandbox_kernel/relay.py``，frames 與 stdio 模式）：用 socketpair 扮演執行器.

任何平台都能跑。

- 權杖每次連線都從 ipc 資料夾的檔案讀；argv 與環境變數都沒有權杖；讀到空的或格式不對就不連線；
- READY 的 ``boot_id`` 和 argv 的不同：``executor_restarted``，不送 START；
- 每收到一塊輸出就寫出；exit 帶遠端的 code（被信號殺掉是 128＋signal）；
- EXIT 之前連線斷掉：``executor_unreachable``；ERROR／遠端啟動器的失敗代碼經 ``--status-fd`` 回報；
- stdio 模式（``start.io`` 是 ``stdio``，C2-3）：stdin 每塊最多 64 KiB 轉成 STDIN，stdin 結束才送
  STDIN_EOF；OUT 寫到 stdout、ERR 寫到 stderr。
"""

from __future__ import annotations

import errno
import json
import os
import socket
import threading

import pytest
from src.sandbox_kernel import relay

fr = relay.framing
sp = relay.spec
TOKEN = "ab" * 32
BOOT = "0123456789abcdef" * 2
START = {
    "argv": ["sh", "-c", "echo hi"],
    "cwd": "/srv/drive/Alice",
    "env": {"PATH": "/usr/bin:/bin"},
    "spec": {"version": 1, "block_ports": [8000, 8083, 11434]},
    "kind": "agents",
    "owner": "Alice",
    "io": "merged",
    "max_seconds": 60,
}
REQUEST = {"parent_pid": 4242, "start": START}


class FakeExecutor:
    """在執行緒裡照腳本回應；記錄收到的每個 frame（類型與 JSON 或位元組）。"""

    def __init__(self, sock: socket.socket, script) -> None:
        self.sock = sock
        self.sock.settimeout(5)  # relay 卡住時這邊逾時關掉連線，測試照實失敗而不是卡住
        self.script = script
        self.received: list[tuple[str, object]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def recv(self):
        frame = fr.read_frame(self.sock.recv)
        if frame is None:
            self.received.append(("EOF", None))
            return None
        body = fr.decode_json(frame) if frame.kind in fr.JSON_TYPES else frame.payload
        self.received.append((fr.TYPE_NAMES[frame.kind], body))
        return frame

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _run(self) -> None:
        try:
            self.script(self)
        except OSError:
            pass
        finally:
            self.sock.close()

    def kinds(self) -> list[str]:
        self.thread.join(timeout=10)
        return [kind for kind, _ in self.received]


def _ready(**overrides) -> bytes:
    document = {"v": 1, "boot_id": BOOT, "abi": 7, "enforced": True}
    document.update(overrides)
    return fr.encode_json(fr.READY, document)


def _until_exit(exit_frame: bytes, *outputs: bytes):
    def _script(server: FakeExecutor) -> None:
        server.recv()  # HELLO
        server.send(_ready())
        server.recv()  # START
        server.send(fr.encode_json(fr.STARTED, {"exec_id": "e" * 32}))
        server.recv()  # STDIN_EOF
        for data in outputs:
            server.send(data)
        server.send(exit_frame)
        server.recv()  # relay 關掉連線

    return _script


def _no_stdin(_size: int) -> bytes:
    return b""


def _session(script, request=REQUEST, boot_id=BOOT, read_stdin=_no_stdin):
    client, server_sock = socket.socketpair()
    server = FakeExecutor(server_sock, script)
    chunks: list[tuple[str, bytes]] = []
    with client:
        try:
            result = relay.run_session(
                client,
                token=TOKEN,
                boot_id=boot_id,
                request=request,
                out=lambda data: chunks.append(("out", data)),
                err=lambda data: chunks.append(("err", data)),
                read_stdin=read_stdin,
            )
        except relay.RelayError as exc:
            result = (sp.FAILURE_EXIT, exc.code)
    return result, chunks, server


def test_happy_path_streams_output_and_returns_the_remote_code():
    script = _until_exit(
        fr.encode_json(fr.EXIT, {"code": 3, "signal": None, "timed_out": False,
                                 "launcher_error": None}),
        fr.encode(fr.OUT, b"hello\n"),
        fr.encode(fr.ERR, b"warn\n"),
        fr.encode(fr.OUT, b"bye\n"),
    )
    result, chunks, server = _session(script)
    assert result == (3, None)
    assert chunks == [("out", b"hello\n"), ("err", b"warn\n"), ("out", b"bye\n")]
    assert server.kinds() == ["HELLO", "START", "STDIN_EOF", "EOF"]
    hello, start = server.received[0][1], server.received[1][1]
    assert hello == {"v": fr.PROTOCOL_VERSION, "token": TOKEN, "block_ports": [8000, 8083, 11434],
                     "mode": "exec"}
    assert start == START
    assert hello["block_ports"] == start["spec"]["block_ports"]  # HELLO 與 START 同一份 B


def test_restarted_executor_never_receives_start():
    def _script(server: FakeExecutor) -> None:
        server.recv()
        server.send(_ready(boot_id="f" * 32))
        server.recv()

    result, chunks, server = _session(_script)
    assert result == (sp.FAILURE_EXIT, "executor_restarted")
    assert server.kinds() == ["HELLO", "EOF"]
    assert chunks == []


@pytest.mark.parametrize(
    ("cut_after", "kinds"),
    [
        ("HELLO", ["HELLO"]),
        ("START", ["HELLO", "START"]),
        ("STARTED", ["HELLO", "START", "STDIN_EOF"]),  # 收到一塊輸出之後斷線
    ],
)
def test_connection_lost_before_exit_is_unreachable(cut_after, kinds):
    def _script(server: FakeExecutor) -> None:
        server.recv()
        if cut_after == "HELLO":
            return
        server.send(_ready())
        server.recv()
        if cut_after == "STARTED":
            server.send(fr.encode_json(fr.STARTED, {"exec_id": "e" * 32}))
            server.recv()  # STDIN_EOF：relay 已經進入讀輸出的迴圈
            server.send(fr.encode(fr.OUT, b"partial"))

    result, chunks, server = _session(_script)
    assert result == (sp.FAILURE_EXIT, "executor_unreachable")
    assert server.kinds()[: len(kinds)] == kinds
    assert chunks == ([("out", b"partial")] if cut_after == "STARTED" else [])


@pytest.mark.parametrize(
    ("stage", "error", "status"),
    [
        ("hello", fr.ERROR_AUTH, "executor_auth_failed"),
        ("hello", fr.ERROR_VERSION, "executor_unreachable"),
        ("hello", fr.ERROR_PROTOCOL, "executor_unreachable"),
        ("start", fr.ERROR_REJECTED, "executor_rejected"),
        ("start", fr.ERROR_AUTH, "executor_auth_failed"),
    ],
)
def test_error_frames_map_to_relay_status_codes(stage, error, status):
    def _script(server: FakeExecutor) -> None:
        server.recv()
        if stage == "start":
            server.send(_ready())
            server.recv()
        server.send(fr.encode_error(error))
        server.recv()

    result, _, _ = _session(_script)
    assert result == (sp.FAILURE_EXIT, status)


@pytest.mark.parametrize(
    ("exit_doc", "result"),
    [
        ({"code": 0, "signal": None, "timed_out": False, "launcher_error": None}, (0, None)),
        ({"code": 255, "signal": None, "timed_out": False, "launcher_error": None}, (255, None)),
        ({"code": None, "signal": 9, "timed_out": True, "launcher_error": None}, (137, None)),
        ({"code": None, "signal": 15, "timed_out": False, "launcher_error": None}, (143, None)),
        (
            {"code": 125, "signal": None, "timed_out": False, "launcher_error": "path_open"},
            (125, "path_open"),
        ),
        (
            {"code": 125, "signal": None, "timed_out": False, "launcher_error": "net_mptcp"},
            (125, "net_mptcp"),
        ),
    ],
)
def test_exit_carries_the_remote_result(exit_doc, result):
    got, _, _ = _session(_until_exit(fr.encode_json(fr.EXIT, exit_doc)))
    assert got == result


@pytest.mark.parametrize(
    "exit_doc",
    [
        {"code": 0},  # 少欄位
        {"code": 0, "signal": None, "timed_out": False, "launcher_error": None, "x": 1},
        {"code": 256, "signal": None, "timed_out": False, "launcher_error": None},
        {"code": True, "signal": None, "timed_out": False, "launcher_error": None},
        {"code": None, "signal": None, "timed_out": False, "launcher_error": None},
        {"code": None, "signal": 0, "timed_out": False, "launcher_error": None},
        {"code": 125, "signal": None, "timed_out": False, "launcher_error": "made_up"},
        {"code": 0, "signal": None, "timed_out": "no", "launcher_error": None},
    ],
)
def test_malformed_exit_is_a_protocol_failure(exit_doc):
    got, _, _ = _session(_until_exit(fr.encode_json(fr.EXIT, exit_doc)))
    assert got == (sp.FAILURE_EXIT, "executor_unreachable")


@pytest.mark.parametrize(
    "reply",
    [
        b"\x63\x00\x00\x00\x00",  # 看不懂的類型
        fr.encode(fr.OUT, b"too early"),  # READY 之前就送輸出
        fr.encode_json(fr.READY, {"v": 2, "boot_id": BOOT}),  # 協定版本不同
        fr.encode_json(fr.READY, {"v": 1}),  # 沒有 boot_id
        fr.encode(fr.READY, f'{{"v":1,"boot_id":"{BOOT}","boot_id":"{BOOT}"}}'.encode()),
    ],
    ids=["unknown-type", "out-before-ready", "version", "no-boot-id", "duplicate-key"],
)
def test_unexpected_handshake_replies_never_reach_start(reply):
    def _script(server: FakeExecutor) -> None:
        server.recv()
        server.send(reply)
        server.recv()

    result, _, server = _session(_script)
    assert result == (sp.FAILURE_EXIT, "executor_unreachable")
    assert "START" not in server.kinds()


# ── stdio 模式（C2-3 §3④）：stdin 轉成 STDIN、EOF 才送 STDIN_EOF、ERR 到 stderr ────────────

STDIO_REQUEST = {"parent_pid": 4242, "start": {**START, "io": "stdio"}}


def _exit(code: int) -> bytes:
    document = {"code": code, "signal": None, "timed_out": False, "launcher_error": None}
    return fr.encode_json(fr.EXIT, document)


class _Stdin:
    """relay 的 stdin 替身：``read(n)`` 同 ``os.read`` 最多回 n 個位元組；``close`` 之後是 EOF."""

    def __init__(self, data: bytes = b"", *, closed: bool = False) -> None:
        self._buffer = bytearray(data)
        self._closed = closed
        self._ready = threading.Condition()

    def read(self, size: int) -> bytes:
        with self._ready:  # 等太久當 EOF：relay 卡住時測試照實失敗而不是卡住
            self._ready.wait_for(lambda: self._buffer or self._closed, timeout=10)
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk

    def close(self) -> None:
        with self._ready:
            self._closed = True
            self._ready.notify_all()


def _started(server: FakeExecutor) -> None:
    server.recv()  # HELLO
    server.send(_ready())
    server.recv()  # START
    server.send(fr.encode_json(fr.STARTED, {"exec_id": "e" * 32}))


def test_stdio_forwards_stdin_and_sends_eof_only_after_stdin_ends():
    stdin = _Stdin(b"ping\n")

    def _script(server: FakeExecutor) -> None:
        _started(server)
        server.recv()  # stdin 還開著：只會收到 STDIN
        server.send(fr.encode(fr.OUT, b"pong\n") + fr.encode(fr.ERR, b"log\n"))
        stdin.close()  # 呼叫端關掉 stdin
        server.recv()  # STDIN_EOF
        server.send(_exit(3))
        server.recv()

    result, chunks, server = _session(_script, STDIO_REQUEST, read_stdin=stdin.read)
    assert result == (3, None)
    assert chunks == [("out", b"pong\n"), ("err", b"log\n")]  # ERR 寫到 stderr
    assert server.kinds() == ["HELLO", "START", "STDIN", "STDIN_EOF", "EOF"]  # 等假執行器收完
    assert server.received[1][1]["io"] == "stdio"
    assert server.received[2] == ("STDIN", b"ping\n")


def test_stdio_exit_while_stdin_is_open_never_sends_stdin_eof():
    stdin = _Stdin()

    def _script(server: FakeExecutor) -> None:
        _started(server)
        server.send(fr.encode(fr.OUT, b"ready\n") + _exit(0))
        server.recv()

    try:
        result, chunks, server = _session(_script, STDIO_REQUEST, read_stdin=stdin.read)
    finally:
        stdin.close()
    assert (result, chunks) == ((0, None), [("out", b"ready\n")])
    assert server.kinds() == ["HELLO", "START", "EOF"]


def test_stdio_splits_stdin_into_frames_of_at_most_64_kib():
    data = bytes(range(256)) * 800  # 200 KiB
    stdin = _Stdin(data, closed=True)

    def _script(server: FakeExecutor) -> None:
        _started(server)
        while (frame := server.recv()) is not None and frame.kind == fr.STDIN:
            pass
        server.send(_exit(0))
        server.recv()

    result, _, server = _session(_script, STDIO_REQUEST, read_stdin=stdin.read)
    assert server.kinds()[-2:] == ["STDIN_EOF", "EOF"]  # 先等假執行器收完
    payloads = [body for kind, body in server.received if kind == "STDIN"]
    assert result == (0, None) and relay.STDIN_CHUNK_BYTES == 64 * 1024
    assert b"".join(payloads) == data
    assert max(map(len, payloads)) == relay.STDIN_CHUNK_BYTES


def test_stdio_read_error_counts_as_end_of_input():
    def _broken(_size: int) -> bytes:
        raise OSError(errno.EBADF, "stdin closed")

    def _script(server: FakeExecutor) -> None:
        _started(server)
        server.recv()  # STDIN_EOF
        server.send(_exit(0))
        server.recv()

    result, _, server = _session(_script, STDIO_REQUEST, read_stdin=_broken)
    assert result == (0, None)
    assert server.kinds() == ["HELLO", "START", "STDIN_EOF", "EOF"]


# ── 命令列與權杖：權杖只從 ipc 資料夾的檔案讀 ───────────────────────────────────────────


def _argv(ipc_dir, request_fd: int, status_fd: int, boot_id: str = BOOT) -> list[str]:
    return [
        "--ipc-dir", str(ipc_dir), "--request-fd", str(request_fd),
        "--status-fd", str(status_fd), "--boot-id", boot_id,
    ]


def test_cli_takes_no_token_and_needs_every_option(tmp_path):
    command = relay.parse_cli(_argv(tmp_path, 4, 5))
    assert (command.request_fd, command.status_fd, command.boot_id) == (4, 5, BOOT)
    assert command.ipc_dir == str(tmp_path)
    for argv in (
        [*_argv(tmp_path, 4, 5), "--token", TOKEN],
        _argv(tmp_path, 4, 4),
        _argv(tmp_path, 1, 5),
        _argv(tmp_path, 4, 5, boot_id="not-hex"),
        _argv(tmp_path, 4, 5)[:-2],
        _argv("relative/ipc", 4, 5),
    ):
        with pytest.raises(relay.RelayError) as caught:
            relay.parse_cli(argv)
        assert caught.value.code == "usage"


class _Harness:
    """跑 ``relay.main``：request 經 pipe、狀態經 pipe；連線由 socketpair 的假執行器接。"""

    def __init__(self, monkeypatch, tmp_path, script, *, token: str | None = TOKEN) -> None:
        self.ipc = tmp_path / "ipc"
        self.ipc.mkdir(exist_ok=True)
        if token is not None:
            (self.ipc / fr.TOKEN_NAME).write_text(token, encoding="ascii")
        self.connects = 0
        self.servers: list[FakeExecutor] = []
        self.prepared: list[int] = []
        self.script = script

        def _connect(ipc_dir):
            assert str(ipc_dir) == str(self.ipc)
            self.connects += 1
            client, server_sock = socket.socketpair()
            self.servers.append(FakeExecutor(server_sock, self.script))
            return client

        monkeypatch.setattr(relay, "_connect", _connect)
        monkeypatch.setattr(relay, "_prepare_process", self.prepared.append)

    def run(self, request: object = REQUEST, *, raw: bytes | None = None) -> tuple[int, bytes]:
        request_r, request_w = os.pipe()
        status_r, status_w = os.pipe()
        data = raw if raw is not None else json.dumps(request).encode("utf-8")
        writer = threading.Thread(target=_write_all, args=(request_w, data), daemon=True)
        writer.start()  # 大的請求塞滿 pipe 時不會卡住
        os.set_blocking(status_r, False)
        try:
            code = relay.main(_argv(self.ipc, request_r, status_w))
            status = os.read(status_r, 4096)
            assert os.read(status_r, 4096) == b""  # relay 一定關掉狀態 fd
        finally:
            os.close(status_r)
            writer.join(timeout=10)
        return code, status


def _write_all(fd: int, data: bytes) -> None:
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        pass  # relay 讀到上限就關掉讀端
    finally:
        os.close(fd)


def test_token_is_read_from_the_ipc_file_on_every_connection(monkeypatch, tmp_path, capfd):
    exit_frame = fr.encode_json(
        fr.EXIT, {"code": 0, "signal": None, "timed_out": False, "launcher_error": None}
    )
    harness = _Harness(monkeypatch, tmp_path, _until_exit(exit_frame, fr.encode(fr.OUT, b"hi\n")))
    assert harness.run() == (0, b"")
    assert capfd.readouterr().out == "hi\n"
    (harness.ipc / fr.TOKEN_NAME).write_text("cd" * 32 + "\n", encoding="ascii")  # 執行器重啟換新
    assert harness.run() == (0, b"")
    tokens = [server.received[0][1]["token"] for server in harness.servers]
    assert tokens == [TOKEN, "cd" * 32]
    assert harness.prepared == [4242, 4242]  # 每次都先設 PDEATHSIG 並核對父行程


@pytest.mark.parametrize("token", [None, "", "\n", "ab" * 31, "AB" * 32, "zz" * 32, "ab" * 33])
def test_missing_or_malformed_token_fails_before_connecting(monkeypatch, tmp_path, token):
    harness = _Harness(monkeypatch, tmp_path, _until_exit(b""), token=token)
    assert harness.run() == (sp.FAILURE_EXIT, b"executor_auth_failed\n")
    assert harness.connects == 0


def test_unreachable_socket_is_reported(monkeypatch, tmp_path):
    harness = _Harness(monkeypatch, tmp_path, _until_exit(b""))

    def _refused(_ipc_dir):
        raise ConnectionRefusedError

    monkeypatch.setattr(relay, "_connect", _refused)
    assert harness.run() == (sp.FAILURE_EXIT, b"executor_unreachable\n")


def test_restart_status_goes_through_the_status_fd(monkeypatch, tmp_path):
    def _script(server: FakeExecutor) -> None:
        server.recv()
        server.send(_ready(boot_id="f" * 32))
        server.recv()

    harness = _Harness(monkeypatch, tmp_path, _script)
    assert harness.run() == (sp.FAILURE_EXIT, b"executor_restarted\n")
    assert harness.servers[0].kinds() == ["HELLO", "EOF"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", "spec_invalid"),
        (json.dumps({**REQUEST, "block_ports": [8000]}).encode(), "spec_invalid"),  # 另給的 B
        (json.dumps({"start": START}).encode(), "spec_invalid"),
        (json.dumps({**REQUEST, "parent_pid": 0}).encode(), "spec_invalid"),
        (json.dumps({**REQUEST, "parent_pid": True}).encode(), "spec_invalid"),
        (json.dumps({**REQUEST, "start": []}).encode(), "spec_invalid"),
        (json.dumps({**REQUEST, "start": {**START, "spec": None}}).encode(), "spec_invalid"),
        (json.dumps({**REQUEST, "start": {**START, "spec": {"version": 1}}}).encode(),
         "spec_invalid"),
        (b'{"parent_pid": 1, "parent_pid": 2}', "spec_invalid"),
        (b" " * (fr.MAX_START_BYTES + fr.MAX_CONTROL_BYTES + 1), "spec_too_large"),
    ],
    ids=["json", "own-hello-ports", "missing-pid", "pid-0", "pid-bool", "start-list",
         "no-spec", "no-spec-ports", "dup", "size"],
)
def test_bad_requests_fail_before_connecting(monkeypatch, tmp_path, raw, code):
    harness = _Harness(monkeypatch, tmp_path, _until_exit(b""))
    assert harness.run(raw=raw) == (sp.FAILURE_EXIT, sp.encode_status(code))
    assert harness.connects == 0 and harness.prepared == []


def test_relay_status_codes_are_known_to_the_runner():
    assert {
        "executor_unreachable", "executor_restarted", "executor_auth_failed", "executor_rejected"
    } == sp.RELAY_FAILURE_CODES
    assert all(sp.FAILURE_CODE_PATTERN.fullmatch(code) for code in sp.RELAY_FAILURE_CODES)
    assert not sp.RELAY_FAILURE_CODES & sp.FAILURE_CODES
