"""執行器伺服器的一條連線（iso-c2 C2-2）：握手、START 驗證、啟動、轉送輸出與 EXIT.

只用標準函式庫；kernel 的 ``framing``、``spec`` 由伺服器依路徑載入後經 ``host`` 傳進來
（``host`` 就是 ``ExecutorServer``）。START 全部 fail-closed（設計 §2.4）：讀寫規則只有這次的
目標 ``<rw root>/<owner>`` 與 ``<state>/<kind>/<owner>/{home,tmp}``，cwd 就是目標；其他
規則只能是系統路徑或唯讀 root；spec 的封鎖埠包含 HELLO 的；env 走 kernel 的允許清單；spec 的
``parent_pid`` 改成伺服器的 pid。啟動器經 ``host.launch``（新行程群組、只傳 spec／狀態／規則集
三個 fd、環境加新的 ``TOUKA_EXEC_ID``）。**EXIT 之前連線斷掉就是 STOP**（relay 收到 SIGTERM
不送 STOP）：殺掉整個行程群組並有界回收；STOP 與 ``max_seconds`` 到期也殺，但照樣回 EXIT。

C2-3：kind ``mcp``（常駐的自建 MCP 伺服器）沒有資料的目標，讀寫只有自己的 state，目標（cwd）
就是 home；程式資料夾最多一條唯讀＋執行，正好是演化部署的 ``<managed>/<skill>``
（部署後唯讀）。``io`` 是 ``stdio`` 時 stderr 另外送 ERR；``max_seconds`` 0（沒有時限）只准
kind ``mcp`` 加 ``stdio``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, TypeGuard

if TYPE_CHECKING:
    import re
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

EXEC_ID_ENV = "TOUKA_EXEC_ID"
MAX_ARGV_ITEMS = 4096
MAX_ARGV_BYTES = 512 * 1024
MAX_SECONDS = 900
START_SECONDS = 30.0  # READY 之後等 START 的上限
REAP_SECONDS = 5.0  # 殺掉之後等回收的上限
CHUNK_BYTES = 64 * 1024
MAX_STDIN_FRAMES = 16
HELLO_KEYS = frozenset({"v", "token", "block_ports", "mode"})
type Hello = tuple[str, frozenset[int]]  # 通過的 HELLO：（mode、封鎖埠）
MODES = frozenset({"health", "exec"})
START_KEYS = frozenset({"argv", "cwd", "env", "spec", "kind", "owner", "io", "max_seconds"})
# merged：stderr 併進 stdout（指令）；stdio：stdin 照轉、stderr 另外送 ERR（MCP 伺服器、演化驗證）
IO_MODES = frozenset({"merged", "stdio"})
# 演化工具所在的讀寫 root（``data/mcp_servers/managed``）；也是伺服器自測用的資料所在 root。
# 只掛 managed、不掛 mcp_servers：掛載根本身不能被改名或刪除，執行器裡的程式換不掉錨點
# （ADR-0293 PR 2）
MANAGED_ROOT = "managed"
# kind → （讀寫 root 的名稱、root 底下到 owner 之前的中間段）
KIND_LAYOUT: dict[str, tuple[str, tuple[str, ...]]] = {
    "agents": ("personal_drive", ()),
    "evolution": (MANAGED_ROOT, ()),
}
MCP_KIND = "mcp"  # 沒有資料的目標（見模組說明）；程式資料夾和 kind evolution 的目標同一個位置
CODE_KIND = "evolution"
KINDS = (*KIND_LAYOUT, MCP_KIND)
# 伺服器不能匯入 profiles／isolation_policy，下面兩份和正本由 parity 測試釘住
SYSTEM_EXEC_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
SYSTEM_READ_PATHS = ("/etc", "/proc")
DEVICE_RW_PATHS = ("/dev/null", "/dev/zero", "/dev/full")
DEVICE_READ_PATHS = ("/dev/urandom", "/dev/random")
TCP_FEATURE = "tcp"
_FEATURE_MIN_ABI = {"fs": 3, TCP_FEATURE: 4, "ioctl": 5, "signal": 6, "abstract_unix": 6}
_VERIFIED_ABI_MAX = 7
SIGKILL = 9  # Windows 的 signal 模組沒有這個常數（單元測試在 Windows 也跑）


# ── kernel 模組的型別 ──────────────────────────────────────────────────────────────
# kernel 由伺服器從 ``--kernel-dir`` 依路徑載入（``importlib``），靜態型別看不到真的模組，
# 所以這裡用 Protocol 寫下伺服器用到的部分；``test_kernel_modules_have_every_protocol_member``
# 拿真的 kernel（src 與依路徑載入的兩份）逐項核對，kernel 改名或拿掉成員時會紅。


class FrameLike(Protocol):
    """``framing.Frame``：一個完整的 frame."""

    kind: int
    payload: bytes


class FrameDecoderLike(Protocol):
    """``framing.FrameDecoder``：逐段餵入位元組、取出完整的 frame."""

    def feed(self, data: bytes) -> list[FrameLike]:
        """餵入資料，回傳已完整的 frame；格式錯誤拋 ``FrameError``."""
        ...

    def close(self) -> None:
        """連線結束：還有沒收完的 frame 就拋 ``FrameError``（截斷）."""
        ...


class CodedError(Exception):
    """kernel 例外的共同形狀：``FrameError``、``SpecError`` 等都帶一個 ``code``（只用在型別）."""

    code: str


class FramingModule(Protocol):
    """伺服器用到的 ``sandbox_kernel/framing.py``（frame 格式、ERROR 代碼、ipc 檔名）."""

    PROTOCOL_VERSION: int
    SOCKET_NAME: str
    TOKEN_NAME: str
    TOKEN_PATTERN: re.Pattern[str]
    HELLO: int
    READY: int
    START: int
    STARTED: int
    OUT: int
    ERR: int
    STDIN: int
    STDIN_EOF: int
    EXIT: int
    STOP: int
    ERROR_PROTOCOL: str
    ERROR_AUTH: str
    ERROR_VERSION: str
    ERROR_REJECTED: str
    FrameError: type[CodedError]
    FrameDecoder: Callable[[], FrameDecoderLike]
    encode: Callable[[int, bytes], bytes]
    encode_json: Callable[[int, dict[str, object]], bytes]
    encode_error: Callable[[str], bytes]
    decode_json: Callable[[FrameLike], dict[str, object]]
    block_ports_digest: Callable[[Iterable[int]], str]


class RuleLike(Protocol):
    """``spec.RuleSpec``：一條驗證過的路徑規則."""

    path: str
    access: str


class ParsedSpec(Protocol):
    """``spec.LaunchSpec`` 裡伺服器核對的欄位."""

    rules: tuple[RuleLike, ...]
    block_ports: tuple[int, ...]


class SelfTestLike(Protocol):
    """``spec.SelfTestResult``：一次啟動器自測的結果."""

    ok: bool
    abi: int
    error: str | None
    net: str | None


class SpecModule(Protocol):
    """伺服器用到的 ``sandbox_kernel/spec.py``（spec 驗證、env 允許清單、狀態碼、自測）."""

    MAX_BLOCK_PORTS: int
    MAX_PORT: int
    DEFAULT_BLOCK_PORTS: frozenset[int]
    STATUS_MAX_BYTES: int
    FAILURE_CODES: frozenset[str]
    FAILURE_EXIT: int
    INTERPRETER_UNTRUSTED: str
    SpecError: type[CodedError]
    InterpreterUntrustedError: type[CodedError]
    SelfTestResult: Callable[..., SelfTestLike]
    parse_spec: Callable[[bytes], ParsedSpec]
    executor_env_issue: Callable[[object], str | None]
    parse_status: Callable[[bytes, frozenset[str]], str | None]
    kernel_digest: Callable[[Path | None], str | None]
    parse_selftest_output: Callable[[int | None, bytes], SelfTestLike]
    fold_name: Callable[[str], str]
    listing_kind: Callable[[Iterable[str]], str | None]

    def trusted_interpreter(self, *, require_root_owned: bool) -> str:
        """可信直譯器的實體路徑；不合格拋 ``InterpreterUntrustedError``."""
        ...


class Rejected(Exception):
    """START 不合格；``reason`` 只寫進 log，回給 relay 的一律是 ERROR rejected。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Layout:
    """伺服器的掛載配置（啟動時驗證過：絕對、不經連結）。"""

    rw: tuple[str, ...]
    ro: tuple[str, ...]
    state: str
    exec_paths: tuple[str, ...]

    def target(self, kind: str, owner: str) -> tuple[str, str] | None:
        """（讀寫 root、這次的目標）；root 沒有掛就回 None；kind ``mcp`` 是（state、home）。"""
        if kind == MCP_KIND:
            return self.state, self.state_dirs(kind, owner)[0]
        name, middle = KIND_LAYOUT[kind]
        roots = [root for root in self.rw if PurePosixPath(root).name == name]
        return (roots[0], str(PurePosixPath(roots[0], *middle, owner))) if roots else None

    def state_dirs(self, kind: str, owner: str) -> tuple[str, str]:
        """``<state>/<kind>/<owner>/{home,tmp}``。"""
        base = PurePosixPath(self.state, kind, owner)
        return str(base / "home"), str(base / "tmp")


