"""執行器 runner 與 acquire 規則（iso-c2 C2-2 U2e；設計 v2 §2.8、§2.4、2.2 版 §2.4）.

- ``ExecutorRunner.plan``：本機 relay 的 argv 只有 ipc 資料夾、兩個 fd 與 boot_id（沒有權杖、沒有
  指令）；請求 ``{parent_pid, start}`` 經繼承的 pipe 傳；START 的 env 先過允許清單（繼承來的
  ``GIT_CONFIG_*`` 拿掉）再套 profile 那組；relay 自己不帶環境；失敗代碼只認狀態 fd。
- ``acquire``：沒有同意時，政策是 executor 就只給執行器（不在範圍或 READY 不見了就拒絕，不退回
  Landlock）；有同意時取「種類不低於卡片、features 包含卡片的」裡面最強的。
全部用替身 monitor 與 tmp_path 的假版面；不 spawn、不連線。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.sandbox_kernel import framing
from src.sandbox_kernel.spec import (
    EXECUTOR_ENV_ALLOWED,
    EXECUTOR_GIT_CONFIG,
    FAILURE_CODES,
    RELAY_FAILURE_CODES,
    kernel_digest,
    parse_spec,
)
from src.services import approval_flow
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import agent_command
from src.services.isolation import policy as policy_module
from src.services.isolation import runners as runners_module
from src.services.isolation.agent_command import PreparedCommand
from src.services.isolation.consent import (
    Consent,
    ConsentKey,
    ConsentLedger,
    ExecutionGrant,
)
from src.services.isolation.executor_client import ExecutorReady, ExecutorRoot
from src.services.isolation.executor_session import MAX_SECONDS, START_KEYS
from src.services.isolation.profiles import (
    PathRule,
    SandboxLayout,
    SandboxProfile,
    launcher_spec_fields,
)
from src.services.isolation.runners import (
    DirectRunner,
    ExecRequest,
    ExecutorRunner,
    IsolationUnavailableError,
    LandlockRunner,
    ProfileSource,
    RunnerBroker,
    executor_max_seconds,
)
from src.services.review import evidence
from src.tools import terminal

from tests.support.isolation import (
    PinnedMonitor,
    fixed_interpreter,
    install_real_broker,
    pinned_snapshot,
)

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
REAL_COMMAND_PROFILE = agent_command.build_command_profile

BOOT = "b" * 32
STATE = "/x/executor_state"
FS_TCP = frozenset({"fs", "tcp"})
FS = frozenset({"fs"})
ALL = frozenset({"fs", "tcp", "ioctl", "signal", "abstract_unix"})


def _layout(tmp_path: Path) -> SandboxLayout:
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    for name in ("launcher.py", "relay.py"):
        (kernel / name).write_text("# kernel\n", encoding="utf-8")
    return SandboxLayout(
        data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy",
        env_file=tmp_path / ".env",
        state_root=tmp_path / "st",
        kernel_dir=kernel,
        executor_dir=tmp_path / "ipc",
        mirror_roots=(),
        in_container=True,
    )


def _ready(**overrides: Any) -> ExecutorReady:
    values: dict[str, Any] = {
        "version": 1, "abi": 7, "enforced": True, "net": None, "features": tuple(sorted(ALL)),
        "roots": (ExecutorRoot("/data/personal_drive", "rw"),), "state_root": STATE,
        "kernel_digest": "k" * 64, "server_digest": "s" * 64, "boot_id": BOOT,
        "block_ports_digest": "d" * 64, "uid": 1001,
    }
    values.update(overrides)
    return ExecutorReady(**values)


def _profile(tmp_path: Path, location: str = "proxy", **overrides: Any) -> SandboxProfile:
    cwd = tmp_path / "data" / "personal_drive" / "Alice"
    home = Path(STATE) / "agents" / "Alice" / "home" if location == "executor" else tmp_path / "h"
    values: dict[str, Any] = {
        "name": "agent_command",
        "cwd": cwd,
        "rules": (PathRule(cwd, "rwx"), PathRule(Path("/usr"), "rox", optional=True)),
        "block_ports": (2375, 2376, 3001, 4000, 8000, 8082, 8083),
        "env_set": {
            "HOME": str(home), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        },
        "owner": "Alice",
    }
    values.update(overrides)
    return SandboxProfile(**values)


class _Source:
    """記錄 acquire 依哪個位置、哪個 state_root 建 profile."""

    def __init__(self, tmp_path: Path, *, issue: str | None = None, ungranted: tuple = ()) -> None:
        self.tmp_path, self.issue, self.ungranted = tmp_path, issue, ungranted
        self.built: list[tuple[str, str | None]] = []

    async def __call__(self, location: str, executor_state: str | None) -> SandboxProfile:
        self.built.append((location, executor_state))
        return _profile(self.tmp_path, location, ungranted=self.ungranted)

    def source(self) -> ProfileSource:
        return ProfileSource(self, executor_issue=self.issue)


def _monitor(
    level: str = "none", *, ready: ExecutorReady | None = None, executor: frozenset | None = None,
    local_status: str = "available", net: str | None = None,
) -> PinnedMonitor:
    snap = pinned_snapshot(level, local_status=local_status, net=net, expected="executor")
    if local_status == "unavailable":
        snap = replace(snap, reason="kernel_not_installed")
    snap = replace(snap, executor=ready, features=executor or snap.features)
    return PinnedMonitor(snap=snap, executor_features=executor)


def _broker(tmp_path: Path, monitor: PinnedMonitor) -> RunnerBroker:
    layout = _layout(tmp_path)
    return RunnerBroker(
        monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter
    )


def _grant(card: str, features: frozenset[str] = frozenset()) -> ExecutionGrant:
    return ExecutionGrant("none", Consent(subject="s", fallback=card, features=features))


def _plan(tmp_path: Path, *, features=FS_TCP, env=None, max_seconds=None, **profile_kw):
    runner = ExecutorRunner(
        _layout(tmp_path).kernel_dir, tmp_path / "ipc", BOOT, features, python=sys.executable
    )
    request = ExecRequest(
        argv=("bash", "-c", "pytest -q"),
        env=env if env is not None else {"PATH": "/usr/bin"},
        profile=_profile(tmp_path, "executor", **profile_kw),
        max_seconds=max_seconds,
    )
    return runner, runner.plan(request)


def _request(plan) -> dict:
    fd = int(plan.argv[plan.argv.index("--request-fd") + 1])
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


# --- ExecutorRunner.plan ----------------------------------------------------------------------


def test_relay_argv_has_only_the_ipc_folder_fds_and_boot_id(tmp_path):
    runner, plan = _plan(tmp_path)
    try:
        relay = str(tmp_path / "kernel" / "relay.py")
        assert plan.argv[:4] == (sys.executable, "-I", "-S", relay)
        options = dict(zip(plan.argv[4::2], plan.argv[5::2], strict=True))
        assert set(options) == {"--ipc-dir", "--request-fd", "--status-fd", "--boot-id"}
        assert (options["--ipc-dir"], options["--boot-id"]) == (str(tmp_path / "ipc"), BOOT)
        fds = (int(options["--request-fd"]), int(options["--status-fd"]))
        assert plan.pass_fds == fds and plan.owned_fds == fds
        assert "pytest -q" not in " ".join(plan.argv)  # 指令只在 START 裡
        assert plan.env == {}  # relay 自己不帶 proxy 的環境
        assert (plan.level, plan.features, plan.preexec_fn) == ("executor", FS_TCP, None)
        assert runner.level == "executor"
    finally:
        plan.release()
        plan.read_status()


def test_request_carries_the_parent_pid_and_a_start_the_server_understands(tmp_path):
    _runner, plan = _plan(tmp_path, max_seconds=60)
    try:
        request = _request(plan)
    finally:
        plan.release()
        plan.read_status()
    assert set(request) == {"parent_pid", "start"} and request["parent_pid"] == os.getpid()
    start = request["start"]
    assert set(start) == START_KEYS
    profile = _profile(tmp_path, "executor")
    assert start["argv"] == ["bash", "-c", "pytest -q"]
    assert (start["cwd"], start["kind"], start["owner"]) == (str(profile.cwd), "agents", "Alice")
    assert (start["io"], start["max_seconds"]) == ("merged", 60)
    spec = start["spec"]
    assert spec == {**launcher_spec_fields(profile), "parent_pid": os.getpid(), "net_strict": True}
    if os.name != "nt":  # Windows 的 tmp 路徑帶磁碟代號，不是 spec 收的 POSIX 形狀
        assert parse_spec(json.dumps(spec).encode("utf-8")).block_ports == profile.block_ports


def test_start_env_is_the_allowlist_plus_the_profile_git_config(tmp_path):
    inherited = {
        "PATH": "/usr/bin", "LANG": "C.UTF-8", "OPENAI_API_KEY": "sk-secret",
        "HTTP_PROXY": "http://user:pw@proxy:3128", "PIP_INDEX_URL": "https://u:p@pypi/simple",
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "core.sshCommand",
        "GIT_CONFIG_VALUE_1": "evil", "TOUKA_EXEC_ID": "forged", "LD_PRELOAD": "/x.so",
    }
    _runner, plan = _plan(tmp_path, env=inherited)
    try:
        env = _request(plan)["start"]["env"]
    finally:
        plan.release()
        plan.read_status()
    git = {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}
    assert git == dict(EXECUTOR_GIT_CONFIG)
    assert set(env) - set(git) <= EXECUTOR_ENV_ALLOWED
    assert (env["PATH"], env["LANG"]) == ("/usr/bin", "C.UTF-8")
    assert not {"OPENAI_API_KEY", "HTTP_PROXY", "PIP_INDEX_URL", "TOUKA_EXEC_ID"} & set(env)
    assert env["HOME"].endswith("home")


@pytest.mark.parametrize(("features", "strict"), [(FS_TCP, True), (FS, False)])
def test_executor_spec_is_net_strict_exactly_when_it_has_tcp(tmp_path, features, strict):
    _runner, plan = _plan(tmp_path, features=features)
    try:
        assert _request(plan)["start"]["spec"]["net_strict"] is strict
    finally:
        plan.release()
        plan.read_status()


@pytest.mark.parametrize(
    ("local", "expected"), [(None, MAX_SECONDS), (0.05, 31), (30, 60), (600, 630), (5000, 900)]
)
def test_max_seconds_is_the_local_timeout_plus_30_capped_at_900(local, expected):
    assert executor_max_seconds(local) == expected


def test_start_without_max_seconds_uses_the_server_limit(tmp_path):
    _runner, plan = _plan(tmp_path)
    try:
        assert _request(plan)["start"]["max_seconds"] == MAX_SECONDS
    finally:
        plan.release()
        plan.read_status()


def test_oversized_start_is_refused_before_spawning(tmp_path, monkeypatch):
    """START 超過 frame 上限：proxy 先拒絕（relay 也送不出去），不開 pipe、不 spawn."""
    assert runners_module.MAX_START_BYTES == framing.MAX_START_BYTES
    monkeypatch.setattr(runners_module, "MAX_START_BYTES", 1024)
    runner = ExecutorRunner(tmp_path, tmp_path / "ipc", BOOT, FS_TCP, python=sys.executable)
    request = ExecRequest(argv=("x" * 1500,), env={}, profile=_profile(tmp_path, "executor"))
    with pytest.raises(IsolationUnavailableError) as raised:
        runner.plan(request)
    assert raised.value.reason == "spec_too_large"


def test_profile_without_an_executor_kind_or_owner_is_refused(tmp_path):
    runner = ExecutorRunner(tmp_path, tmp_path / "ipc", BOOT, FS_TCP, python=sys.executable)
    for profile in (_profile(tmp_path, "executor", name="unbound"),
                    _profile(tmp_path, "executor", owner=None)):
        with pytest.raises(IsolationUnavailableError):
            runner.plan(ExecRequest(argv=("true",), env={}, profile=profile))


def test_executor_failure_codes_come_only_from_the_status_fd(tmp_path):
    runner = ExecutorRunner(tmp_path, tmp_path / "ipc", BOOT, FS_TCP, python=sys.executable)
    for code in sorted(RELAY_FAILURE_CODES | FAILURE_CODES):
        assert runner.failure(f"{code}\n".encode()).code == code
    for forged in (b"", b"made_up\n", b"TOUKA_SANDBOX_ERROR:path_open\n"):
        assert runner.failure(forged) is None


# --- acquire：沒有同意 ----------------------------------------------------------------------------


async def test_executor_policy_gets_the_executor_runner(tmp_path):
    """政策是 executor：在執行器位置建 profile（READY 的 state_root），relay 帶 READY 的 boot_id."""
    monitor = _monitor("executor", ready=_ready(), executor=ALL)
    source = _Source(tmp_path)
    acquired = await _broker(tmp_path, monitor).acquire(ExecutionGrant("executor"), source.source())
    assert isinstance(acquired.runner, ExecutorRunner)
    assert source.built == [("executor", STATE)]
    plan = acquired.runner.plan(ExecRequest(argv=("true",), env={}, profile=acquired.profile))
    plan.release()
    plan.read_status()
    assert plan.argv[plan.argv.index("--boot-id") + 1] == BOOT
    assert plan.argv[plan.argv.index("--ipc-dir") + 1] == str(tmp_path / "ipc")
    assert plan.features == ALL


@pytest.mark.parametrize("case", ["out_of_scope", "ready_gone"])
async def test_executor_policy_never_falls_back_to_landlock(tmp_path, case):
    ready = None if case == "ready_gone" else _ready()
    monitor = _monitor("executor", ready=ready, executor=ALL)
    source = _Source(tmp_path, issue="project_not_in_executor" if case == "out_of_scope" else None)
    with pytest.raises(IsolationUnavailableError) as raised:
        await _broker(tmp_path, monitor).acquire(ExecutionGrant("executor"), source.source())
    if case == "out_of_scope":
        assert raised.value.reason == "project_not_in_executor"
    assert source.built == []


async def test_executor_policy_after_the_level_dropped_is_refused(tmp_path):
    monitor = _monitor("none", ready=_ready(), executor=FS)
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire(
            ExecutionGrant("executor"), _Source(tmp_path).source()
        )


# --- acquire：人核准的降級（種類不低於卡片、features 包含卡片的） --------------------------------


async def test_card_executor_runs_in_the_executor_without_tcp(tmp_path):
    monitor = _monitor(ready=_ready(net="net_mptcp"), executor=ALL - {"tcp"})
    source = _Source(tmp_path)
    acquired = await _broker(tmp_path, monitor).acquire(_grant("executor", FS), source.source())
    assert isinstance(acquired.runner, ExecutorRunner)
    assert source.built == [("executor", STATE)]
    plan = acquired.runner.plan(ExecRequest(argv=("true",), env={}, profile=acquired.profile))
    try:
        assert plan.features == ALL - {"tcp"}
        assert _request(plan)["start"]["spec"]["net_strict"] is False
    finally:
        plan.release()
        plan.read_status()


@pytest.mark.parametrize("case", ["executor_down", "out_of_scope"])
async def test_card_executor_never_falls_back_to_landlock(tmp_path, case):
    ready, executor = (None, None) if case == "executor_down" else (_ready(), ALL)
    monitor = _monitor(ready=ready, executor=executor)
    issue = "agent_dir_not_in_executor" if case == "out_of_scope" else None
    source = _Source(tmp_path, issue=issue)
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire(_grant("executor", FS), source.source())
    assert source.built == []


async def test_card_landlock_prefers_the_executor_when_it_qualifies(tmp_path):
    monitor = _monitor(ready=_ready(), executor=ALL)
    source = _Source(tmp_path)
    acquired = await _broker(tmp_path, monitor).acquire(_grant("landlock", FS_TCP), source.source())
    assert isinstance(acquired.runner, ExecutorRunner)


async def test_executor_without_the_card_features_is_skipped_for_landlock(tmp_path):
    """卡片寫的 features 有 tcp、執行器只差網路設定（沒有 tcp）：改用本機 Landlock（有 tcp）."""
    monitor = _monitor(ready=_ready(net="net_mptcp"), executor=ALL - {"tcp"})
    source = _Source(tmp_path)
    acquired = await _broker(tmp_path, monitor).acquire(_grant("landlock", FS_TCP), source.source())
    assert isinstance(acquired.runner, LandlockRunner)
    assert source.built == [("proxy", None)]


async def test_card_features_missing_everywhere_is_refused(tmp_path):
    """卡片標示擋得住連線（tcp），當下哪個 runner 都沒有 tcp：拒絕，不默默變弱."""
    monitor = _monitor(net="net_tcp_fastopen")
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire(
            _grant("landlock", FS_TCP), _Source(tmp_path).source()
        )


async def test_card_landlock_out_of_executor_scope_uses_local_landlock(tmp_path):
    monitor = _monitor(ready=_ready(), executor=ALL)
    source = _Source(tmp_path, issue="project_not_in_executor")
    acquired = await _broker(tmp_path, monitor).acquire(_grant("landlock", FS), source.source())
    assert isinstance(acquired.runner, LandlockRunner) and source.built == [("proxy", None)]


@pytest.mark.parametrize(
    ("ready", "local", "issue", "expected"),
    [
        (True, "available", None, ExecutorRunner),
        (True, "available", "project_not_in_executor", LandlockRunner),
        (False, "available", None, LandlockRunner),
        (False, "unavailable", None, DirectRunner),
    ],
)
async def test_card_direct_gets_the_strongest_runner(tmp_path, ready, local, issue, expected):
    monitor = _monitor(
        ready=_ready() if ready else None, executor=ALL if ready else None, local_status=local
    )
    source = _Source(tmp_path, issue=issue)
    acquired = await _broker(tmp_path, monitor).acquire(_grant("direct"), source.source())
    assert type(acquired.runner) is expected


async def test_card_direct_with_an_ungranted_project_stays_direct(tmp_path):
    monitor = _monitor()
    source = _Source(tmp_path, issue="project_not_in_executor", ungranted=("scan_limit",))
    acquired = await _broker(tmp_path, monitor).acquire(_grant("direct"), source.source())
    assert isinstance(acquired.runner, DirectRunner)


# --- 同意帳本記得執行器的降級去處與卡片 features --------------------------------------------------


def test_ledger_records_an_executor_ack_and_the_card_features():
    ledger = ConsentLedger(clock=lambda: 0.0)
    key = ConsentKey("Alice", "s1", "c1")
    ledger.record_policy(key, level="none", subject="sub")
    assert ledger.grant(key, fallback="executor", features=FS)
    grant = ledger.take(key, subject="sub")
    assert grant is not None and grant.consent == Consent("sub", "executor", FS)


# --- 接線：終端經 relay；範圍由 monitor 判定（資料夾、專案） ---------------------------


class _Spawned:
    """``spawn_exec`` 的替身：記下 argv 與 relay 請求，以 ``output``／``code`` 結束."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.output, self.code = b"", 0

    async def __call__(self, *argv: Any, **kwargs: Any) -> Any:
        fd = int(argv[argv.index("--request-fd") + 1])
        self.calls.append((argv, json.loads(os.read(fd, 1 << 20))))
        return _DoneProc(self.output, self.code)


