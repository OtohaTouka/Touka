"""執行器伺服器（iso-c2 C2-2）：在 ``touka-executor`` 容器裡替 proxy 的 relay 跑代理人指令.

``python -m src.services.isolation.executor_server --ipc-dir D --kernel-dir K --state-dir S
--root rw:P … --root ro:P … [--require-foreign-peer]``（PYTHONPATH 來自映像的 ENV）。只用標準
函式庫、``execution_broker`` 與 ``executor_session``；kernel 從 ``--kernel-dir`` 依路徑載入；不讀
環境變數、不載入 ``src.config``。

啟動順序（``ExecutorServer.start``；任何一步出錯就**保持存活、不服務**，不退出，避免重啟循環）：

0. 設定其他項都對時，先把兩個私有資料夾（state、ipc）補回擁有者位元、拿掉群組與其他人的位元
   （只修自己擁有、不是連結的；其餘不碰，照樣判 ``private_dir``）；
1. ipc 要是自己的私有資料夾，拿到 flock 才刪上一次留下的 socket 與 alive；
2. 驗證設定（``config_problem``）、找可信直譯器、算 kernel 與伺服器程式的 digest；
3. 補擁有者位元、清空自己的 tmp 與殘留的自測資料夾；
4. 在資料所在的 9p 與 state 上各做一次啟動器自測；
5. 開 subreaper → bind（umask 讓 socket 是 0600）→ 寫新權杖與 alive → listen。

服務中：accept **先**查 SO_PEERCRED 才算連線上限；每 5 秒更新 alive、補 ipc 與 state_root 的
擁有者位元並拿掉群組與其他人的位元（約每 60 秒加上掛載根，只補擁有者位元）；每 30 秒與每條
指令結束時收孤兒；自測失敗時每 10 分鐘重做。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import logging
import os
import secrets
import shutil
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

from src.services.execution_broker import spawn_exec
from src.services.isolation import executor_session as es

try:
    import fcntl
except ImportError:  # Windows：只跑假 stream 的單元測試
    fcntl = None

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from types import ModuleType

logger = logging.getLogger(__name__)

# 伺服器的程式清單（相對 ToukaProxy）：``server_digest`` 與 proxy 端核對都用這一份
EXECUTOR_CODE_FILES = (
    "src/services/isolation/executor_server.py",
    "src/services/isolation/executor_session.py",
    "src/services/execution_broker.py",
)
PROXY_ROOT = Path(__file__).resolve().parents[3]
KERNEL_MODULES = ("framing", "spec", "landlock", "owner_repair")
MAX_CONNECTIONS, MAX_PENDING = 256, 8  # 全部連線／還沒通過 HELLO 的連線
HELLO_SECONDS = 2.0
MAINTAIN_SECONDS, ROOT_REPAIR_EVERY = 5.0, 12  # 掛載根每 12 次（約 60 秒）修一次
SWEEP_SECONDS, SWEEP_PASSES, SWEEP_PAUSE_SECONDS = 30.0, 8, 0.05
SELFTEST_SECONDS, SELFTEST_RETRY_SECONDS = 30.0, 600.0
RULESET_CACHE_SIZE = 2
MAX_SOCKET_PATH_BYTES = 100
MAX_TMP_OWNERS = 4096
# START 前的修復：同一目標 10 秒一次、每 60 秒最多 120 次（下一個時間窗自動恢復）
REPAIR_TARGET_GAP, REPAIR_WINDOW_SECONDS, REPAIR_PER_WINDOW = 10.0, 60.0, 120
REPAIR_COUNTERS = ("repaired", "stopped", "throttled")
PR_SET_CHILD_SUBREAPER = 36
LOCK_NAME, ALIVE_NAME = ".lock", "alive"
SELFTEST_PREFIX = ".touka-executor-selftest-"  # 和 proxy 的 ``.touka-selftest-`` 互不清除
_PEERCRED = struct.Struct("3i")
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


class ConfigError(Exception):
    """命令列或 kernel 不合格；``code`` 寫進 log。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def executor_code_digest(proxy_root: Path) -> str:
    """``EXECUTOR_CODE_FILES`` 的 sha256（依路徑排序，逐檔「路徑＋NUL＋內容＋NUL」）."""
    digest = hashlib.sha256()
    for name in sorted(EXECUTOR_CODE_FILES):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((proxy_root / name).read_bytes() + b"\0")
    return digest.hexdigest()


class LandlockApi(Protocol):
    """``landlock.LinuxLandlock`` 用到的部分（libc 的 syscall／prctl）."""

    def prctl(self, option: int, value: int) -> None:
        """``prctl(option, value, 0, 0, 0)``；失敗拋 OSError."""
        ...


class LandlockModule(Protocol):
    """伺服器用到的 ``sandbox_kernel/landlock.py``（共用網路規則集、subreaper 的 prctl）."""

    MIN_NET_ABI: int
    LinuxLandlock: Callable[[], LandlockApi]
    build_net_ruleset: Callable[[LandlockApi, tuple[int, ...]], int]