@dataclass(frozen=True)
class StartPlan:
    """驗證過的 START（spec 已改寫 ``parent_pid``；``root`` 是目標所在的讀寫 root）.

    ``max_seconds`` 0 是沒有時限；``code`` 是 kind ``mcp`` 的程式資料夾（唯讀＋執行），
    ``code_root`` 是它所在的讀寫 root（``managed``）。
    """

    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    spec: bytes
    ports: tuple[int, ...]
    root: str
    home: str
    tmp: str
    max_seconds: int
    io: str = "merged"
    code: str | None = None
    code_root: str | None = None


# ── 伺服器的型別 ──────────────────────────────────────────────────────────────────────


class ChildProcess(Protocol):
    """``asyncio.subprocess.Process`` 用到的部分（``host.launch`` 的回傳；單元測試換成假的）."""

    @property
    def pid(self) -> int:
        """啟動器的 pid（broker 開新 session，也是 pgid 與 sid）."""
        ...

    @property
    def returncode(self) -> int | None:
        """還沒回收是 None；被信號殺掉是負的信號編號."""
        ...

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        """指令的 stdin（PIPE）."""
        ...

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        """指令的 stdout（PIPE；merged 時 stderr 也併進來）."""
        ...

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        """指令的 stderr（stdio 時是 PIPE；merged 時併進 stdout，是 None）."""
        ...

    async def wait(self) -> int:
        """等到回收，回傳 returncode."""
        ...

    async def communicate(self) -> tuple[bytes, bytes]:
        """讀完輸出並回收（啟動器自測用）."""
        ...

    def kill(self) -> None:
        """SIGKILL 這個行程（自測逾時用）."""
        ...


