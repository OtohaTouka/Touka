"""Touka 隔離啟動器（信任根；只用標準函式庫，以 ``python -I -S`` 依路徑執行）.

用法::

    launcher.py --spec-fd N [--net-ruleset-fd M] [--status-fd S] -- <argv…>
    launcher.py --self-test [--pin|--no-pin] --dir <DIR>
    launcher.py --probe

啟動步驟（``--spec-fd``）：

1. 從繼承來的 pipe fd 讀 spec（上限 1 MiB，讀完就關），用 ``spec.parse_spec`` 驗證；
2. ``NO_NEW_PRIVS``、``PDEATHSIG(SIGKILL)``、``oom_score_adj=500``（失敗只略過）、``umask(022)``；
3. 查 ABI：沒有 Landlock 或 ABI<3 就失敗；超過 7 當 7；``net_strict`` 而且有 TCP 規則時，每次都讀
   Fast Open／MPTCP 設定，不安全就以 ``net_*`` 拒絕（``check_net_sysctls``）；
4. **路徑層**：每條規則從 ``/`` 逐段用 ``O_PATH|O_NOFOLLOW`` 開啟（中間段加 ``O_DIRECTORY``），
   依實際型別授權（``rule_rights``），加上 scope（ABI≥6），然後 ``restrict_self``；
5. **網路層最後套**：有 ``--net-ruleset-fd`` 就套用共用規則集並關掉；沒有、但 spec 有封鎖埠
   且 ABI≥4，就自己建（MCP 用）。先路徑層再網路層，每條指令的核心記憶體約 4.7 MiB
   （反過來約 9.2 MiB，U1a 量測），封鎖效果相同；
6. ``setrlimit``；關掉 stdio 與 pin 以外的所有 fd；``execvpe``：spec 的 ``exec_env``（條目自己的
   環境變數）到這一步才合併進交給目標的環境，啟動器自己的 ``os.environ`` 從頭到尾不變
   （``LD_PRELOAD`` 這類變數不會在限制之前被啟動器讀到）。

失敗時 exit 125，stderr 一行 ``TOUKA_SANDBOX_ERROR:<code>``（代碼見 ``spec.FAILURE_CODES``，
給人看）；有 ``--status-fd`` 時代碼另寫進那條 pipe（一拿到就設 CLOEXEC，exec 前也會關掉，指令
寫不到），runner 只認那條通道，指令自己印的標記不算。

**pin 模式（預設）與它的限制**：Docker Desktop 的 data 掛載是 9p（drvfs ``cache=0x5``），
dentry 用完就丟，下一次 lookup 會配新的 inode，而 Landlock 規則綁在舊 inode 上，所以不 pin
時 9p 上的授權每次都會失效（U1a：0/100）。pin 模式把規則路徑的 O_PATH fd 搬到 100
以上帶過 exec（不設 CLOEXEC；shell 的 ``exec 12>檔案`` 蓋不到），讓這些 inode 一直被
引用。這些 fd 只能拿來定位，不能讀內容；用
``/proc/self/fd/N`` 重新開啟仍要經過 Landlock 的權利檢查。限制：只有「持有這些 fd 的行程
還活著」時有效。下列情況在 9p 上會被誤拒（fail-closed，不會多放行）：

- 指令或 MCP 自己關掉繼承來的 fd（``closefrom``／``close_range``、daemon 化）；
- 父行程先結束，子行程（例如 ``close_fds`` 之後的孫行程）才存取；
- Windows 端在指令執行中改名或刪除被 pin 住的資料夾（未實測）。

放在 named volume（ext4，例如 sandbox state）的路徑不受影響。
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import platform
import re
import select
import shutil
import socket
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

try:
    import fcntl
    import resource
except ImportError:  # Windows：只跑純函式測試，不會走到 pin 與 setrlimit
    fcntl = resource = None

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

_KERNEL_DIR = Path(__file__).resolve().parent
_LAUNCHER = Path(__file__).resolve()


def _load_sibling(name: str) -> ModuleType:
    """依路徑載入同目錄的模組（``-I`` 不會把腳本目錄放進 ``sys.path``）。"""
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


landlock = _load_sibling("landlock")
spec = _load_sibling("spec")

_MAX_FD_ARG = re.compile(r"[0-9]{1,7}")
_FIRST_INHERITED_FD = 3  # 0～2 是 stdio
_PIN_FD_FLOOR = 100  # pin 搬到這以上，shell 的 exec 3>…～exec 99>… 蓋不到
_STAGE2_ARGC = 2  # --self-test-stage2 <config>
_SKIP_WHEN_OPTIONAL = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_SELFTEST_TIMEOUT_SECONDS = 30.0
_SYSTEM_EXEC_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_SCRIPT = b"#!/bin/sh\necho ok > /dev/null\nexit 0\n"
NET_SYSCTL_PATHS = ("/proc/sys/net/ipv4/tcp_fastopen", "/proc/sys/net/mptcp/enabled")
_LAUNCH_OPTIONS = frozenset({"--spec-fd", "--net-ruleset-fd", "--status-fd"})


class LaunchError(Exception):
    """啟動器自己的失敗；``code`` 會變成失敗標記。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Command:
    """解析後的命令列。"""

    mode: str
    spec_fd: int | None = None
    net_fd: int | None = None
    status_fd: int | None = None
    argv: tuple[str, ...] = ()
    directory: str | None = None
    pin: bool = True
    stage2: str | None = None