class RepairOutcomeLike(Protocol):
    """``owner_repair.RepairOutcome``：改了幾個 inode、停在哪裡."""

    changed: int
    stopped: str | None


class OwnerRepairModule(Protocol):
    """伺服器用到的 ``sandbox_kernel/owner_repair.py``（路徑鏈修復；自己的私有 inode 另外收回）."""

    repair_chain: Callable[[str, str], RepairOutcomeLike]
    repair_private: Callable[[str, str], RepairOutcomeLike]


@dataclass(frozen=True)
class Kernel:
    """從 ``--kernel-dir`` 依路徑載入的 kernel 模組，各自照上面的 Protocol 標註."""

    directory: Path
    framing: es.FramingModule
    spec: es.SpecModule
    landlock: LandlockModule
    owner_repair: OwnerRepairModule


def load_kernel(directory: Path) -> Kernel:
    """依路徑載入 kernel（模組名稱同 ``launcher._load_sibling``）；載不到拋 ``ConfigError``.

    靜態型別看不到 importlib 載入的模組，所以在這裡 cast 成 Protocol；成員是否齊全由
    ``test_kernel_modules_have_every_protocol_member`` 拿真的 kernel 核對。
    """
    directory = directory.resolve()
    tag = hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:12]
    modules: dict[str, ModuleType] = {}
    for name in KERNEL_MODULES:
        module_name = f"touka_sandbox_kernel_{tag}_{name}"
        if module_name not in sys.modules:
            found = importlib.util.spec_from_file_location(module_name, directory / f"{name}.py")
            if found is None or found.loader is None:
                raise ConfigError("kernel")
            sys.modules[module_name] = importlib.util.module_from_spec(found)
            try:
                found.loader.exec_module(sys.modules[module_name])
            except (OSError, SyntaxError, ImportError) as exc:
                sys.modules.pop(module_name, None)
                raise ConfigError("kernel") from exc
        modules[name] = sys.modules[module_name]
    return Kernel(
        directory,
        cast("es.FramingModule", modules["framing"]),
        cast("es.SpecModule", modules["spec"]),
        cast("LandlockModule", modules["landlock"]),
        cast("OwnerRepairModule", modules["owner_repair"]),
    )


@dataclass(frozen=True)
class ServerConfig:
    """命令列給的設定（都是容器內的絕對 POSIX 路徑，``config_problem`` 再核對）."""

    ipc: str  # ``ctl.sock``、``token``、``alive``、``.lock`` 所在的私有資料夾
    kernel: str  # 唯讀的 sandbox kernel（依路徑載入）
    state: str  # 每個代理人的 home／tmp 在 ``<state>/<kind>/<owner>/``
    rw: tuple[str, ...]  # 讀寫掛載根；名稱不能重複（``personal_drive``、``managed``）
    ro: tuple[str, ...]  # 唯讀掛載根（技能）
    require_foreign_peer: bool  # 只服務別的 pid namespace 來的連線（proxy 的 relay）


def parse_cli(argv: Iterable[str]) -> ServerConfig:
    """命令列 → ``ServerConfig``；用法錯誤拋 ``ConfigError("usage")``."""
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    for name in ("--ipc-dir", "--kernel-dir", "--state-dir"):
        parser.add_argument(name, required=True)
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--require-foreign-peer", action="store_true")
    try:
        args = parser.parse_args(list(argv))
    except (argparse.ArgumentError, SystemExit) as exc:
        raise ConfigError("usage") from exc
    roots = [root.partition(":")[::2] for root in args.root]
    if any(access not in {"rw", "ro"} or not path for access, path in roots):
        raise ConfigError("usage")
    rw, ro = (tuple(path for access, path in roots if access == want) for want in ("rw", "ro"))
    foreign = args.require_foreign_peer
    return ServerConfig(
        ipc=args.ipc_dir, kernel=args.kernel_dir, state=args.state_dir, rw=rw, ro=ro,
        require_foreign_peer=foreign,
    )


class FsView(Protocol):
    """設定檢查（``config_problem``、ipc 私有資料夾）看檔案系統的方式；真的是 ``PosixView``.

    只讀不寫；單元測試換成假的，每一項檢查在任何平台都能跑。
    """

    uid: int  # 伺服器的有效 uid：私有資料夾要是它擁有

    def lstat(self, path: str) -> os.stat_result:
        """不跟隨連結的 stat；不存在拋 OSError."""
        ...

    def resolve(self, path: str) -> str:
        """跟隨全部連結後的實際路徑（strict）；不存在拋 OSError."""
        ...

    def names(self, path: str) -> list[str]:
        """資料夾裡的名稱；讀不到拋 OSError."""
        ...


