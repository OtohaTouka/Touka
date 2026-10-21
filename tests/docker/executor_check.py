"""iso-c2 C2-2：iso-client（proxy 的形狀）經另一個容器裡真的執行器做端到端檢查.

只能由 ``tests/docker/sandboxed.py`` 先開 smoke 沙盒再執行（沒有沙盒就拒絕）；compose 見
``tests/docker/isolation.compose.yml``。proxy 這邊全部用映像裡的程式：真 monitor（真健康檢查，
READY 要和 proxy 的預期完全相同）、真 ``RunnerBroker``／``ExecutorRunner``／relay；代理人指令在
執行器容器裡以 Landlock 隔離執行。每一項寫一行 JSON（``check``、``ok`` 與細節）到 stdout；
全部通過才 exit 0。

沙盒會封鎖本行程連外網（audit hook），所以 proxy 這邊「連得到 Touka 內部服務」的對照在子行程做。
需要從主機控制 Docker 的項目（執行器重啟後 boot_id 換掉、升級順序、OOM）不在這裡，交 U2j 手動做。
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from src.models.identity import AgentIdentity
from src.sandbox_kernel.framing import SOCKET_NAME, TOKEN_NAME
from src.sandbox_kernel.spec import DEFAULT_BLOCK_PORTS
from src.services.execution_broker import spawn_exec
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.profiles import build_agent_command_profile, current_layout
from src.services.isolation.runners import ExecRequest, ProfileSource, RunnerBroker
from src.services.isolation.status import IsolationMonitor

from tests import network_guard

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from src.services.isolation.profiles import SandboxLayout, SandboxProfile

OWNER, OTHER = "Alice", "Bob"
OTHER_CONTENT = "only-bob-may-read-this"
AGENT_PATH = "/usr/local/bin:/usr/bin:/bin"
FREE_PORT = 18081  # 不在封鎖埠裡的對照埠（檢查時會再核對一次）
COMMAND_SECONDS = 60.0
DISCONNECT_AFTER_SECONDS = 3.0  # 本機逾時：殺掉 relay，執行器要收掉遠端
STOP_WITHIN_SECONDS = 2.0  # 遠端要在這段時間內停下
MAX_SECONDS = 2  # 執行器端的上限（START 的 max_seconds）

_CONTROL_PROBE = """
import json, socket, sys
token, sock = sys.argv[1], sys.argv[2]
result = {}
try:
    with open(token) as handle:
        handle.read()
    result["token"] = "readable"
except OSError as exc:
    result["token"] = type(exc).__name__
try:
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(3)
    client.connect(sock)
    client.sendall(b"\\0" * 5)
    result["socket"] = "closed" if client.recv(1) == b"" else "answered"
except OSError as exc:
    result["socket"] = type(exc).__name__
print(json.dumps(result))
"""

_REACH_PROBE = """
import json, socket, sys
name, ip, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
result = {}
try:
    socket.getaddrinfo(name, port)
    result["name"] = "resolved"
except OSError as exc:
    result["name"] = type(exc).__name__
try:
    socket.create_connection((ip, port), timeout=3).close()
    result["ip"] = "connected"
except OSError as exc:
    result["ip"] = type(exc).__name__
print(json.dumps(result))
"""

_PORT_PROBE = """
import json, socket, sys
def attempt(action, port):
    sock = socket.socket()
    try:
        if action == "connect":
            sock.settimeout(3)
            sock.connect(("127.0.0.1", port))
        else:
            sock.bind(("0.0.0.0", port))
        return "ok"
    except OSError as exc:
        return exc.errno
    finally:
        sock.close()