class SessionHost(Protocol):
    """一條連線用到的伺服器（``ExecutorServer``）：kernel、掛載配置、權杖、啟動與收尾."""

    framing: FramingModule
    spec: SpecModule
    layout: Layout
    server_pid: int  # 寫進 spec 的 ``parent_pid``
    hello_seconds: float
    launcher_prefix: tuple[str, ...]  # 可信直譯器、``-I -S``、``launcher.py``

    def token_ok(self, token: object) -> bool:
        """HELLO 的權杖對不對（常數時間比對）."""
        ...

    async def prepare(self, plan: StartPlan) -> None:
        """START 前修路徑鏈、建 home／tmp；不行拋 ``Rejected``."""
        ...

    async def lend_ruleset(self, ports: tuple[int, ...]) -> int | None:
        """共用網路規則集的複本（呼叫端負責關）；不借就回 None."""
        ...

    async def launch(self, exec_id: str, argv: list[str], **kwargs: object) -> ChildProcess:
        """先登記 exec id 再啟動啟動器；失敗拋 OSError."""
        ...

    def kill_group(self, pid: int) -> None:
        """SIGKILL 整個行程群組."""
        ...

    async def finished(self, exec_id: str) -> None:
        """這條指令結束：取消登記、收掉它留下的行程."""
        ...


def safe_segment(name: object) -> TypeGuard[str]:
    """單一安全路徑段（同 ``isolation_policy.is_safe_segment``）。"""
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    return bool(stripped) and stripped == name and stripped not in {".", ".."} and not any(
        ch in name for ch in ("/", "\\", "\x00")
    )