class PosixView:
    """真的檔案系統（容器裡是 Linux；Windows 沒有 euid，當 0）."""

    def __init__(self) -> None:
        self.uid = os.geteuid() if hasattr(os, "geteuid") else 0

    def lstat(self, path: str) -> os.stat_result:
        """見 ``FsView.lstat``."""
        return os.lstat(path)

    def resolve(self, path: str) -> str:
        """見 ``FsView.resolve``."""
        return str(Path(path).resolve(strict=True))

    def names(self, path: str) -> list[str]:
        """見 ``FsView.names``."""
        with os.scandir(path) as entries:
            return [entry.name for entry in entries]


def _canonical_dir(view: FsView, path: str, uid: int | None = None) -> os.stat_result | None:
    """不經連結（resolve 後不變）的資料夾；有給 ``uid`` 時還要是它擁有。"""
    try:
        info = view.lstat(path)
        canonical = view.resolve(path) == path
    except OSError:
        return None
    ok = canonical and stat.S_ISDIR(info.st_mode) and uid in (None, info.st_uid)
    return info if ok else None


def _private_problem(view: FsView, path: str) -> str | None:
    """``path`` 要是伺服器擁有、群組與其他人沒有任何權限的資料夾."""
    info = _canonical_dir(view, path, view.uid)
    return None if info and not stat.S_IMODE(info.st_mode) & 0o077 else "private_dir"


def data_root(rw: Iterable[str]) -> str | None:
    """資料所在的讀寫 root（9p、自測用）：名稱是 ``managed`` 的那個；沒有回 None（不退回別的）."""
    return next((p for p in rw if PurePosixPath(p).name == es.MANAGED_ROOT), None)


def config_problem(config: ServerConfig, kernel: Kernel, view: FsView) -> str | None:
    """設定的問題代碼（沒有回 None），依檢查順序.

    ``roots``：沒有讀寫 root 或名稱重複；``data_root``：沒有名稱是 ``managed`` 的讀寫 root；
    ``root_path``：root 不存在、不是資料夾或路上有連結；
    ``touka_root``：root 第一層像 Touka 資料根或帶標記（讀不到也算）；``nested``：roots、state、
    ipc 彼此巢狀；``socket_path``：socket 路徑超過 100 bytes；``private_dir``：state 或 ipc 不是
    伺服器擁有、群組與其他人沒有權限的資料夾。
    """
    roots = (*config.rw, *config.ro)
    names = [PurePosixPath(p).name for p in config.rw]
    if not config.rw or len(set(names)) != len(names):
        return "roots"
    if data_root(config.rw) is None:
        return "data_root"
    for root in roots:
        if _canonical_dir(view, root) is None:
            return "root_path"
        with contextlib.suppress(OSError):  # 讀不到內容也當成 Touka 的資料夾（fail-closed）
            folded = [kernel.spec.fold_name(name) for name in view.names(root)]
            if kernel.spec.listing_kind(folded) is None:
                continue
        return "touka_root"
    pairs = combinations((*roots, config.state, config.ipc), 2)
    if any(es.within(a, b) or es.within(b, a) for a, b in pairs):
        return "nested"
    socket_path = str(PurePosixPath(config.ipc, kernel.framing.SOCKET_NAME))
    if len(socket_path.encode("utf-8")) > MAX_SOCKET_PATH_BYTES:
        return "socket_path"
    return _private_problem(view, config.state) or _private_problem(view, config.ipc)