def _fd_arg(value: str | None) -> int:
    if value is None or not _MAX_FD_ARG.fullmatch(value) or int(value) < _FIRST_INHERITED_FD:
        raise spec.SpecError("usage")
    return int(value)


def _options(tokens: list[str], allowed: frozenset[str]) -> dict[str, str]:
    if len(tokens) % 2:
        raise spec.SpecError("usage")
    values: dict[str, str] = {}
    for name, value in zip(tokens[::2], tokens[1::2], strict=True):
        if name not in allowed or name in values:
            raise spec.SpecError("usage")
        values[name] = value
    return values


def _parse_self_test(tokens: list[str]) -> Command:
    pin = True
    rest = []
    for arg in tokens:
        if arg in {"--pin", "--no-pin"}:
            pin = arg == "--pin"
        else:
            rest.append(arg)
    directory = _options(rest, frozenset({"--dir"})).get("--dir")
    if directory is None or not directory.startswith("/"):
        raise spec.SpecError("usage")
    return Command("self-test", directory=directory, pin=pin)


def parse_cli(argv: Sequence[str]) -> Command:
    """解析命令列；不合格式拋 ``SpecError("usage")``。"""
    args = list(argv)
    if args == ["--probe"]:
        return Command("probe")
    if args[:1] == ["--self-test"]:
        return _parse_self_test(args[1:])
    if args[:1] == ["--self-test-stage2"]:
        if len(args) != _STAGE2_ARGC:
            raise spec.SpecError("usage")
        return Command("self-test-stage2", stage2=args[1])
    if "--" not in args:
        raise spec.SpecError("usage")
    split = args.index("--")
    command = tuple(args[split + 1 :])
    values = _options(args[:split], _LAUNCH_OPTIONS)
    if not command or not command[0]:
        raise spec.SpecError("usage")
    spec_fd = _fd_arg(values.get("--spec-fd"))
    net_fd = _fd_arg(values["--net-ruleset-fd"]) if "--net-ruleset-fd" in values else None
    status_fd = _fd_arg(values["--status-fd"]) if "--status-fd" in values else None
    fds = [fd for fd in (spec_fd, net_fd, status_fd) if fd is not None]
    if len(set(fds)) != len(fds):
        raise spec.SpecError("usage")
    return Command("launch", spec_fd=spec_fd, net_fd=net_fd, status_fd=status_fd, argv=command)


# ── 路徑規則 ────────────────────────────────────────────────────────────────────


