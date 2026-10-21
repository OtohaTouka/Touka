"""proxy 端的 relay：執行器裡那條指令的本機替身（只用標準函式庫，以 ``python -I -S`` 依路徑執行）.

用法::

    relay.py --ipc-dir D --request-fd R --status-fd S --boot-id B

兩種模式由 ``start.io`` 決定：frames（C2-2，指令）與 stdio（C2-3，MCP 伺服器）：

1. 從 request fd 讀請求（JSON 物件：``parent_pid``、``start``），讀完就關；HELLO 的封鎖埠一律
   取 ``start.spec.block_ports``（和 START 同一個物件，relay 不另外算），所以 START 的 B 一定
   包含 HELLO 的 B；
2. 設 PDEATHSIG(SIGKILL)，父行程不是 ``parent_pid`` 就不連線；
3. 從 ``D/token`` 讀權杖：每次連線都重讀（執行器重啟換新後自動恢復），argv 與環境變數都沒有
   權杖；讀不到或格式不對就不連線；
4. 連 ``D/ctl.sock``：HELLO → READY（``boot_id`` 和 argv 的不同就是執行器重啟過，不送 START）
   → START → STARTED。frames 模式接著送 STDIN_EOF（呼叫端的 stdin 一律是 DEVNULL）；stdio 模式
   由另一條執行緒讀自己的 stdin，每塊（≤``STDIN_CHUNK_BYTES``）送 STDIN，讀到 EOF 才送
   STDIN_EOF。讀 stdin 不和讀連線放在同一條執行緒：兩個方向都塞滿時 relay 仍一直在收輸出，
   不會和執行器互等；
5. 之後每收到一塊 OUT／ERR 就寫到自己的 stdout／stderr；EXIT 帶遠端的 code（被信號殺掉是
   128＋signal）。

失敗時 exit 125，代碼寫進 ``--status-fd``（一拿到就設 CLOEXEC；成功時也會關掉）：
``spec.RELAY_FAILURE_CODES``，或遠端啟動器原本的代碼（``spec.FAILURE_CODES``）。relay 被殺或
EXIT 之前斷線時，執行器收到 EOF 就殺掉遠端的整個行程群組。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import re
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

_KERNEL_DIR = Path(__file__).resolve().parent


def _load_sibling(name: str) -> ModuleType:
    """依路徑載入同目錄的模組（``-I`` 不會把腳本目錄放進 ``sys.path``；同 ``launcher.py``）。"""
    tag = hashlib.sha256(str(_KERNEL_DIR).encode("utf-8")).hexdigest()[:12]
    module_name = f"touka_sandbox_kernel_{tag}_{name}"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    module_spec = importlib.util.spec_from_file_location(module_name, _KERNEL_DIR / f"{name}.py")
    if module_spec is None or module_spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)
    return module


framing = _load_sibling("framing")
spec = _load_sibling("spec")
landlock = _load_sibling("landlock")

UNREACHABLE, RESTARTED = "executor_unreachable", "executor_restarted"
AUTH_FAILED, REJECTED = "executor_auth_failed", "executor_rejected"
HANDSHAKE_SECONDS = 30.0
MAX_REQUEST_BYTES = framing.MAX_START_BYTES + framing.MAX_CONTROL_BYTES
STDIN_CHUNK_BYTES = 64 * 1024  # stdio 模式每個 STDIN frame 的上限
_MAX_TOKEN_FILE = 256
_FD_ARG = re.compile(r"[0-9]{1,7}")
_FIRST_INHERITED_FD = 3
_OPTIONS = frozenset({"--ipc-dir", "--request-fd", "--status-fd", "--boot-id"})
_REQUEST_KEYS = frozenset({"parent_pid", "start"})
_EXIT_KEYS = frozenset({"code", "signal", "timed_out", "launcher_error"})
_SIGNAL_BASE, _MAX_EXIT = 128, 255
_ERROR_STATUS = {framing.ERROR_AUTH: AUTH_FAILED, framing.ERROR_REJECTED: REJECTED}


class RelayError(Exception):
    """relay 的失敗；``code`` 會寫進狀態 fd。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _OutputClosedError(Exception):
    """自己的 stdout／stderr 寫不進去（呼叫端已經不讀了）。"""


@dataclass(frozen=True)
class Command:
    """解析後的命令列。"""

    ipc_dir: str
    request_fd: int
    status_fd: int
    boot_id: str


def _fd_arg(value: str) -> int:
    if not _FD_ARG.fullmatch(value) or int(value) < _FIRST_INHERITED_FD:
        raise RelayError("usage")
    return int(value)