class RepairBudget:
    """START 前的修復頻率（見 ``REPAIR_*``）。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._window = (float("-inf"), 0)

    def allow(self, target: str, now: float) -> bool:
        """這次可不可以修 ``target``；可以就計入。"""
        start, used = self._window
        if now - start >= REPAIR_WINDOW_SECONDS:
            start, used = now, 0
            self._last = {k: v for k, v in self._last.items() if now - v < REPAIR_TARGET_GAP}
        recent = now - self._last.get(target, float("-inf")) < REPAIR_TARGET_GAP
        allowed = used < REPAIR_PER_WINDOW and not recent
        if allowed:
            self._last[target] = now
        self._window = (start, used + allowed)
        return allowed


class RulesetCache:
    """共用網路規則集：以 spec 的封鎖埠為鍵、最多留 ``RULESET_CACHE_SIZE`` 份.

    借出的一律是鎖內 ``os.dup`` 的複本（呼叫端 spawn 完就關）：淘汰時關掉的是快取那一份，正要
    spawn 的指令拿到的 fd 不會被關，號碼也不會被別的檔重用（r4 LOW-2）。
    """

    def __init__(self, build: Callable[[tuple[int, ...]], int]) -> None:
        self._build = build
        self._fds: OrderedDict[tuple[int, ...], int] = OrderedDict()
        self._lock = asyncio.Lock()

    async def lend(self, ports: Iterable[int]) -> int:
        """這組封鎖埠的規則集複本；建不出來拋 OSError。"""
        key = tuple(sorted(set(ports)))
        async with self._lock:
            if key not in self._fds:
                self._fds[key] = await asyncio.to_thread(self._build, key)
                while len(self._fds) > RULESET_CACHE_SIZE:
                    os.close(self._fds.popitem(last=False)[1])
            self._fds.move_to_end(key)
            return os.dup(self._fds[key])


def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    try:  # （狀態、ppid、sid）
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[-1].split()
        return fields[0].decode("ascii"), int(fields[1]), int(fields[3])
    except (OSError, IndexError, ValueError, UnicodeDecodeError):
        return None


def _proc_exec_id(pid: int) -> str | None:
    try:  # 讀不到回 None；沒有這個變數回空字串
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    prefix = es.EXEC_ID_ENV.encode("ascii") + b"="
    found = next((item for item in raw.split(b"\0") if item.startswith(prefix)), b"")
    return found[len(prefix) :].decode("ascii", "replace")


class ProcessTable(Protocol):
    """孤兒清掃看的行程表：真的是 ``LinuxProcesses``（``/proc``），單元測試換成假的."""

    def children(self, parent: int) -> list[tuple[int, bool]]:
        """父行程是 ``parent`` 的（pid、是不是殭屍）."""
        ...

    def reap(self, pid: int) -> None:
        """回收這個殭屍（只對這個 pid）."""
        ...

    def kill_unless(
        self, pid: int, parent: int, keep: Callable[[str | None, int | None], bool]
    ) -> bool:
        """``keep(exec id, sid)`` 不成立就殺掉；回傳有沒有殺."""
        ...


class LinuxProcesses:
    """``/proc`` 上的 ``ProcessTable``（伺服器開了 subreaper 才用）."""

    def __init__(self, exclude: Callable[[int], bool] | None = None) -> None:
        self._exclude = exclude or (lambda _pid: False)

    def children(self, parent: int) -> list[tuple[int, bool]]:
        """父行程是 ``parent`` 的（pid、是不是殭屍）。"""
        found = []
        with os.scandir("/proc") as entries:
            pids = [int(e.name) for e in entries if e.name.isdigit()]
        for pid in pids:
            info = None if self._exclude(pid) else _proc_stat(pid)
            if info is not None and info[1] == parent:
                found.append((pid, info[0] == "Z"))
        return found

    def reap(self, pid: int) -> None:
        """只對這個 pid ``waitpid(WNOHANG)``（不搶 asyncio 的回收）。"""
        with contextlib.suppress(OSError):
            os.waitpid(pid, os.WNOHANG)

    def kill_unless(
        self, pid: int, parent: int, keep: Callable[[str | None, int | None], bool]
    ) -> bool:
        """先 pidfd 釘住再讀 stat／environ（pid 被重用也不會殺錯）；``keep`` 不成立就 SIGKILL."""
        try:
            pidfd = os.pidfd_open(pid)
        except OSError:
            return False
        try:
            info = _proc_stat(pid)
            if info is None or info[1] != parent or keep(_proc_exec_id(pid), info[2]):
                return False
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except OSError:
            return False
        finally:
            os.close(pidfd)
        return True


def sweep_orphans(
    table: ProcessTable, parent: int, *, direct: frozenset[int], running_ids: frozenset[str],
    running_sids: frozenset[int],
) -> int:
    """收孤兒：環境的 exec id 或 sid 屬於仍在執行的指令才保留，結束的、不明的、讀不到的都殺.

    直接子行程（asyncio 會回收）不碰；殭屍只回收；被殺的行程的子行程會再被收養，所以最多掃
    ``SWEEP_PASSES`` 輪。回傳殺掉的數量。
    """

    def keep(exec_id: str | None, sid: int | None) -> bool:
        return exec_id in running_ids or sid in running_sids

    killed = 0
    for _ in range(SWEEP_PASSES):
        acted = False
        for pid, zombie in table.children(parent):
            if pid in direct:
                continue
            if zombie:
                table.reap(pid)
            elif table.kill_unless(pid, parent, keep):
                killed, acted = killed + 1, True
        if not acted:
            break
        time.sleep(SWEEP_PAUSE_SECONDS)
    return killed


def peer_pid(writer: asyncio.StreamWriter) -> int | None:
    """``SO_PEERCRED`` 的 pid（別的 pid namespace 是 0）；拿不到回 None。"""
    try:
        sock = writer.get_extra_info("socket")
        return _PEERCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _PEERCRED.size))[0]
    except (AttributeError, OSError, TypeError, struct.error):
        return None


def kill_group(pid: int) -> None:
    """SIGKILL 整個行程群組（broker 以 ``start_new_session`` 啟動，pgid 就是 pid）。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def make_private_dir(path: str) -> None:
    """建一層 0700 的資料夾（已存在就不動）。"""
    Path(path).mkdir(mode=0o700, exist_ok=True)  # noqa: IO_PRISON_VIOLATION  # 獨立容器，依設計不載入 ResourceManager（§2.1）