class _DoneProc:
    def __init__(self, output: bytes = b"", code: int = 0) -> None:
        self.stdout, self.returncode = self, code
        self._lines = output.splitlines(keepends=True)
        self._output = output

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, None]:
        return self._output, None


def _terminal_in_the_executor(monkeypatch, tmp_path: Path) -> tuple[PinnedMonitor, _Spawned]:
    layout = replace(_layout(tmp_path), in_container=False)
    drive = layout.data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    snap = replace(pinned_snapshot("executor"), executor=_ready(), features=ALL)
    monitor = install_real_broker(monkeypatch, snap, layout)
    monitor.checked_kernel_digest = kernel_digest(layout.kernel_dir)
    monkeypatch.setattr(agent_command, "build_command_profile", REAL_COMMAND_PROFILE)
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    monkeypatch.setattr(terminal, "_get_agent_drive_path", lambda _identity: drive)
    monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: {"PATH": "/usr/bin"})
    spawned = _Spawned()
    monkeypatch.setattr(terminal, "spawn_exec", spawned)
    monkeypatch.setattr(evidence, "spawn_exec", spawned)
    return monitor, spawned


async def _execute(command: str = "echo hi") -> str:
    return await terminal.execute_command(
        command, ALICE, auth_level="Manual", _hitl_preapproved=True, session_id="s1",
        _call_id="c1", _ctx={"identity": ALICE},
    )