def parse_cli(argv: Sequence[str]) -> Command:
    """四個選項都要有、各一次；權杖不在命令列上。不合格拋 ``RelayError("usage")``。"""
    args = list(argv)
    if len(args) % 2:
        raise RelayError("usage")
    values: dict[str, str] = {}
    for name, value in zip(args[::2], args[1::2], strict=True):
        if name not in _OPTIONS or name in values:
            raise RelayError("usage")
        values[name] = value
    if set(values) != _OPTIONS or not Path(values["--ipc-dir"]).is_absolute():
        raise RelayError("usage")
    if not framing.BOOT_ID_PATTERN.fullmatch(values["--boot-id"]):
        raise RelayError("usage")
    request_fd, status_fd = _fd_arg(values["--request-fd"]), _fd_arg(values["--status-fd"])
    if request_fd == status_fd:
        raise RelayError("usage")
    return Command(values["--ipc-dir"], request_fd, status_fd, values["--boot-id"])


def _read_request(fd: int) -> dict:
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise RelayError("spec_too_large")
            chunks.append(chunk)
    except OSError as exc:
        raise RelayError("spec_read") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    try:
        document = framing.decode_json(framing.Frame(framing.START, b"".join(chunks)))
    except framing.FrameError as exc:
        raise RelayError("spec_invalid") from exc
    parent_pid, start = document.get("parent_pid"), document.get("start")
    launch_spec = start.get("spec") if isinstance(start, dict) else None
    if (
        set(document) != _REQUEST_KEYS
        or type(parent_pid) is not int
        or parent_pid <= 0
        or not isinstance(launch_spec, dict)
        or not isinstance(launch_spec.get("block_ports"), list)
    ):
        raise RelayError("spec_invalid")
    return document


def _prepare_process(parent_pid: int) -> None:
    """PDEATHSIG(SIGKILL)：proxy 一死 relay 就死，執行器收到 EOF 就收掉遠端；父行程要對得上。"""
    try:
        landlock.LinuxLandlock().prctl(landlock.PR_SET_PDEATHSIG, landlock.SIGKILL)
    except (OSError, AttributeError, TypeError) as exc:
        raise RelayError("process_setup") from exc
    if os.getppid() != parent_pid:
        raise RelayError("process_setup")  # 設好 PDEATHSIG 之前 proxy 就結束了