def landlock_features(abi: int, net: str | None) -> list[str]:
    """這個 ABI 生效的 features（同 ``isolation_policy``）；網路設定不安全時扣掉 tcp。"""
    abi = min(abi, _VERIFIED_ABI_MAX)
    names = (name for name, need in _FEATURE_MIN_ABI.items() if abi >= need)
    return sorted(name for name in names if not (net and name == TCP_FEATURE))


def system_exec_paths() -> tuple[str, ...]:
    """系統可執行路徑加上伺服器自己的 sys.prefix 與 base_prefix（同 profiles）。"""
    own = (str(Path(p).resolve()) for p in (sys.prefix, sys.base_prefix))
    return tuple(dict.fromkeys((*SYSTEM_EXEC_PATHS, *own)))


def within(path: str, root: str) -> bool:
    """``path`` 等於 ``root`` 或在它底下（純字串，不碰檔案系統）。"""
    candidate, base = PurePosixPath(path), PurePosixPath(root)
    return candidate == base or candidate.is_relative_to(base)


def _rule_allowed(
    rule: RuleLike, rw_paths: frozenset[str], layout: Layout, rox_paths: frozenset[str]
) -> bool:
    path, access = rule.path, rule.access
    if access == "rwx":
        return path in rw_paths
    if access == "rox" and path in rox_paths:
        return True
    if path in DEVICE_RW_PATHS:
        return access in {"dev_rw", "ro"}
    if access not in {"ro", "rox"}:
        return False
    if path in DEVICE_READ_PATHS:
        return access == "ro"
    if any(within(path, root) for root in (*layout.exec_paths, *layout.ro)):
        return True
    return access == "ro" and any(within(path, root) for root in SYSTEM_READ_PATHS)


def _argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ARGV_ITEMS:
        raise Rejected("argv")
    if not all(isinstance(arg, str) and "\x00" not in arg for arg in value):
        raise Rejected("argv")
    if sum(len(arg.encode("utf-8", "surrogatepass")) + 1 for arg in value) > MAX_ARGV_BYTES:
        raise Rejected("argv")
    return tuple(value)


def _code_folder(rules: Iterable[RuleLike], layout: Layout) -> tuple[str, str] | None:
    """Kind ``mcp`` 的程式資料夾（讀寫 root、資料夾）：``rox`` 而且正好是 ``managed/<skill>``.

    最多一條，兩條以上拒絕；其他形狀不算，交給 ``_rule_allowed`` 拒絕（含 ``rwx``：部署後唯讀）。
    """
    found = []
    for rule in rules:
        name = PurePosixPath(rule.path).name
        place = layout.target(CODE_KIND, name) if safe_segment(name) else None
        if rule.access == "rox" and place is not None and place[1] == rule.path:
            found.append(place)
    if len(found) > 1:
        raise Rejected("spec_rule")
    return found[0] if found else None


def _spec(
    document: dict[str, object], spec: SpecModule, kind: str, rw_paths: frozenset[str],
    layout: Layout,
) -> tuple[ParsedSpec, tuple[str, str] | None]:
    """驗證 spec 與每條規則；回（spec、kind ``mcp`` 的程式資料夾或 None）。"""
    try:
        parsed = spec.parse_spec(json.dumps(document).encode("utf-8"))
    except spec.SpecError as exc:
        raise Rejected(exc.code) from None
    code = _code_folder(parsed.rules, layout) if kind == MCP_KIND else None
    rox_paths = frozenset({code[1]}) if code else frozenset()
    if not all(_rule_allowed(rule, rw_paths, layout, rox_paths) for rule in parsed.rules):
        raise Rejected("spec_rule")
    return parsed, code


def _seconds_allowed(seconds: object, kind: str, io: str) -> bool:
    """1～900；0（沒有時限）只准常駐的 kind ``mcp`` 加 ``stdio``（演化驗證走 stdio 也要時限）。"""
    if type(seconds) is not int:
        return False
    if seconds == 0:
        return kind == MCP_KIND and io == "stdio"
    return 1 <= seconds <= MAX_SECONDS