def rule_rights(rule: spec.RuleSpec, info: os.stat_result, abi: int) -> int | None:
    """依目標的實際型別決定權利；不能授權回 None。

    - 資料夾：``access`` 的權利依 ABI 遮罩（``dev_rw`` 不給資料夾）；
    - 一般檔與九個公開設定檔名：只有名稱在九個裡、一般檔、唯讀、``st_nlink == 1`` 才給 READ_FILE；
    - 字元裝置：只在 ``/dev`` 底下、``ro`` 或 ``dev_rw``；
    - 其他（符號連結本身、FIFO、socket、區塊裝置）：不授權。
    """
    mode = info.st_mode
    handled = landlock.handled_fs(abi)
    parent, _, name = rule.path.rpartition("/")
    if stat.S_ISREG(mode) or name in spec.PUBLIC_CONFIG_NAMES:
        single = stat.S_ISREG(mode) and info.st_nlink == 1 and rule.access == "ro"
        return landlock.READ_FILE if single and name in spec.PUBLIC_CONFIG_NAMES else None
    if stat.S_ISDIR(mode):
        return None if rule.access == "dev_rw" else landlock.ACCESS_RIGHTS[rule.access] & handled
    if stat.S_ISCHR(mode) and parent == "/dev" and rule.access in {"ro", "dev_rw"}:
        return landlock.ACCESS_RIGHTS[rule.access] & landlock.DEVICE_RIGHTS & handled
    return None


class PathOpener(Protocol):
    """開規則路徑（真實作是 ``PosixOpener``，測試用假的）。"""

    def open_rule_path(self, path: str) -> tuple[int, os.stat_result]:
        """回傳 O_PATH fd 與它的 stat（目標是符號連結時 stat 的是連結本身）。"""
        ...

    def keep(self, fd: int) -> int:
        """pin：讓 fd 帶過 exec；回傳要保留的 fd（可能換了號碼）。"""
        ...

    def close(self, fd: int) -> None:
        """關掉 fd。"""
        ...


class PosixOpener:
    """從 ``/`` 逐段開啟：中間段 ``O_PATH|O_DIRECTORY|O_NOFOLLOW``，最後一段不加 ``O_DIRECTORY``.

    中間段是符號連結時核心回 ENOTDIR；最後一段是符號連結時會開到連結本身（不報錯），
    由 ``rule_rights`` 依 ``S_ISLNK`` 拒絕。
    """

    def open_rule_path(self, path: str) -> tuple[int, os.stat_result]:
        """見類別說明。"""
        parts = path.split("/")[1:]
        walk = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in parts[:-1]:
                child = os.open(part, walk, dir_fd=current)
                os.close(current)
                current = child
            fd = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
        finally:
            os.close(current)
        try:
            return fd, os.fstat(fd)
        except OSError:
            os.close(fd)
            raise

    def keep(self, fd: int) -> int:
        """搬到 ``_PIN_FD_FLOOR`` 以上（``F_DUPFD`` 不帶 CLOEXEC，會帶過 exec），關掉原本的。"""
        try:
            return fcntl.fcntl(fd, fcntl.F_DUPFD, _PIN_FD_FLOOR)
        except OSError as exc:
            raise LaunchError("process_setup", "pin_fd") from exc
        finally:
            os.close(fd)

    def close(self, fd: int) -> None:
        """關掉 fd。"""
        os.close(fd)


def _grant_rule(
    rule: spec.RuleSpec, abi: int, ruleset_fd: int, api: landlock.LandlockApi, opener: PathOpener
) -> int | None:
    """開啟並授權一條規則；回傳已授權的 fd，略過回 None，必要規則不能授權就拋錯。"""
    try:
        fd, info = opener.open_rule_path(rule.path)
    except OSError as exc:
        skip = exc.errno in _SKIP_WHEN_OPTIONAL if rule.optional else exc.errno == errno.ELOOP
        if skip and (rule.optional or rule.skip_symlink):
            return None
        name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        raise LaunchError("path_open", f"{rule.path} {name}") from exc
    rights = rule_rights(rule, info, abi)
    if not rights:
        opener.close(fd)
        if rule.optional or (rule.skip_symlink and stat.S_ISLNK(info.st_mode)):
            return None
        raise LaunchError("path_type", rule.path)
    try:
        api.add_path_rule(ruleset_fd, rights, fd)
    except OSError as exc:
        opener.close(fd)
        raise LaunchError("landlock_rule", rule.path) from exc
    return fd


def _restrict_and_close(api: landlock.LandlockApi, ruleset_fd: int, what: str) -> None:
    try:
        api.restrict_self(ruleset_fd)
    except OSError as exc:
        raise LaunchError("landlock_rule", what) from exc
    finally:
        api.close(ruleset_fd)