async def test_terminal_runs_through_the_relay_at_the_executor_level(monkeypatch, tmp_path):
    _monitor_double, spawned = _terminal_in_the_executor(monkeypatch, tmp_path)
    assert "[EXIT_CODE: 0]" in await _execute()
    ((argv, request),) = spawned.calls
    assert argv[3].endswith("relay.py") and "echo hi" not in " ".join(argv)
    start = request["start"]
    assert start["argv"][-1] == "echo hi" and (start["kind"], start["owner"]) == ("agents", "Alice")


async def test_terminal_out_of_executor_scope_is_refused_not_downgraded(monkeypatch, tmp_path):
    monitor, spawned = _terminal_in_the_executor(monkeypatch, tmp_path)
    monitor.scope_issue = "project_not_in_executor"
    result = json.loads(await _execute())
    assert (result["reason"], result["isolation_reason"]) == (
        "isolation_unavailable", "project_not_in_executor"
    )
    assert spawned.calls == []



# --- 執行器那邊的時限：本機逾時＋30 秒（設計 §2.2） -------------------------------------------


async def test_terminal_and_verification_tell_the_executor_their_timeout(monkeypatch, tmp_path):
    _monitor_double, spawned = _terminal_in_the_executor(monkeypatch, tmp_path)
    await _execute()  # 前景 30 秒

    async def _allow(_cmd: str) -> None:
        return None

    await evidence.collect_evidence(
        artifact_refs=[], verification_cmd="pytest -q", test_log=None,
        artifact_root=tmp_path / "data" / "personal_drive" / "Alice", command_gate=_allow,
        identity=ALICE, timeout_seconds=45,
    )
    assert [request["start"]["max_seconds"] for _argv, request in spawned.calls] == [60, 75]