def _plain_dir(path: str, uid: int | None = None) -> bool:
    """``path`` 本身是資料夾（不跟連結）；有給 ``uid`` 時還要是它擁有."""
    try:
        info = os.lstat(path)  # 連結不算資料夾
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and uid in (None, info.st_uid)


@dataclass
class Hooks:
    """伺服器對外的動作（測試換成假的）；None 的欄位用 kernel 補上真的。"""

    spawn: Callable[..., Awaitable[es.ChildProcess]] = spawn_exec
    selftest: Callable[[str], Awaitable[es.SelfTestLike]] | None = None
    repair: Callable[[str, str], RepairOutcomeLike] | None = None
    repair_private: Callable[[str, str], RepairOutcomeLike] | None = None
    build_ruleset: Callable[[tuple[int, ...]], int] | None = None
    processes: ProcessTable | None = None
    peer_pid: Callable[[asyncio.StreamWriter], int | None] = peer_pid
    kill_group: Callable[[int], None] = kill_group


class ExecutorServer:
    """執行器伺服器；一條連線的處理在 ``executor_session``（它把這個物件當 ``host``）。"""

    def __init__(
        self, config: ServerConfig, kernel: Kernel, hooks: Hooks | None = None, *,
        require_root_interpreter: bool = True,
    ) -> None:
        self.config, self.kernel, self.hooks = config, kernel, hooks or Hooks()
        self.framing, self.spec = kernel.framing, kernel.spec
        self.layout = es.Layout(config.rw, config.ro, config.state, es.system_exec_paths())
        self.server_pid, self.boot_id = os.getpid(), secrets.token_hex(16)
        self.view: FsView = PosixView()
        self.uid = self.view.uid
        self.hello_seconds = HELLO_SECONDS
        self.max_connections, self.max_pending = MAX_CONNECTIONS, MAX_PENDING
        self.refusals: Counter[str] = Counter()
        self.repairs: Counter[str] = Counter()
        self.problem: str | None = None
        self.launcher_prefix: tuple[str, ...] = ()
        self._require_root = require_root_interpreter
        self._token: str | None = None
        self._open = self._pending = 0
        self._running: dict[str, int | None] = {}  # exec id → sid（＝群組長的 pid）
        self._direct: set[int] = set()
        self._waiters: set[asyncio.Future[int]] = set()
        self._spawn_lock = asyncio.Lock()
        self._tests: dict[str, es.SelfTestLike] = {}
        self._rulesets = RulesetCache(self.hooks.build_ruleset or self._build_ruleset)
        self._budget = RepairBudget()
        self._listener: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._digests = ("", "")
        self._tasks: list[asyncio.Task[None]] = []

    # ── 判定與 READY ─────────────────────────────────────────────────────────────────

    @property
    def abi(self) -> int:
        """自測量到的 ABI。"""
        return max((result.abi for result in self._tests.values()), default=0)

    @property
    def enforced(self) -> bool:
        """資料所在的 9p 與 state 兩次自測都通過才算（9p 失敗要等 9p 自測通過才恢復）。"""
        return all(self._tests.get(n) is not None and self._tests[n].ok for n in ("data", "state"))

    def token_ok(self, token: object) -> bool:
        """權杖格式正確而且相同（常數時間比對）；還沒服務時一律不對。"""
        valid = isinstance(token, str) and self.framing.TOKEN_PATTERN.fullmatch(token)
        return bool(valid and self._token and hmac.compare_digest(token, self._token))

    def ready(self, ports: frozenset[int]) -> dict[str, object]:
        """READY：proxy 用它判定層級（kernel、伺服器程式、封鎖埠、roots 都要一致）。"""
        net = next((result.net for result in self._tests.values() if result.net), None)
        roots = [{"path": p, "access": "rw"} for p in self.config.rw]
        return {
            "v": self.framing.PROTOCOL_VERSION, "abi": self.abi, "enforced": self.enforced,
            "net": net, "features": es.landlock_features(self.abi, net) if self.enforced else [],
            "roots": roots + [{"path": p, "access": "ro"} for p in self.config.ro],
            "state_root": self.config.state, "kernel_digest": self._digests[0],
            "server_digest": self._digests[1], "boot_id": self.boot_id,
            "block_ports_digest": self.framing.block_ports_digest(ports), "uid": self.uid,
            "repairs": {name: self.repairs[name] for name in REPAIR_COUNTERS},
        }

    # ── 連線 ─────────────────────────────────────────────────────────────────────────

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """一條連線：先查 peer（被擋的不計入上限）、再算上限、HELLO、READY、exec。"""
        try:
            if self.config.require_foreign_peer and self.hooks.peer_pid(writer) != 0:
                self.refusals["peer"] += 1
            elif self._open >= self.max_connections:
                self.refusals["connections"] += 1
            elif self._pending >= self.max_pending:
                self.refusals["pending"] += 1
            else:
                self._open += 1
                try:
                    await self._serve(es.FrameIO(self.framing, reader, writer))
                finally:
                    self._open -= 1
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, io: es.FrameIO) -> None:
        self._pending += 1
        try:
            hello = await es.handshake(self, io)
        finally:
            self._pending -= 1
        if hello is None:
            self.refusals["hello"] += 1
            return
        mode, ports = hello
        ready = self.framing.encode_json(self.framing.READY, self.ready(ports))
        if await io.send(ready) and mode == "exec":
            await es.serve_exec(self, io, ports)

    # ── 一條指令（``executor_session`` 經 ``SessionHost`` 呼叫）─────────────────────────

    async def prepare(self, plan: es.StartPlan) -> None:
        """修 state、cwd 與程式資料夾的路徑鏈（有頻率限制）、建自己的 {home,tmp}（0700）.

        Kind ``mcp`` 的 cwd 就是 home；程式資料夾只修擁有者位元、不建（不見了就讓啟動器照實失敗）。
        """
        now = time.monotonic()
        targets = tuple(dict.fromkeys(t for t in (plan.home, plan.tmp, plan.cwd, plan.code) if t))
        allowed = {target for target in targets if self._budget.allow(target, now)}
        self.repairs["throttled"] += len(targets) - len(allowed)
        problem = await asyncio.to_thread(self._prepare_dirs, plan, allowed)
        if problem:
            raise es.Rejected(problem)

    def _prepare_dirs(self, plan: es.StartPlan, allowed: set[str]) -> str | None:
        for target in (plan.home, plan.tmp):
            if target in allowed:
                self._repair(self.config.state, target)
        owner, uid = PurePosixPath(plan.home).parent, self.uid
        try:
            for directory in (str(owner.parent), str(owner), plan.home, plan.tmp):
                make_private_dir(directory)
                if not _plain_dir(directory, uid):
                    return "state_dir"
        except OSError:
            return "state_dir"
        if plan.cwd in allowed and plan.cwd not in (plan.home, plan.tmp):
            self._repair(plan.root, plan.cwd)
        if plan.code is not None and plan.code_root is not None and plan.code in allowed:
            self._repair(plan.code_root, plan.code)
        return None if _plain_dir(plan.cwd) else "cwd"

    def _repair(
        self, root: str, target: str, fix: Callable[[str, str], RepairOutcomeLike] | None = None
    ) -> None:
        outcome = (fix or self.hooks.repair or self.kernel.owner_repair.repair_chain)(root, target)
        if outcome.changed:
            self.repairs["repaired"] += 1
            logger.warning("Executor repaired owner permission bits (%s inodes)", outcome.changed)
        elif outcome.stopped:
            self.repairs["stopped"] += 1

    async def lend_ruleset(self, ports: tuple[int, ...]) -> int | None:
        """ABI≥4 而且有封鎖埠才借；建不出來就交給啟動器自己建（封鎖效果相同）。"""
        if self.abi < self.kernel.landlock.MIN_NET_ABI or not ports:
            return None
        try:
            return await self._rulesets.lend(ports)
        except OSError:
            logger.warning("Executor could not build the shared net ruleset", exc_info=True)
            return None

    def _build_ruleset(self, ports: tuple[int, ...]) -> int:
        landlock = self.kernel.landlock
        return landlock.build_net_ruleset(landlock.LinuxLandlock(), ports)

    async def launch(self, exec_id: str, argv: list[str], **kwargs: object) -> es.ChildProcess:
        """先登記 exec id 再啟動（掃描拿同一把鎖，看不到啟動到一半的行程）。"""
        async with self._spawn_lock:
            self._running[exec_id] = None
            try:
                proc = await self.hooks.spawn(*argv, **kwargs)
            except BaseException:
                self._running.pop(exec_id, None)
                raise
            self._running[exec_id] = proc.pid
            self._track(proc)
        return proc

    def _track(self, proc: es.ChildProcess) -> None:
        """直接子行程交給 asyncio 回收；回收之前掃描一律跳過它。"""
        pid, waiter = proc.pid, asyncio.ensure_future(proc.wait())
        self._direct.add(pid)
        self._waiters.add(waiter)
        waiter.add_done_callback(lambda w: (self._direct.discard(pid), self._waiters.discard(w)))

    def kill_group(self, pid: int) -> None:
        """SIGKILL 整個行程群組。"""
        self.hooks.kill_group(pid)

    async def finished(self, exec_id: str) -> None:
        """取消登記，馬上收掉這條指令留下的行程。"""
        self._running.pop(exec_id, None)
        await self.sweep()

    async def sweep(self) -> int:
        """收孤兒（在執行緒裡；拿著啟動鎖，看到的登記是一致的）。"""
        if self.hooks.processes is None:
            return 0
        async with self._spawn_lock:
            sids = frozenset(sid for sid in self._running.values() if sid is not None)
            return await asyncio.to_thread(
                sweep_orphans, self.hooks.processes, self.server_pid,
                direct=frozenset(self._direct), running_ids=frozenset(self._running),
                running_sids=sids,
            )

    # ── 啟動 ─────────────────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """照模組說明的順序啟動；有錯就記下 ``problem``、回 False（保持存活、不服務）。"""
        if config_problem(self.config, self.kernel, self.view) in {None, "private_dir"}:
            self._maintain(roots=False, alive=False)  # 先收回兩個私有資料夾再檢查
        self.problem = self._claim_ipc() or self._check_config()
        if self.problem is None:
            self._maintain(roots=True, alive=False)
            self._clean_state_tmp()
            self._clean_stale_selftests()
            await self.run_selftests(("data", "state"))
            self.problem = self._enable_subreaper() or await self._listen()
        if self.problem:
            logger.error("Executor is not serving: %s", self.problem)
            return False
        ticks = iter(range(1, 1 << 62))

        async def maintain() -> None:
            roots = next(ticks) % ROOT_REPAIR_EVERY == 0
            await asyncio.to_thread(self._maintain, roots=roots)

        loop = asyncio.get_running_loop()
        for seconds, action in (
            (MAINTAIN_SECONDS, maintain), (SWEEP_SECONDS, self.sweep),
            (SELFTEST_RETRY_SECONDS, self._retry_selftests),
        ):
            self._tasks.append(loop.create_task(self._every(seconds, action)))
        return True

    def _claim_ipc(self) -> str | None:
        """Ipc 要是自己的私有資料夾；拿到 flock 才刪殘留的 socket 與 alive。"""
        problem = _private_problem(self.view, self.config.ipc)
        if problem or fcntl is None:
            return problem or "platform"
        lock_path = PurePosixPath(self.config.ipc, LOCK_NAME)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)  # noqa: IO_PRISON_VIOLATION  # 獨立容器，依設計不載入 ResourceManager（§2.1）
        except OSError:
            return "ipc_lock"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return "ipc_locked"
        self._lock_fd = fd
        for name in (self.framing.SOCKET_NAME, ALIVE_NAME):
            Path(self.config.ipc, name).unlink(missing_ok=True)  # noqa: IO_PRISON_VIOLATION  # 獨立容器，依設計不載入 ResourceManager（§2.1）
        return None

    def _check_config(self) -> str | None:
        problem = config_problem(self.config, self.kernel, self.view)
        if problem:
            return problem
        try:
            python = self.spec.trusted_interpreter(require_root_owned=self._require_root)
            kernel_digest = self.spec.kernel_digest(self.kernel.directory)
            self._digests = (kernel_digest, executor_code_digest(PROXY_ROOT))
        except self.spec.InterpreterUntrustedError:
            return self.spec.INTERPRETER_UNTRUSTED
        except OSError:
            return "digest"
        if not self._digests[0]:  # READY 的三個 digest 都必須是 hex（proxy 的合約）
            return "digest"
        self.launcher_prefix = (python, "-I", "-S", str(self.kernel.directory / "launcher.py"))
        return None

    def _enable_subreaper(self) -> str | None:
        """孤兒改由伺服器收養（才收得掉 setsid 出去的行程）。"""
        try:
            self.kernel.landlock.LinuxLandlock().prctl(PR_SET_CHILD_SUBREAPER, 1)
            self.hooks.processes = self.hooks.processes or LinuxProcesses()
        except (OSError, AttributeError):
            return "subreaper"
        return None

    def _clean_state_tmp(self) -> None:
        """清每個 kind 的 ``state/<kind>/<owner>/tmp``：整個刪掉再建（不跟連結），有數量上限。"""
        owners = 0
        for kind in es.KINDS:
            base = str(PurePosixPath(self.config.state, kind))
            with contextlib.suppress(OSError), os.scandir(base) as entries:
                for entry in entries:
                    tmp = str(PurePosixPath(entry.path, "tmp"))
                    if owners < MAX_TMP_OWNERS and _plain_dir(entry.path) and _plain_dir(tmp):
                        owners += 1
                        shutil.rmtree(tmp, ignore_errors=True)
                        with contextlib.suppress(OSError):
                            make_private_dir(tmp)

    def _clean_stale_selftests(self) -> None:
        for base in (data_root(self.config.rw), self.config.state):
            if base is None:  # config_problem 已擋下（data_root）
                continue
            with contextlib.suppress(OSError), os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith(SELFTEST_PREFIX) and _plain_dir(entry.path):
                        shutil.rmtree(entry.path, ignore_errors=True)

    async def run_selftests(self, names: Iterable[str]) -> None:
        """在資料所在的 9p（data）與 state 上各做一次啟動器自測（``--pin``，同 proxy）。"""
        for name in names:
            base = data_root(self.config.rw) if name == "data" else self.config.state
            if base is None:  # config_problem 已擋下（data_root）；萬一走到也只是失敗
                self._tests[name] = self.spec.SelfTestResult(ok=False, error="data_root")
                continue
            target = str(PurePosixPath(base, SELFTEST_PREFIX + secrets.token_hex(8)))
            try:
                self._tests[name] = await (self.hooks.selftest or self._launcher_selftest)(target)
            except OSError:
                self._tests[name] = self.spec.SelfTestResult(ok=False, error="selftest_error")
            if not self._tests[name].ok:
                logger.warning("Executor self-test failed on %s: %s", name, self._tests[name].error)

    async def _launcher_selftest(self, directory: str) -> es.SelfTestLike:
        async with self._spawn_lock:
            proc = await self.hooks.spawn(
                *self.launcher_prefix, "--self-test", "--pin", "--dir", directory,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={}, close_fds=True,
            )
            self._track(proc)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), SELFTEST_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return self.spec.SelfTestResult(ok=False, error="timeout")
        return self.spec.parse_selftest_output(proc.returncode, stdout)

    async def _listen(self) -> str | None:
        """Bind（umask 0o177 → socket 0600）→ 新權杖 → listen；listen 之前連線一律被拒。"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            previous = os.umask(0o177)
            try:
                sock.bind(str(PurePosixPath(self.config.ipc, self.framing.SOCKET_NAME)))
            finally:
                os.umask(previous)
            token = secrets.token_hex(32)
            self._replace_file(self.framing.TOKEN_NAME, token.encode("ascii"))
            self._replace_file(ALIVE_NAME, b"")
            self._token = token
            self._listener = await asyncio.start_unix_server(self.handle, sock=sock)
        except OSError:
            sock.close()
            return "listen"
        return None

    def _replace_file(self, name: str, data: bytes) -> None:
        """Mkstemp（0600、不跟連結）＋fsync＋原子置換。"""
        fd, temporary = tempfile.mkstemp(dir=self.config.ipc, prefix=f".{name}-")
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        Path(temporary).replace(Path(self.config.ipc, name))

    # ── 背景工作 ─────────────────────────────────────────────────────────────────────

    def _maintain(self, *, roots: bool, alive: bool = True) -> None:
        """自我修復（W6）：沿固定的路徑鏈補擁有者位元（kernel 的 ``repair_chain``）.

        白名單只有 ipc（資料夾、socket、權杖、alive）與 state_root；``roots`` 時加上讀寫掛載根
        （``managed`` 本身就是掛載根）。W6 是伺服器自己的私有 inode，另外拿掉群組與其他人的
        位元（``repair_private``，啟動檢查要它們是 0700）；掛載根不動群組與其他人的位元。
        不 chown；不受 START 前修復的頻率限制。
        """
        ipc, state, framing = self.config.ipc, self.config.state, self.framing
        names = (framing.SOCKET_NAME, framing.TOKEN_NAME, ALIVE_NAME)
        private = [(ipc, ipc), *((ipc, str(PurePosixPath(ipc, n))) for n in names), (state, state)]
        chains = list(private)
        if roots:
            chains += [(root, root) for root in self.config.rw]
        for root, target in chains:
            self._repair(root, target)
        close = self.hooks.repair_private or self.kernel.owner_repair.repair_private
        for root, target in private:
            self._repair(root, target, close)
        if alive:
            try:
                os.utime(PurePosixPath(ipc, ALIVE_NAME))
            except FileNotFoundError:
                self._replace_file(ALIVE_NAME, b"")

    @staticmethod
    async def _every(seconds: float, action: Callable[[], Awaitable[object]]) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await action()
            except Exception:  # 下一輪照常（例如開不了執行緒的 RuntimeError）；取消照樣往上丟
                logger.warning("Executor background round failed", exc_info=True)

    async def _retry_selftests(self) -> None:
        """自測失敗時每 10 分鐘重做失敗的那幾次。"""
        await self.run_selftests(sorted(n for n, r in self._tests.items() if not r.ok))

    async def stop(self) -> None:
        """停止服務與背景工作（測試用；正式環境隨容器結束）。"""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._listener is not None:
            self._listener.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)


async def _serve_forever(argv: list[str]) -> None:
    """設定或啟動有錯也不退出（避免重啟循環）：只記 log、不建 socket。"""
    server = None
    try:
        config = parse_cli(argv)
        server = ExecutorServer(config, load_kernel(Path(config.kernel)))
        await server.start()
    except ConfigError as exc:
        logger.warning("Executor is not serving: %s", exc.code)
    await asyncio.Event().wait()  # ``server`` 一直被這個 frame 引用


def main(argv: list[str] | None = None) -> int:
    """命令列入口（只在被信號結束時離開）。"""
    asyncio.run(_serve_forever(sys.argv[1:] if argv is None else argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