def apply_sandbox(
    launch_spec: spec.LaunchSpec,
    abi: int,
    net_fd: int | None,
    api: landlock.LandlockApi,
    opener: PathOpener,
) -> list[int]:
    """先套路徑層（含 scope），最後才套網路層；回傳 pin 住的 fd。"""
    abi = landlock.effective_abi(abi)
    try:
        fs_fd = api.create_ruleset(landlock.handled_fs(abi), 0, landlock.handled_scopes(abi))
    except OSError as exc:
        raise LaunchError("landlock_rule", "create") from exc
    pins: list[int] = []
    try:
        for rule in launch_spec.rules:
            fd = _grant_rule(rule, abi, fs_fd, api, opener)
            if fd is None:
                continue
            if launch_spec.pin:
                pins.append(opener.keep(fd))
            else:
                opener.close(fd)
    except BaseException:
        api.close(fs_fd)
        raise
    _restrict_and_close(api, fs_fd, "fs")
    if net_fd is not None:
        _restrict_and_close(api, net_fd, "net")
    elif launch_spec.block_ports and abi >= landlock.MIN_NET_ABI:
        try:
            built = landlock.build_net_ruleset(api, launch_spec.block_ports)
        except OSError as exc:
            raise LaunchError("landlock_rule", "net_build") from exc
        _restrict_and_close(api, built, "net")
    return pins


# ── 啟動 ────────────────────────────────────────────────────────────────────────


def _read_spec(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > spec.MAX_SPEC_BYTES:
                raise spec.SpecError("spec_too_large")
            chunks.append(chunk)
    except OSError as exc:
        raise LaunchError("spec_read") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return b"".join(chunks)


def _linux_landlock() -> tuple[landlock.LinuxLandlock, int]:
    try:
        api = landlock.LinuxLandlock()
        abi = api.abi()
    except (OSError, AttributeError, TypeError) as exc:
        raise LaunchError("landlock_unavailable") from exc
    if abi <= 0:
        raise LaunchError("landlock_unavailable")
    if abi < landlock.MIN_ABI:
        raise LaunchError("abi_too_old", str(abi))
    return api, abi


def _set_process_attributes(api: landlock.LinuxLandlock) -> None:
    try:
        api.prctl(landlock.PR_SET_NO_NEW_PRIVS, 1)
        api.prctl(landlock.PR_SET_PDEATHSIG, landlock.SIGKILL)
    except OSError as exc:
        raise LaunchError("process_setup") from exc
    with contextlib.suppress(OSError):
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY | os.O_CLOEXEC)
        try:
            os.write(fd, b"500")
        finally:
            os.close(fd)
    os.umask(0o022)


def _apply_rlimits(limits: tuple[tuple[str, int], ...]) -> None:
    if not limits:
        return
    names = {"cpu": resource.RLIMIT_CPU, "fsize": resource.RLIMIT_FSIZE, "as": resource.RLIMIT_AS}
    for name, value in limits:
        try:
            resource.setrlimit(names[name], (value, value))
        except (OSError, ValueError) as exc:
            raise LaunchError("rlimit", name) from exc


def _close_inherited_fds(keep: list[int]) -> None:
    """關掉 stdio 與 pin 以外的 fd（spawn 端本來就該 close_fds，這裡再保險一次）。"""
    low = _FIRST_INHERITED_FD
    for fd in sorted(set(keep)):
        if fd > low:
            os.closerange(low, fd)
        low = max(low, fd + 1)
    os.closerange(low, os.sysconf("SC_OPEN_MAX"))


def _exec(argv: tuple[str, ...], exec_env: tuple[tuple[str, str], ...]) -> int:
    """成功就不會回來；找不到指令回 127，其他 exec 失敗回 126（和 shell 一樣，不是隔離失敗）.

    ``exec_env`` 只合併進交給目標的環境（同名時蓋過啟動器的值），不寫進自己的 ``os.environ``。
    啟動器是信任根，只能用標準函式庫、以 ``-I -S`` 執行（不能經 ``src.config``）；它的環境是
    spawn 端已經過允許清單的那份，這裡只讀來組交給目標的環境，不當設定讀。
    """
    target_env = {**os.environ, **dict(exec_env)}  # noqa: TID251, ENV_SSOT_VIOLATION
    try:
        os.execvpe(argv[0], list(argv), target_env)
    except OSError as exc:
        os.write(2, f"{argv[0]}: {exc.strerror}\n".encode("utf-8", "replace"))
        return 127 if exc.errno == errno.ENOENT else 126
    return 126


