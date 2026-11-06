"""Linux 真核心隔離測試（iso-c2）：唯一呼叫真 Landlock 的模組，``isolation_linux`` marker.

- 非 Linux 平台整個模組跳過（只算 1 個）。Linux 上 Landlock 或啟動器不可用時：
  ``TOUKA_REQUIRE_LANDLOCK=1``（CI、Docker 測試管道）失敗，否則跳過。
- 整個模組以 ``real_isolation`` 退出 conftest 的替身。
- 核心層（啟動器本身）的案例以 ``python -I -S launcher.py --spec-fd N -- argv`` 執行，載入的是
  受測的這份程式；runner、monitor、終端、MCP 的案例由各自的單位加在同一個模組。
在 Owner 的 Docker 以一次性容器跑（設計 §9.6），屬自動化測試沙盒，不是 live probe。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import itertools
import json
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import yaml
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError
from memory_server.tools import core_file_ops as cfo
from memory_server.utils.no_follow import ZoneLinkRefusedError
from memory_server.utils.zone_paths import Anchor, ZonePath
from src.config import is_container_runtime
from src.core.retry import with_retry
from src.models.identity import AgentIdentity
from src.protocols import manager as mcp_manager
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp import isolated_stdio as mcp_stdio
from src.protocols.mcp import owner as mcp_owner
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.sandbox_kernel import launcher, owner_repair, relay
from src.sandbox_kernel.spec import NET_SYSCTL_CODES, SelfTestResult
from src.services.evolution_security import EvolutionPathGuard
from src.services.execution_broker import run_sync, spawn_exec
from src.services.isolation import agent_command, evolution_command, home_wipe
from src.services.isolation import executor_server as exs
from src.services.isolation import executor_session as exe
from src.services.isolation.agent_command import CommandScope
from src.services.isolation.consent import Consent, ExecutionGrant
from src.services.isolation.mcp_launch import McpLaunchBook, McpLauncher
from src.services.isolation.mcp_profiles import (
    build_mcp_self_profile,
    build_mcp_third_party_profile,
)
from src.services.isolation.profiles import (
    authorize_derived_path,
    build_agent_command_profile,
    current_layout,
    public_config_rules,
)
from src.services.isolation.project_scan import (
    SCAN_MAX_DEPTH,
    SCAN_MAX_DIRS,
    SCAN_SECONDS,
    ProjectScanner,
)
from src.services.isolation.repair import PermissionRepairService
from src.services.isolation.runners import ExecRequest, ProfileSource, RunnerBroker
from src.services.isolation.status import LAUNCHER_NAME, IsolationMonitor
from src.services.mcp_isolation_store import RecordView, bound_of
from src.services.resource_manager import resource_manager
from src.services.review import evidence
from src.tools import terminal
from src.tools.builtin import evolution
from src.utils import touka_dirs
from src.utils.mcp_origin import OriginRecord, RawMcpEntry, classify
from src.utils.permission_repair import set_permission_repair
from src.utils.time_utils import get_monotonic_ms
from src.utils.touka_dirs import scan_below

from tests.helpers import mcp_asgi_peers
from tests.helpers.evolution_stdio_peer import EVOLUTION_PROBE_SOURCE, build_skill_venv
from tests.support.isolation import match_state_root
from tests.support.zone_records import expect_zone_refusal, raises_zone
from tests.support.zone_roots import point_zone_roots

if TYPE_CHECKING:
    from src.services.isolation.profiles import SandboxLayout

if not sys.platform.startswith("linux"):
    pytest.skip(
        "[skip:platform] Landlock isolation tests run only on Linux", allow_module_level=True
    )

pytestmark = pytest.mark.isolation_linux

LAUNCHER = Path(launcher.__file__).resolve()
ll = launcher.landlock
PY = [sys.executable, "-I", "-S", "-c"]
# 系統路徑刻意不 resolve：映像裡 /bin、/lib 等是符號連結，要被略過而不是讓啟動失敗
SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", sys.prefix, sys.base_prefix]


@pytest.fixture(autouse=True)
def _uses_real_isolation(real_isolation):
    """整個模組退出 conftest 的替身。"""
    return real_isolation


@pytest.fixture(autouse=True)
def _rm_state_follows_the_layout(tmp_path, monkeypatch):
    """RM 的 ST 指到 ``_layout`` 的（ADR-0293 P7：ST 只有一個來源，版面不同就拒絕）.

    data 根不換：這裡的測試直接在版面的個人空間裡 spawn 與讀寫，不經區內方法。自己組版面的
    測試（執行器路線等）另外指到自己的 ST。
    """
    match_state_root(monkeypatch, tmp_path / "state")


@pytest.fixture(scope="module")
def landlock_abi(touka_landlock_requirement: bool) -> int:
    try:
        abi = ll.LinuxLandlock().abi()
    except OSError:
        abi = 0
    if abi < ll.MIN_ABI:
        message = f"Landlock ABI {abi} < {ll.MIN_ABI}"
        if touka_landlock_requirement:
            pytest.fail(message)
        pytest.skip(f"[skip:landlock] {message}，且沒設 TOUKA_REQUIRE_LANDLOCK")
    return ll.effective_abi(abi)


def _rules(*extra: dict) -> list[dict]:
    rules = [{"path": p, "access": "rox", "optional": True} for p in SYSTEM]
    rules += [
        {"path": "/etc", "access": "ro", "optional": True},
        {"path": "/proc", "access": "ro"},
        {"path": "/dev/null", "access": "dev_rw"},
    ]
    return [*rules, *extra]


def _spec(*extra: dict, **fields: object) -> bytes:
    document = {"version": launcher.spec.SPEC_VERSION, "profile": "test", "rules": _rules(*extra)}
    document.update(fields)
    return json.dumps(document).encode("utf-8")


def _launch(
    spec_bytes: bytes,
    argv: list[str],
    *,
    net_fd: int | None = None,
    extra_fds: tuple[int, ...] = (),
    cwd: Path | None = None,
):
    read_fd, write_fd = os.pipe()
    writer = threading.Thread(target=_write_all, args=(write_fd, spec_bytes))
    writer.start()
    command = [sys.executable, "-I", "-S", str(LAUNCHER), "--spec-fd", str(read_fd)]
    if net_fd is not None:
        command += ["--net-ruleset-fd", str(net_fd)]
    fds = (read_fd, *((net_fd,) if net_fd is not None else ()), *extra_fds)
    try:
        return run_sync(
            [*command, "--", *argv],
            pass_fds=fds,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        os.close(read_fd)
        writer.join(timeout=30)


def _write_all(fd: int, data: bytes) -> None:
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except BrokenPipeError:
        pass  # 啟動器讀到上限就關掉 fd
    finally:
        os.close(fd)


def _report(result) -> dict:
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── 自測（exec 模式、pin）──────────────────────────────────────────────────────


def test_self_test_passes_in_pin_mode(tmp_path, landlock_abi):
    result = run_sync(
        [*PY[:3], str(LAUNCHER), "--self-test", "--pin", "--dir", str(tmp_path / "st")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = _report(result)
    assert report["ok"] is True and report["error"] is None, report
    assert report["abi"] >= landlock_abi and report["pin"] is True
    assert not (tmp_path / "st").exists()  # 自測清掉自己建的目錄


def test_self_test_passes_with_a_leftover_fixture(tmp_path, landlock_abi):
    """上次被打斷留下的 --dir 與 fixture 還在：照樣通過，不撞名，也不刪不是這次建的東西。"""
    (tmp_path / "st" / "selftest" / "rw").mkdir(parents=True)
    result = run_sync(
        [*PY[:3], str(LAUNCHER), "--self-test", "--pin", "--dir", str(tmp_path / "st")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = _report(result)
    assert report["ok"] is True and report["error"] is None, report
    assert [p.name for p in (tmp_path / "st").iterdir()] == ["selftest"]


SYSCTLS = ("/proc/sys/net/ipv4/tcp_fastopen", "/proc/sys/net/mptcp/enabled")


def _unsafe_net_sysctl(abi: int) -> str | None:
    """這個網路命名空間的實際設定（只讀）會讓哪種連線繞過 Landlock；ABI<4 沒有 TCP 規則，不看."""
    if abi < ll.MIN_NET_ABI:
        return None
    fastopen, mptcp = (Path(p).read_text("ascii") if Path(p).exists() else None for p in SYSCTLS)
    if fastopen is None or int(fastopen) & 1:
        return "net_tcp_fastopen"
    return "net_mptcp" if mptcp is not None and int(mptcp) else None


def test_self_test_reports_unsafe_net_sysctls_and_still_passes(tmp_path, landlock_abi):
    """網路設定（只讀）另外回報在 ``net``：§9.6 加 ``--sysctl net.ipv4.tcp_fastopen=0`` 時是 None，
    不加時是 Fast Open；檔案、bind 與一般 TCP 的檢查照跑，自測都通過."""
    result = run_sync(
        [*PY[:3], str(LAUNCHER), "--self-test", "--pin", "--dir", str(tmp_path / "st")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = _report(result)
    assert (report["ok"], report["error"], report["failed"]) == (True, None, []), report
    assert report["net"] == _unsafe_net_sysctl(landlock_abi)
    assert not (tmp_path / "st").exists()


def test_required_landlock_host_keeps_net_sysctls_safe(landlock_abi, touka_landlock_requirement):
    """設了 TOUKA_REQUIRE_LANDLOCK（§9.6 容器、C2-2 的 Landlock CI job）就必須關掉 Fast Open
    與 MPTCP；沒設的主機（例如 contracts job 在 runner 上原生執行）照實 skip、寫出是哪個設定."""
    issue = _unsafe_net_sysctl(landlock_abi)
    _require(issue is None, f"網路設定不安全（{issue}）", touka_landlock_requirement, tag="net")


# ── 路徑規則 ─────────────────────────────────────────────────────────────────


def test_scripts_run_from_rw_area_and_read_only_skill_area(tmp_path, landlock_abi):
    agent, skills = tmp_path / "agent", tmp_path / "skills"
    agent.mkdir()
    skills.mkdir()
    tool = skills / "tool.sh"
    tool.write_text("#!/bin/sh\necho skill-ok\n", encoding="utf-8")
    tool.chmod(0o755)
    script = (
        'printf "#!/bin/sh\\necho rw-ok\\n" > run.sh && chmod 755 run.sh && ./run.sh'
        ' && echo x > /dev/null && "$1" && { touch "$2" 2>/dev/null || echo denied-ok; }'
    )
    result = _launch(
        _spec({"path": str(agent), "access": "rwx"}, {"path": str(skills), "access": "rox"}),
        ["/bin/sh", "-c", script, "sh", str(tool), str(skills / "new")],
        cwd=agent,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["rw-ok", "skill-ok", "denied-ok"]


_READ_EACH = """
import errno, json, sys
out = {}
for path in sys.argv[1:]:
    try:
        open(path, "rb").read()
        out[path] = "ok"
    except OSError as exc:
        out[path] = errno.errorcode[exc.errno]
print(json.dumps(out))
"""


def test_upper_public_config_symlink_and_hardlink_are_skipped(tmp_path, landlock_abi):
    link_target, hard_target = tmp_path / "link-target.txt", tmp_path / "hard-target.txt"
    for secret in (link_target, hard_target):
        secret.write_text("NOT-FOR-AGENTS\n", encoding="utf-8")
    linked, hard, real = (tmp_path / name for name in ("linked", "hard", "real"))
    for parent in (linked, hard, real):
        (parent / "work").mkdir(parents=True)
    (linked / "pytest.ini").symlink_to(link_target)
    os.link(hard_target, hard / "pyproject.toml")
    (real / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    configs = [linked / "pytest.ini", hard / "pyproject.toml", real / "tox.ini"]
    rules = [{"path": str(p / "work"), "access": "rwx"} for p in (linked, hard, real)]
    rules += [
        {"path": str(p), "access": "ro", "optional": True, "skip_symlink": True} for p in configs
    ]
    paths = [*configs, link_target, hard_target]
    seen = _report(_launch(_spec(*rules), [*PY, _READ_EACH, *(str(p) for p in paths)]))
    assert seen == {
        str(configs[0]): "EACCES",  # 符號連結：略過、不授權，指令照常啟動
        str(configs[1]): "EACCES",  # 硬連結：略過
        str(configs[2]): "ok",
        str(link_target): "EACCES",
        str(hard_target): "EACCES",
    }


def test_missing_required_path_fails_closed_with_marker(tmp_path, landlock_abi):
    result = _launch(
        _spec({"path": str(tmp_path / "missing"), "access": "rwx"}),
        ["/bin/sh", "-c", "echo should-not-run"],
    )
    assert result.returncode == launcher.spec.FAILURE_EXIT
    assert f"{launcher.spec.FAILURE_MARKER}path_open" in result.stderr
    assert "should-not-run" not in result.stdout


def test_required_symlink_is_refused_not_followed(tmp_path, landlock_abi):
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    result = _launch(_spec({"path": str(tmp_path / "link"), "access": "rwx"}), ["/bin/true"])
    assert result.returncode == launcher.spec.FAILURE_EXIT
    assert f"{launcher.spec.FAILURE_MARKER}path_type" in result.stderr


def test_spec_over_one_mebibyte_is_refused_from_the_fd(landlock_abi):
    big = b" " * (launcher.spec.MAX_SPEC_BYTES + 1)
    result = _launch(big, ["/bin/sh", "-c", "echo should-not-run"])
    assert result.returncode == launcher.spec.FAILURE_EXIT
    assert f"{launcher.spec.FAILURE_MARKER}spec_too_large" in result.stderr


# ── fd：pin、/proc/self/fd 重新開啟、繼承來的 fd ─────────────────────────────────

_PIN_PROBE = """
import errno, json, os, sys
target = sys.argv[1]
pinned = []
for name in os.listdir("/proc/self/fd"):
    try:
        if os.readlink("/proc/self/fd/" + name) == target:
            pinned.append(int(name))
    except OSError:
        pass
out = {"pinned": len(pinned)}
def attempt(path, flags):
    try:
        os.close(os.open(path, flags))
        return "ok"
    except OSError as exc:
        return errno.errorcode[exc.errno]
if pinned:
    via = "/proc/self/fd/%d" % pinned[0]
    out["reopen_dir"] = attempt(via, os.O_RDONLY | os.O_DIRECTORY)
    out["reopen_write"] = attempt(via + "/data.txt", os.O_WRONLY)
print(json.dumps(out))
"""


def test_pin_mode_keeps_rule_fds_and_reopen_is_still_checked(tmp_path, landlock_abi):
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "data.txt").write_text("ro\n", encoding="utf-8")
    seen = _report(_launch(_spec({"path": str(ro), "access": "ro"}), [*PY, _PIN_PROBE, str(ro)]))
    assert seen == {"pinned": 1, "reopen_dir": "ok", "reopen_write": "EACCES"}
    unpinned_spec = _spec({"path": str(ro), "access": "ro"}, pin=False)
    unpinned = _launch(unpinned_spec, [*PY, _PIN_PROBE, str(ro)])
    assert _report(unpinned) == {"pinned": 0}


_PIN_FDS = """
import json, os, sys
target = sys.argv[1]
fds = []
for name in os.listdir("/proc/self/fd"):
    try:
        if os.readlink("/proc/self/fd/" + name) == target:
            fds.append(int(name))
    except OSError:
        pass
data = open(os.path.join(target, "data.txt"), encoding="utf-8").read()
print(json.dumps({"pins": len(fds), "all_high": all(fd >= 100 for fd in fds), "data": data}))
"""
# shell 用 exec 3>…～exec 99>… 改掉低號碼 fd 之後，再 exec 成探測程式
_REUSE_LOW_FDS = 'for n in {3..99}; do eval "exec $n>/dev/null"; done; exec "$0" -I -S -c "$1" "$2"'


def test_pins_survive_a_shell_that_reuses_low_fds(tmp_path, landlock_abi):
    """pin 搬到 100 以上：shell 的 ``exec 12>檔案`` 這類寫法不會蓋掉 pin。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "data.txt").write_text("ro\n", encoding="utf-8")
    result = _launch(
        _spec({"path": str(ro), "access": "ro"}),
        ["/bin/bash", "-c", _REUSE_LOW_FDS, sys.executable, _PIN_FDS, str(ro)],
    )
    assert _report(result) == {"pins": 1, "all_high": True, "data": "ro\n"}


_FD_PROBE = """
import json, os, sys
fd = int(sys.argv[1])
try:
    where = os.readlink("/proc/self/fd/%d" % fd)
except OSError:
    where = None
print(json.dumps({"open_to": where}))
"""


def test_inherited_fds_other_than_pins_are_closed(tmp_path, landlock_abi):
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive\n", encoding="utf-8")
    leaked = os.open(secret, os.O_RDONLY)
    try:
        result = _launch(_spec(), [*PY, _FD_PROBE, str(leaked)], extra_fds=(leaked,))
    finally:
        os.close(leaked)
    assert _report(result)["open_to"] != str(secret)


# ── 網路、/proc、信號 ────────────────────────────────────────────────────────────

_CONNECT = """
import errno, json, os, socket, sys
out = {}
for port in sys.argv[2:]:
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", int(port)))
        out[port] = "ok"
    except OSError as exc:
        out[port] = errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        s.close()
fd = int(sys.argv[1])
try:
    os.fstat(fd)
    out["net_fd"] = "open"
except OSError:
    out["net_fd"] = "closed"
print(json.dumps(out))
"""


@pytest.mark.parametrize("shared", [True, False], ids=["shared-ruleset-fd", "launcher-built"])
def test_blocked_ports_are_refused_and_others_connect(landlock_abi, shared):
    if landlock_abi < ll.MIN_NET_ABI:
        pytest.skip("[skip:abi4] TCP 規則要 ABI 4")
    listeners = []
    for _ in range(2):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        listeners.append(sock)
    blocked, allowed = (str(sock.getsockname()[1]) for sock in listeners)
    net_fd = ll.build_net_ruleset(ll.LinuxLandlock(), (int(blocked),)) if shared else None
    try:
        result = _launch(
            _spec(block_ports=[int(blocked)], net_strict=False),  # 驗規則本身；設定另有案例
            [*PY, _CONNECT, str(net_fd if shared else 999), blocked, allowed],
            net_fd=net_fd,
        )
    finally:
        if net_fd is not None:
            os.close(net_fd)
        for sock in listeners:
            sock.close()
    assert _report(result) == {blocked: "EACCES", allowed: "ok", "net_fd": "closed"}


_BIND = """
import errno, json, socket, sys
out = {}
for port in sys.argv[1:]:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", int(port)))
        out[port] = "ok"
    except OSError as exc:
        out[port] = errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        s.close()
print(json.dumps(out))
"""