# --- 埠提示：代理人自己用封鎖埠起服務失敗時，提示「請換埠」 -----------------------

DENIED = b"PermissionError: [Errno 13] Permission denied\n"


@pytest.mark.parametrize(
    ("command", "output", "code", "hinted"),
    [
        ("python -m http.server 8000", DENIED, 1, True),
        ("node app.js", b"Error: listen EACCES: permission denied 0.0.0.0:8083\n", 1, True),
        ("python -m http.server 9999", DENIED, 1, False),  # 9999 不在 B
        ("python -m http.server 8000", DENIED, 0, False),  # 成功結束
        ("python -m http.server 8000", b"Address already in use\n", 1, False),
    ],
)
async def test_terminal_hints_to_change_a_blocked_port(
    monkeypatch, tmp_path, command, output, code, hinted
):
    _monitor_double, spawned = _terminal_in_the_executor(monkeypatch, tmp_path)
    spawned.output, spawned.code = output, code
    result = await _execute(command)
    assert ("different port" in result) is hinted


@pytest.mark.parametrize(("runner", "hinted"), [("direct", False), ("landlock", True)])
def test_port_hint_only_when_the_command_ran_isolated(tmp_path, runner, hinted):
    """未隔離（none）沒有封鎖埠，失敗不是被擋：不給提示."""
    profile = _profile(tmp_path)
    if runner == "direct":
        prepared = PreparedCommand(DirectRunner(), DirectRunner().plan(_exec(profile)), profile)
    else:
        landlock = LandlockRunner(tmp_path, FS_TCP, python=sys.executable)
        prepared = PreparedCommand(landlock, landlock.plan(_exec(profile)), profile)
        prepared.plan.release()
        prepared.report()
    hint = prepared.port_hint(1, "python -m http.server 8000\nPermission denied")
    assert (hint is not None) is hinted