def check_net_sysctls(launch_spec: spec.LaunchSpec, abi: int, net_fd: int | None) -> None:
    """``net_strict`` 而且這次有 TCP 規則時，每次啟動都讀 Fast Open／MPTCP；不安全就拒絕執行。

    只有人核准、features 不含 tcp 的降級送 ``net_strict: false``（照實標示「埠擋不住」）。
    """
    tcp_rules = net_fd is not None or (
        bool(launch_spec.block_ports) and landlock.effective_abi(abi) >= landlock.MIN_NET_ABI
    )
    if not (launch_spec.net_strict and tcp_rules):
        return
    issue = net_sysctl_issue(*map(_read_sysctl, NET_SYSCTL_PATHS))
    if issue:
        raise LaunchError(issue)


def launch(command: Command) -> int:
    """``--spec-fd`` 模式。"""
    launch_spec = spec.parse_spec(_read_spec(command.spec_fd))
    api, abi = _linux_landlock()
    _set_process_attributes(api)
    if launch_spec.parent_pid and os.getppid() != launch_spec.parent_pid:
        raise LaunchError("process_setup", "parent_gone")  # PDEATHSIG 生效前 spawn 端就結束了
    check_net_sysctls(launch_spec, abi, command.net_fd)
    pins = apply_sandbox(launch_spec, abi, command.net_fd, api, PosixOpener())
    _apply_rlimits(launch_spec.rlimits)
    _close_inherited_fds(pins)
    return _exec(command.argv, launch_spec.exec_env)


def _claim_status_fd(fd: int | None) -> int | None:
    """一拿到就設 CLOEXEC：指令與之後 fork 的東西都拿不到；exec 前的 fd 清理也會關掉它。"""
    if fd is None:
        return None
    try:
        os.set_inheritable(fd, False)
    except OSError as exc:
        raise LaunchError("usage", "status_fd") from exc
    return fd


def _fail(code: str, detail: str = "", status_fd: int | None = None) -> int:
    """Stderr 一行標記給人看；有狀態 fd 時只把代碼（不含 detail）寫進去並關掉。"""
    line = spec.FAILURE_MARKER + code + (" " + detail if detail else "")
    os.write(2, (line.replace("\n", " ") + "\n").encode("utf-8", "replace"))
    if status_fd is not None:
        with contextlib.suppress(OSError):
            os.write(status_fd, spec.encode_status(code))
        with contextlib.suppress(OSError):
            os.close(status_fd)
    return spec.FAILURE_EXIT


def _emit(report: dict, code: int) -> int:
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


# ── --probe ─────────────────────────────────────────────────────────────────────


def run_probe() -> int:
    """印出核心、ABI、errata 與 kernel 雜湊（一行 JSON）；不做任何限制。"""
    abi = errata = 0
    with contextlib.suppress(OSError, AttributeError, TypeError):
        api = landlock.LinuxLandlock()
        abi, errata = api.abi(), api.errata()
    report = {"kernel": platform.release(), "machine": platform.machine(), "abi": abi}
    report.update(abi_effective=landlock.effective_abi(abi), errata=errata)
    report.update(python=platform.python_version(), spec_version=spec.SPEC_VERSION)
    return _emit({**report, "kernel_digest": spec.kernel_digest(_KERNEL_DIR)}, 0)


# ── --self-test：用 exec 模式（和正式環境相同）在 --dir 上實測 ────────────────────


def _selftest_fixture(root: Path) -> dict[str, str]:
    """建好讀寫區、唯讀區、拒絕區與一個單檔規則；回傳路徑。"""
    root.mkdir(mode=0o700)
    for name in ("rw", "ro", "deny", "pub"):
        (root / name).mkdir(mode=0o700)
    (root / "ro" / "data.txt").write_bytes(b"read-only\n")
    tool = root / "ro" / "tool.sh"
    tool.write_bytes(_SCRIPT)
    tool.chmod(0o755)
    (root / "deny" / "secret.txt").write_bytes(b"denied\n")
    (root / "pub" / "pytest.ini").write_bytes(b"[pytest]\n")
    (root / "pub" / "other.txt").write_bytes(b"denied\n")
    return {name: str(root / name) for name in ("rw", "ro", "deny", "pub")}