def read_token(ipc_dir: str) -> str:
    """每次連線前從 ``<ipc>/token`` 讀（不跟連結）；讀不到、空的或格式不對就是認證失敗。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(Path(ipc_dir) / framing.TOKEN_NAME, flags)
        try:
            text = os.read(fd, _MAX_TOKEN_FILE).decode("ascii")
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError) as exc:
        raise RelayError(AUTH_FAILED) from exc
    token = text.removesuffix("\n")
    if not framing.TOKEN_PATTERN.fullmatch(token):
        raise RelayError(AUTH_FAILED)
    return token


def _connect(ipc_dir: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(HANDSHAKE_SECONDS)
        sock.connect(str(Path(ipc_dir) / framing.SOCKET_NAME))
    except BaseException:
        sock.close()
        raise
    return sock


def _send(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError as exc:
        raise RelayError(UNREACHABLE) from exc


def _encoded(kind: int, document: dict) -> bytes:
    try:
        return framing.encode_json(kind, document)
    except (framing.FrameError, TypeError, ValueError) as exc:
        raise RelayError("spec_too_large") from exc


def _receive(sock: socket.socket) -> framing.Frame:
    """下一個 frame；EOF、格式錯誤都是連不上，ERROR 依代碼對應。"""
    try:
        frame = framing.read_frame(sock.recv)
    except (OSError, framing.FrameError) as exc:
        raise RelayError(UNREACHABLE) from exc
    if frame is None:
        raise RelayError(UNREACHABLE)
    if frame.kind == framing.ERROR:
        raise RelayError(_ERROR_STATUS.get(framing.error_code(frame), UNREACHABLE))
    return frame


def _expect(sock: socket.socket, kind: int) -> dict:
    frame = _receive(sock)
    if frame.kind != kind:
        raise RelayError(UNREACHABLE)
    try:
        return framing.decode_json(frame)
    except framing.FrameError as exc:
        raise RelayError(UNREACHABLE) from exc


def _check_ready(ready: dict, boot_id: str) -> None:
    version, seen = ready.get("v"), ready.get("boot_id")
    if type(version) is not int or version != framing.PROTOCOL_VERSION:
        raise RelayError(UNREACHABLE)
    if not isinstance(seen, str) or not framing.BOOT_ID_PATTERN.fullmatch(seen):
        raise RelayError(UNREACHABLE)
    if seen != boot_id:
        raise RelayError(RESTARTED)  # proxy 判定之後執行器重啟過：不送 START


def _exit_result(document: dict) -> tuple[int, str | None]:
    """EXIT 的結果：遠端啟動器失敗照原代碼回報；被信號殺掉是 128＋signal；其他照 code。"""
    if set(document) != _EXIT_KEYS or type(document["timed_out"]) is not bool:
        raise RelayError(UNREACHABLE)
    code, signal, launcher_error = (document[k] for k in ("code", "signal", "launcher_error"))
    if launcher_error is not None:
        if launcher_error not in spec.FAILURE_CODES:
            raise RelayError(UNREACHABLE)
        return spec.FAILURE_EXIT, launcher_error
    if signal is not None:
        if type(signal) is not int or not 1 <= signal <= _MAX_EXIT - _SIGNAL_BASE:
            raise RelayError(UNREACHABLE)
        return _SIGNAL_BASE + signal, None
    if type(code) is not int or not 0 <= code <= _MAX_EXIT:
        raise RelayError(UNREACHABLE)
    return code, None


def _pump_stdin(sock: socket.socket, read_stdin: Callable[[int], bytes]) -> None:
    """在 stdio 模式自己的執行緒裡：stdin 每讀到一塊就送 STDIN；EOF 或讀不到送 STDIN_EOF 就停.

    連線斷了也停：主執行緒在讀連線，會看到斷線並照常回報。
    """
    with contextlib.suppress(OSError):
        while True:
            try:
                chunk = read_stdin(STDIN_CHUNK_BYTES)
            except OSError:
                chunk = b""
            if not chunk:
                sock.sendall(framing.encode(framing.STDIN_EOF))
                return
            sock.sendall(framing.encode(framing.STDIN, chunk))


def _read_stdin(size: int) -> bytes:
    return os.read(0, size)


def run_session(
    sock: socket.socket,
    *,
    token: str,
    boot_id: str,
    request: dict,
    out: Callable[[bytes], None],
    err: Callable[[bytes], None],
    read_stdin: Callable[[int], bytes],
) -> tuple[int, str | None]:
    """跑完一條指令；回（exit code、要寫進狀態 fd 的代碼）。失敗拋 ``RelayError``。"""
    sock.settimeout(HANDSHAKE_SECONDS)
    start = request["start"]
    stdio = start.get("io") == "stdio"
    hello = {"v": framing.PROTOCOL_VERSION, "token": token}
    hello.update(block_ports=start["spec"]["block_ports"], mode="exec")  # 和 START 同一份
    _send(sock, _encoded(framing.HELLO, hello))
    _check_ready(_expect(sock, framing.READY), boot_id)
    _send(sock, _encoded(framing.START, start))
    _expect(sock, framing.STARTED)
    if not stdio:
        _send(sock, framing.encode(framing.STDIN_EOF))
    sock.settimeout(None)  # 時限由執行器的 max_seconds 與 proxy 的逾時負責
    if stdio:  # 主執行緒之後只讀連線；送出只在這條執行緒
        threading.Thread(target=_pump_stdin, args=(sock, read_stdin), daemon=True).start()
    while True:
        frame = _receive(sock)
        if frame.kind == framing.OUT:
            out(frame.payload)
        elif frame.kind == framing.ERR:
            err(frame.payload)
        elif frame.kind == framing.EXIT:
            try:
                return _exit_result(framing.decode_json(frame))
            except framing.FrameError as exc:
                raise RelayError(UNREACHABLE) from exc
        else:
            raise RelayError(UNREACHABLE)


def _writer(fd: int) -> Callable[[bytes], None]:
    def _write(data: bytes) -> None:
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(fd, view) :]
        except OSError as exc:
            raise _OutputClosedError from exc

    return _write


def run(command: Command) -> tuple[int, str | None]:
    """讀請求、設好行程屬性、讀權杖、連線，跑完一條指令。"""
    request = _read_request(command.request_fd)
    _prepare_process(request["parent_pid"])
    token = read_token(command.ipc_dir)
    try:
        sock = _connect(command.ipc_dir)
    except (OSError, AttributeError) as exc:  # Windows 沒有 AF_UNIX 時也當成連不上
        raise RelayError(UNREACHABLE) from exc
    with sock:
        return run_session(
            sock,
            token=token,
            boot_id=command.boot_id,
            request=request,
            out=_writer(1),
            err=_writer(2),
            read_stdin=_read_stdin,
        )


def _claim_status_fd(fd: int) -> int:
    try:
        os.set_inheritable(fd, False)
    except OSError as exc:
        raise RelayError("usage") from exc
    return fd


def main(argv: Sequence[str] | None = None) -> int:
    """命令列入口；狀態 fd 不論成敗都會關掉。"""
    status_fd = None
    try:
        command = parse_cli(sys.argv[1:] if argv is None else argv)
        status_fd = _claim_status_fd(command.status_fd)
        code, status = run(command)
    except RelayError as exc:
        code, status = spec.FAILURE_EXIT, exc.code
    except _OutputClosedError:
        code, status = spec.FAILURE_EXIT, None  # 呼叫端不讀了；關掉連線，執行器收掉遠端
    if status_fd is not None:
        if status:
            with contextlib.suppress(OSError):
                os.write(status_fd, spec.encode_status(status))
        with contextlib.suppress(OSError):
            os.close(status_fd)
    return code


if __name__ == "__main__":
    sys.exit(main())