def _exec(profile: SandboxProfile) -> ExecRequest:
    return ExecRequest(argv=("true",), env={}, profile=profile)



# --- 審批卡：這次在執行器範圍內才寫「執行器」；核准記下卡片的去處與 features ---------------------

CARD_KEY = ConsentKey("Alice", "s1", "c1")


async def _card_fallback(monkeypatch, *, eligible: bool) -> approval_flow.CardIsolation:
    monitor = _monitor(ready=_ready(net="net_mptcp"), executor=ALL - {"tcp"})
    monkeypatch.setattr(approval_flow, "isolation_monitor", monitor)
    approval_flow.consent_ledger.record_policy(
        CARD_KEY, level="none", subject="x", reason="net_mptcp", executor_eligible=eligible
    )
    card = await approval_flow.card_isolation(CARD_KEY, "execute_command")
    assert card is not None
    return card


@pytest.mark.parametrize(("eligible", "fallback"), [(True, "executor"), (False, "landlock")])
async def test_card_fallback_is_the_executor_only_in_its_scope(monkeypatch, eligible, fallback):
    card = await _card_fallback(monkeypatch, eligible=eligible)
    assert card.fallback == fallback


async def test_executor_ack_records_the_card_and_its_features(monkeypatch):
    card = await _card_fallback(monkeypatch, eligible=True)
    payload = {approval_flow.ISOLATION_ACK_FIELD: "executor"}
    assert approval_flow._isolation_verdict(CARD_KEY, card, payload) == "approved"
    grant = approval_flow.consent_ledger.take(CARD_KEY, subject="x")
    assert grant is not None and grant.consent is not None
    assert (grant.consent.fallback, grant.consent.features) == ("executor", card.features)


@pytest.mark.parametrize(
    ("issue", "eligible"), [(None, True), ("agent_dir_not_in_executor", False)]
)
async def test_policy_records_whether_the_call_is_in_the_executor_scope(
    monkeypatch, issue, eligible
):
    """卡片在政策之後才組：範圍在政策時（有身分、有專案根）就記進帳本."""
    monitor = install_isolation_doubles_for(monkeypatch, issue)
    del monitor
    await policy_module.policy_level(
        ALICE, {"identity": ALICE}, "s1", "c1", tool="execute_command", args={"command": "ls"}
    )
    record = approval_flow.consent_ledger.pending(CARD_KEY)
    assert record is not None and record.executor_eligible is eligible


def install_isolation_doubles_for(monkeypatch, issue: str | None) -> PinnedMonitor:
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="net_mptcp"), scope_issue=issue)
    for name in ("snapshot", "ensure_ready", "level_for", "executor_scope_issue"):
        monkeypatch.setattr(policy_module.isolation_monitor, name, getattr(monitor, name))
    return monitor