def check_start(
    document: dict[str, object], *, spec: SpecModule, layout: Layout,
    hello_ports: frozenset[int], server_pid: int,
) -> StartPlan:
    """驗證 START（規則見模組說明）；不合格拋 ``Rejected``。"""
    if set(document) != START_KEYS:
        raise Rejected("start_keys")
    kind, owner, seconds = document["kind"], document["owner"], document["max_seconds"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise Rejected("kind")
    if not safe_segment(owner):
        raise Rejected("owner")
    found = layout.target(kind, owner)
    if found is None:  # 這個 kind 的讀寫 root 沒有掛進來
        raise Rejected("kind")
    io = document["io"]
    if not isinstance(io, str) or io not in IO_MODES:
        raise Rejected("io")
    if not _seconds_allowed(seconds, kind, io):
        raise Rejected("max_seconds")
    argv, env, spec_document = _argv(document["argv"]), document["env"], document["spec"]
    issue = spec.executor_env_issue(env)
    if issue or not isinstance(env, dict):
        raise Rejected(issue or "env_type")
    if not isinstance(spec_document, dict):
        raise Rejected("spec_invalid")
    root, target = found
    home, tmp = layout.state_dirs(kind, owner)
    parsed, code = _spec(spec_document, spec, kind, frozenset({target, home, tmp}), layout)
    if not hello_ports <= set(parsed.block_ports):
        raise Rejected("spec_ports")
    granted = {rule.path for rule in parsed.rules if rule.access == "rwx"}
    if document["cwd"] != target or target not in granted:
        raise Rejected("cwd")
    data = json.dumps({**spec_document, "parent_pid": server_pid}).encode("utf-8")
    environment = {str(name): str(value) for name, value in env.items()}  # 已驗證都是字串
    code_root, folder = code or (None, None)
    return StartPlan(
        argv, target, environment, data, parsed.block_ports, root, home, tmp, seconds, io,
        folder, code_root,
    )


class FrameIO:
    """一條連線的 frame 讀寫（格式與上限由 kernel 的 ``framing`` 決定）。"""

    def __init__(
        self, framing: FramingModule, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.framing = framing
        self._reader, self._writer = reader, writer
        self._decoder = framing.FrameDecoder()
        self._frames: deque[FrameLike] = deque()

    async def read(self) -> FrameLike | None:
        """下一個 frame；連線結束回 None；格式錯誤拋 ``framing.FrameError``。"""
        while not self._frames:
            try:
                data = await self._reader.read(CHUNK_BYTES)
            except OSError:
                return None
            if not data:
                self._decoder.close()
                return None
            self._frames.extend(self._decoder.feed(data))
        return self._frames.popleft()

    async def send(self, data: bytes) -> bool:
        """送出；對方已經斷線回 False。"""
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, RuntimeError):
            return False
        return True

    async def error(self, code: str) -> None:
        """回 ERROR（之後由呼叫端關閉連線）。"""
        await self.send(self.framing.encode_error(code))


def hello_ports(value: object, spec: SpecModule) -> frozenset[int] | None:
    """HELLO 的封鎖埠：同 spec 的格式（不重複、1～65535、最多 1024 個），而且包含預設集合。"""
    if not isinstance(value, list) or len(value) > spec.MAX_BLOCK_PORTS:
        return None
    if not all(type(p) is int and 1 <= p <= spec.MAX_PORT for p in value):
        return None
    ports = frozenset(value)
    return ports if len(ports) == len(value) and ports >= spec.DEFAULT_BLOCK_PORTS else None


def _hello_error(frame: FrameLike, host: SessionHost) -> tuple[str | None, Hello | None]:
    """（ERROR 代碼、HELLO）；代碼是 None 才算通過。權杖先於版本檢查。"""
    framing = host.framing
    try:
        hello = framing.decode_json(frame) if frame.kind == framing.HELLO else None
    except framing.FrameError:
        hello = None
    if hello is None or set(hello) != HELLO_KEYS:
        return framing.ERROR_PROTOCOL, None
    if not host.token_ok(hello["token"]):
        return framing.ERROR_AUTH, None
    if type(hello["v"]) is not int or hello["v"] != framing.PROTOCOL_VERSION:
        return framing.ERROR_VERSION, None
    ports = hello_ports(hello["block_ports"], host.spec)
    if hello["mode"] not in MODES or ports is None:
        return framing.ERROR_PROTOCOL, None
    return None, (hello["mode"], ports)


async def handshake(host: SessionHost, io: FrameIO) -> Hello | None:
    """HELLO 要在 ``host.hello_seconds`` 內到；通過回（mode、HELLO 的封鎖埠）。"""
    try:
        frame = await asyncio.wait_for(io.read(), host.hello_seconds)
    except TimeoutError:
        return None
    except host.framing.FrameError:
        await io.error(host.framing.ERROR_PROTOCOL)
        return None
    if frame is None:
        return None
    code, hello = _hello_error(frame, host)
    if code:
        await io.error(code)
    return hello


async def serve_exec(host: SessionHost, io: FrameIO, ports: frozenset[int]) -> None:
    """READY 之後：收 START、驗證、準備資料夾，跑完一條指令。"""
    framing = host.framing
    try:
        frame = await asyncio.wait_for(io.read(), START_SECONDS)
        if frame is None:
            return
        document = framing.decode_json(frame) if frame.kind == framing.START else None
    except (TimeoutError, framing.FrameError):
        document = None
    if document is None:
        await io.error(framing.ERROR_PROTOCOL)
        return
    try:
        plan = check_start(
            document, spec=host.spec, layout=host.layout, hello_ports=ports,
            server_pid=host.server_pid,
        )
        await host.prepare(plan)
    except Rejected as exc:
        logger.warning("Executor START rejected (%s)", exc.reason)
        await io.error(framing.ERROR_REJECTED)
        return
    await _run(host, io, plan)


def _write_all(fd: int, data: bytes) -> None:
    """Spec 寫進 pipe（啟動器讀到 EOF 才開始）；啟動器沒讀就結束時照樣收尾。"""
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        pass
    finally:
        os.close(fd)


def _close(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def read_status(fd: int, spec: SpecModule) -> str | None:
    """指令結束後讀啟動器的狀態 pipe（不等待）；只認 ``FAILURE_CODES``。"""
    try:
        os.set_blocking(fd, False)
        raw = os.read(fd, spec.STATUS_MAX_BYTES + 1)
    except OSError:
        return None
    return spec.parse_status(raw, spec.FAILURE_CODES)


def exit_document(
    returncode: int | None, launcher_error: str | None, *, timed_out: bool, failure_exit: int
) -> dict[str, object]:
    """EXIT：啟動器失敗照原代碼；被信號殺掉帶 signal（沒回收到就當 SIGKILL）；其他帶 code。"""
    if launcher_error:
        code, sig = failure_exit, None
    elif returncode is None or returncode < 0:
        code, sig = None, -returncode if returncode else SIGKILL
    else:
        code, sig = returncode, None
    return {"code": code, "signal": sig, "timed_out": timed_out, "launcher_error": launcher_error}


async def _run(host: SessionHost, io: FrameIO, plan: StartPlan) -> None:
    framing, stdio = host.framing, plan.io == "stdio"
    exec_id = secrets.token_hex(16)
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    net_fd = await host.lend_ruleset(plan.ports)
    fds = (spec_r, status_w, *((net_fd,) if net_fd is not None else ()))
    argv = [*host.launcher_prefix, "--spec-fd", str(spec_r), "--status-fd", str(status_w)]
    argv += ["--net-ruleset-fd", str(net_fd)] if net_fd is not None else []
    threading.Thread(target=_write_all, args=(spec_w, plan.spec), daemon=True).start()
    try:
        proc = await host.launch(
            exec_id, [*argv, "--", *plan.argv], cwd=plan.cwd,
            env={**plan.env, EXEC_ID_ENV: exec_id}, pass_fds=fds, close_fds=True,
            new_process_group=True, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if stdio else asyncio.subprocess.STDOUT,
        )
    except OSError:
        logger.warning("Executor could not start the launcher", exc_info=True)
        proc = None
    finally:
        _close(spec_r, status_w, net_fd)
    try:
        if proc is None:
            await io.error(framing.ERROR_REJECTED)
            return
        if not await io.send(framing.encode_json(framing.STARTED, {"exec_id": exec_id})):
            await _kill(host, proc)
            return
        outcome = await _supervise(host, io, proc, plan.max_seconds, stdio=stdio)
        if outcome == "protocol":
            await io.error(framing.ERROR_PROTOCOL)
        elif outcome != "gone":
            document = exit_document(
                proc.returncode, read_status(status_r, host.spec),
                timed_out=outcome == "timeout", failure_exit=host.spec.FAILURE_EXIT,
            )
            await io.send(framing.encode_json(framing.EXIT, document))
    finally:
        _close(status_r)
        await host.finished(exec_id)


async def _kill(host: SessionHost, proc: ChildProcess) -> None:
    """群組長還沒被回收才 killpg（pgid 不會被重用），再有界等待回收。"""
    if proc.returncode is None:
        host.kill_group(proc.pid)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), REAP_SECONDS)