def _free_ports(count: int) -> list[str]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [str(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("shared", [True, False], ids=["shared-ruleset-fd", "launcher-built"])
def test_bind_is_refused_on_blocked_ports_and_allowed_elsewhere(landlock_abi, shared):
    """封鎖埠不能 bind（搶不到 Touka 的埠）；埠 0（暫時埠）與沒封鎖的埠照常 bind。"""
    if landlock_abi < ll.MIN_NET_ABI:
        pytest.skip("[skip:abi4] TCP 規則要 ABI 4")
    blocked, allowed = _free_ports(2)
    net_fd = ll.build_net_ruleset(ll.LinuxLandlock(), (int(blocked),)) if shared else None
    try:
        result = _launch(
            _spec(block_ports=[int(blocked)], net_strict=False),  # 不安全的網路設定另有案例
            [*PY, _BIND, blocked, "0", allowed],
            net_fd=net_fd,
        )
    finally:
        if net_fd is not None:
            os.close(net_fd)
    assert _report(result) == {blocked: "EACCES", "0": "ok", allowed: "ok"}


_PROC = """
import errno, json, os
out = {}
for pid in (1, os.getppid()):
    try:
        open("/proc/%d/environ" % pid, "rb").read()
        out[str(pid)] = "ok"
    except OSError as exc:
        out[str(pid)] = errno.errorcode[exc.errno]
print(json.dumps(out))
"""


def test_other_processes_environ_is_denied(landlock_abi):
    seen = _report(_launch(_spec(), [*PY, _PROC]))
    assert set(seen.values()) == {"EACCES"}, seen


_SCOPES = """
import errno, json, os, socket, sys
out = {}
try:
    os.kill(os.getppid(), 0)
    out["signal"] = "ok"
except OSError as exc:
    out["signal"] = errno.errorcode[exc.errno]
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("\\0" + sys.argv[1])
    out["abstract"] = "ok"
except OSError as exc:
    out["abstract"] = errno.errorcode[exc.errno]
finally:
    s.close()
print(json.dumps(out))
"""


def test_signals_and_abstract_sockets_stay_inside_the_domain(landlock_abi):
    if landlock_abi < 6:
        pytest.skip("[skip:abi6] signal／abstract unix 的 scope 要 ABI 6")
    name = f"touka-iso-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind("\0" + name)
        server.listen(1)
        seen = _report(_launch(_spec(), [*PY, _SCOPES, name]))
    assert seen == {"signal": "EPERM", "abstract": "EPERM"}


_PARENT = """
import json, os, subprocess, sys
read_fd, write_fd = os.pipe()
os.write(write_fd, sys.argv[2].encode())
os.close(write_fd)
sleeper = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
child = subprocess.Popen(
    [sys.executable, "-I", "-S", sys.argv[1], "--spec-fd", str(read_fd), "--",
     sys.executable, "-I", "-S", "-c", sleeper],
    pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
print(child.stdout.readline().decode().strip(), flush=True)
os._exit(0)
"""


_EARLY_EXIT_PARENT = """
import json, os, subprocess, sys
read_fd, write_fd = os.pipe()
spec = json.loads(sys.argv[2])
spec["parent_pid"] = os.getpid()
os.write(write_fd, json.dumps(spec).encode())
os.close(write_fd)
child = subprocess.Popen(
    [sys.executable, "-I", "-S", sys.argv[1], "--spec-fd", str(read_fd), "--",
     "/bin/sh", "-c", 'echo ran > "$1"', "sh", sys.argv[3]],
    pass_fds=(read_fd,), stdout=subprocess.DEVNULL, stderr=open(sys.argv[4], "wb"),
)
print(child.pid, flush=True)
os._exit(0)
"""


def test_command_does_not_run_when_the_parent_exits_before_pdeathsig(tmp_path, landlock_abi):
    """spawn 端在啟動器設好 PDEATHSIG 前就結束：啟動器看到 ppid 不對就退出，指令不會變孤兒執行。"""
    ran, errors = tmp_path / "ran.txt", tmp_path / "launcher.err"
    spec = _spec({"path": str(tmp_path), "access": "rwx"}).decode()
    result = run_sync(
        [*PY, _EARLY_EXIT_PARENT, str(LAUNCHER), spec, str(ran), str(errors)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pid = int(result.stdout.split()[0])
    for _ in range(100):
        if _state(pid) in {"Z", "X", "gone"}:
            break
        time.sleep(0.1)
    assert _state(pid) in {"Z", "X", "gone"}
    assert not ran.exists()
    assert f"{launcher.spec.FAILURE_MARKER}process_setup" in errors.read_text(encoding="utf-8")


def _state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"


def test_command_dies_with_its_parent(landlock_abi):
    """PDEATHSIG：spawn 啟動器的行程結束時，指令一起被殺（不會變成孤兒繼續跑）。"""
    result = run_sync(
        [*PY, _PARENT, str(LAUNCHER), _spec().decode()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pid = int(result.stdout.split()[0])
    try:
        for _ in range(50):
            if _state(pid) in {"Z", "X", "gone"}:
                break
            time.sleep(0.1)
        assert _state(pid) in {"Z", "X", "gone"}
    finally:
        if _state(pid) not in {"Z", "X", "gone"}:
            os.kill(pid, 9)


_PROCESS_SETTINGS = """
import json, os
status = open("/proc/self/status", encoding="ascii").read().splitlines()
nnp = next(int(line.split()[1]) for line in status if line.startswith("NoNewPrivs:"))
oom = int(open("/proc/self/oom_score_adj", encoding="ascii").read())
print(json.dumps({"nnp": nnp, "oom": oom, "umask": os.umask(0)}))
"""


def test_launcher_sets_no_new_privs_oom_score_and_umask_itself(landlock_abi):
    """隔離內 NoNewPrivs 1、oom_score_adj 500（OOM 先殺代理人指令）、umask 022（不沿用 077）."""
    previous = os.umask(0o077)
    try:
        report = _report(_launch(_spec(), [*PY, _PROCESS_SETTINGS]))
    finally:
        os.umask(previous)
    assert report == {"nnp": 1, "oom": 500, "umask": 0o022}


# ── runner 與 monitor（proxy 端組 spec、自測判讀、端到端）────────────────────────

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])


def _require(available: bool, reason: str, required: bool, *, tag: str) -> None:
    """``tag`` 是 skip 訊息開頭的 ``[skip:<tag>]``（CI 的 checker 依它判斷這個 skip 准不准）。"""
    if available:
        return
    if required:
        pytest.fail(f"TOUKA_REQUIRE_LANDLOCK=1 但 {reason}")
    pytest.skip(f"[skip:{tag}] {reason}，且沒設 TOUKA_REQUIRE_LANDLOCK")


def _require_landlock_level(monitor: IsolationMonitor, required: bool) -> None:
    """landlock 層要可用；網路設定不安全（例如 CI 主機原生執行、沒設 sysctl）時原因寫清楚。"""
    snap = monitor.snapshot()
    reason = f"landlock 層不可用（{snap.reason}）"
    if snap.reason in NET_SYSCTL_CODES:
        reason = f"網路設定不安全（{snap.reason}），landlock 層不可用"
    _require(
        snap.level == "landlock", reason, required,
        tag="net" if snap.reason in NET_SYSCTL_CODES else "level",
    )


def _layout(tmp_path: Path) -> SandboxLayout:
    data = tmp_path / "data"
    for sub in ("personal_drive/Alice", "mcp_servers", "soul"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir()
    return replace(
        current_layout(),
        data_root=data,
        config_dir=tmp_path / "config",
        env_file=tmp_path / "host_root" / ".env",
        state_root=state,
        mirror_roots=(),
        in_container=False,
    )


# CI runner 上開機自測可能超過預設的 5 秒（FE 未解 1）：測試等到 30 秒，判定照舊
READY_TIMEOUT_SECONDS = 30


async def _ready_monitor(layout: SandboxLayout, required: bool) -> IsolationMonitor:
    kernel = layout.kernel_dir
    launcher_present = kernel is not None and (kernel / LAUNCHER_NAME).is_file()
    _require(launcher_present, "還沒有 sandbox_kernel 啟動器", required, tag="launcher")
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, start_background=False, ready_timeout=READY_TIMEOUT_SECONDS
    )
    snap = await monitor.ensure_ready()
    _require(
        snap.local.status == "available", f"本機 Landlock 不可用：{snap.local}", required,
        tag="landlock",
    )
    return monitor


async def _available_selftest(_kernel_dir: Path, _test_dir: Path) -> SelfTestResult:
    return SelfTestResult(ok=True, abi=7)


async def test_state_dir_without_write_permission_is_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root 不受目錄權限限制")
    layout = _layout(tmp_path)
    layout.state_root.chmod(0o500)
    try:
        monitor = IsolationMonitor(
            layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
        )
        # 本機 Landlock 先定為可用（還在 pending 時判定停在 probe_pending），只驗真的 ST 權限
        await monitor.run_selftest("data", notify=False)
        snap = await monitor.refresh()
    finally:
        layout.state_root.chmod(0o700)
    assert (snap.reason, snap.detail) == ("sandbox_state_unconfigured", "not_writable")


def test_real_symlink_and_hardlink_public_configs_are_skipped(tmp_path):
    layout = _layout(tmp_path)
    workspace = tmp_path / "workspace"
    project = workspace / "nested" / "proj"
    project.mkdir(parents=True)
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=1\n", encoding="utf-8")
    (workspace / "pytest.ini").symlink_to(secret)
    os.link(secret, workspace / "nested" / "pyproject.toml")
    (workspace / "tox.ini").write_text("", encoding="utf-8")
    granted = {rule.path for rule in public_config_rules((project,), layout)}
    assert (workspace / "tox.ini").resolve() in granted
    assert (workspace / "pytest.ini") not in granted and secret.resolve() not in granted
    assert (workspace / "nested" / "pyproject.toml").resolve() not in granted


async def test_stale_selftest_cleanup_does_not_follow_symlinks(tmp_path):
    """清殘留自測資料夾只清 data/mcp_servers 底下本前綴的資料夾；同前綴的符號連結不跟隨。"""
    layout = _layout(tmp_path)
    base = layout.data_root / "mcp_servers"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep\n", encoding="utf-8")
    link = base / ".touka-selftest-link"
    link.symlink_to(elsewhere, target_is_directory=True)
    stale = base / ".touka-selftest-1"
    (stale / "selftest").mkdir(parents=True)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
    )
    await monitor.run_selftest("data", notify=False)
    assert monitor.snapshot().local.status == "available"
    assert not stale.exists()
    assert link.is_symlink() and (elsewhere / "keep.txt").is_file()


async def test_launcher_selftest_reaches_available(tmp_path, touka_landlock_requirement):
    monitor = await _ready_monitor(_layout(tmp_path), touka_landlock_requirement)
    snap = monitor.snapshot()
    assert snap.local.abi >= 3 and "fs" in snap.features


async def test_unsafe_net_sysctl_is_its_own_reason_not_fs_9p(
    tmp_path, landlock_abi, touka_landlock_requirement
):
    """data 位置的自測照樣通過；網路設定不安全時層級 none、原因照實是網路設定（卡片寫出要加的
    那一行），不是 fs_9p；features 扣掉 tcp，核准後的降級仍是 Landlock."""
    layout = _layout(tmp_path)
    kernel = layout.kernel_dir
    present = kernel is not None and (kernel / LAUNCHER_NAME).is_file()
    _require(present, "還沒有 sandbox_kernel 啟動器", touka_landlock_requirement, tag="launcher")
    expected = _unsafe_net_sysctl(landlock_abi)
    monitor = IsolationMonitor(layout_provider=lambda: layout, start_background=False)
    await monitor.run_selftest("data", notify=False)
    await monitor.refresh()  # C2-2：還沒讀過設定（connections.json）之前封鎖埠不完整，判 none
    snap = monitor.snapshot()
    assert (snap.local.status, snap.local.reason, snap.local.net) == ("available", None, expected)
    level = ("landlock", None) if expected is None else ("none", expected)
    assert (snap.level, snap.reason) == level
    assert ("tcp" in snap.features) == (expected is None)
    plan = monitor.fallback_for(profile_complete=True)
    assert (plan.kind, plan.features) == ("landlock", snap.features)


async def test_consented_fallback_keeps_file_isolation_when_net_sysctls_are_unsafe(
    tmp_path, landlock_abi, touka_landlock_requirement
):
    """人核准「隔離降級」後的指令仍在 Landlock 裡：讀不到受保護的路徑，自己的資料夾照常讀寫.

    不加 sysctl 的容器裡就是網路設定不安全的情境（features 沒有 tcp）；這裡只驗檔案隔離。
    """
    layout = _layout(tmp_path)
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    expected = _unsafe_net_sysctl(landlock_abi)
    plan = monitor.fallback_for(profile_complete=True)
    assert plan.kind == "landlock" and ("tcp" in plan.features) == (expected is None)
    secret = layout.data_root / "soul" / "secret.txt"
    secret.write_text("SOUL\n", encoding="utf-8")
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    consent = ExecutionGrant("none", Consent(subject="linux", fallback="landlock"))
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    runner = (await broker.acquire(consent, ProfileSource.fixed(profile))).runner
    script = f'cat "{secret}" 2>/dev/null && echo LE""AK; echo own > own.txt && cat own.txt'
    launch = runner.plan(ExecRequest(argv=("sh", "-c", script), env={"PATH": "/usr/bin:/bin"},
                                     profile=profile))
    try:
        proc = await spawn_exec(
            *launch.argv, cwd=launch.cwd, env=launch.env, pass_fds=launch.pass_fds,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        launch.release()
    output, _ = await asyncio.wait_for(proc.communicate(), 60)
    assert (launch.level, launch.features) == ("landlock", plan.features)
    assert b"LEAK" not in output and b"SOUL" not in output, output
    assert (agent_dir / "own.txt").read_text(encoding="utf-8") == "own\n"


async def test_landlock_runner_end_to_end(tmp_path, touka_landlock_requirement):
    layout = _layout(tmp_path)
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    secret = layout.data_root / "soul" / "secret.txt"
    secret.write_text("SOUL\n", encoding="utf-8")
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    runner = (await broker.acquire(ExecutionGrant("landlock"), ProfileSource.fixed(profile))).runner
    script = (
        f'cat "{secret}" 2>/dev/null && echo LEAK; '
        'echo hi > "$HOME/probe" && cat "$HOME/probe"; '
        'echo own > ./own.txt && cat ./own.txt'
    )
    plan = runner.plan(ExecRequest(argv=("sh", "-c", script), env={"PATH": "/usr/bin:/bin"},
                                   profile=profile))
    try:
        proc = await spawn_exec(
            *plan.argv,
            cwd=plan.cwd,
            env=plan.env,
            pass_fds=plan.pass_fds,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        plan.release()
    output, _ = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, output
    assert b"LEAK" not in output and b"SOUL" not in output
    assert b"hi" in output and b"own" in output
    assert (agent_dir / "own.txt").read_text(encoding="utf-8") == "own\n"


# ── 終端與互審驗證指令經真 runner（U1e，設計 §9.2）────────────────────────────────

# 只有讀到 checkout 的 pytest.ini 才收得到 check_ok（沒讀到就是 no tests ran）
PYTEST_INI = "[pytest]\naddopts = -p no:cacheprovider -p no:randomly\npython_functions = check_*\n"
PROJECT_TEST = "def check_ok():\n    assert True\n"


def _checkout_layout(tmp_path: Path) -> SandboxLayout:
    """Touka checkout 的形狀：頂層有 pytest.ini、ToukaProxy/、.git；代理人資料夾在 data 底下。"""
    checkout = tmp_path / "checkout"
    (checkout / "ToukaProxy").mkdir(parents=True)
    (checkout / "ToukaProxy" / "secret.txt").write_text("PROXY\n", encoding="utf-8")
    (checkout / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    if shutil.which("git"):
        run_sync(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    else:
        (checkout / ".git").mkdir()
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    layout = replace(_layout(tmp_path), data_root=checkout / "data")
    for sub in ("personal_drive/Alice", "mcp_servers", "soul"):
        (layout.data_root / sub).mkdir(parents=True, exist_ok=True)
    return layout


async def _isolated_terminal(monkeypatch, layout: SandboxLayout, required: bool) -> Path:
    """終端與驗證指令都改用這個版面的真 monitor／broker；回代理人資料夾。"""
    monitor = await _ready_monitor(layout, required)
    _require_landlock_level(monitor, required)
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    monkeypatch.setattr(agent_command, "isolation_monitor", monitor)
    monkeypatch.setattr(agent_command, "runner_broker", broker)
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    point_zone_roots(monkeypatch, layout.data_root, layout.state_root)  # 蒐證的個人空間（ADR-0293）
    terminal.reset_async_state_for_test()
    return layout.data_root / "personal_drive" / "Alice"


async def _run(command: str, **extra) -> str:
    return await terminal.execute_command(
        command, ALICE, auth_level="Manual", _hitl_preapproved=True, session_id=None,
        _call_id="linux", _ctx={"identity": ALICE}, **extra,
    )


async def test_terminal_command_is_isolated_and_keeps_its_drive(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    secret = layout.data_root / "soul" / "secret.txt"
    secret.write_text("SOUL\n", encoding="utf-8")
    output = await _run(
        f'cat "{secret}" 2>/dev/null && echo LE""AK; echo own > own.txt; '
        'echo "$HOME" > home.txt; echo L1; echo L2'
    )
    assert "[EXIT_CODE: 0]" in output and "LEAK" not in output and "SOUL" not in output
    assert "L1\nL2" in output  # 逐行輸出
    assert (agent_dir / "own.txt").read_text(encoding="utf-8") == "own\n"
    home = (agent_dir / "home.txt").read_text(encoding="utf-8").strip()
    assert Path(home).is_relative_to(layout.state_root / "proxy" / "agents" / "Alice")


async def test_terminal_timeout_kills_the_isolated_group(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    scope = await agent_command.command_scope(ALICE, None, None)
    exit_code, output, _ms = await terminal._execute_core(
        "sleep 60 & echo $! > child.pid; echo L1; wait", "wsl",
        resource_manager.agent_drive(ALICE), None, timeout=3, scope=scope,
    )
    assert exit_code == -1 and "L1" in output and "timed out after 3 seconds" in output
    child = int((agent_dir / "child.pid").read_text(encoding="utf-8"))
    for _ in range(50):
        if _state(child) in {"Z", "X", "gone"}:
            break
        await asyncio.sleep(0.1)
    assert _state(child) in {"Z", "X", "gone"}


async def test_background_command_keeps_its_pid_and_stop_kills_the_group(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    handle = json.loads(
        await _run("sleep 60 & echo $! > child.pid; wait", background=True)
    )
    record = terminal._ASYNC_RESULTS[handle["execution_id"]]
    for _ in range(100):
        if record.get("_proc") is not None and (agent_dir / "child.pid").exists():
            break
        await asyncio.sleep(0.05)
    proc = record["_proc"]
    comm = Path(f"/proc/{proc.pid}/comm").read_text(encoding="utf-8").strip()
    assert comm == "bash"  # 啟動器 exec 之後 PID 不變，跑的就是指令本身
    child = int((agent_dir / "child.pid").read_text(encoding="utf-8"))
    stopped = json.loads(await terminal.stop_command(handle["execution_id"], identity=ALICE))
    assert stopped["ok"] is True
    for _ in range(50):
        if _state(child) in {"Z", "X", "gone"}:
            break
        await asyncio.sleep(0.1)
    assert _state(child) in {"Z", "X", "gone"}


async def test_verification_command_is_isolated(tmp_path, monkeypatch, touka_landlock_requirement):
    layout = _layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    secret = layout.data_root / "soul" / "secret.txt"
    secret.write_text("SOUL\n", encoding="utf-8")

    async def _allow(_cmd: str) -> None:
        return None

    (agent_dir / "probe.py").write_text(
        "import sys\ntry:\n    open(sys.argv[1]).read()\n    print('LE' + 'AK')\n"
        "except PermissionError:\n    print('denied')\n",
        encoding="utf-8",
    )
    bundle = await evidence.collect_evidence(
        artifact_refs=[], verification_cmd=f"{sys.executable} probe.py {secret}", test_log=None,
        artifact_root=resource_manager.agent_drive(ALICE), command_gate=_allow, identity=ALICE,
    )
    (result,) = bundle.cmd_results
    assert result.exit_code == 0 and result.output_snippet == "denied", result


async def test_project_with_its_own_pytest_ini_runs_inside_a_touka_checkout(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _checkout_layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    (agent_dir / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    (agent_dir / "test_ok.py").write_text(PROJECT_TEST, encoding="utf-8")
    output = await _run(f'"{sys.executable}" -m pytest test_ok.py')
    assert "1 passed" in output, output
    assert f"rootdir: {agent_dir}" in output


async def test_project_in_a_touka_checkout_reads_the_repo_pytest_ini(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    """上層 checkout 的 pytest.ini 照樣讀到（check_* 才收得到），rootdir 是代理人資料夾。"""
    layout = _checkout_layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    (agent_dir / "test_ok.py").write_text(PROJECT_TEST, encoding="utf-8")
    checkout = layout.data_root.parent
    secret = checkout / "ToukaProxy" / "secret.txt"
    output = await _run(f'"{sys.executable}" -m pytest test_ok.py; cat "{secret}"')
    assert "1 passed" in output, output
    assert f"rootdir: {agent_dir}" in output and "configfile: ../../../pytest.ini" in output
    assert "PROXY" not in output


async def test_verification_command_reads_the_repo_pytest_ini_in_isolation(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _checkout_layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    (agent_dir / "test_ok.py").write_text(PROJECT_TEST, encoding="utf-8")

    async def _allow(_cmd: str) -> None:
        return None

    bundle = await evidence.collect_evidence(
        artifact_refs=[], verification_cmd=f"{sys.executable} -m pytest -q test_ok.py",
        test_log=None, artifact_root=resource_manager.agent_drive(ALICE), command_gate=_allow,
        identity=ALICE,
    )
    (result,) = bundle.cmd_results
    assert result.exit_code == 0, result
    assert "1 passed" in (result.output_snippet or "")


REPO_SETTINGS_INI = (
    "[pytest]\n"
    'addopts = -p no:cacheprovider -p no:randomly --strict-markers -m "not repo_slow"\n'
    "markers =\n    repo_slow: registered by the Touka checkout pytest.ini\n"
)
MARKED_TESTS = (
    "import pytest\n\n\n@pytest.mark.repo_slow\ndef test_deselected_by_the_repo_addopts():\n"
    "    raise AssertionError('the checkout addopts were not applied')\n\n\n"
    "def test_plain():\n    assert True\n"
)


async def test_repo_pytest_ini_settings_apply_and_checkout_conftest_is_not_loaded(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    """repo 的 addopts 與 markers 生效；上層 checkout 的 conftest.py 不載入（也沒有權限讀）。"""
    layout = _checkout_layout(tmp_path)
    checkout = layout.data_root.parent
    (checkout / "pytest.ini").write_text(REPO_SETTINGS_INI, encoding="utf-8")
    (checkout / "conftest.py").write_text("raise SystemExit('checkout conftest')\n", "utf-8")
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    (agent_dir / "test_marked.py").write_text(MARKED_TESTS, encoding="utf-8")
    output = await _run(f'"{sys.executable}" -m pytest test_marked.py')
    assert "1 passed, 1 deselected" in output, output


async def test_users_own_rootdir_wins_over_the_profile(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    """profile 的選項排在指令列之前：使用者自己給的 --rootdir 最後生效。"""
    layout = _checkout_layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    (agent_dir / "sub").mkdir()
    (agent_dir / "sub" / "test_ok.py").write_text(PROJECT_TEST, encoding="utf-8")
    output = await _run(f'"{sys.executable}" -m pytest --rootdir=sub sub/test_ok.py')
    assert f"rootdir: {agent_dir / 'sub'}" in output, output
    assert "1 passed" in output


SUBPROJECT_PYPROJECT = (
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n'
    'addopts = "-p no:cacheprovider -p no:randomly"\n'
)


async def test_subproject_in_the_drive_keeps_its_own_testpaths(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    """資料夾裡 clone 的子專案有自己的 testpaths：上層沒有授權到的公開設定檔時不改 rootdir。"""
    layout = _layout(tmp_path)
    agent_dir = await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    repo = agent_dir / "repo"
    for sub in ("tests", "scripts"):
        (repo / sub).mkdir(parents=True)
    (repo / "pyproject.toml").write_text(SUBPROJECT_PYPROJECT, encoding="utf-8")
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", "utf-8")
    (repo / "scripts" / "test_broken.py").write_text("import not_a_real_module\n", "utf-8")
    output = await _run(f'cd repo && "{sys.executable}" -m pytest')
    assert "1 passed" in output and "Interrupted" not in output, output
    assert f"rootdir: {repo}" in output


async def test_git_does_not_climb_into_the_touka_checkout(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _checkout_layout(tmp_path)
    await _isolated_terminal(monkeypatch, layout, touka_landlock_requirement)
    checkout = layout.data_root.parent
    head = checkout / ".git" / "HEAD"
    output = await _run(
        f'cat "{head}" 2>/dev/null && echo LE""AK; echo "ceiling=$GIT_CEILING_DIRECTORIES"'
    )
    assert "LEAK" not in output and "ref:" not in output
    assert f"ceiling={layout.data_root / 'personal_drive'}" in output
    if not shutil.which("git"):
        return  # C2-1 的映像沒有 git（C2-2 加入）；真防線是上面讀不到上層 .git
    output = await _run("git rev-parse --show-toplevel")
    assert "not a git repository" in output, output


# ── 專案授權（U1g）：Touka 目錄偵測與真檔案系統的掃描 ─────────────────────────────


def _touka_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """工作區是 Touka checkout；使用者的 myproj 裡有一個指向 E（host_root）的符號連結。"""
    workspace = tmp_path / "workspace"
    subs = ("ToukaProxy/src", "scripts", "data/soul", "DATA/soul", ".git", ".GIT", "myproj/src")
    for sub in subs:
        (workspace / sub).mkdir(parents=True)
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (workspace / ".env").write_text("WORKSPACE_SECRET=1\n", encoding="utf-8")
    host_root = tmp_path / "host_root"
    host_root.mkdir()
    (host_root / ".env").write_text("HOST_SECRET=1\n", encoding="utf-8")
    project = workspace / "myproj"
    (project / "link").symlink_to(host_root, target_is_directory=True)
    return workspace, project


async def test_case_variants_are_distinct_on_linux_and_all_denied(tmp_path):
    """ext4 分大小寫：data／DATA、.git／.GIT 是不同資料夾，比對 casefold 後全部擋。"""
    workspace, project = _touka_workspace(tmp_path)
    layout = replace(_layout(tmp_path), mirror_roots=(workspace,))
    for name in ("data", "DATA", ".git", ".GIT", "scripts"):
        assert await authorize_derived_path(workspace / name, layout) == "protected_location", name
    assert await authorize_derived_path(project, layout) is None


async def test_project_root_swapped_for_a_symlink_is_refused_and_does_not_reach_bob(
    tmp_path, touka_landlock_requirement
):
    """G 只有自己的資料夾；專案根換成真符號連結就不授權，Landlock 也走不過去。"""
    layout = _layout(tmp_path)
    drive = layout.data_root / "personal_drive" / "Alice"
    bob = layout.data_root / "personal_drive" / "Bob"
    (bob / "proj").mkdir(parents=True)
    (bob / "secret.txt").write_text("BOB_SECRET\n", encoding="utf-8")
    (drive / "real").mkdir()
    (drive / "to_bob").symlink_to("../Bob", target_is_directory=True)
    (drive / "to_own").symlink_to("real", target_is_directory=True)
    protected = "protected_location"
    assert await authorize_derived_path(drive / "real", layout, owner="Alice") is None
    assert await authorize_derived_path(bob / "proj", layout, owner="Alice") == protected
    for link in ("to_bob", "to_own"):
        assert await authorize_derived_path(drive / link, layout, owner="Alice") == protected
    swapped = await build_agent_command_profile(
        ALICE, drive, layout=layout, project_root=drive / "to_bob"
    )
    assert swapped.ungranted == (protected,) and swapped.project_root is None
    assert not [rule for rule in swapped.rules if "Bob" in rule.path.parts]
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    own = await build_agent_command_profile(ALICE, drive, layout=layout)
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    runner = (await broker.acquire(ExecutionGrant("landlock"), ProfileSource.fixed(own))).runner
    script = 'cat to_bob/secret.txt 2>/dev/null && echo LE""AK; echo DONE'
    plan = runner.plan(ExecRequest(argv=("sh", "-c", script), env={"PATH": "/usr/bin:/bin"},
                                   profile=own))
    try:
        proc = await spawn_exec(
            *plan.argv, cwd=plan.cwd, env=plan.env, pass_fds=plan.pass_fds,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        plan.release()
    output, _ = await asyncio.wait_for(proc.communicate(), 60)
    assert b"DONE" in output and b"LEAK" not in output and b"BOB_SECRET" not in output, output


async def test_project_in_a_touka_workspace_runs_without_its_secrets(
    tmp_path, touka_landlock_requirement
):
    """可授權的專案：讀寫專案、讀到上層 pytest.ini；讀不到 .env、連結後的 E、受保護的名稱。"""
    workspace, project = _touka_workspace(tmp_path)
    layout = replace(_layout(tmp_path), mirror_roots=(workspace,))
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    denied = await build_agent_command_profile(
        ALICE, agent_dir, layout=layout, project_root=workspace / "DATA"
    )
    assert denied.ungranted == ("protected_location",)
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    profile = await build_agent_command_profile(
        ALICE, agent_dir, layout=layout, project_root=project
    )
    assert profile.project_root == project.resolve() and not profile.ungranted
    script = (
        'cat "$1/pytest.ini" > /dev/null && echo CFG_OK; '
        'cat "$1/.env" 2>/dev/null; echo "env=$?"; '
        'cat "$2/link/.env" 2>/dev/null; echo "link=$?"; '
        'ls "$1/DATA" > /dev/null 2>&1; echo "data=$?"; '
        'echo own > "$2/own.txt" && echo PROJ_OK; '
        '{ echo x >> "$1/pytest.ini"; } 2>/dev/null; echo "cfgw=$?"'
    )
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    runner = (await broker.acquire(ExecutionGrant("landlock"), ProfileSource.fixed(profile))).runner
    argv = ("sh", "-c", script, "sh", str(workspace), str(project))
    plan = runner.plan(ExecRequest(argv=argv, env={"PATH": "/usr/bin:/bin"}, profile=profile))
    try:
        proc = await spawn_exec(
            *plan.argv,
            cwd=plan.cwd,
            env=plan.env,
            pass_fds=plan.pass_fds,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        plan.release()
    output, _ = await asyncio.wait_for(proc.communicate(), 60)
    lines = output.decode().split()
    assert "CFG_OK" in lines and "PROJ_OK" in lines, output
    assert not {"env=0", "link=0", "data=0", "cfgw=0"} & set(lines), output
    assert b"SECRET" not in output
    assert (project / "own.txt").read_text(encoding="utf-8") == "own\n"


def test_unreadable_directory_makes_the_scan_an_error_not_clean(tmp_path):
    """掃描途中讀不到的資料夾無法確認裡面沒有 Touka：回 error（呼叫端當 scan_limit）。"""
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root ignores directory permissions")
    locked = tmp_path / "proj" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert scan_below(tmp_path / "proj", max_depth=3, max_dirs=100, seconds=30.0) == "error"
    finally:
        locked.chmod(0o700)


def test_dir_fd_scan_does_not_follow_a_directory_swapped_for_a_symlink(tmp_path, monkeypatch):
    """列出之後才被換成符號連結（這裡讓列出的結果都說是資料夾）：O_NOFOLLOW 讓它被略過。"""
    assert touka_dirs._FD_WALK  # Linux 走 dir fd 逐層開啟
    touka = tmp_path / "touka"
    (touka / "ToukaProxy").mkdir(parents=True)
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "swapped").symlink_to(touka, target_is_directory=True)
    real_entries = touka_dirs._entries

    def _all_dirs(listing, budget):
        return [(name, True) for name, _ in real_entries(listing, budget)]

    monkeypatch.setattr(touka_dirs, "_entries", _all_dirs)
    assert scan_below(root, max_depth=3, max_dirs=100, seconds=30.0) == "clean"


def test_scan_of_a_thousand_directories_fits_the_background_budget(
    tmp_path, record_testsuite_property
):
    """U1a 的校正情境（3 層、1,111 個資料夾）；Docker 管道把 basetemp 放在 9p 時就是 9p 實測。"""
    root = tmp_path / "scan1000"
    for a, b, c in itertools.product(range(10), repeat=3):
        leaf = root / f"a{a}" / f"b{b}" / f"c{c}"
        leaf.mkdir(parents=True)
        (leaf / "f.txt").write_text("x", encoding="utf-8")
    started = get_monotonic_ms()
    outcome = scan_below(
        root, max_depth=SCAN_MAX_DEPTH, max_dirs=SCAN_MAX_DIRS, seconds=SCAN_SECONDS
    )
    elapsed = (get_monotonic_ms() - started) / 1000
    record_testsuite_property("u1g_scan_dirs", 1111)
    record_testsuite_property("u1g_scan_seconds", round(elapsed, 3))
    record_testsuite_property("u1g_scan_fd_walk", touka_dirs._FD_WALK)
    assert outcome == "clean"


# ── 演化建置與驗證經真 runner（U1h，設計 §2、§3.2、§3.3）─────────────────────────────

EVO_SKILL = "probe_evo"
MIB = 1024 * 1024
# 驗證用的假 MCP 伺服器，不需要任何套件（建置時 dependencies 是空的，容器不連網）。工具回報
# HOME、讀別的代理人與 Touka 檔案的結果（os.open 繞過 wrapper 的 JailedWriter，看的是
# Landlock），以及寫超過 FSIZE 上限的結果（Python 忽略 SIGXFSZ，超過時 write 回 EFBIG）。
EVO_SERVER = EVOLUTION_PROBE_SOURCE


async def _isolated_evolution(monkeypatch, layout: SandboxLayout, required: bool) -> Path:
    """演化改用這個版面的真 monitor／broker；已提案（有 spec.json）；回技能的 managed 資料夾。"""
    monitor = await _ready_monitor(layout, required)
    _require_landlock_level(monitor, required)
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    monkeypatch.setattr(agent_command, "isolation_monitor", monitor)
    monkeypatch.setattr(evolution_command, "isolation_monitor", monitor)
    monkeypatch.setattr(evolution_command, "runner_broker", broker)
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    state = layout.state_root or layout.data_root.parent / "zone_state"
    point_zone_roots(monkeypatch, layout.data_root, state)  # 技能資料夾（ADR-0293 PR 2）
    limits = {"execution_timeout_sec": 60, "max_storage_mb": 50, "max_memory_mb": 1024}
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": limits})
    EvolutionPathGuard.write_tool_file(EVO_SKILL, "spec.json", "{}")
    return Path(EvolutionPathGuard.get_tool_root(EVO_SKILL))


def _verify_data(result: str) -> dict:
    line = next(x for x in result.splitlines() if x.startswith('{"status"'))
    report = json.loads(line)
    assert report["status"] == "success", report
    return report["data"]["structuredContent"]


async def test_evolution_build_and_verify_run_inside_landlock(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    layout = _layout(tmp_path)
    root = await _isolated_evolution(monkeypatch, layout, touka_landlock_requirement)
    secrets = {
        "other_agent": layout.data_root / "personal_drive" / "Bob" / "secret.txt",
        "soul": layout.data_root / "soul" / "secret.txt",
        "other_skill": layout.data_root / "mcp_servers" / "managed" / "other" / "server.py",
        "env_file": layout.env_file,
    }
    for path in secrets.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT-FOR-EVOLUTION\n", encoding="utf-8")
    result = await evolution.evolution_implement_and_verify(
        skill_id=EVO_SKILL,
        code=EVO_SERVER,
        test_tool_name="probe",
        test_arguments={
            "paths": {key: str(path) for key, path in secrets.items()},
            "big": str(root / "big.bin"),
            "size": 2 * MIB,
        },
        identity=ALICE,
        session_id=None,
        _call_id="evo-linux",
        max_storage_mb=1,  # 只能收緊：驗證的 FSIZE 上限 1 MiB
    )
    assert "Virtual environment created" in result, result
    assert (root / ".venv" / "bin" / "python").exists()  # venv 建在 managed/<skill>
    data = _verify_data(result)
    assert data["reads"] == dict.fromkeys(secrets, "EACCES"), data
    state = layout.state_root / "proxy" / "evolution" / EVO_SKILL
    assert Path(data["home"]) == (state / "home").resolve()
    assert data["big_write"] == "EFBIG"  # 驗證保留 FSIZE（啟動器在 exec 前套用）
    assert (root / "big.bin").stat().st_size <= MIB


async def test_evolution_commands_from_a_reused_worker_keep_running(
    tmp_path, monkeypatch, touka_landlock_requirement
):
    """PDEATHSIG 綁在建立子行程的執行緒：worker 等子行程結束才回到執行緒池；回去之後（以及
    worker 結束、換新的之後）下一條指令照常跑完，不會被殺。"""
    layout = _layout(tmp_path)
    await _isolated_evolution(monkeypatch, layout, touka_landlock_requirement)
    grant = await evolution_command.evolution_grant(
        ALICE, None, "pdeathsig", tool="evolution_implement_and_verify", args={}
    )
    loop = asyncio.get_running_loop()
    seen: list[tuple[int, int, str]] = []

    def _run(tag: str, launch: evolution_command.EvolutionLaunch) -> None:
        done = EvolutionPathGuard.run_sandbox_command(
            EVO_SKILL,
            ["/bin/sh", "-c", 'sleep 1; echo "done-$1"', "sh", tag],
            launch=launch,
            capture_output=True,
            text=True,
            timeout=30,
        )
        seen.append((threading.get_ident(), done.returncode, done.stdout.strip()))

    for pool_round in range(2):
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"touka-evo-{pool_round}")
        loop.set_default_executor(pool)
        try:
            for n in range(2):
                launch = await evolution_command.acquire_evolution_launch(grant, EVO_SKILL)
                await asyncio.to_thread(_run, f"{pool_round}{n}", launch)
        finally:
            pool.shutdown(wait=True)  # worker 結束；下一輪換新的 worker
    assert [(code, out) for _, code, out in seen] == [
        (0, "done-00"), (0, "done-01"), (0, "done-10"), (0, "done-11")
    ]
    assert seen[0][0] == seen[1][0]  # 同一個 worker 回到池子後又 spawn 一次


# ── U2b：可信直譯器、狀態 fd、每次啟動讀網路設定（C2-2 設計 §2.9）────────────────────


def test_trusted_interpreter_is_root_owned_in_the_image():
    """映像裡的基底直譯器（不是 venv 那個）整條擁有者鏈都是 root、群組與其他人不可寫.

    原生 Linux（例如 CI runner）沒有信任根：不跳過，只核對解析出的是基底直譯器。
    """
    in_image = is_container_runtime()
    python = Path(launcher.spec.trusted_interpreter(require_root_owned=in_image))
    assert python == Path(sys._base_executable).resolve()
    if in_image:
        assert all(node.stat().st_uid == 0 for node in (python, *python.parents))


def _launch_with_status(spec_bytes: bytes, argv: list[str]) -> tuple[object, bytes]:
    """經 ``--status-fd`` 啟動（argv 裡的 ``@STATUS_FD@`` 換成它的號碼）；回結果與通道內容。"""
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    writer = threading.Thread(target=_write_all, args=(spec_w, spec_bytes))
    writer.start()
    argv = [arg.replace("@STATUS_FD@", str(status_w)) for arg in argv]
    command = [sys.executable, "-I", "-S", str(LAUNCHER), "--spec-fd", str(spec_r)]
    command += ["--status-fd", str(status_w), "--", *argv]
    try:
        result = run_sync(
            command, pass_fds=(spec_r, status_w), capture_output=True, text=True, timeout=120,
            check=False,
        )
    finally:
        os.close(spec_r)
        os.close(status_w)
        writer.join(timeout=30)
    try:
        chunks = []
        while chunk := os.read(status_r, 4096):
            chunks.append(chunk)
    finally:
        os.close(status_r)
    return result, b"".join(chunks)


def test_failure_code_arrives_through_the_status_fd(tmp_path, landlock_abi):
    missing = {"path": str(tmp_path / "missing"), "access": "rwx"}
    result, status = _launch_with_status(_spec(missing), ["true"])
    assert result.returncode == launcher.spec.FAILURE_EXIT
    assert status == b"path_open\n"
    assert launcher.spec.parse_status(status, launcher.spec.FAILURE_CODES) == "path_open"


_FORGE_STATUS = """
import errno, json, os, sys
try:
    os.write(int(sys.argv[1]), b"path_open\\n")
    out = "written"
except OSError as exc:
    out = errno.errorcode[exc.errno]
sys.stderr.write("TOUKA_SANDBOX_ERROR:path_open\\n")
print(json.dumps({"status_fd": out}))
"""


def test_command_cannot_write_the_status_fd(landlock_abi):
    """指令拿不到狀態通道（EBADF）；它自己印的標記也進不了通道，runner 不會當成隔離失敗."""
    result, status = _launch_with_status(_spec(), [*PY, _FORGE_STATUS, "@STATUS_FD@"])
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"status_fd": "EBADF"}
    assert launcher.spec.FAILURE_MARKER in result.stderr
    assert status == b""
    assert launcher.spec.parse_status(status, launcher.spec.FAILURE_CODES) is None


@pytest.mark.parametrize("strict", [True, False], ids=["net-strict", "consented-fallback"])
def test_each_launch_follows_the_real_net_sysctls(landlock_abi, strict):
    """有封鎖埠時每次啟動都讀這個命名空間的設定：不安全就以 ``net_*`` 拒絕；``net_strict: false``
    （人核准、features 不含 tcp 的降級）照常執行。§9.6 加 sysctl 與不加 sysctl 兩種都成立."""
    expected = _unsafe_net_sysctl(landlock_abi) if strict else None
    result, status = _launch_with_status(
        _spec(block_ports=[int(_free_ports(1)[0])], net_strict=strict), [*PY, "print('ran')"]
    )
    if expected is None:
        assert (result.returncode, result.stdout.strip(), status) == (0, "ran", b"")
    else:
        assert result.returncode == launcher.spec.FAILURE_EXIT and "ran" not in result.stdout
        assert status == launcher.spec.encode_status(expected)


# ── U2b：relay 以 ``-I -S`` 依路徑執行，經真的 unix socket（執行器由測試扮演）───────────────

RELAY = Path(relay.__file__).resolve()
FR = relay.framing
BOOT_ID = "0123456789abcdef" * 2
RELAY_START = {"argv": ["true"], "spec": {"version": 1, "block_ports": [8000, 11434]}}


def _serve_once(listener: socket.socket, boot_id: str, seen: list) -> None:
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(10)
        hello = FR.decode_json(FR.read_frame(conn.recv))
        seen.append(hello)
        conn.sendall(FR.encode_json(FR.READY, {"v": FR.PROTOCOL_VERSION, "boot_id": boot_id}))
        frame = FR.read_frame(conn.recv)
        if frame is None:
            seen.append("no-start")
            return
        seen.append(FR.decode_json(frame))
        conn.sendall(FR.encode_json(FR.STARTED, {"exec_id": "e" * 32}))
        FR.read_frame(conn.recv)  # STDIN_EOF
        conn.sendall(FR.encode(FR.OUT, b"remote out\n") + FR.encode(FR.ERR, b"remote err\n"))
        conn.sendall(FR.encode_json(
            FR.EXIT, {"code": 7, "signal": None, "timed_out": False, "launcher_error": None}
        ))


@pytest.mark.parametrize("restarted", [False, True], ids=["same-boot", "restarted"])
def test_relay_runs_by_path_over_a_real_unix_socket(tmp_path, restarted):
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    token = "ab" * 32
    (ipc / FR.TOKEN_NAME).write_text(token, encoding="ascii")
    seen: list = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(ipc / FR.SOCKET_NAME))
        listener.listen(1)
        server = threading.Thread(
            target=_serve_once, args=(listener, "f" * 32 if restarted else BOOT_ID, seen)
        )
        server.start()
        request_r, request_w = os.pipe()
        status_r, status_w = os.pipe()
        request = {"parent_pid": os.getpid(), "start": RELAY_START}
        _write_all(request_w, json.dumps(request).encode())  # 請求很小，寫完就關寫端
        argv = [sys.executable, "-I", "-S", str(RELAY), "--ipc-dir", str(ipc)]
        argv += ["--request-fd", str(request_r), "--status-fd", str(status_w), "--boot-id", BOOT_ID]
        try:
            result = run_sync(argv, pass_fds=(request_r, status_w), capture_output=True,
                              timeout=60, check=False)
        finally:
            os.close(request_r)
            os.close(status_w)
            server.join(timeout=30)
        status = os.read(status_r, 4096)
        os.close(status_r)
    assert seen[0] == {"v": 1, "token": token, "block_ports": [8000, 11434], "mode": "exec"}
    if restarted:
        assert (result.returncode, status, seen[1:]) == (125, b"executor_restarted\n", ["no-start"])
    else:
        assert (result.returncode, status) == (7, b"")
        assert (result.stdout, result.stderr) == (b"remote out\n", b"remote err\n")
        assert seen[1] == RELAY_START


# ── U2b：擁有者位元自癒的 kernel 函式（O_PATH fd 經 /proc/self/fd 做 chmod）─────────────

OR = owner_repair.RepairOutcome


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_owner_repair_chmods_an_o_path_fd_through_proc(tmp_path):
    """一條鏈上被鎖的資料夾與目標都修回來（只補擁有者位元）；鏈外的兄弟資料夾不動（不遞迴）."""
    root = tmp_path / "drive"
    sub = root / "agent" / "sub"
    sub.mkdir(parents=True)
    target, sibling = sub / "notes.txt", root / "agent" / "other"
    target.write_text("hi", encoding="utf-8")
    sibling.mkdir()
    for path in (target, sub, sibling):
        path.chmod(0)
    try:
        outcome = owner_repair.repair_chain(str(root), str(target))
        assert outcome == OR(changed=2) and outcome.repaired
        assert (_mode(sub), _mode(target), _mode(sibling)) == (0o700, 0o600, 0)
        assert target.read_text(encoding="utf-8") == "hi"
        assert owner_repair.repair_chain(str(root), str(target)) == OR()  # 位元已對：沒有改
    finally:
        sibling.chmod(0o700)


def test_owner_repair_keeps_group_bits_and_drops_setid(tmp_path):
    root = tmp_path / "drive"
    folder = root / "shared"
    folder.mkdir(parents=True)
    tool = folder / "tool.sh"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o4050)
    assert _mode(tool) == 0o4050
    folder.chmod(0o2050)  # 之後擁有者進不去這個資料夾
    assert _mode(folder) == 0o2050
    assert owner_repair.repair_chain(str(root), str(tool)) == OR(changed=2)
    assert (_mode(folder), _mode(tool)) == (0o750, 0o750)  # 原本有 x，所以也補 u+x


def test_owner_repair_does_not_follow_a_symlink_out_of_the_subtree(tmp_path):
    assert owner_repair._OPEN_FLAGS & os.O_PATH and owner_repair._OPEN_FLAGS & os.O_NOFOLLOW
    root, outside = tmp_path / "drive", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "x").write_text("secret", encoding="utf-8")
    outside.chmod(0o500)
    (root / "link").symlink_to(outside, target_is_directory=True)
    try:
        outcome = owner_repair.repair_chain(str(root), str(root / "link" / "x"))
        assert outcome == OR(stopped=owner_repair.SYMLINK)
        assert _mode(outside) == 0o500
    finally:
        outside.chmod(0o700)


@pytest.mark.parametrize("tail", [".notes.txt.tmp", "new-folder/deeper"])
def test_owner_repair_fixes_the_locked_folder_of_a_path_that_does_not_exist_yet(tmp_path, tail):
    """在被鎖的資料夾裡建新檔或新資料夾：修到最後存在的一段，之後就建得出來."""
    root = tmp_path / "drive"
    locked = root / "agent"
    locked.mkdir(parents=True)
    locked.chmod(0)
    outcome = owner_repair.repair_chain(str(root), str(locked / tail))
    assert outcome == OR(changed=1, missing_tail=True)
    created = locked / tail
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("ok", encoding="utf-8")


def test_owner_repair_leaves_hard_linked_files_alone(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    first = root / "a.txt"
    first.write_text("x", encoding="utf-8")
    os.link(first, root / "b.txt")
    first.chmod(0)
    assert owner_repair.repair_chain(str(root), str(first)) == OR(stopped=owner_repair.HARDLINK)
    assert _mode(first) == 0


def test_owner_repair_never_touches_inodes_it_does_not_own():
    assert os.geteuid() != 0, "隔離測試不以 root 執行"
    outcome = owner_repair.repair_chain("/", "/usr/bin")
    assert outcome == OR(stopped=owner_repair.NOT_OWNER)


# ── U2n：proxy 端權限自癒（真核心；掛鉤由測試自己安裝在 tmp 版面上）──────────────────


class _CountingHook:
    """包住真的服務，數掛鉤被呼叫幾次（豁免方法、政策拒絕要是 0）。"""

    def __init__(self, inner: PermissionRepairService) -> None:
        self.inner, self.calls = inner, 0

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        self.calls += 1
        return self.inner.repair(method, paths, retry=retry)

    def note_retry(self, method: str, succeeded: bool) -> None:
        self.inner.note_retry(method, succeeded)


class _Repairing:
    """tmp 版面＋真的自癒服務；``lock`` 記下改過權限的路徑，結束時改回來好讓 tmp 清得掉。"""

    def __init__(self, layout: SandboxLayout) -> None:
        self.layout = layout
        self.drive = layout.data_root / "personal_drive" / "Alice"
        self.backups = layout.env_file.parent / "backups"
        self.service = PermissionRepairService(lambda: layout)
        self.hook = _CountingHook(self.service)
        self._locked: list[Path] = []

    def lock(self, path: Path, mode: int = 0) -> Path:
        self._locked.append(path)
        path.chmod(mode)
        return path

    def counts(self) -> dict:
        return self.service.snapshot()

    def restore(self) -> None:
        for path in reversed(self._locked):
            with contextlib.suppress(OSError):
                path.chmod(0o700 if path.is_dir() else 0o600)


def _drive(*parts: str) -> ZonePath:
    """``personal_drive`` 底下的區內路徑：個人空間只經 RM 的區內方法讀寫（ADR-0293）."""
    return ZonePath(Anchor.PERSONAL_DRIVE, parts)


@pytest.fixture
def repairing(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root 不受權限位元限制")
    layout = _layout(tmp_path)
    for folder in (layout.data_root / "vault" / "core_memory_backups", tmp_path / "host_root"):
        folder.mkdir(parents=True, exist_ok=True)
    # ResourceManager 的受保護判斷（soul、legacy）也指向這個版面的 data 根
    point_zone_roots(monkeypatch, layout.data_root, layout.state_root)
    env = _Repairing(layout)
    env.backups.mkdir()
    set_permission_repair(env.hook)
    yield env
    env.restore()


def test_repair_restores_a_locked_agent_folder_with_owner_bits_only(repairing):
    notes = repairing.drive / "notes.txt"
    notes.write_text("hi", encoding="utf-8")
    repairing.lock(repairing.drive, 0o005)
    assert resource_manager.zone_read_text(_drive("Alice", "notes.txt")) == "hi"
    assert _mode(repairing.drive) == 0o705  # 只多了擁有者位元，其他人的 r-x 不動
    counts = repairing.counts()
    assert (counts["repaired"], counts["retried"]) == (1, 1)


def test_repair_of_a_locked_file_does_not_retry_an_append(repairing):
    log = repairing.drive / "log.txt"
    log.write_text("a\n", encoding="utf-8")
    repairing.lock(log)
    with pytest.raises(PermissionError):
        resource_manager.zone_append_text(_drive("Alice", "log.txt"), "b\n")
    assert _mode(log) == 0o600
    assert log.read_text(encoding="utf-8") == "a\n"  # 修了，但這一次照實失敗、沒寫進去
    resource_manager.zone_append_text(_drive("Alice", "log.txt"), "b\n")
    assert log.read_text(encoding="utf-8") == "a\nb\n"


def test_repair_heals_a_locked_folder_before_a_new_file_is_written(repairing):
    folder = repairing.drive / "sub"
    folder.mkdir()
    repairing.lock(folder, 0o500)
    new = _drive("Alice", "sub", "new.txt")
    with pytest.raises(PermissionError):
        resource_manager.zone_write_text(new, "x")  # retry=False：這次照實失敗
    assert _mode(folder) == 0o700
    resource_manager.zone_write_text(new, "x")
    assert (folder / "new.txt").read_text(encoding="utf-8") == "x"


def test_repair_heals_a_locked_parent_so_make_dir_succeeds(repairing):
    locked = repairing.drive / "locked"
    locked.mkdir()
    repairing.lock(locked)
    resource_manager.zone_make_dir(_drive("Alice", "locked", "a", "b"))
    assert (locked / "a" / "b").is_dir() and _mode(locked) == 0o700


def test_path_exists_raises_when_the_parent_is_locked(repairing):
    """Python 語意釘子（LOW-12）：上層 000 時 ``Path.exists`` 丟 PermissionError，不回 False."""
    locked = repairing.drive / "locked"
    locked.mkdir()
    repairing.lock(locked)
    with pytest.raises(PermissionError):
        (locked / "x").exists()


def test_locked_soul_folder_is_not_repaired(repairing):
    soul = repairing.layout.data_root / "soul" / "Alice"
    soul.mkdir(parents=True)
    (soul / "core.json").write_text("{}", encoding="utf-8")
    repairing.lock(soul)
    with pytest.raises(PermissionError) as caught:
        resource_manager.read_file_text(soul / "core.json")
    assert caught.value.errno == errno.EACCES
    assert _mode(soul) == 0
    counts = repairing.counts()
    assert (counts["refused_outside"], counts["repaired"]) == (1, 0)


def test_soul_protection_refusal_keeps_errno_none_and_never_reaches_the_hook(repairing):
    soul = repairing.layout.data_root / "soul" / "Alice"
    soul.mkdir(parents=True)
    core = soul / "core.json"
    core.write_text("{}", encoding="utf-8")
    repairing.lock(core, 0o400)
    for call in (
        lambda: resource_manager.write_file_text(core, "x"),
        lambda: resource_manager.append_file_text(core, "x"),
        lambda: resource_manager.write_file_binary(core, b"x"),
    ):
        with pytest.raises(PermissionError) as caught:
            call()
        assert caught.value.errno is None
    assert repairing.hook.calls == 0
    assert _mode(core) == 0o400 and core.read_text(encoding="utf-8") == "{}"


def test_a_link_to_another_agent_is_refused_and_does_not_spend_its_budget(repairing):
    bob = repairing.layout.data_root / "personal_drive" / "Bob"
    bob.mkdir()
    (bob / "x").write_text("bob", encoding="utf-8")
    (repairing.drive / "link").symlink_to("../Bob", target_is_directory=True)
    repairing.lock(bob)
    with raises_zone(ZoneLinkRefusedError):  # 連結先被拒，自癒根本沒被叫到
        resource_manager.zone_read_text(_drive("Alice", "link", "x"))
    assert _mode(bob) == 0 and repairing.hook.calls == 0
    assert resource_manager.zone_read_text(_drive("Bob", "x")) == "bob"  # B 自己的路徑仍修得回來
    counts = repairing.counts()
    assert (counts["refused_outside"], counts["repaired"], counts["throttled"]) == (0, 1, 0)


def test_a_symlink_in_the_middle_of_the_path_is_not_followed(repairing):
    real = repairing.drive / "real"
    real.mkdir()
    (real / "x").write_text("x", encoding="utf-8")
    (repairing.drive / "link").symlink_to("real", target_is_directory=True)
    repairing.lock(real)
    with raises_zone(ZoneLinkRefusedError):  # 停在連結、自癒沒被叫到
        resource_manager.zone_read_text(_drive("Alice", "link", "x"))
    assert _mode(real) == 0 and repairing.hook.calls == 0  # 連結指向的資料夾不動


def test_a_hard_linked_file_is_not_repaired(repairing):
    first = repairing.drive / "a.txt"
    first.write_text("x", encoding="utf-8")
    os.link(first, repairing.drive / "b.txt")
    repairing.lock(first)
    with pytest.raises(PermissionError):
        resource_manager.zone_read_text(_drive("Alice", "a.txt"))
    assert _mode(first) == 0 and repairing.counts()["nothing_changed"] == 1


def test_check_roots_repairs_a_locked_personal_drive_root(repairing):
    root = repairing.layout.data_root / "personal_drive"
    repairing.lock(root)
    assert repairing.service.check_roots() == 1
    assert _mode(root) == 0o700


# ── U2n 附錄：系統快照與記憶庫升級備份（ADR-0270）在白名單外（不修、權限不變、例外照丟）──


def test_a_read_only_snapshot_zip_is_not_repaired(repairing):
    snapshot = repairing.backups / "Touka_Backup_x.zip"
    snapshot.write_bytes(b"zip")
    repairing.lock(snapshot)
    with pytest.raises(PermissionError) as caught:
        resource_manager.read_file_binary(snapshot)
    assert (caught.value.errno, caught.value.filename) == (errno.EACCES, str(snapshot))
    assert _mode(snapshot) == 0
    counts = repairing.counts()
    assert (counts["refused_outside"], counts["repaired"]) == (1, 0)


def test_a_locked_snapshot_folder_keeps_its_leftover(repairing):
    leftover = repairing.backups / ".snapshot-1"
    leftover.mkdir()
    repairing.lock(repairing.backups, 0o500)
    with pytest.raises(PermissionError):
        resource_manager.remove_dir(leftover)
    assert _mode(repairing.backups) == 0o500 and leftover.is_dir()


def test_an_upgrade_backup_is_not_repaired(repairing):
    backup = repairing.layout.data_root / "vault" / "core_memory_backups" / "b.db"
    backup.write_bytes(b"db")
    repairing.lock(backup)
    with pytest.raises(PermissionError):
        resource_manager.read_file_binary(backup)
    assert _mode(backup) == 0 and repairing.counts()["repaired"] == 0


def test_exempt_remove_file_never_reaches_the_hook(repairing):
    snapshot = repairing.backups / "Touka_Backup_y.zip"
    snapshot.write_bytes(b"zip")
    repairing.lock(repairing.backups, 0o500)
    with pytest.raises(PermissionError):
        resource_manager.remove_file(snapshot)
    assert repairing.hook.calls == 0
    assert snapshot.exists() and _mode(repairing.backups) == 0o500


# ── proxy 端判定（U2d）：清 tmp 不跟連結、9p 上的掃描快取 ────────────────────────────────


async def test_proxy_tmp_cleanup_does_not_follow_symlinks(tmp_path):
    """開機清 ST/proxy 的 tmp：tmp 本身是連結只刪連結；tmp 裡的連結只刪連結、不刪目標.

    kind 資料夾（``evolution``）是連結時記 log、略過（區內方法拒絕，ADR-0293 PR 2 決定 21）。
    """
    layout = _layout(tmp_path)
    proxy = layout.state_root / "proxy"
    outside, elsewhere = tmp_path / "outside", tmp_path / "elsewhere"
    for folder in (outside, elsewhere / "x" / "tmp"):
        folder.mkdir(parents=True)
        (folder / "keep.txt").write_text("keep", encoding="utf-8")
    alice = proxy / "agents" / "Alice"
    alice.mkdir(parents=True)
    (alice / "tmp").symlink_to(outside, target_is_directory=True)
    bob_tmp = proxy / "agents" / "Bob" / "tmp"
    bob_tmp.mkdir(parents=True)
    (bob_tmp / "link").symlink_to(outside, target_is_directory=True)
    (proxy / "evolution").symlink_to(elsewhere, target_is_directory=True)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
    )
    with expect_zone_refusal(ZoneLinkRefusedError, display_prefix="zone://sandbox_state/proxy/evolution"):
        await monitor.ensure_ready()
    for _ in range(50):
        if not any(p.name.startswith("tmp.trash-") for p in (bob_tmp.parent).iterdir()):
            break
        await asyncio.sleep(0.02)
    assert not (alice / "tmp").is_symlink() and not (alice / "tmp").exists()
    assert (outside / "keep.txt").is_file()
    assert not bob_tmp.exists() and sorted(p.name for p in bob_tmp.parent.iterdir()) == []
    assert (elsewhere / "x" / "tmp" / "keep.txt").is_file()


def _atomic_replace(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def test_top_level_directory_inodes_are_stable_across_listings(tmp_path):
    """9p（U2j 重跑時 basetemp 在 9p）：第一層資料夾的 inode 每次列出都一樣，快取才比得起來."""
    root = tmp_path / "proj"
    for name in ("src", "docs", "tests"):
        (root / name).mkdir(parents=True)
    first = touka_dirs.top_level_dirs(root)
    _atomic_replace(root / "README.md", "v1")
    assert set(first) == {"src", "docs", "tests"}
    assert touka_dirs.top_level_dirs(root) == first


async def test_scan_cache_keeps_its_verdict_after_an_atomic_file_replace_in_the_root(tmp_path):
    """根目錄裡原子置換一個檔（mtime 變了、第一層資料夾沒變）：沿用快取，不重掃（②N4）."""
    layout = _layout(tmp_path)
    root = tmp_path / "work" / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    scans: list[Path] = []

    def _counting(path, **kwargs):
        scans.append(path)
        return scan_below(path, **kwargs)

    scanner = ProjectScanner(scan=_counting, foreground_wait=30.0)
    assert await scanner.verdict(root, layout) is None
    # 根目錄的 mtime 會變（9p 的粒度可能較粗，沒變也一樣要沿用快取）
    _atomic_replace(root / "pyproject.toml", "[project]\nname = 'x'\n")
    assert await scanner.verdict(root, layout) is None
    (root / "added").mkdir()  # 新增第一層資料夾：只補掃新增的那個
    assert await scanner.verdict(root, layout) is None
    assert scans == [root, root]


# ── U2c：執行器伺服器（in-process：真 unix socket、真 relay、真啟動器；C2-2 設計 §2.2～§2.4）──────
# 伺服器在測試的事件迴圈裡跑；它把 pytest 設成 subreaper，收尾時取消。relay 是測試的子行程，
# 掃描孤兒時用 ``exclude`` 跳過（正式容器裡伺服器只會有 relay 以外的孫行程）。

EXEC_KERNEL = Path(launcher.__file__).resolve().parent
_PR_SET_CHILD_SUBREAPER = 36


def _executor_dirs(tmp_path: Path) -> SimpleNamespace:
    base = tmp_path / "ex"
    data = base / "data"
    dirs = SimpleNamespace(
        pd=data / "personal_drive", mcp=data / "mcp_servers" / "managed", skills=base / "skills",
        state=base / "state", ipc=base / "ipc", outside=base / "outside",
    )
    for path in (dirs.pd / "Alice", dirs.pd / "Bob", dirs.mcp, dirs.skills):
        path.mkdir(parents=True)
    for path in (dirs.state, dirs.ipc, dirs.outside):
        path.mkdir(mode=0o700)
    (dirs.pd / "Bob" / "secret.txt").write_text("bob\n", encoding="utf-8")
    (dirs.outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    return dirs


def _executor_argv(dirs: SimpleNamespace, *extra: str) -> list[str]:
    argv = ["--ipc-dir", str(dirs.ipc), "--kernel-dir", str(EXEC_KERNEL)]
    argv += ["--state-dir", str(dirs.state), "--root", f"rw:{dirs.pd}", "--root", f"rw:{dirs.mcp}"]
    return [*argv, "--root", f"ro:{dirs.skills}", *extra]


@pytest.fixture
async def executor(tmp_path, landlock_abi):
    """``start(*extra_argv)`` 起一個伺服器（可以起多個）；收尾時停掉並取消 subreaper。"""
    dirs = _executor_dirs(tmp_path)
    children: set[int] = set()
    servers: list = []

    async def start(*extra: str):
        config = exs.parse_cli(_executor_argv(dirs, *extra))
        hooks = exs.Hooks(processes=exs.LinuxProcesses(exclude=children.__contains__))
        server = exs.ExecutorServer(
            config, exs.load_kernel(EXEC_KERNEL), hooks,
            require_root_interpreter=is_container_runtime(),
        )
        servers.append(server)
        return server, await server.start()

    yield SimpleNamespace(dirs=dirs, children=children, start=start, abi=landlock_abi)
    for server in servers:
        await server.stop()
    ll.LinuxLandlock().prctl(_PR_SET_CHILD_SUBREAPER, 0)


def _exec_start(server, owner: str, argv: list[str], *, max_seconds: int = 60, ports=()) -> dict:
    home, tmp = server.layout.state_dirs("agents", owner)
    target = server.layout.target("agents", owner)[1]
    rules = [{"path": p, "access": "rox", "optional": True} for p in exe.system_exec_paths()]
    rules += [{"path": p, "access": "ro", "optional": True} for p in exe.SYSTEM_READ_PATHS]
    rules += [{"path": "/dev/null", "access": "dev_rw", "optional": True}]
    rules += [{"path": server.config.ro[0], "access": "rox", "optional": True}]
    rules += [{"path": p, "access": "rwx"} for p in (target, home, tmp)]
    net = server.ready(frozenset())["net"]
    return {
        "argv": argv, "cwd": target,
        "env": {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": tmp},
        "spec": {
            "version": launcher.spec.SPEC_VERSION, "profile": "agent_command", "rules": rules,
            "block_ports": sorted({*launcher.spec.DEFAULT_BLOCK_PORTS, *ports}), "pin": True,
            "net_strict": net is None,  # 同 proxy：網路設定不安全時是人核准的降級
        },
        "kind": "agents", "owner": owner, "io": "merged", "max_seconds": max_seconds,
    }


async def _spawn_relay(executor, server, start: dict):
    request_r, request_w = os.pipe()
    status_r, status_w = os.pipe()
    _write_all(request_w, json.dumps({"parent_pid": os.getpid(), "start": start}).encode())
    argv = [sys.executable, "-I", "-S", str(RELAY), "--ipc-dir", str(executor.dirs.ipc)]
    argv += ["--request-fd", str(request_r), "--status-fd", str(status_w)]
    proc = await spawn_exec(
        *argv, "--boot-id", server.boot_id, pass_fds=(request_r, status_w),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    executor.children.add(proc.pid)  # 同一個事件迴圈回合：掃描不可能在這之前開始
    os.close(request_r)
    os.close(status_w)
    return proc, status_r


async def _relay(executor, server, start: dict, timeout: float = 60) -> tuple[int, str, bytes]:
    proc, status_r = await _spawn_relay(executor, server, start)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:  # 先收掉 relay 才讀狀態 pipe，否則讀取會卡住整個事件迴圈
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        os.close(status_r)
        raise
    status = os.read(status_r, 256)  # relay 已結束：寫端都關了，不會卡
    os.close(status_r)
    return proc.returncode, out.decode("utf-8", "replace"), status


def _last_json(text: str) -> dict:
    return json.loads([line for line in text.splitlines() if line.startswith("{")][-1])


async def _gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text("ascii").rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        await asyncio.sleep(0.05)
    return False


_EXEC_PROBE = """
import errno, json, os, sys
env = os.environ
out = {"cwd": os.getcwd(), "home": env.get("HOME"), "exec_id": env.get("TOUKA_EXEC_ID")}
for name, path in (("token", sys.argv[1]), ("bob", sys.argv[2]), ("server_environ", sys.argv[3])):
    try:
        open(path, "rb").read()
        out[name] = "ok"
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, str(exc.errno))
print(json.dumps(out))
"""


async def test_executor_runs_an_isolated_command_through_the_relay(executor):
    """cwd 是自己的資料夾、HOME 在 state、exec id 由伺服器給；讀不到權杖、Bob 的檔與伺服器的環境."""
    server, started = await executor.start()
    assert started, server.problem
    assert server.enforced
    dirs = executor.dirs
    paths = [str(dirs.ipc / FR.TOKEN_NAME), str(dirs.pd / "Bob" / "secret.txt")]
    argv = [*PY, _EXEC_PROBE, *paths, f"/proc/{os.getpid()}/environ"]
    code, out, status = await _relay(executor, server, _exec_start(server, "Alice", argv))
    assert (code, status) == (0, b""), out
    report = _last_json(out)
    assert report["cwd"] == str(dirs.pd / "Alice")
    assert report["home"] == server.layout.state_dirs("agents", "Alice")[0]
    assert re.fullmatch(r"[0-9a-f]{32}", report["exec_id"])
    assert (report["token"], report["bob"], report["server_environ"]) == ("EACCES",) * 3
    for directory in server.layout.state_dirs("agents", "Alice"):
        assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    for name in (FR.SOCKET_NAME, FR.TOKEN_NAME):  # socket 與權杖都是 0600
        assert stat.S_IMODE(os.lstat(dirs.ipc / name).st_mode) == 0o600


_SOCKET_PROBE = """
import json, socket, sys
sock = socket.socket(socket.AF_UNIX)
sock.settimeout(5)
try:
    sock.connect(sys.argv[1])
    sock.sendall(bytes.fromhex(sys.argv[2]))
    data = sock.recv(4096)
    out = data[5:].decode("utf-8", "replace") if data else "closed"
except OSError as exc:
    out = "error:" + type(exc).__name__
print(json.dumps({"reply": out}))
"""


@pytest.mark.parametrize("foreign", [False, True], ids=["token", "foreign-peer"])
async def test_isolated_command_cannot_use_the_control_socket(executor, foreign):
    """隔離內連 ctl.sock：猜不到權杖就是 auth 錯誤；要求外部 peer 時連線直接被關（pid 不是 0）."""
    server, started = await executor.start(*(["--require-foreign-peer"] if foreign else []))
    assert started, server.problem
    hello = FR.encode_json(FR.HELLO, {
        "v": 1, "token": "ab" * 32, "mode": "health",
        "block_ports": sorted(launcher.spec.DEFAULT_BLOCK_PORTS),
    })
    argv = [*PY, _SOCKET_PROBE, str(executor.dirs.ipc / FR.SOCKET_NAME), hello.hex()]
    if foreign:  # relay 本身也在同一個 pid namespace：一樣被擋
        code, _out, status = await _relay(executor, server, _exec_start(server, "Alice", argv))
        assert (code, status) == (125, b"executor_unreachable\n")
        assert server.refusals["peer"] == 1
        return
    code, out, _status = await _relay(executor, server, _exec_start(server, "Alice", argv))
    reply = _last_json(out)["reply"]
    assert code == 0 and "READY" not in reply and "boot_id" not in reply
    assert reply in {'{"code":"auth"}', "closed"} or reply.startswith("error:"), reply


_DAEMON = """
import os, sys, time
keep_env = sys.argv[1] == "keep"
pid = os.fork()
if pid == 0:
    os.setsid()
    devnull = os.open("/dev/null", os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    env = dict(os.environ) if keep_env else {}
    sleeper = [sys.executable, "-I", "-S", "-c", "import time; time.sleep(300)"]
    os.execve(sys.executable, sleeper, env)
time.sleep(1)  # 等 daemon exec 完成（cleared 時 environ 已經沒有 exec id）
print(pid, flush=True)
"""


@pytest.mark.parametrize("env", ["keep", "cleared"])
async def test_setsid_daemon_is_killed_after_the_connection_ends(executor, env):
    """setsid 出去、不佔 stdout 的 daemon 被伺服器收養；指令結束、連線結束時一併殺掉並回收."""
    server, started = await executor.start()
    assert started, server.problem
    code, out, _status = await _relay(
        executor, server, _exec_start(server, "Alice", [*PY, _DAEMON, env])
    )
    assert code == 0, out
    daemon = int(out.split()[-1])
    assert await _gone(daemon, 5), f"daemon {daemon} still alive"


async def test_max_seconds_kills_the_remote_group(executor):
    server, started = await executor.start()
    assert started, server.problem
    began = time.monotonic()
    start = _exec_start(server, "Alice", [*PY, "import time; time.sleep(60)"], max_seconds=1)
    code, _out, status = await _relay(executor, server, start)
    assert (code, status) == (128 + 9, b"")  # 遠端被 SIGKILL
    assert time.monotonic() - began < 15


async def test_disconnect_kills_the_remote_group(executor):
    """斷線＝STOP：relay 被殺（不送 STOP）之後，遠端的整個行程群組在 2 秒內被收掉."""
    server, started = await executor.start()
    assert started, server.problem
    script = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
    start = _exec_start(server, "Alice", [*PY, script])
    proc, status_r = await _spawn_relay(executor, server, start)
    try:
        remote = int((await asyncio.wait_for(proc.stdout.readline(), 30)).decode())
        proc.kill()
        await proc.wait()
        assert await _gone(remote, 2), "remote command survived the disconnect"
    finally:
        os.close(status_r)


async def test_shared_ruleset_blocks_connect_and_bind(executor):
    if executor.abi < ll.MIN_NET_ABI:
        pytest.skip("[skip:abi4] TCP 規則要 ABI 4")
    server, started = await executor.start()
    assert started, server.problem
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    blocked = listener.getsockname()[1]
    allowed = int(_free_ports(1)[0])
    probe = "\n".join([_CONNECT.replace("fd = int(sys.argv[1])", "fd = 999"), _BIND])
    argv = [*PY, probe, "0", str(blocked)]
    try:
        start = _exec_start(server, "Alice", argv, ports=(blocked,))
        code, out, _ = await _relay(executor, server, start)
    finally:
        listener.close()
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert code == 0 and lines[0][str(blocked)] == "EACCES" and lines[1][str(blocked)] == "EACCES"
    assert tuple(sorted({*launcher.spec.DEFAULT_BLOCK_PORTS, blocked})) in server._rulesets._fds
    bind_ok = _exec_start(server, "Alice", [*PY, _BIND, str(allowed)], ports=(blocked,))
    assert _last_json((await _relay(executor, server, bind_ok))[1]) == {str(allowed): "ok"}


async def test_ipc_permissions_are_restored_within_five_seconds(executor):
    server, started = await executor.start()
    assert started, server.problem
    ipc = executor.dirs.ipc
    (ipc / FR.TOKEN_NAME).chmod(0)
    ipc.chmod(0)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and stat.S_IMODE(os.lstat(ipc).st_mode) & 0o700 != 0o700:
        await asyncio.sleep(0.2)
    assert stat.S_IMODE(os.lstat(ipc).st_mode) & 0o700 == 0o700
    assert stat.S_IMODE(os.lstat(ipc / FR.TOKEN_NAME).st_mode) & 0o600 == 0o600
    assert server.repairs["repaired"] >= 2
    code, _out, status = await _relay(executor, server, _exec_start(server, "Alice", ["true"]))
    assert (code, status) == (0, b"")


async def test_private_folders_opened_to_everyone_are_closed_within_five_seconds(executor):
    """代理人把 state、ipc 改成 777、權杖與 socket 改成 666：下一輪維護收回成 0700／0600，
    照常服務（F1）."""
    server, started = await executor.start()
    assert started, server.problem
    dirs = executor.dirs
    private = (dirs.state, dirs.ipc, dirs.ipc / FR.TOKEN_NAME, dirs.ipc / FR.SOCKET_NAME)
    for path in private:
        path.chmod(0o777 if path.is_dir() else 0o666)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(_mode(path) & 0o077 for path in private):
        await asyncio.sleep(0.2)
    assert [_mode(path) for path in private] == [0o700, 0o700, 0o600, 0o600]
    code, _out, status = await _relay(executor, server, _exec_start(server, "Alice", ["true"]))
    assert (code, status) == (0, b"")


@pytest.mark.parametrize("mode", [0o777, 0o000], ids=["777", "000"])
@pytest.mark.parametrize("name", ["state", "ipc"])
async def test_restart_closes_a_private_folder_opened_to_everyone_and_serves(executor, name, mode):
    """重啟時 state 或 ipc 是 777 或 000：先改回 0700 再檢查，不判 ``private_dir``（777）也不判
    ``ipc_lock``（000：連自己都進不去），舊 socket 換新."""
    folder = getattr(executor.dirs, name)
    for stale in (FR.SOCKET_NAME, "alive"):  # 先寫殘留檔：ipc 改成 000 之後自己也寫不進去
        (executor.dirs.ipc / stale).write_text("stale", encoding="utf-8")
    folder.chmod(mode)
    server, started = await executor.start()
    assert started, server.problem
    assert _mode(folder) == 0o700 and (executor.dirs.ipc / FR.SOCKET_NAME).is_socket()
    code, _out, status = await _relay(executor, server, _exec_start(server, "Alice", ["true"]))
    assert (code, status) == (0, b"")


def _link_to_an_open_folder(dirs: SimpleNamespace, name: str) -> Path:
    link = getattr(dirs, name)
    real = link.with_name(f"real_{name}")
    link.rename(real)
    real.chmod(0o777)
    link.symlink_to(real, target_is_directory=True)
    return real


def _someone_elses_folder(dirs: SimpleNamespace, name: str) -> Path:
    setattr(dirs, name, Path("/usr"))  # root 擁有（例如舊 volume 是 0:0 755）
    return Path("/usr")


@pytest.mark.parametrize(
    "make", [_link_to_an_open_folder, _someone_elses_folder], ids=["symlink", "not-owned"]
)
@pytest.mark.parametrize("name", ["state", "ipc"])
async def test_a_private_folder_that_is_a_link_or_not_its_own_is_left_alone(executor, make, name):
    """連結不跟隨、別人擁有的不碰：權限不變，照樣判 ``private_dir``（F1 只收回自己擁有的）."""
    assert os.geteuid() != 0, "隔離測試不以 root 執行"
    folder = make(executor.dirs, name)
    before = _mode(folder)
    server, started = await executor.start()
    assert (started, server.problem) == (False, "private_dir")
    assert _mode(folder) == before


async def test_startup_empties_its_tmp_without_following_links(executor):
    dirs = executor.dirs
    tmp = dirs.state / "agents" / "Alice" / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "junk.txt").write_text("x", encoding="utf-8")
    (tmp / "link").symlink_to(dirs.outside, target_is_directory=True)
    (tmp / "nested").mkdir()
    (tmp / "nested" / "link").symlink_to(dirs.outside / "keep.txt")
    server, started = await executor.start()
    assert started, server.problem
    assert list(tmp.iterdir()) == [] and stat.S_IMODE(os.lstat(tmp).st_mode) == 0o700
    assert (dirs.outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def _make_touka_root(dirs):
    (dirs.pd / "soul").mkdir()


def _nest_state(dirs):
    (dirs.pd / "state").mkdir(mode=0o700)
    dirs.state = dirs.pd / "state"


def _symlink_root(dirs):
    real = dirs.pd.parent / "real_mcp"
    dirs.mcp.rename(real)
    dirs.mcp.symlink_to(real, target_is_directory=True)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (_make_touka_root, "touka_root"),
        (_nest_state, "nested"),
        (_symlink_root, "root_path"),
    ],
    ids=["touka-root", "nested", "symlink-root"],  # 群組／其他人位元：見 F1 的收回測試
)
async def test_config_errors_keep_the_server_alive_without_a_socket(executor, change, problem):
    dirs = executor.dirs
    for name in (FR.SOCKET_NAME, "alive"):
        (dirs.ipc / name).write_text("stale", encoding="utf-8")
    change(dirs)
    server, started = await executor.start()
    assert (started, server.problem) == (False, problem)
    assert not (dirs.ipc / FR.SOCKET_NAME).is_socket()
    assert server._tasks == [] and server._listener is None


async def test_stale_socket_and_alive_are_removed_before_validation(executor):
    dirs = executor.dirs
    for name in (FR.SOCKET_NAME, "alive"):
        (dirs.ipc / name).write_text("stale", encoding="utf-8")
    _make_touka_root(dirs)
    _server, started = await executor.start()
    assert started is False
    assert not (dirs.ipc / FR.SOCKET_NAME).exists() and not (dirs.ipc / "alive").exists()


async def test_a_second_server_on_the_same_ipc_folder_does_not_serve(executor):
    first, started = await executor.start()
    assert started, first.problem
    second, started_again = await executor.start()
    assert (started_again, second.problem) == (False, "ipc_locked")
    assert (executor.dirs.ipc / FR.SOCKET_NAME).is_socket()
    code, _out, status = await _relay(executor, first, _exec_start(first, "Alice", ["true"]))
    assert (code, status) == (0, b"")


async def test_start_repairs_the_locked_drive_before_running(executor):
    """代理人把自己的資料夾鎖成 000、掛載根拿掉寫入：START 前沿路徑鏈補回擁有者位元，指令照跑."""
    server, started = await executor.start()
    assert started, server.problem
    drive, root = executor.dirs.pd / "Alice", executor.dirs.pd
    drive.chmod(0)
    root.chmod(0o500)
    try:
        start = _exec_start(server, "Alice", [*PY, "import os; print(sorted(os.listdir('.')))"])
        code, out, status = await _relay(executor, server, start)
    finally:
        root.chmod(0o755)
    assert (code, status) == (0, b""), out
    assert stat.S_IMODE(os.lstat(drive).st_mode) == 0o700
    assert server.repairs["repaired"] >= 1


async def test_start_repairs_a_locked_home_before_running(executor):
    """上一次的指令把自己 state 裡的 HOME 鎖成 000：START 前補回擁有者位元，指令寫得進 HOME."""
    server, started = await executor.start()
    assert started, server.problem
    home = executor.dirs.state / "agents" / "Alice" / "home"
    home.mkdir(parents=True)
    home.chmod(0)
    write_home = "import os; open(os.path.join(os.environ['HOME'], 'f'), 'w').write('x')"
    start = _exec_start(server, "Alice", [*PY, write_home])
    code, out, status = await _relay(executor, server, start)
    assert (code, status) == (0, b""), out
    assert stat.S_IMODE(os.lstat(home).st_mode) == 0o700
    assert (home / "f").read_text(encoding="utf-8") == "x"


# ── U2n 第二段：monitor 的接線（安裝掛鉤、定期檢查、ST/proxy 先修再判斷）──────────────


class _ServiceClock:
    """服務自己的時鐘（同一目標的 10 秒冷卻用）；monitor 照用真的時鐘。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SpyRepair(PermissionRepairService):
    """真的服務；另外記下 ``repair`` 收到的方法名，分得出是定期檢查還是 ST 判斷前修的。"""

    def __init__(self, layout: SandboxLayout, clock: _ServiceClock) -> None:
        super().__init__(lambda: layout, clock=clock)
        self.methods: list[str] = []

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        self.methods.append(method)
        return super().repair(method, paths, retry=retry)


async def _wired_monitor(tmp_path: Path) -> tuple[IsolationMonitor, _SpyRepair, _ServiceClock]:
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root 不受權限位元限制")
    layout = _layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
    )
    await monitor.run_selftest("data", notify=False)
    clock = _ServiceClock()
    monitor._repair = _SpyRepair(layout, clock)
    return monitor, monitor._repair, clock


async def test_a_locked_st_proxy_is_repaired_by_the_periodic_check(tmp_path):
    monitor, spy, _ = await _wired_monitor(tmp_path)
    proxy = tmp_path / "state" / "proxy"
    proxy.mkdir()
    proxy.chmod(0)
    try:
        await monitor.refresh()
        assert _mode(proxy) == 0o700
    finally:
        proxy.chmod(0o700)
    assert "state_issue" not in spy.methods  # 定期檢查先修好，ST 的判斷不必再修
    assert monitor.fallback_for(profile_complete=True).kind == "landlock"


async def test_a_locked_st_proxy_is_repaired_before_the_state_is_judged(tmp_path):
    monitor, spy, clock = await _wired_monitor(tmp_path)
    await monitor.refresh()  # 這一輪做過定期檢查；下一輪在一分鐘內，不再做
    proxy = tmp_path / "state" / "proxy"
    proxy.chmod(0)
    clock.now += 11  # 過了同一目標的冷卻
    try:
        await monitor.refresh()
        assert _mode(proxy) == 0o700
    finally:
        proxy.chmod(0o700)
    assert spy.methods == ["state_issue"]
    assert monitor.fallback_for(profile_complete=True).kind == "landlock"


async def test_the_periodic_check_repairs_a_locked_custom_root(tmp_path):
    monitor, _, _ = await _wired_monitor(tmp_path)
    custom = tmp_path / "data" / "custom"
    custom.mkdir()
    custom.chmod(0)
    try:
        await monitor.refresh()
        assert _mode(custom) == 0o700  # W8
    finally:
        custom.chmod(0o700)


async def test_ensure_ready_installs_the_hook_that_heals_an_agent_folder(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root 不受權限位元限制")
    layout = _layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
    )
    await monitor.ensure_ready()
    drive = layout.data_root / "personal_drive" / "Alice"
    (drive / "notes.txt").write_text("hi", encoding="utf-8")
    drive.chmod(0)
    try:
        assert resource_manager.read_file_text(drive / "notes.txt") == "hi"
    finally:
        drive.chmod(0o700)
    assert monitor.snapshot().repairs["repaired"] == 1


# 代理人的檔案動詞（ADR-0292 決定 12：不跟隨連結）讀不到時一樣自癒。寫入與刪除不重試（不是冪等）：
# 這次修好但照樣失敗，下一次就成功。
HEALING_VERBS = {
    "read": lambda root: cfo.core_read("sub/notes.txt", root, resource_proxy=resource_manager),
    "edit": lambda root: cfo.core_str_replace(
        "sub/notes.txt", root, old_str="hi", new_str="yo", resource_proxy=resource_manager
    ),
    "list": lambda root: cfo.core_list("sub", root, resource_proxy=resource_manager),
    "write": lambda root: cfo.core_write(
        "sub/notes.txt", root, "new", resource_proxy=resource_manager
    ),
    "delete": lambda root: cfo.core_delete("sub/notes.txt", root, resource_proxy=resource_manager),
}
SECOND_TRY_VERBS = frozenset({"write", "delete"})


@pytest.mark.parametrize(
    ("locked", "verb"),
    [
        ("drive", "read"),
        ("folder", "read"),
        ("file", "read"),
        ("drive", "edit"),
        ("file", "edit"),
        ("drive", "list"),
        ("drive", "write"),
        ("folder", "write"),
        ("drive", "delete"),
        ("folder", "delete"),
    ],
)
async def test_file_verbs_heal_a_locked_agent_folder(tmp_path, locked, verb):
    if os.geteuid() == 0:
        pytest.skip("[skip:root] root 不受權限位元限制")
    layout = _layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_available_selftest, start_background=False
    )
    await monitor.ensure_ready()
    drive = layout.data_root / "personal_drive" / "Alice"
    (drive / "sub").mkdir()
    (drive / "sub" / "notes.txt").write_text("hi", encoding="utf-8")
    modes = {drive: 0o700, drive / "sub": 0o700, drive / "sub" / "notes.txt": 0o600}
    targets = dict(zip(("drive", "folder", "file"), modes, strict=True))
    targets[locked].chmod(0)
    try:
        result = await HEALING_VERBS[verb](str(drive))
        if verb in SECOND_TRY_VERBS and result.startswith("Error"):
            result = await HEALING_VERBS[verb](str(drive))
    finally:
        for path, mode in modes.items():
            with contextlib.suppress(FileNotFoundError):
                path.chmod(mode)
    assert not result.startswith("Error"), result
    repairs = monitor.snapshot().repairs
    assert repairs["repaired"] >= 1
    assert repairs["refused_outside"] == 0


# ── U2f：compose 的 healthcheck 要 socket 加上新鮮的 alive（真 sh、find、unix socket）────────
# 伺服器死掉或卡住時，volume 裡的 socket 還在、alive 不再更新：只看 socket 會被騙過。

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
STALE_ALIVE_SECONDS = 120


def _healthcheck(ipc: Path) -> list[str]:
    """Compose 的 healthcheck（``$$`` 還原成 ``$``），容器裡的 ipc 路徑換成 ``ipc``."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = compose["services"]["touka-executor"]
    kind, command = service["healthcheck"]["test"]
    assert kind == "CMD-SHELL"
    container_ipc = service["command"][service["command"].index("--ipc-dir") + 1]
    return ["sh", "-c", command.replace("$$", "$").replace(container_ipc, str(ipc))]


def _healthy(ipc: Path) -> bool:
    return run_sync(_healthcheck(ipc), capture_output=True, timeout=30, check=False).returncode == 0


async def test_compose_healthcheck_passes_on_a_serving_executor(executor):
    server, started = await executor.start("--require-foreign-peer")
    assert started, server.problem
    assert await asyncio.to_thread(_healthy, executor.dirs.ipc)


@pytest.mark.parametrize(
    ("entry", "alive_age", "healthy"),
    [
        ("socket", 0, True),
        ("socket", STALE_ALIVE_SECONDS, False),  # 伺服器死了或卡住：socket 還在、alive 沒更新
        ("socket", None, False),
        ("file", 0, False),
        (None, 0, False),
    ],
    ids=["fresh", "stale-alive", "no-alive", "not-a-socket", "no-socket"],
)
def test_compose_healthcheck_needs_a_socket_and_a_fresh_alive(tmp_path, entry, alive_age, healthy):
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    sock_path = ipc / FR.SOCKET_NAME
    if entry == "socket":  # 關掉之後 socket 檔留在原地，和殘留在 volume 裡的一樣
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(sock_path))
    elif entry == "file":
        sock_path.write_text("stale", encoding="utf-8")
    if alive_age is not None:
        alive = ipc / exs.ALIVE_NAME
        alive.touch()
        then = alive.stat().st_mtime - alive_age
        os.utime(alive, (then, then))
    assert _healthy(ipc) is healthy


# ── U2g：CI 的層級斷言（.github/workflows/isolation.yml 的 pass B；C2-2 設計 §4.1）──────────


async def test_ci_host_reaches_the_landlock_level(
    tmp_path, touka_landlock_requirement, record_testsuite_property
):
    """CI 的 pass B（設 REQUIRE、關掉 Fast Open 與 MPTCP）以這個案例斷言層級：真 monitor 判
    landlock、網路設定安全、features 含 tcp；checker 要求它 passed.

    pytest 自己的 pid 記進 junit：pid 1 時孤兒本來就歸 pytest，「沒設 subreaper」的突變抓不到
    （docker run 就是這樣）；checker 要求它不是 1。
    """
    record_testsuite_property("pytest_pid", os.getpid())
    monitor = await _ready_monitor(_layout(tmp_path), touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    snap = monitor.snapshot()
    assert (snap.level, snap.reason, snap.local.net) == ("landlock", None, None)
    assert "tcp" in snap.features


# ── U2e：終端、驗證、演化經真的執行器（in-process 伺服器、真 relay、真啟動器；C2-2 §2.8）──────────
# proxy 這邊用真 monitor（真健康檢查、伺服器的雜湊 SSOT）與真 broker，版面對上 in-process 執行器的
# roots 與 ipc。伺服器和 proxy 在同一個行程：proxy 那邊的子行程（relay、自測）沒有 exec id，
# 掃孤兒時不算（正式部署是兩個容器，伺服器看不到 proxy 的行程）。網路設定不安全（不加 sysctl）時
# 執行器判 none，改用人核准的執行器降級（features 不含 tcp）。


def _proxy_side(pid: int) -> bool:
    return not exs._proc_exec_id(pid)  # 讀不到（None）或沒有 exec id（空字串）


async def _executor_wiring(monkeypatch, executor, tmp_path: Path) -> SimpleNamespace:
    server, started = await executor.start()
    assert started, server.problem
    monkeypatch.setattr(server.hooks.processes, "_exclude", _proxy_side)
    dirs = executor.dirs
    layout = replace(
        current_layout(), data_root=dirs.pd.parent, config_dir=dirs.skills.parent,
        env_file=tmp_path / "host_root" / ".env", state_root=tmp_path / "proxy_st",
        kernel_dir=EXEC_KERNEL, executor_dir=dirs.ipc, mirror_roots=(), in_container=False,
    )
    layout.state_root.mkdir()
    point_zone_roots(monkeypatch, layout.data_root, layout.state_root)  # 蒐證的個人空間（ADR-0293）
    monitor = IsolationMonitor(layout_provider=lambda: layout, start_background=False)
    snap = await monitor.ensure_ready()
    assert snap.executor is not None, (snap.level, snap.reason)  # READY 全部相符
    assert snap.executor.state_root == str(dirs.state)

    def _scope(_identity, project_root):  # 測試的資料夾不在 ResourceManager 的版面：範圍照形狀判
        return "project_not_in_executor" if project_root is not None else None

    monkeypatch.setattr(monitor, "executor_scope_issue", _scope)
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    for module in (agent_command, evolution_command, home_wipe):
        monkeypatch.setattr(module, "isolation_monitor", monitor)
        monkeypatch.setattr(module, "runner_broker", broker)
        monkeypatch.setattr(module, "current_layout", lambda: layout)
    if snap.level == "executor":
        grant = ExecutionGrant("executor", executor_eligible=True)
    else:  # 網路設定不安全：卡片寫執行器、features 不含 tcp
        assert snap.reason in NET_SYSCTL_CODES, snap.reason
        consent = Consent("linux", "executor", frozenset({"fs"}))
        grant = ExecutionGrant("none", consent, executor_eligible=True)
    terminal.reset_async_state_for_test()
    zone = resource_manager.agent_drive(ALICE)  # 終端的個人空間（ADR-0293）
    return SimpleNamespace(dirs=dirs, grant=grant, drive=dirs.pd / "Alice", zone=zone, snap=snap)


_EXEC_SHELL = (
    'echo "home=$HOME"; echo "cwd=$(pwd)"; echo "exec=${TOUKA_EXEC_ID:+yes}"; '
    'cat ../Bob/secret.txt 2>/dev/null && echo LE""AK; echo own > own.txt'
)


async def test_terminal_command_runs_inside_the_executor(tmp_path, monkeypatch, executor):
    wiring = await _executor_wiring(monkeypatch, executor, tmp_path)
    # 上層的公開設定檔：proxy 的 profile 會給唯讀規則，執行器的 profile 不給（伺服器會拒絕 START）
    (wiring.dirs.pd.parent.parent / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    scope = CommandScope(ALICE, wiring.grant)
    exit_code, output, _ms = await terminal._execute_core(
        _EXEC_SHELL, "wsl", wiring.zone, None, timeout=60, scope=scope
    )
    assert exit_code == 0, output
    home = wiring.dirs.state / "agents" / "Alice" / "home"
    assert f"home={home}" in output and f"cwd={wiring.drive}" in output
    assert "exec=yes" in output and "LEAK" not in output and "bob" not in output
    assert (wiring.drive / "own.txt").read_text(encoding="utf-8") == "own\n"
    assert stat.S_IMODE(os.lstat(home).st_mode) == 0o700  # 伺服器建的，proxy 沒碰


async def test_executor_timeout_kills_the_remote_command(tmp_path, monkeypatch, executor):
    """本機逾時先到：殺 relay，執行器收到 EOF 就收掉遠端的整個群組."""
    wiring = await _executor_wiring(monkeypatch, executor, tmp_path)
    scope = CommandScope(ALICE, wiring.grant)
    exit_code, output, _ms = await terminal._execute_core(
        "sleep 60 & echo $! > child.pid; echo L1; wait", "wsl", wiring.zone, None,
        timeout=3, scope=scope,
    )
    assert exit_code == -1 and "timed out after 3 seconds" in output
    child = int((wiring.drive / "child.pid").read_text(encoding="utf-8"))
    assert await _gone(child, 10)


async def test_soul_delete_wipe_empties_its_executor_home(tmp_path, monkeypatch, executor):
    """刪除靈魂前的清除（home_wipe）經真的執行器清空自己的 home／tmp：權限被拿掉的資料夾也清，
    連結本身刪掉、不跟過去，別的代理人與外面的檔案不動."""
    wiring = await _executor_wiring(monkeypatch, executor, tmp_path)
    seed = (
        'echo t > "$HOME/.token"; mkdir -p "$HOME/locked/x"; echo y > "$HOME/locked/x/f"; '
        f'chmod 000 "$HOME/locked"; ln -s {wiring.dirs.outside} "$HOME/out"; echo z > "$TMPDIR/t"'
    )
    exit_code, output, _ms = await terminal._execute_core(
        seed, "wsl", wiring.zone, None, timeout=60, scope=CommandScope(ALICE, wiring.grant)
    )
    assert exit_code == 0, output
    home = wiring.dirs.state / "agents" / "Alice" / "home"
    assert (home / ".token").exists()

    await home_wipe.wipe_agent_homes(ALICE)

    assert list(home.iterdir()) == [] and list((home.parent / "tmp").iterdir()) == []
    assert (wiring.dirs.outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (wiring.dirs.pd / "Bob" / "secret.txt").read_text(encoding="utf-8") == "bob\n"


async def test_verification_command_runs_inside_the_executor(tmp_path, monkeypatch, executor):
    wiring = await _executor_wiring(monkeypatch, executor, tmp_path)

    async def _allow(_cmd: str) -> None:
        return None

    bundle = await evidence.collect_evidence(
        artifact_refs=[], verification_cmd="cat ../Bob/secret.txt", test_log=None,
        artifact_root=resource_manager.agent_drive(ALICE), command_gate=_allow, identity=ALICE,
        consent=lambda _cmd: wiring.grant,
    )
    (result,) = bundle.cmd_results
    assert result.rejected_reason is None and result.exit_code != 0  # 讀不到別人的資料夾
    assert "bob" not in (result.output_snippet or "")


async def test_evolution_verify_runs_inside_the_executor(tmp_path, monkeypatch, executor):
    """venv 先建好（建置要連網的 pip 不在這裡驗）；驗證在執行器裡跑、HOME 在執行器的 state."""
    wiring = await _executor_wiring(monkeypatch, executor, tmp_path)  # 已讓 data 根指到執行器的
    limits = {"execution_timeout_sec": 60, "max_storage_mb": 50, "max_memory_mb": 1024}
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": limits})
    EvolutionPathGuard.write_tool_file(EVO_SKILL, "spec.json", "{}")
    root = Path(EvolutionPathGuard.get_tool_root(EVO_SKILL))
    venv = [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")]
    run_sync(venv, check=True, capture_output=True, timeout=120)
    secret = wiring.dirs.pd / "Bob" / "secret.txt"
    result = await evolution.evolution_implement_and_verify(
        skill_id=EVO_SKILL,
        code=EVO_SERVER,
        test_tool_name="probe",
        test_arguments={"paths": {"bob": str(secret)}, "big": str(root / "b.bin"), "size": 1},
        identity=ALICE,
        session_id=None,
        _call_id="evo-executor",
        _evolution_grant=wiring.grant,
    )
    data = _verify_data(result)
    assert data["reads"] == {"bob": "EACCES"}, data
    assert data["home"] == str(wiring.dirs.state / "evolution" / EVO_SKILL / "home")


# ── U3a：spec 的 exec_env 只到目標（C2-3 設計 §2.6）────────────────────────────────────────

_EXEC_ENV_NAMES = ("U3A_ENTRY", "U3A_SHARED", "U3A_LAUNCHER_ONLY", "LD_PRELOAD", "PYTHONPATH")
_EXEC_ENV_PROBE = (
    "import json, os, sys\nprint(json.dumps({n: os.environ.get(n) for n in sys.argv[1:]}))"
)


async def _environ_while_reading_the_spec(pid: int) -> dict[str, str]:
    """啟動器已經 exec 成 python、還卡在讀 spec（限制之前）時的 ``/proc/<pid>/environ``."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if str(LAUNCHER).encode() in Path(f"/proc/{pid}/cmdline").read_bytes():
            raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
            return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
        await asyncio.sleep(0.02)
    pytest.fail("launcher did not start")


async def test_exec_env_reaches_the_target_only(tmp_path, landlock_abi):
    """啟動器自己的環境（等 spec 時）沒有條目的 env；目標在 Landlock 裡拿到它，同名的蓋過啟動器
    的值；禁令 19 那一套（``LD_PRELOAD``、``PYTHONPATH``、``NODE_OPTIONS``）目標拿不到."""
    exec_env = {"U3A_ENTRY": "entry-secret", "U3A_SHARED": "entry", "NODE_OPTIONS": "--require x"}
    exec_env |= {"LD_PRELOAD": str(tmp_path / "missing.so"), "PYTHONPATH": str(tmp_path)}
    spec_r, spec_w = os.pipe()
    proc = await spawn_exec(
        sys.executable, "-I", "-S", str(LAUNCHER), "--spec-fd", str(spec_r), "--",
        *PY, _EXEC_ENV_PROBE, *_EXEC_ENV_NAMES, "NODE_OPTIONS", pass_fds=(spec_r,),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "U3A_SHARED": "launcher", "U3A_LAUNCHER_ONLY": "1"},
    )
    os.close(spec_r)
    try:
        before = await _environ_while_reading_the_spec(proc.pid)
    finally:
        _write_all(spec_w, _spec(exec_env=exec_env))  # 寫完就關：啟動器讀到 EOF 才往下走
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert not {"U3A_ENTRY", "LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS"} & set(before)
    assert (before["U3A_SHARED"], before["U3A_LAUNCHER_ONLY"]) == ("launcher", "1")
    assert proc.returncode == 0, (out, err)
    seen = json.loads(out.decode().strip().splitlines()[-1])
    assert seen == {
        "U3A_ENTRY": "entry-secret", "U3A_SHARED": "entry", "U3A_LAUNCHER_ONLY": "1",
        "LD_PRELOAD": None, "PYTHONPATH": None, "NODE_OPTIONS": None,
    }
    assert b"ld.so" not in err  # 動態連結器沒看到 LD_PRELOAD


# ── U3a：relay 的 stdio 模式（C2-3 設計 §3④；relay 依路徑執行，執行器由測試扮演）──────


def _serve_stdio(listener: socket.socket, seen: list) -> None:
    """假執行器：每塊 STDIN 原樣以 OUT 回、另送一行 ERR；收到 STDIN_EOF 才回 EXIT 5."""
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(30)
        seen.append(FR.decode_json(FR.read_frame(conn.recv)))  # HELLO
        conn.sendall(FR.encode_json(FR.READY, {"v": FR.PROTOCOL_VERSION, "boot_id": BOOT_ID}))
        seen.append(FR.decode_json(FR.read_frame(conn.recv)))  # START
        conn.sendall(FR.encode_json(FR.STARTED, {"exec_id": "e" * 32}))
        while (frame := FR.read_frame(conn.recv)) is not None:
            seen.append((FR.TYPE_NAMES[frame.kind], frame.payload))
            if frame.kind != FR.STDIN:
                exit_doc = {"code": 5, "signal": None, "timed_out": False, "launcher_error": None}
                conn.sendall(FR.encode_json(FR.EXIT, exit_doc))
                return
            reply = FR.encode(FR.OUT, frame.payload)
            conn.sendall(reply + FR.encode(FR.ERR, b"seen %d\n" % len(frame.payload)))


async def test_relay_stdio_round_trip(tmp_path):
    """一問一答：stdin 還開著時就收到回應（沒有先送 STDIN_EOF）；ERR 只到 stderr；關掉 stdin
    之後才送 STDIN_EOF，exit code 是遠端的."""
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    (ipc / FR.TOKEN_NAME).write_text("ab" * 32, encoding="ascii")
    seen: list = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(ipc / FR.SOCKET_NAME))
        listener.listen(1)
        server = threading.Thread(target=_serve_stdio, args=(listener, seen))
        server.start()
        request_r, request_w = os.pipe()
        status_r, status_w = os.pipe()
        start = {**RELAY_START, "io": "stdio"}
        _write_all(request_w, json.dumps({"parent_pid": os.getpid(), "start": start}).encode())
        argv = [sys.executable, "-I", "-S", str(RELAY), "--ipc-dir", str(ipc), "--boot-id", BOOT_ID]
        proc = await spawn_exec(
            *argv, "--request-fd", str(request_r), "--status-fd", str(status_w),
            pass_fds=(request_r, status_w), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        os.close(request_r)
        os.close(status_w)
        try:
            replies = []
            for line in (b"ping\n", b"second\n"):
                proc.stdin.write(line)
                await proc.stdin.drain()
                replies.append(await asyncio.wait_for(proc.stdout.readline(), 30))
            proc.stdin.close()  # 呼叫端關掉 stdin：relay 這時才送 STDIN_EOF
            rest, err = await asyncio.wait_for(
                asyncio.gather(proc.stdout.read(), proc.stderr.read()), 30
            )
            code = await asyncio.wait_for(proc.wait(), 30)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            server.join(timeout=30)
        status = os.read(status_r, 256)
        os.close(status_r)
    assert (replies, rest, err) == ([b"ping\n", b"second\n"], b"", b"seen 5\nseen 7\n")
    assert (code, status) == (5, b"")
    assert seen[1] == start
    assert seen[2:] == [("STDIN", b"ping\n"), ("STDIN", b"second\n"), ("STDIN_EOF", b"")]


# ── MCP 的 profile（C2-3 U3c）：自建寫不了自己部署的程式；scaffold 只看得到自己的資料夾 ──────────


async def _run_in_mcp_profile(
    layout: SandboxLayout, monitor: IsolationMonitor, profile, script: str
) -> bytes:
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    runner = (await broker.acquire(ExecutionGrant("landlock"), ProfileSource.fixed(profile))).runner
    plan = runner.plan(
        ExecRequest(argv=("sh", "-c", script), env={"PATH": "/usr/bin:/bin"}, profile=profile)
    )
    try:
        # cwd 是 ST 裡的 home：和 isolated_stdio 一樣在 spawn 當下交接（ADR-0293 P6）
        with resource_manager.handoff_cwd(str(plan.cwd)) as cwd:
            proc = await spawn_exec(
                *plan.argv, cwd=cwd, env=plan.env, pass_fds=plan.pass_fds,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
    finally:
        plan.release()
    output, _ = await asyncio.wait_for(proc.communicate(), 60)
    return output


async def test_mcp_self_profile_reads_its_deployed_code_but_cannot_write_it(
    tmp_path, touka_landlock_requirement
):
    """防禦清單 C1：演化部署的程式資料夾唯讀＋執行（改名的條目也一樣），代理人資料夾讀不到."""
    layout = _layout(tmp_path)
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    code = layout.data_root / "mcp_servers" / "managed" / "foo"
    code.mkdir(parents=True)
    (code / "server.py").write_text("CODE\n", encoding="utf-8")
    secret = layout.data_root / "personal_drive" / "Alice" / "secret.txt"
    secret.write_text("AGENT\n", encoding="utf-8")
    python = str(code / ".venv" / "bin" / "python")
    entry = RawMcpEntry(python, (str(code / "server.py"),), None, None)
    record = OriginRecord("present", "evolution")
    result = classify("renamed", entry, record, layout, base_dir=tmp_path)
    profile = build_mcp_self_profile("renamed", result, layout=layout)
    script = (
        f'cat "{code}/server.py"; '
        f'echo x >> "{code}/server.py" 2>/dev/null && echo WROTE; '
        f'touch "{code}/new.py" 2>/dev/null && echo CREATED; '
        f'cat "{secret}" 2>/dev/null; '
        'echo hi > "$HOME/probe" && cat "$HOME/probe"; pwd'
    )
    output = await _run_in_mcp_profile(layout, monitor, profile, script)
    assert b"CODE" in output and b"hi" in output and str(profile.cwd).encode() in output, output
    assert b"WROTE" not in output and b"CREATED" not in output and b"AGENT" not in output, output
    assert (code / "server.py").read_text(encoding="utf-8") == "CODE\n"
    assert not (code / "new.py").exists()


async def test_mcp_scaffold_profile_sees_only_its_own_folder(tmp_path, touka_landlock_requirement):
    """防禦清單 C5：scaffold 只有自己的 config/mcp_servers/<name> 唯讀＋執行."""
    layout = _layout(tmp_path)
    monitor = await _ready_monitor(layout, touka_landlock_requirement)
    _require_landlock_level(monitor, touka_landlock_requirement)
    servers = layout.config_dir / "mcp_servers"
    own, other = servers / "demo", servers / "other"
    for folder, text in ((own, "STUB"), (other, "OTHER")):
        folder.mkdir(parents=True)
        (folder / "server.py").write_text(f"{text}\n", encoding="utf-8")
    config_secret = layout.config_dir / "users.json"
    config_secret.write_text("CFG\n", encoding="utf-8")
    entry = RawMcpEntry("python3", (str(own / "server.py"),), None, None)
    result = classify("demo", entry, OriginRecord("present", "scaffold"), layout, base_dir=tmp_path)
    assert result.kind == "scaffold"
    launch = await build_mcp_third_party_profile(
        "demo", entry, result, layout=layout
    )
    script = (
        f'cat "{own}/server.py"; '
        f'echo x >> "{own}/server.py" 2>/dev/null && echo WROTE; '
        f'cat "{other}/server.py" "{config_secret}" 2>/dev/null; '
        'echo hi > "$HOME/probe" && cat "$HOME/probe"'
    )
    output = await _run_in_mcp_profile(layout, monitor, launch.profile, script)
    assert b"STUB" in output and b"hi" in output, output
    assert b"WROTE" not in output and b"OTHER" not in output and b"CFG" not in output, output
    assert (own / "server.py").read_text(encoding="utf-8") == "STUB\n"


# ── U3b：執行器 kind mcp、io stdio（C2-3 設計 §2.3；in-process 伺服器、真 relay、真啟動器）──────

_MCP_SEG = "m" + "0123456789abcdef01234567"  # Touka 算的段名（sha256 前 24 字）
_MCP_PROBE = """
import errno, json, os, sys
code = sys.argv[1]
out = {"cwd": os.getcwd(), "home": os.environ.get("HOME"), "echo": sys.stdin.readline().strip()}
with open(os.path.join(code, "server.py"), encoding="utf-8") as handle:
    out["read_code"] = handle.read().strip()
for name, path in (("write_code", os.path.join(code, "new.py")),
                   ("write_home", os.path.join(os.environ["HOME"], "cache.txt"))):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        out[name] = "ok"
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, str(exc.errno))
print("probe log", file=sys.stderr, flush=True)
print(json.dumps(out), flush=True)
sys.stdin.read()  # 常駐伺服器的形狀：stdin 關掉（STDIN_EOF）才結束
"""


def _mcp_exec_start(server, code: Path, argv: list[str]) -> dict:
    """kind mcp：讀寫只有自己的 home／tmp、cwd 是 home、程式資料夾唯讀＋執行、stdio、不限時."""
    home, tmp = server.layout.state_dirs("mcp", _MCP_SEG)
    rules = [{"path": p, "access": "rox", "optional": True} for p in exe.system_exec_paths()]
    rules += [{"path": p, "access": "ro", "optional": True} for p in exe.SYSTEM_READ_PATHS]
    rules += [{"path": "/dev/null", "access": "dev_rw", "optional": True}]
    rules += [{"path": str(code), "access": "rox"}]
    rules += [{"path": p, "access": "rwx"} for p in (home, tmp)]
    net = server.ready(frozenset())["net"]
    return {
        "argv": argv, "cwd": home, "env": {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": tmp},
        "spec": {
            "version": launcher.spec.SPEC_VERSION, "profile": "mcp_self", "rules": rules,
            "block_ports": sorted(launcher.spec.DEFAULT_BLOCK_PORTS), "pin": True,
            "net_strict": net is None,
        },
        "kind": "mcp", "owner": _MCP_SEG, "io": "stdio", "max_seconds": 0,
    }


async def test_kind_mcp_runs_from_its_state_home_and_cannot_write_its_code(executor):
    """kind mcp 經 relay stdio：cwd 與 HOME 在執行器 state；程式資料夾讀得到、寫不進去（EACCES）；
    stdin 開著就收到回應；stderr 只到 relay 的 stderr；stdin 關掉後照遠端的 exit code 結束."""
    server, started = await executor.start()
    assert started, server.problem
    code = executor.dirs.mcp / "probe_tool"
    code.mkdir()
    (code / "server.py").write_text("print('server')\n", encoding="utf-8")
    start = _mcp_exec_start(server, code, [*PY, _MCP_PROBE, str(code)])
    request_r, request_w = os.pipe()
    status_r, status_w = os.pipe()
    _write_all(request_w, json.dumps({"parent_pid": os.getpid(), "start": start}).encode())
    argv = [sys.executable, "-I", "-S", str(RELAY), "--ipc-dir", str(executor.dirs.ipc)]
    proc = await spawn_exec(
        *argv, "--request-fd", str(request_r), "--status-fd", str(status_w),
        "--boot-id", server.boot_id, pass_fds=(request_r, status_w),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    executor.children.add(proc.pid)
    os.close(request_r)
    os.close(status_w)
    try:
        proc.stdin.write(b"hello\n")
        await proc.stdin.drain()
        report = json.loads(await asyncio.wait_for(proc.stdout.readline(), 60))
        proc.stdin.close()  # relay 這時才送 STDIN_EOF
        rest, err = await asyncio.wait_for(
            asyncio.gather(proc.stdout.read(), proc.stderr.read()), 60
        )
        returncode = await asyncio.wait_for(proc.wait(), 60)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    status = os.read(status_r, 256)
    os.close(status_r)
    home = server.layout.state_dirs("mcp", _MCP_SEG)[0]
    assert (report["cwd"], report["home"]) == (home, home)
    assert home == str(executor.dirs.state / "mcp" / _MCP_SEG / "home")
    assert (report["echo"], report["read_code"]) == ("hello", "print('server')")
    assert (report["write_code"], report["write_home"]) == ("EACCES", "ok")
    assert not (code / "new.py").exists()
    assert (rest, err, returncode, status) == (b"", b"probe log\n", 0, b"")


# ── U3e：真啟動判定 → Landlock／executor → 隔離 stdio → SDK Client ────────────────

_MCP_STDIO_FIXTURE = Path(__file__).parents[1] / "fixtures" / "mcp_v2" / "stdio_modern_server.py"
_MCP_STDIO_IMPORTS = """
import ctypes
import errno
import json
import os
import signal
import sys
import time
from pathlib import Path
"""
_MCP_STDIO_EXTRA = r'''

# PDEATHSIG is per-thread; sync MCP tools run in worker threads, so sample the main thread.
_pdeathsig_value = ctypes.c_int()
_pdeathsig_result = ctypes.CDLL(None, use_errno=True).prctl(
    2, ctypes.byref(_pdeathsig_value), 0, 0, 0
)
_pdeathsig = _pdeathsig_value.value if _pdeathsig_result == 0 else -1


@mcp.tool()
def isolation_probe() -> str:
    """Report the target's filesystem boundary and parent-death signal."""
    report = {"home": os.environ["HOME"], "cwd": os.getcwd(), "pid": os.getpid()}
    for name, path in json.loads(sys.argv[2]).items():
        try:
            Path(path).read_bytes()
            report[name] = "readable"
        except OSError as exc:
            report[name] = errno.errorcode.get(exc.errno, str(exc.errno))
    try:
        Path(__file__).with_name("forbidden.py").write_text("x", encoding="utf-8")
        report["write_code"] = "writable"
    except OSError as exc:
        report["write_code"] = errno.errorcode.get(exc.errno, str(exc.errno))
    Path("own-cache.txt").write_text("owned", encoding="utf-8")
    report["pdeathsig"] = _pdeathsig
    return json.dumps(report)


@mcp.tool()
def large_line(size: int) -> str:
    """Return one JSON-RPC line with a payload of the requested size."""
    return "x" * size


@mcp.tool()
def exit_now() -> str:
    """Exit without waiting for stdin EOF."""
    os._exit(3)


if __name__ == "__main__":
    mode = sys.argv[1]
    child = None
    if mode in {"hang", "stubborn"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = os.fork()
        if child == 0:
            while True:
                time.sleep(60)
    ready = {"pid": os.getpid(), "pgid": os.getpgrp(), "child": child}
    marker = Path(os.environ["HOME"]) / "stdio-ready.json"
    staging = marker.with_suffix(".tmp")
    staging.write_text(json.dumps(ready), encoding="utf-8")
    staging.replace(marker)
    if mode == "hang":
        while True:
            time.sleep(60)
    if mode == "stderr":
        # 40 MiB: the reader buffers 2 x MCP_STDIO_LINE_LIMIT before it stops reading the pipe.
        for _ in range(40 * 1024):
            sys.stderr.buffer.write(b"s" * 1023 + b"\n")
        sys.stderr.buffer.flush()
    mcp.run(transport="stdio")
    if mode == "stubborn":
        while True:
            time.sleep(60)
'''


class _McpStdioRecords:
    def record_for(self, _name: str, entry: dict) -> RecordView:
        return RecordView("ok", "creation", (), bound_of(entry))


def _mcp_group_members(pgid: int) -> dict[int, str]:
    members = {}
    for path in (Path("/") / "proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text("ascii").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid:
            members[int(path.name)] = fields[0]
    return members


async def _mcp_ready(peer) -> dict:
    async with asyncio.timeout(20):
        while not peer.ready_path.exists():
            await asyncio.sleep(0.01)
    return json.loads(peer.ready_path.read_text("utf-8"))


async def _mcp_groups_gone(peer, ready: dict | None = None) -> None:
    """連同孫行程檢查；PID 1／subreaper 收養的測試孫行程在這裡回收.

    zombie 已經結束、不再持有 pipe，不算殘留：PID 1 不是 pytest（例如突變腳本）時沒有人回收它。
    """
    groups = {proc.pid for proc in peer.spawned}
    if ready is not None:
        groups.add(ready["pgid"])
    deadline = get_monotonic_ms() + 8000
    while True:
        if ready is not None and ready["child"] is not None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(ready["child"], os.WNOHANG)
        remaining = {}
        for pgid in groups:
            live = {pid: state for pid, state in _mcp_group_members(pgid).items() if state != "Z"}
            if live:
                remaining[pgid] = live
        if not remaining:
            return
        assert get_monotonic_ms() < deadline, remaining
        await asyncio.sleep(0.02)


@pytest.fixture(params=["landlock", "executor"])
async def mcp_stdio_peer(
    request, tmp_path, monkeypatch, executor, touka_landlock_requirement, mcp_no_unretrieved,
):
    if request.param == "landlock":
        layout = _layout(tmp_path)
        monitor = await _ready_monitor(layout, touka_landlock_requirement)
        _require_landlock_level(monitor, touka_landlock_requirement)
        state_root = layout.state_root
    else:
        server, started = await executor.start()
        assert started, server.problem
        monkeypatch.setattr(server.hooks.processes, "_exclude", _proxy_side)
        dirs = executor.dirs
        layout = replace(
            current_layout(), data_root=dirs.pd.parent, config_dir=dirs.skills.parent,
            env_file=tmp_path / "host_root" / ".env", state_root=tmp_path / "proxy_st",
            kernel_dir=EXEC_KERNEL, executor_dir=dirs.ipc, mirror_roots=(), in_container=False,
        )
        layout.state_root.mkdir()
        match_state_root(monkeypatch, layout.state_root)
        monitor = IsolationMonitor(layout_provider=lambda: layout, start_background=False)
        snap = await monitor.ensure_ready()
        assert snap.executor is not None, (snap.level, snap.reason)
        assert snap.executor.state_root == str(dirs.state)
        state_root = Path(snap.executor.state_root)
        if snap.level != "executor":
            _require(
                False, f"網路設定不安全（{snap.reason}）", touka_landlock_requirement,
                tag="net" if snap.reason in NET_SYSCTL_CODES else "level",
            )
    code = layout.data_root / "mcp_servers" / "managed" / "stdio_probe"
    code.mkdir(parents=True)
    base, separator, _main = _MCP_STDIO_FIXTURE.read_text("utf-8").partition(
        '\nif __name__ == "__main__":'
    )
    assert separator
    script = code / "server.py"
    script.write_text(_MCP_STDIO_IMPORTS + base + _MCP_STDIO_EXTRA, encoding="utf-8")
    forbidden = {"data_canary": layout.data_root / "canary.txt", "secret": tmp_path / "secret.txt"}
    for path in forbidden.values():
        path.write_text("sandbox-private-marker", encoding="utf-8")
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    book = McpLaunchBook()
    planner = McpLauncher(
        monitor=monitor, broker=broker, layout_provider=lambda: layout,
        base_dir=lambda: tmp_path, book=book,
    )
    peer = SimpleNamespace(
        route=request.param, state_root=state_root, code=code, spawned=[], owners=[], lost=[],
        failures=[], lost_event=asyncio.Event(), close_stalled=asyncio.Event(), ready_path=None,
        planner=planner, monitor=monitor, book=book, layout=layout, broker=broker,
    )

    async def spawn(*argv, **kwargs):
        proc = await spawn_exec(*argv, **kwargs)
        peer.spawned.append(proc)
        executor.children.add(proc.pid)
        return proc

    def transport(*args, **kwargs):
        return mcp_stdio.isolated_stdio_client(*args, spawn=spawn, **kwargs)

    monkeypatch.setattr(mcp_client, "isolated_stdio_client", transport)

    def report_failure(code: str) -> None:
        peer.failures.append(code)
        monitor.report_failure(code)

    async def on_lost(owner, error) -> None:
        peer.lost.append((owner, error))
        peer.lost_event.set()

    async def owner_for(
        mode: str = "normal", *, invalid_env: bool = False, stall_close: bool = False,
        relative: bool = False,
    ):
        config = ProtocolServerConfig(
            command=sys.executable,
            args=[os.path.relpath(script, tmp_path) if relative else str(script), mode,
                  json.dumps({key: str(p) for key, p in forbidden.items()})],
            env={"INVALID-NAME": "test-value"} if invalid_env else {},
        )
        peer.config = config
        launch = await planner.plan_server_launch(
            "stdio-probe", config, _McpStdioRecords(), tool_base_url="http://localhost:8000"
        )
        assert launch.acquired is not None and launch.info.level == peer.route
        peer.ready_path = Path(launch.acquired.profile.env_set["HOME"]) / "stdio-ready.json"
        isolated = mcp_client.McpIsolatedLaunch(
            launch, label="stdio-probe", report_failure=report_failure, on_eof="raise"
        )

        async def stall_session_close():
            peer.close_stalled.set()
            await asyncio.Event().wait()

        async def connect():
            client, stack = await mcp_client.connect_mcp_server(
                config, isolated=isolated, read_timeout_seconds=10
            )
            if stall_close:
                stack.push_async_callback(stall_session_close)
            return client, stack

        def new_owner():
            owner = mcp_owner.McpConnectionOwner(
                "stdio-probe", connect, on_lost=on_lost, registry=set()
            )
            peer.owners.append(owner)
            return owner

        peer.new_owner = new_owner
        return new_owner()

    peer.owner_for = owner_for
    try:
        yield peer
    finally:
        for owner in peer.owners:
            await owner.aclose()
        # 突變故意留下行程時也只清本案例記錄的群組；斷言已先執行，不會把漏殺掩蓋成綠燈。
        ready = None
        if peer.ready_path is not None and peer.ready_path.exists():
            ready = json.loads(peer.ready_path.read_text("utf-8"))
        groups = {proc.pid for proc in peer.spawned if proc.returncode is None}
        if ready is not None and _mcp_group_members(ready["pgid"]):
            groups.add(ready["pgid"])
        for pgid in groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, mcp_stdio.SIGKILL)
        for proc in peer.spawned:
            await asyncio.wait_for(proc.wait(), 10)
        await _mcp_groups_gone(peer, ready)
        monitor.stop()


@pytest.fixture
async def mcp_no_unretrieved(caplog):
    # The peer depends on this fixture so its cleanup finishes before the leak check.
    yield
    await asyncio.sleep(0)
    assert mcp_asgi_peers.task_leak_messages(caplog) == []


async def _mcp_tool_text(client, name: str, arguments: dict | None = None) -> str:
    result = await asyncio.wait_for(client.call_tool(name, arguments), 10)
    assert not result.is_error, result
    assert len(result.content) == 1 and result.content[0].type == "text", result
    return result.content[0].text


async def test_mcp_stdio_round_trip_and_filesystem_boundary(mcp_stdio_peer):
    """L1／L2：discover、list、call 都經真 runner；只能寫自己的 state。"""
    peer = mcp_stdio_peer
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    ready = await _mcp_ready(peer)
    assert client.protocol_version == mcp_types.LATEST_PROTOCOL_VERSION
    listed = await asyncio.wait_for(client.list_tools(), 10)
    assert {tool.name for tool in listed.tools} >= {"p3_ping", "isolation_probe"}
    assert await _mcp_tool_text(client, "p3_ping") == "pong"
    report = json.loads(await _mcp_tool_text(client, "isolation_probe"))
    assert report["pid"] == ready["pid"]
    assert (report["data_canary"], report["secret"], report["write_code"]) == ("EACCES",) * 3
    assert Path(report["home"]) == peer.ready_path.parent == Path(report["cwd"])
    assert Path(report["home"]).is_relative_to(peer.state_root)
    assert (peer.ready_path.parent / "own-cache.txt").read_text("utf-8") == "owned"
    assert not (peer.code / "forbidden.py").exists()
    await owner.aclose()
    await _mcp_groups_gone(peer, ready)
    assert peer.lost == [] and peer.failures == []


async def test_mcp_stdio_accepts_a_200_kib_line(mcp_stdio_peer):
    """L3：超過 asyncio 預設的 64 KiB，必須仍是完整的一次工具回應。"""
    peer = mcp_stdio_peer
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    assert await _mcp_tool_text(client, "large_line", {"size": 200 * 1024}) == "x" * (200 * 1024)
    await owner.aclose()
    await _mcp_groups_gone(peer, await _mcp_ready(peer))


async def test_mcp_stdio_overlong_line_loses_connection_and_reaps(mcp_stdio_peer, monkeypatch):
    """L3：上限 256 KiB 時拒收 1 MiB，失聯原因精確且不留行程。"""
    peer = mcp_stdio_peer
    monkeypatch.setattr(mcp_stdio, "MCP_STDIO_LINE_LIMIT", 256 * 1024)
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    with pytest.raises(MCPError):
        await asyncio.wait_for(client.call_tool("large_line", {"size": 1024 * 1024}), 5)
    await asyncio.wait_for(peer.lost_event.wait(), 10)
    await asyncio.wait_for(owner.task, 10)
    assert len(peer.lost) == 1
    assert isinstance(peer.lost[0][1], mcp_stdio.McpStdoutLineTooLong)
    await _mcp_groups_gone(peer, await _mcp_ready(peer))


async def test_mcp_stdio_eof_notifies_owner_once(mcp_stdio_peer, mcp_no_unretrieved):
    """L4：只驗 transport → owner；manager hook 與重連由後續接線驗證。"""
    peer = mcp_stdio_peer
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    ready = await _mcp_ready(peer)
    async with asyncio.timeout(10):
        with pytest.raises(MCPError):
            await client.call_tool("exit_now")
        await peer.lost_event.wait()
        await owner.task
    assert peer.lost[0][0] is owner and len(peer.lost) == 1
    assert isinstance(peer.lost[0][1], mcp_stdio.McpServerExited)
    assert not owner.alive and asyncio.current_task().cancelling() == 0
    with pytest.raises(MCPError):
        await asyncio.wait_for(client.list_tools(), 10)
    await owner.aclose()
    await _mcp_groups_gone(peer, ready)
    assert len(peer.lost) == 1


@pytest.mark.parametrize("closing", ["handshake-cancel", "graceful", "owner-deadline"])
async def test_mcp_stdio_close_reaps_stubborn_descendants(mcp_stdio_peer, monkeypatch, closing):
    """L5：同群組孫行程忽略 SIGTERM、不讀 stdin；三條收尾路都不得留下任何群組成員。"""
    peer = mcp_stdio_peer
    owner = await peer.owner_for(
        "hang" if closing == "handshake-cancel" else "stubborn",
        stall_close=closing == "owner-deadline",
    )
    starting = asyncio.create_task(owner.start())
    try:
        ready = await _mcp_ready(peer)
        members = _mcp_group_members(ready["pgid"])
        assert ready["pgid"] == ready["pid"]
        assert ready["child"] in members and ready["pid"] in members
        if closing == "handshake-cancel":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(starting, 0.5)
        else:
            client = await asyncio.wait_for(starting, 20)
            assert await _mcp_tool_text(client, "p3_ping") == "pong"
            if closing == "owner-deadline":
                monkeypatch.setattr(mcp_owner, "OWNER_CLOSE_SECONDS", 0.1)
            await asyncio.wait_for(owner.aclose(), 6)
            if closing == "owner-deadline":
                assert peer.close_stalled.is_set() and owner._hard_closed
        assert owner.task.done() and not owner.task.cancelled()
        assert owner.task.exception() is None and peer.lost == []
        await _mcp_groups_gone(peer, ready)
    finally:
        if not starting.done():
            starting.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await starting


async def test_mcp_stdio_drains_stderr_past_the_reader_buffer_before_discovery(mcp_stdio_peer):
    """L6：伺服器先填滿 stderr pipe，仍須在期限內完成握手及工具呼叫。"""
    # The stub's stderr must outgrow the reader buffer, or a missing drain would not show.
    assert 2 * mcp_stdio.MCP_STDIO_LINE_LIMIT < 40 * 1024 * 1024
    peer = mcp_stdio_peer
    owner = await peer.owner_for("stderr")
    client = await asyncio.wait_for(owner.start(), 20)
    assert await _mcp_tool_text(client, "p3_ping") == "pong"
    await owner.aclose()
    await _mcp_groups_gone(peer, await _mcp_ready(peer))


async def test_mcp_stdio_invalid_exec_env_reports_failure_with_bounded_retry(mcp_stdio_peer):
    """L7：啟動器拒絕壞名稱，只有外層 with_retry 會再試（初次＋兩次重試）。"""
    peer = mcp_stdio_peer
    first = await peer.owner_for(invalid_env=True)

    @with_retry(max_retries=2, base_delay=0.01)
    async def connect_with_retry():
        owner = first if not peer.spawned else peer.new_owner()
        try:
            return await owner.start()
        finally:
            await owner.aclose()

    with pytest.raises((ConnectionError, MCPError, ExceptionGroup)) as caught:
        await asyncio.wait_for(connect_with_retry(), 20)
    assert isinstance(mcp_owner.root_cause(caught.value), (ConnectionError, MCPError))
    expected = "spec_invalid" if peer.route == "landlock" else "executor_rejected"
    assert peer.failures == [expected] * 3
    assert len(peer.spawned) == len(peer.owners) == 3 and peer.lost == []
    assert all(owner.task.done() and owner.task.exception() is None for owner in peer.owners)
    assert not peer.ready_path.exists()
    await _mcp_groups_gone(peer)


async def test_mcp_stdio_target_keeps_launcher_pdeathsig(mcp_stdio_peer):
    """L8：executor 測到的是遠端啟動器的 PDEATHSIG，並未量到 relay。"""
    peer = mcp_stdio_peer
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    report = json.loads(await _mcp_tool_text(client, "isolation_probe"))
    assert report["pdeathsig"] == mcp_stdio.SIGKILL
    await owner.aclose()
    await _mcp_groups_gone(peer, await _mcp_ready(peer))


@pytest.mark.parametrize("mcp_stdio_peer", ["landlock"], indirect=True)
async def test_mcp_manager_eof_hook_reconnects_real_process(
    mcp_stdio_peer, monkeypatch, mcp_no_unretrieved,
):
    """L10：真 stdio EOF → owner → hook → 新行程；舊群組回收且不繼承請求 context。"""
    peer = mcp_stdio_peer
    await peer.owner_for()
    registry = ProtocolRegistry("__no_linux_mcp_config__.json")
    registry.servers = {"stdio-probe": peer.config}
    manager = mcp_manager.ProtocolManager(
        registry, launcher=peer.planner, launch_book=peer.book, monitor=peer.monitor,
        isolation_reader=_McpStdioRecords,
    )
    marker = ContextVar("linux_manager_creator", default=None)
    contexts, hooks = [], []
    connect = mcp_client.connect_mcp_server
    hook = manager._connection_lost_hook

    async def tracked_connect(*args, **kwargs):
        contexts.append(marker.get())
        return await connect(*args, **kwargs)

    def tracked_hook(*args):
        hooks.append(args)
        hook(*args)

    monkeypatch.setattr(mcp_manager, "connect_mcp_server", tracked_connect)
    monkeypatch.setattr(manager, "_connection_lost_hook", tracked_hook)
    monkeypatch.setattr(mcp_manager, "MCP_LOST_RELAUNCH_DELAY_SECONDS", 0.01)
    token = marker.set("request-secret")
    try:
        assert await manager.add_server("stdio-probe", peer.config)
        old = manager.handle_pool.get(manager.handle_key_for("stdio-probe", None))
        ready = await _mcp_ready(peer)
        old_processes = list(peer.spawned)
        with pytest.raises(MCPError):
            await asyncio.wait_for(old.session.call_tool("exit_now"), 10)
        async with asyncio.timeout(20):
            while True:
                current = manager.handle_pool.get(manager.handle_key_for("stdio-probe", None))
                if current is not None and current.stack is not old.stack and current.stack.alive:
                    break
                await asyncio.sleep(0.01)
        report = json.loads(await _mcp_tool_text(current.session, "isolation_probe"))
        assert report["pid"] != ready["pid"]
        assert contexts == ["request-secret", None] and len(hooks) == 1
        assert hooks[0][2] is old.stack and old.stack.task.done()
        assert isinstance(hooks[0][1], mcp_stdio.McpServerExited)
        assert asyncio.current_task().cancelling() == 0
        await _mcp_groups_gone(SimpleNamespace(spawned=old_processes), ready)
    finally:
        marker.reset(token)
        await manager.shutdown()


@pytest.mark.parametrize("behavior", ["ok", "error", "marker", "no_run", "quota"])
async def test_evolution_protocol_over_real_isolation(mcp_stdio_peer, monkeypatch, behavior):
    """真 runner 驗證，拒絕標記冒充、無服務與工具錯誤，保留寫入配額。"""
    peer = mcp_stdio_peer
    skill = peer.code.name
    code = '''from mcp.server import MCPServer
mcp = MCPServer("evolution-protocol-test")
@mcp.tool()
def ping() -> str:
    return "pong"
if __name__ == "__main__":
    mcp.run(transport="stdio")
'''
    if behavior in {"error", "marker"}:
        code = code.replace('return "pong"', 'raise RuntimeError("tool failed")')
    if behavior == "marker":
        code = 'print(\'{"status": "success"}\', flush=True)\n' + code
    if behavior == "no_run":
        code = code.replace('mcp.run(transport="stdio")', 'pass')
    if behavior == "quota":
        code = code.replace('return "pong"', '''for index in range(2):
        with open(f"quota-{index}.txt", "w") as stream:
            stream.write("x" * (700 * 1024))
    return "quota escaped"''')
    (peer.code / "server.py").write_text(code, encoding="utf-8")
    point_zone_roots(monkeypatch, peer.layout.data_root, peer.layout.state_root)
    build_skill_venv(evolution._venv_python(skill))  # 技能資料夾裡真的 venv python（T3）
    monkeypatch.setattr(evolution_command, "current_layout", lambda: peer.layout)
    monkeypatch.setattr(evolution_command, "isolation_monitor", peer.monitor)
    monkeypatch.setattr(evolution_command, "runner_broker", peer.broker)
    monkeypatch.setattr(evolution, "isolation_monitor", peer.monitor)
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": {
        "execution_timeout_sec": 10, "max_storage_mb": 1, "max_memory_mb": 512,
    }})
    outcome = await evolution._verify_mcp(
        skill, "ping", {}, _evolution_grant=ExecutionGrant(peer.route, executor_eligible=True),
    )
    assert peer.spawned, outcome.text
    assert outcome.passed is (behavior == "ok"), outcome.text
    if behavior in {"error", "quota"}:
        assert '"isError": true' in outcome.text, outcome.text
        assert ("tool failed" if behavior == "error" else "Quota Exceeded") in outcome.text
    if behavior == "marker":
        assert "McpStdoutLineInvalid" in outcome.text or '"isError": true' in outcome.text
    if behavior == "no_run":
        assert "MCPError" in outcome.text
        assert "Traceback" not in outcome.text
    if behavior == "ok":
        assert "pong" in outcome.text
    assert not (peer.code / "test_wrapper.py").exists()
    await _mcp_groups_gone(peer)


async def test_mcp_stdio_relative_script_requires_an_absolute_target(mcp_stdio_peer):
    peer = mcp_stdio_peer
    owner = await peer.owner_for(relative=True)
    with pytest.raises((ConnectionError, MCPError, ExceptionGroup)):
        await asyncio.wait_for(owner.start(), 20)
    assert not peer.ready_path.exists()
    await owner.aclose()
    await _mcp_groups_gone(peer)
    owner = await peer.owner_for()
    client = await asyncio.wait_for(owner.start(), 20)
    ready = await _mcp_ready(peer)
    assert await _mcp_tool_text(client, 'p3_ping') == 'pong'
    await owner.aclose()
    await _mcp_groups_gone(peer, ready)


async def test_executor_reaps_blocked_stdin_after_relay_disconnect(executor):
    server, started = await executor.start()
    assert started, server.problem
    code = executor.dirs.mcp / 'blocked_stdin'
    code.mkdir()
    source = (
        "import os, sys, time\nsys.stdin.readline()\nchild = os.fork()\n"
        "if child == 0:\n    time.sleep(60)\nelse:\n"
        "    print(os.getpid(), flush=True)\n    time.sleep(60)\n"
    )
    start = _mcp_exec_start(server, code, [*PY, source])
    request_r, request_w = os.pipe()
    status_r, status_w = os.pipe()
    _write_all(request_w, json.dumps({'parent_pid': os.getpid(), 'start': start}).encode())
    proc = await spawn_exec(
        sys.executable, '-I', '-S', str(RELAY), '--ipc-dir', str(executor.dirs.ipc),
        '--request-fd', str(request_r), '--status-fd', str(status_w),
        '--boot-id', server.boot_id, pass_fds=(request_r, status_w),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    executor.children.add(proc.pid)
    os.close(request_r)
    os.close(status_w)
    drain = None
    try:
        proc.stdin.write(b'handshake\n')
        await proc.stdin.drain()
        pid = int(await asyncio.wait_for(proc.stdout.readline(), 10))
        proc.stdin.write(b'x' * (512 * 1024))
        drain = asyncio.create_task(proc.stdin.drain())
        await asyncio.sleep(0.1)
        assert proc.returncode is None and server._running
        proc.kill()
        await asyncio.wait_for(proc.wait(), 2)
        async with asyncio.timeout(2):
            while server._running or server._direct or _mcp_group_members(pid):
                await asyncio.sleep(0.01)
    finally:
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        os.close(status_r)