ports = [int(p) for p in sys.argv[1:]]
print(json.dumps({f"{a}:{p}": attempt(a, p) for a in ("connect", "bind") for p in ports}))
"""

# proxy 這邊的對照（子行程，不受本行程的外網封鎖）：名稱解析得到、HTTP 連得到
_CLIENT_REACH_PROBE = """
import socket, sys, urllib.request
name, port = sys.argv[1], int(sys.argv[2])
ip = socket.gethostbyname(name)
urllib.request.urlopen(f"http://{name}:{port}/", timeout=5).read(1)
print(ip)
"""

_COUNTER = "i=0; while :; do i=$((i+1)); echo $i > {name}; sleep 0.2; done"


@dataclass(frozen=True)
class Outcome:
    """一條代理人指令的結果；``code`` 是 None 表示本機逾時、relay 被殺掉。"""

    code: int | None
    output: str
    failure: str | None
    seconds: float


@dataclass(frozen=True)
class Session:
    """proxy 這邊的接線：版面、monitor、broker、grant 與執行器 READY 的 state_root。"""

    layout: SandboxLayout
    monitor: IsolationMonitor
    broker: RunnerBroker
    grant: ExecutionGrant
    state_root: str

    def drive(self, owner: str) -> Path:
        """代理人的資料夾（proxy 與執行器掛的是同一個實體資料夾）。"""
        return self.layout.data_root / "personal_drive" / owner


def emit(check: str, ok: bool, **detail: object) -> bool:
    """寫一行結果；回傳 ``ok``。"""
    sys.stdout.write(json.dumps({"check": check, "ok": ok, **detail}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return ok


async def run_agent(
    session: Session,
    argv: tuple[str, ...],
    *,
    timeout: float = COMMAND_SECONDS,
    max_seconds: int | None = None,
) -> Outcome:
    """照正式的路徑跑一條代理人指令：acquire → plan → spawn relay → release → 讀狀態."""
    identity = AgentIdentity.for_role(OWNER, "Owner", capabilities=[])

    async def build(location: str, executor_state: str | None) -> SandboxProfile:
        return await build_agent_command_profile(
            identity, session.drive(OWNER), layout=session.layout, location=location,
            block_ports=session.monitor.block_ports(location), executor_state=executor_state,
        )

    acquired = await session.broker.acquire(session.grant, ProfileSource(build, None))
    request = ExecRequest(
        argv=argv, env={"PATH": AGENT_PATH}, profile=acquired.profile, max_seconds=max_seconds
    )
    plan = acquired.runner.plan(request)
    started = time.monotonic()
    try:
        proc = await spawn_exec(
            *plan.argv, cwd=plan.cwd, env=plan.env, pass_fds=plan.pass_fds, close_fds=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, new_process_group=True,
        )
    finally:
        plan.release()
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)  # 同正式的終端：殺掉 relay 的整個群組
        await proc.wait()
        return Outcome(None, "", None, time.monotonic() - started)
    status = plan.status.read() if plan.status is not None else b""
    failure = acquired.runner.failure(status)
    return Outcome(
        proc.returncode, out.decode("utf-8", "replace"), failure.code if failure else None,
        time.monotonic() - started,
    )


def _python(code: str, *args: str) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", code, *args)


def _last_json(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}


async def _frozen(path: Path) -> bool:
    """計數檔在 ``STOP_WITHIN_SECONDS`` 之後不再變（遠端那條迴圈已經停了）."""
    await asyncio.sleep(STOP_WITHIN_SECONDS)
    first = path.read_text(encoding="utf-8")
    await asyncio.sleep(1.0)
    return path.read_text(encoding="utf-8") == first


async def check_identity(session: Session) -> bool:
    script = 'id -u; printf "%s\\n" "$HOME"; pwd; echo "exec=${TOUKA_EXEC_ID:+yes}"'
    outcome = await run_agent(session, ("sh", "-c", script))
    home = str(PurePosixPath(session.state_root, "agents", OWNER, "home"))
    expected = ["1001", home, str(session.drive(OWNER)), "exec=yes"]
    lines = outcome.output.split()
    return emit("uid_home_cwd", outcome.code == 0 and lines == expected, lines=lines)


async def check_control_channel(session: Session, ipc: Path) -> bool:
    argv = _python(_CONTROL_PROBE, str(ipc / TOKEN_NAME), str(ipc / SOCKET_NAME))
    result = _last_json((await run_agent(session, argv)).output)
    ok = result.get("token") not in {None, "readable"}
    ok = ok and result.get("socket") not in {None, "answered"}
    return emit("control_channel_hidden", ok, **result)


async def check_other_agent(session: Session) -> bool:
    outcome = await run_agent(session, ("sh", "-c", f"cat ../{OTHER}/secret.txt; echo own > own"))
    own = (session.drive(OWNER) / "own").read_text(encoding="utf-8").strip()
    ok = OTHER_CONTENT not in outcome.output and own == "own"
    return emit("other_agent_hidden", ok, own_file=own)


async def check_internal(session: Session, name: str, port: int) -> bool:
    probe = await spawn_exec(
        sys.executable, "-I", "-c", _CLIENT_REACH_PROBE, name, str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(probe.communicate(), COMMAND_SECONDS)
    ip = out.decode().strip()
    if probe.returncode != 0 or not ip:
        return emit("internal_unreachable", False, control=err.decode()[-300:])
    outcome = await run_agent(session, _python(_REACH_PROBE, name, ip, str(port)))
    result = _last_json(outcome.output)
    ok = result.get("name") not in {None, "resolved"}
    ok = ok and result.get("ip") not in {None, "connected"}
    return emit("internal_unreachable", ok, client_reaches=ip, **result)


async def check_block_ports(session: Session) -> bool:
    blocked = max(DEFAULT_BLOCK_PORTS)
    if FREE_PORT in session.monitor.block_ports("executor"):
        return emit("block_ports", False, error="the control port is blocked")
    argv = _python(_PORT_PROBE, str(blocked), str(FREE_PORT))
    result = _last_json((await run_agent(session, argv)).output)
    expected = {
        f"connect:{blocked}": errno.EACCES, f"bind:{blocked}": errno.EACCES,
        f"connect:{FREE_PORT}": errno.ECONNREFUSED, f"bind:{FREE_PORT}": "ok",
    }
    return emit("block_ports", result == expected, **result)


async def check_docker_socket(session: Session) -> bool:
    script = "test ! -e /var/run/docker.sock && echo absent"
    outcome = await run_agent(session, ("sh", "-c", script))
    return emit("no_docker_socket", outcome.output.strip() == "absent")


async def check_disconnect(session: Session) -> bool:
    beat = session.drive(OWNER) / "beat"
    script = _COUNTER.format(name=beat.name)
    outcome = await run_agent(session, ("sh", "-c", script), timeout=DISCONNECT_AFTER_SECONDS)
    ok = outcome.code is None and beat.exists() and await _frozen(beat)
    return emit("relay_killed_stops_remote", ok, killed=outcome.code is None)


async def check_max_seconds(session: Session) -> bool:
    argv = ("sh", "-c", "sleep 20; echo late")
    outcome = await run_agent(session, argv, max_seconds=MAX_SECONDS)
    ok = outcome.code not in {None, 0} and "late" not in outcome.output and outcome.seconds < 15
    return emit("max_seconds", ok, code=outcome.code, seconds=round(outcome.seconds, 1))


async def check_orphans(session: Session) -> bool:
    orphan = session.drive(OWNER) / "orphan"
    loop = _COUNTER.format(name=orphan.name)
    script = f"setsid sh -c '{loop}' </dev/null >/dev/null 2>&1 & sleep 0.5; echo started"
    outcome = await run_agent(session, ("sh", "-c", script))
    ok = outcome.code == 0 and orphan.exists() and await _frozen(orphan)
    return emit("orphans_reaped", ok, output=outcome.output.strip())


async def check_git(session: Session) -> bool:
    script = "git init -q repo && git -C repo status --short --branch && echo git-ok"
    outcome = await run_agent(session, ("sh", "-c", script))
    ok = outcome.code == 0 and "git-ok" in outcome.output and "dubious" not in outcome.output
    return emit("git_without_dubious_ownership", ok, output=outcome.output[-300:])


def _prepare(session: Session) -> None:
    """測試資料：兩個代理人的資料夾、B 的祕密；清掉上一輪留下的計數檔與 repo."""
    for owner in (OWNER, OTHER):
        session.drive(owner).mkdir(parents=True, exist_ok=True)
    (session.drive(OTHER) / "secret.txt").write_text(OTHER_CONTENT, encoding="utf-8")
    for name in ("beat", "orphan", "own"):
        (session.drive(OWNER) / name).unlink(missing_ok=True)
    shutil.rmtree(session.drive(OWNER) / "repo", ignore_errors=True)


async def connect(args: argparse.Namespace) -> Session | None:
    """接上執行器：版面指向容器裡的資料、設定、kernel、ipc；monitor 要判執行器層."""
    state = Path(tempfile.mkdtemp(prefix="touka_iso_client_st_"))
    layout = replace(
        current_layout(), data_root=args.data, config_dir=args.config, kernel_dir=args.kernel,
        executor_dir=args.ipc, state_root=state, mirror_roots=(), in_container=True,
    )
    monitor = IsolationMonitor(layout_provider=lambda: layout, start_background=False)
    snap = await monitor.ensure_ready()
    ready = snap.executor
    ok = snap.level == "executor" and ready is not None and "tcp" in snap.features
    emit("executor_level", ok, level=snap.level, reason=snap.reason, features=sorted(snap.features))
    if not ok or ready is None:
        return None
    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    grant = ExecutionGrant("executor", executor_eligible=True)
    return Session(layout, monitor, broker, grant, ready.state_root)


async def run_checks(args: argparse.Namespace) -> int:
    session = await connect(args)
    if session is None:
        return 1
    _prepare(session)
    checks: list[Callable[[], Awaitable[bool]]] = [
        lambda: check_identity(session),
        lambda: check_control_channel(session, args.ipc),
        lambda: check_other_agent(session),
        lambda: check_internal(session, args.internal, args.port),
        lambda: check_block_ports(session),
        lambda: check_docker_socket(session),
        lambda: check_disconnect(session),
        lambda: check_max_seconds(session),
        lambda: check_orphans(session),
        lambda: check_git(session),
    ]
    results = []
    for check in checks:
        try:
            results.append(await check())
        except Exception as exc:  # 一項壞掉不擋後面的；照實記下型別與訊息
            results.append(emit("error", False, error=f"{type(exc).__name__}: {exc}"))
    return 0 if all(results) else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--data", "--ipc", "--kernel", "--config"):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument("--internal", required=True, help="Touka 內部服務替身的名稱")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args(argv)


def main() -> int:
    if not network_guard.is_installed():
        sys.stderr.write("run this through tests/docker/sandboxed.py (smoke sandbox first)\n")
        return 2
    return asyncio.run(run_checks(parse_args(sys.argv[1:])))


if __name__ == "__main__":
    sys.exit(main())