async def _supervise(
    host: SessionHost, io: FrameIO, proc: ChildProcess, seconds: int, *, stdio: bool
) -> str:
    """轉送輸出、聽 relay；回 exit／stop／timeout／gone（斷線）／protocol。``seconds`` 0 不限時。"""
    output = asyncio.ensure_future(_forward(io, proc, stdio=stdio))
    client = asyncio.ensure_future(_listen(io, proc))
    try:
        done, _ = await asyncio.wait(
            {output, client}, timeout=seconds or None, return_when=asyncio.FIRST_COMPLETED
        )
        if output in done:
            outcome = output.result()
        else:
            outcome = client.result() if client in done else "timeout"
        if outcome != "exit":
            await _kill(host, proc)
    except BaseException:
        if proc.returncode is None:
            host.kill_group(proc.pid)
        raise
    finally:
        for task in (output, client):
            task.cancel()
        await asyncio.gather(output, client, return_exceptions=True)
    return outcome


async def _forward(io: FrameIO, proc: ChildProcess, *, stdio: bool) -> str:
    """Stdout 送 OUT；stdio 時 stderr 另外送 ERR；都讀到 EOF 才等回收。"""
    pumps = [_pump(io, io.framing.OUT, proc.stdout)]
    if stdio:
        pumps.append(_pump(io, io.framing.ERR, proc.stderr))
    if not all(await asyncio.gather(*pumps)):
        return "gone"
    await proc.wait()
    return "exit"