def _selftest_rules(paths: dict[str, str]) -> list[dict]:
    system = (*_SYSTEM_EXEC_PATHS, *(str(Path(p).resolve()) for p in (sys.prefix, sys.base_prefix)))
    rules = [{"path": p, "access": "rox", "optional": True} for p in system]
    rules += [
        {"path": "/etc", "access": "ro", "optional": True},
        {"path": "/proc", "access": "ro"},
        {"path": "/dev/null", "access": "dev_rw"},
        {"path": str(_KERNEL_DIR), "access": "rox"},
        {"path": paths["rw"], "access": "rwx"},
        {"path": paths["ro"], "access": "rox"},
        {"path": f"{paths['pub']}/pytest.ini", "access": "ro", "optional": True},
    ]
    return rules


def _listener(family: int, address: object) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind(address)
    sock.listen(4)
    return sock


def _wait_child(pid: int, out_fd: int, deadline: float) -> tuple[int | None, bytes]:
    """讀子行程輸出直到 EOF；逾時就 SIGKILL，回 (None, 已讀到的輸出)。"""
    chunks: list[bytes] = []
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        ready = select.select([out_fd], [], [], max(remaining, 0))[0] if remaining > 0 else []
        if not ready:
            timed_out = True
            os.kill(pid, landlock.SIGKILL)
            break
        chunk = os.read(out_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    status = os.waitpid(pid, 0)[1]
    return (None if timed_out else os.waitstatus_to_exitcode(status)), b"".join(chunks)


def _launcher_command(spec_fd: int) -> list[str]:
    """自測用的啟動器命令列（可信直譯器、``-I -S``、這份啟動器）。"""
    python = spec.trusted_interpreter(require_root_owned=False)
    return [python, "-I", "-S", str(_LAUNCHER), "--spec-fd", str(spec_fd), "--"]


def _spawn_launcher(spec_bytes: bytes, argv: list[str]) -> tuple[int | None, bytes]:
    """以 exec 模式跑一次啟動器（spec 經 pipe fd 傳）；stdout 與 stderr 合併回傳。"""
    spec_r, spec_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(spec_w, spec_bytes)  # 自測的 spec 很小，不會塞滿 pipe
    os.close(spec_w)
    launcher_argv = _launcher_command(spec_r)
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(out_w, 1)
            os.dup2(out_w, 2)
            os.set_inheritable(spec_r, True)
            os.execv(launcher_argv[0], [*launcher_argv, *argv])
        finally:
            os._exit(127)
    os.close(spec_r)
    os.close(out_w)
    try:
        return _wait_child(pid, out_r, time.monotonic() + _SELFTEST_TIMEOUT_SECONDS)
    finally:
        os.close(out_r)


def _judge(code: int | None, output: bytes, expected: list[str]) -> tuple[str | None, list[str]]:
    text = output.decode("utf-8", "replace")
    if code is None:
        return "timeout", []
    marker = re.search(re.escape(spec.FAILURE_MARKER) + r"([a-z0-9_]{1,64})", text)
    if code == spec.FAILURE_EXIT and marker:
        return marker.group(1), []
    lines = [line for line in text.splitlines() if line.startswith("{")]
    try:
        stage2 = json.loads(lines[-1]) if lines else {}
    except ValueError:
        stage2 = {}
    checks = stage2.get("checks") if isinstance(stage2, dict) else None
    if code != 0 or not isinstance(checks, dict):
        return f"stage2_exit_{code}", []
    failed = [str(item) for item in stage2.get("failed", [])]
    failed += [f"{name}:missing" for name in expected if name not in checks]
    return ("check_failed" if failed else None), failed


def _read_sysctl(path: str) -> str | None:
    """只讀設定值；不存在回 None，其他讀取錯誤回空字串（看不懂，當成開著）。"""
    try:
        return Path(path).read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ""


def _sysctl_bits(text: str | None) -> int:
    try:
        return int(text) if text is not None else -1  # -1：每個位元都當成開著
    except ValueError:
        return -1


def net_sysctl_issue(fastopen: str | None, mptcp: str | None) -> str | None:
    """網路設定讓連線繞過 Landlock（TCP 規則只看 connect／bind）時回失敗代碼，否則 None.

    Fast Open 的 client 位元（1）讓 sendto 直接連線，讀不到也當成開著；MPTCP socket 不算 TCP，
    MPTCP 只能編進核心，檔案不存在（``None``）就是沒有 MPTCP。
    """
    if _sysctl_bits(fastopen) & 1:
        return spec.NET_TCP_FASTOPEN
    return spec.NET_MPTCP if mptcp is not None and _sysctl_bits(mptcp) else None


def run_self_test(directory: str, *, pin: bool) -> int:
    """最後一行 JSON：``{"ok", "abi", "error", "pin", "failed", "net"}``；通過 exit 0.

    ``net``：TCP 規則擋不住的網路設定，只另外回報（proxy 依它扣掉 tcp）；檔案、bind 與一般 TCP
    的檢查照跑，自測本身照常判定。
    """
    report: dict = {"ok": False, "abi": 0, "error": None, "pin": pin, "failed": [], "net": None}
    try:
        _api, abi = _linux_landlock()
    except LaunchError as exc:
        report["abi"] = int(exc.detail) if exc.detail.isdigit() else 0
        report["error"] = exc.code
        return _emit(report, spec.FAILURE_EXIT)
    report["abi"] = abi
    try:
        python = spec.trusted_interpreter(require_root_owned=False)
    except spec.InterpreterUntrustedError as exc:
        report["error"] = exc.code
        return _emit(report, 1)
    effective = landlock.effective_abi(abi)
    if effective >= landlock.MIN_NET_ABI:  # 有 TCP 規則才看；只讀這個命名空間的設定，不建 socket
        report["net"] = net_sysctl_issue(*map(_read_sysctl, NET_SYSCTL_PATHS))
    root = Path(directory)
    created = not root.exists()
    if created:
        root.mkdir(mode=0o700)
    fixture = root / f"selftest-{os.urandom(4).hex()}"  # 上次被打斷留下的 fixture 不會撞名
    try:
        with contextlib.ExitStack() as stack:
            config: dict = {**_selftest_fixture(fixture), "tcp": None, "abstract": None}
            ports: list[int] = []
            if effective >= landlock.MIN_NET_ABI:
                listeners = [_listener(socket.AF_INET, ("127.0.0.1", 0)) for _ in range(2)]
                for sock in listeners:
                    stack.callback(sock.close)
                ports = [sock.getsockname()[1] for sock in listeners]
                config["tcp"] = {"blocked": ports[0], "allowed": ports[1]}
            if landlock.handled_scopes(effective):
                name = f"\0touka-selftest-{os.getpid()}"
                stack.callback(_listener(socket.AF_UNIX, name).close)
                config["abstract"] = name
            launch_spec = {
                "version": spec.SPEC_VERSION,
                "profile": "self_test",
                "rules": _selftest_rules(config),
                "block_ports": ports[:1],
                "pin": pin,
                "net_strict": False,  # 網路設定只另外回報（net），其餘檢查照跑
            }
            stage2 = [python, "-I", "-S", str(_LAUNCHER), "--self-test-stage2"]
            code, output = _spawn_launcher(
                json.dumps(launch_spec).encode("utf-8"), [*stage2, json.dumps(config)]
            )
        expected = [name for name, _action, _want in _stage2_checks(config)]
        report["error"], report["failed"] = _judge(code, output, expected)
    except OSError as exc:
        report["error"] = "selftest_setup"
        report["failed"] = [errno.errorcode.get(exc.errno or 0, str(exc.errno))]
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        if created:
            with contextlib.suppress(OSError):
                root.rmdir()
    report["ok"] = report["error"] is None
    return _emit(report, 0 if report["ok"] else 1)


def _run_script(path: Path) -> None:
    """fork＋exec 一支 shebang 腳本（stdout 丟到 /dev/null），exit 不是 0 就拋錯。"""
    pid = os.fork()
    if pid == 0:
        try:
            null = os.open("/dev/null", os.O_WRONLY)
            os.dup2(null, 1)
            os.execv(str(path), [str(path)])
        finally:
            os._exit(127)
    code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    if code != 0:
        raise OSError(errno.ECHILD, f"exit {code}")


def _roundtrip(path: Path, mode: int = 0o644) -> Path:
    """在限制之後才建的子資料夾裡寫檔、讀回；回傳路徑（之後整個 fixture 一起刪）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_SCRIPT)
    path.chmod(mode)
    if path.read_bytes() != _SCRIPT:
        raise OSError(errno.EIO, str(path))
    return path


def _connect(family: int, address: object) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(address)


def _bind(address: tuple[str, int]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(address)


def _stage2_checks(cfg: dict) -> list[tuple[str, Callable[[], object], int | None]]:
    rw, ro, deny, pub = (Path(cfg[name]) for name in ("rw", "ro", "deny", "pub"))
    data, public = ro / "data.txt", pub / "pytest.ini"
    eacces, eperm = errno.EACCES, errno.EPERM
    ppid = os.getppid()
    checks: list[tuple[str, Callable[[], object], int | None]] = [
        ("rw_write", lambda: _roundtrip(rw / "f.txt"), None),
        ("rw_new_subdir", lambda: _roundtrip(rw / "new" / "deeper" / "f.txt"), None),
        ("rw_exec_script", lambda: _run_script(_roundtrip(rw / "run.sh", 0o755)), None),
        ("ro_read", data.read_bytes, None),
        ("ro_exec_script", lambda: _run_script(ro / "tool.sh"), None),
        ("ro_write_denied", lambda: os.close(os.open(data, os.O_WRONLY)), eacces),
        ("ro_truncate_denied", lambda: os.truncate(data, 0), eacces),
        ("ro_create_denied", lambda: os.close(os.open(ro / "n", os.O_WRONLY | os.O_CREAT)), eacces),
        ("deny_read", (deny / "secret.txt").read_bytes, eacces),
        ("deny_list", lambda: list(deny.iterdir()), eacces),
        ("file_rule_read", public.read_bytes, None),
        ("file_rule_write_denied", lambda: os.close(os.open(public, os.O_WRONLY)), eacces),
        ("file_rule_sibling_denied", (pub / "other.txt").read_bytes, eacces),
        ("parent_environ_denied", Path(f"/proc/{ppid}/environ").read_bytes, eacces),
    ]
    if cfg["tcp"]:
        blocked, allowed = (("127.0.0.1", cfg["tcp"][k]) for k in ("blocked", "allowed"))
        checks += [
            ("tcp_blocked", lambda: _connect(socket.AF_INET, blocked), eacces),
            ("tcp_allowed", lambda: _connect(socket.AF_INET, allowed), None),
            ("tcp_bind_blocked", lambda: _bind(blocked), eacces),
        ]
    if cfg["abstract"]:
        checks += [
            ("signal_parent_denied", lambda: os.kill(ppid, 0), eperm),
            ("abstract_unix_denied", lambda: _connect(socket.AF_UNIX, cfg["abstract"]), eperm),
        ]
    return checks


def _self_test_stage2(config_text: str) -> int:
    """在 Landlock 之內跑：每項記成 true／false，最後一行輸出 JSON。"""
    cfg = json.loads(config_text)
    results: dict[str, bool] = {}
    failed: list[str] = []
    for name, action, want in _stage2_checks(cfg):
        try:
            action()
            got = None
        except OSError as exc:
            got = exc.errno
        results[name] = got == want
        if got != want:
            failed.append(f"{name}:{errno.errorcode.get(got or 0, 'allowed')}")
    return _emit({"checks": results, "failed": failed}, 0)


def main(argv: Sequence[str] | None = None) -> int:
    """命令列入口。"""
    status_fd = None
    try:
        command = parse_cli(sys.argv[1:] if argv is None else argv)
        status_fd = _claim_status_fd(command.status_fd)
        if command.mode == "probe":
            return run_probe()
        if command.mode == "self-test":
            return run_self_test(command.directory, pin=command.pin)
        if command.mode == "self-test-stage2":
            return _self_test_stage2(command.stage2)
        return launch(command)
    except (spec.SpecError, LaunchError) as exc:
        return _fail(exc.code, exc.detail, status_fd)


if __name__ == "__main__":
    sys.exit(main())