async def _pump(io: FrameIO, kind: int, stream: asyncio.StreamReader | None) -> bool:
    """一條輸出照原樣轉送到 EOF（一定是 PIPE；沒有就不讀）；連線斷了回 False。"""
    while stream is not None and (chunk := await stream.read(CHUNK_BYTES)):
        if not await io.send(io.framing.encode(kind, chunk)):
            return False
    return True


async def _listen(io: FrameIO, proc: ChildProcess) -> str:
    pending: asyncio.Queue[bytes | None] = asyncio.Queue(MAX_STDIN_FRAMES)
    writer = asyncio.create_task(_write_stdin(proc, pending))
    try:
        return await _read_control(io, proc, pending)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def _write_stdin(proc: ChildProcess, pending: asyncio.Queue[bytes | None]) -> None:
    """寫 pipe 不得佔用控制通道；EOF 仍排在已接收的輸入之後。"""
    stdin = proc.stdin
    while stdin is not None:
        chunk = await pending.get()
        if chunk is None:
            stdin.close()
            return
        if not stdin.is_closing():
            with contextlib.suppress(OSError):
                stdin.write(chunk)
                await stdin.drain()


async def _read_control(
    io: FrameIO, proc: ChildProcess, pending: asyncio.Queue[bytes | None]
) -> str:
    """STDIN／STDIN_EOF 照轉，STOP 停止，EOF 就是斷線（＝STOP），其他是協定錯誤。"""
    framing, stdin = io.framing, proc.stdin
    eof = False
    while True:
        try:
            frame = await io.read()
        except framing.FrameError:
            return "protocol"
        if frame is None:
            return "gone"
        if frame.kind == framing.STOP:
            return "stop"
        if stdin is None or eof or frame.kind not in (framing.STDIN, framing.STDIN_EOF):
            return "protocol"
        eof = frame.kind == framing.STDIN_EOF
        try:
            pending.put_nowait(None if eof else frame.payload)
        except asyncio.QueueFull:
            # 不等待空位：持續灌入而不消費的伺服器必須終止，不能卡住 STOP／EOF。
            return "protocol"
