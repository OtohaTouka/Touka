"""SandboxRunner 合約與降級順序（iso-c2 U1c）.

runner 只改寫啟動參數；層級 none 沒有人的同意一律拒絕；經人核准後用「當下最強、而且
不弱於卡片所寫」的方式；卡片寫隔離降級而 Landlock 不能用時拒絕，不改成直接執行。
"""

from __future__ import annotations

import configparser
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from src.sandbox_kernel.spec import (
    INTERPRETER_UNTRUSTED,
    SPEC_VERSION,
    InterpreterUntrustedError,
)
from src.services.isolation import runners as runners_module
from src.services.isolation.consent import Consent, ExecutionGrant
from src.services.isolation.executor_client import ExecutorReady
from src.services.isolation.profiles import PathRule, SandboxLayout, SandboxProfile
from src.services.isolation.runners import (
    DirectRunner,
    ExecRequest,
    ExecutorRunner,
    IsolationUnavailableError,
    LandlockRunner,
    ProfileSource,
    RunnerBroker,
    launcher_spec,
    runner_broker,
    spec_pipe,
)
from src.services.isolation.status import IsolationMonitor, isolation_monitor, kernel_digest

from tests.support.isolation import (
    FixedBroker,
    PassthroughRunner,
    PinnedMonitor,
    fixed_interpreter,
    pinned_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_fd(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _layout(tmp_path: Path, *, in_container: bool = True) -> SandboxLayout:
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    return SandboxLayout(
        data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy",
        env_file=tmp_path / ".env",
        state_root=tmp_path / "state",
        kernel_dir=kernel,
        executor_dir=None,
        mirror_roots=(),
        in_container=in_container,
    )


def _profile(tmp_path: Path, **overrides) -> SandboxProfile:
    values = {
        "name": "agent_command",
        "cwd": tmp_path / "agent",
        "rules": (
            PathRule(tmp_path / "agent", "rwx"),
            PathRule(Path("/usr").resolve(), "rox", optional=True),
            PathRule(tmp_path / "pytest.ini", "ro", optional=True, skip_symlink=True),
        ),
        "block_ports": (2375, 8000),
        "env_set": {"HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp")},
        "env_unset": frozenset({"HF_HOME", "PIP_CACHE_DIR"}),
    }
    values.update(overrides)
    return SandboxProfile(**values)


def _request(tmp_path: Path, **overrides) -> ExecRequest:
    values = {
        "argv": ("bash", "-c", "pytest -q"),
        "env": {"PATH": "/usr/bin", "HF_HOME": "x", "HOME": "y", "SECRET_TOKEN": "s3cr3t"},
        "profile": _profile(tmp_path),
    }
    values.update(overrides)
    return ExecRequest(**values)


EXECUTOR_READY = ExecutorReady(
    version=1, abi=7, enforced=True, net=None, features=("fs", "tcp"), roots=(),
    state_root="/x/executor_state", kernel_digest="k" * 64, server_digest="s" * 64,
    boot_id="b" * 32, block_ports_digest="d" * 64, uid=1001,
)
CONSENT_LANDLOCK = Consent(subject="s", fallback="landlock")
CONSENT_DIRECT = Consent(subject="s", fallback="direct")


def _runner(tmp_path: Path, features: frozenset[str], provider=None) -> LandlockRunner:
    return LandlockRunner(_layout(tmp_path).kernel_dir, features, provider, python=sys.executable)


def _broker(tmp_path: Path, monitor: PinnedMonitor, **kwargs) -> RunnerBroker:
    layout = _layout(tmp_path, **kwargs)
    return FixedBroker(
        RunnerBroker(monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter)
    )


# --- runner 只改寫啟動參數 --------------------------------------------------------------


def test_direct_runner_keeps_argv_env_and_preexec(tmp_path):
    def _hook() -> None:
        return None

    request = _request(tmp_path, preexec_fn=_hook)
    plan = DirectRunner().plan(request)
    assert plan.argv == request.argv
    assert plan.env == dict(request.env)
    assert plan.cwd == request.profile.cwd
    assert plan.preexec_fn is _hook
    assert (plan.pass_fds, plan.owned_fds, plan.level) == ((), (), "none")


def test_landlock_runner_prefixes_launcher_with_isolated_python(tmp_path):
    runner = _runner(tmp_path, frozenset({"fs", "tcp"}))
    request = _request(tmp_path)
    plan = runner.plan(request)
    launcher = tmp_path / "kernel" / "launcher.py"
    assert plan.argv[:5] == (sys.executable, "-I", "-S", str(launcher), "--spec-fd")
    spec_fd = int(plan.argv[5])
    assert plan.argv[6] == "--status-fd"
    status_fd = int(plan.argv[7])
    separator = plan.argv.index("--")
    assert separator == 8
    assert plan.argv[separator + 1 :] == request.argv
    assert plan.level == "landlock" and plan.features == frozenset({"fs", "tcp"})
    assert plan.preexec_fn is None
    assert plan.pass_fds == (spec_fd, status_fd) and plan.owned_fds == (spec_fd, status_fd)
    assert plan.cwd == request.profile.cwd
    plan.release()


def test_spec_goes_through_inherited_pipe_not_argv(tmp_path):
    request = _request(tmp_path)
    plan = _runner(tmp_path, frozenset()).plan(request)
    spec_fd = int(plan.argv[5])
    expected = launcher_spec(request.profile, net_strict=False)  # 這個 runner 的 features 沒有 tcp
    assert json.loads(_read_fd(spec_fd)) == json.loads(expected)
    assert not any("rules" in part or "agent" in part for part in plan.argv)
    plan.release()
    assert not _is_open(spec_fd)
    plan.release()  # 可重複呼叫


@pytest.mark.parametrize(
    ("features", "strict"),
    [
        (frozenset({"fs", "tcp", "scope"}), True),  # landlock 層：TCP 規則必須擋得住
        (frozenset({"fs", "scope"}), False),  # 人核准的降級（網路設定不安全或 ABI 3）
        (frozenset(), False),
    ],
)
def test_landlock_runner_spec_is_net_strict_exactly_when_it_has_tcp(tmp_path, features, strict):
    """``net_strict == ("tcp" in runner 的 features)``：降級後的指令不被網路設定擋掉."""
    plan = _runner(tmp_path, features).plan(_request(tmp_path))
    try:
        assert json.loads(_read_fd(int(plan.argv[5])))["net_strict"] is strict
    finally:
        plan.release()


def test_launcher_spec_defaults_to_net_strict():
    profile = SandboxProfile(name="agent_command", cwd=Path(), rules=(), block_ports=())
    assert json.loads(launcher_spec(profile))["net_strict"] is True
    assert json.loads(launcher_spec(profile, net_strict=False))["net_strict"] is False


def test_spec_pipe_refuses_instead_of_blocking():
    with pytest.raises(IsolationUnavailableError) as raised:
        spec_pipe(b"x" * (2 * 1024 * 1024))
    assert raised.value.reason == "spec_too_large"


def test_landlock_runner_rewrites_env_last(tmp_path):
    plan = _runner(tmp_path, frozenset()).plan(_request(tmp_path))
    plan.release()
    assert "HF_HOME" not in plan.env
    assert plan.env["HOME"] == str(tmp_path / "home")
    assert plan.env["TMPDIR"] == str(tmp_path / "tmp")
    assert plan.env["PATH"] == "/usr/bin"


@pytest.mark.parametrize(
    ("profile_value", "inherited", "expected"),
    [
        ("--rootdir=/p", None, "--rootdir=/p"),
        ("--rootdir=/p", "-x -q", "--rootdir=/p -x -q"),
        (None, "-x", "-x"),
    ],
)
def test_landlock_runner_keeps_inherited_pytest_addopts_after_the_profile(
    tmp_path, profile_value, inherited, expected
):
    """profile 的 pytest 選項在前，原本環境裡的接在後面（指令列給的同名選項仍然最後生效）。"""
    env_set = {"HOME": str(tmp_path / "home")}
    if profile_value:
        env_set["PYTEST_ADDOPTS"] = profile_value
    env = {"PATH": "/usr/bin", **({"PYTEST_ADDOPTS": inherited} if inherited else {})}
    request = _request(tmp_path, env=env, profile=_profile(tmp_path, env_set=env_set))
    plan = _runner(tmp_path, frozenset()).plan(request)
    plan.release()
    assert plan.env["PYTEST_ADDOPTS"] == expected


def test_landlock_runner_passes_shared_net_ruleset_fd(tmp_path):
    seen: list[tuple[tuple[int, ...], frozenset[str]]] = []

    def _provider(block_ports: tuple[int, ...], features: frozenset[str]) -> int:
        seen.append((block_ports, features))
        return 42

    runner = _runner(tmp_path, frozenset({"fs", "tcp"}), _provider)
    plan = runner.plan(_request(tmp_path))
    index = plan.argv.index("--net-ruleset-fd")
    assert plan.argv[index + 1] == "42" and index < plan.argv.index("--")
    spec_fd = int(plan.argv[5])
    status_fd = int(plan.argv[plan.argv.index("--status-fd") + 1])
    assert plan.pass_fds == (spec_fd, status_fd, 42) and plan.owned_fds == (spec_fd, status_fd)
    plan.release()
    assert seen == [((2375, 8000), frozenset({"fs", "tcp"}))]


def test_launcher_spec_carries_rules_ports_and_no_env(tmp_path):
    request = _request(tmp_path, profile=_profile(tmp_path, rlimits={"fsize": 1024}))
    spec_text = launcher_spec(request.profile)
    spec = json.loads(spec_text)
    assert set(spec) == {
        "version", "profile", "rules", "block_ports", "rlimits", "pin", "parent_pid", "net_strict"
    }
    assert spec["net_strict"] is True
    assert spec["version"] == SPEC_VERSION
    assert spec["pin"] is True
    assert spec["block_ports"] == [2375, 8000]
    assert spec["rlimits"] == {"fsize": 1024}
    assert {"path": str(tmp_path / "agent"), "access": "rwx", "optional": False,
            "skip_symlink": False} in spec["rules"]
    config_rule = next(r for r in spec["rules"] if r["path"].endswith("pytest.ini"))
    assert config_rule == {"path": str(tmp_path / "pytest.ini"), "access": "ro",
                           "optional": True, "skip_symlink": True}
    assert "s3cr3t" not in spec_text and "HOME" not in spec_text


def test_landlock_runner_failure_comes_only_from_the_status_fd(tmp_path):
    """失敗代碼只認啟動器寫進狀態 fd 的（C2-2 §2.9）；指令輸出裡的標記不再拿來判定."""
    runner = _runner(tmp_path, frozenset())
    assert runner.failure(b"landlock_rule\n").code == "landlock_rule"
    assert runner.failure(b"") is None
    # 只認啟動器的代碼表（FD）：不認得的代碼、多行或標記形式都不算
    for forged in (b"made_up\n", b"path_open\npath_open\n", b"TOUKA_SANDBOX_ERROR:path_open\n"):
        assert runner.failure(forged) is None, forged
    assert DirectRunner().failure(b"landlock_rule\n") is None


def test_landlock_runner_gives_the_launcher_a_status_fd_and_keeps_the_read_end(tmp_path):
    """寫端交給啟動器（spawn 後關掉），讀端留在 proxy：跑完讀一次就關，再讀是空的."""
    plan = _runner(tmp_path, frozenset()).plan(_request(tmp_path))
    status_w = int(plan.argv[plan.argv.index("--status-fd") + 1])
    assert plan.argv.index("--status-fd") < plan.argv.index("--")
    assert status_w in plan.pass_fds and status_w in plan.owned_fds
    os.write(status_w, b"path_open\n")  # 啟動器失敗時寫的那一行
    plan.release()
    assert not _is_open(status_w)
    assert plan.read_status() == b"path_open\n"
    assert plan.read_status() == b""


def test_status_read_while_the_launcher_still_holds_the_fd_does_not_block(tmp_path):
    """寫端還開著（還沒結束）：不等待、當作沒有代碼（逾時與取消時也一定讀得完、關得掉）."""
    plan = _runner(tmp_path, frozenset()).plan(_request(tmp_path))
    seen: list[bytes] = []
    reader = threading.Thread(target=lambda: seen.append(plan.read_status()), daemon=True)
    reader.start()
    reader.join(5)
    blocked = reader.is_alive()
    plan.release()  # 關掉寫端：卡住的讀取也會結束
    reader.join(5)
    assert not blocked and seen == [b""]


# --- acquire：降級要先經人核准 ------------------------------------------------------------


async def test_policy_none_without_consent_is_refused(tmp_path):
    snap = pinned_snapshot("none", reason="kernel_not_installed")
    broker = _broker(tmp_path, PinnedMonitor(snap=snap))
    with pytest.raises(IsolationUnavailableError) as raised:
        await broker.acquire(ExecutionGrant("none"), _profile(tmp_path))
    assert raised.value.reason == "kernel_not_installed"


async def test_policy_landlock_gets_landlock_runner(tmp_path):
    runner = await _broker(tmp_path, PinnedMonitor()).acquire(
        ExecutionGrant("landlock"), _profile(tmp_path)
    )
    assert isinstance(runner, LandlockRunner) and runner.level == "landlock"
    plan = runner.plan(_request(tmp_path))
    plan.release()
    assert plan.argv[3] == str(tmp_path / "kernel" / "launcher.py")


async def test_level_drop_after_policy_is_refused_not_downgraded(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="kernel_outdated"))
    with pytest.raises(IsolationUnavailableError) as raised:
        await _broker(tmp_path, monitor).acquire(ExecutionGrant("landlock"), _profile(tmp_path))
    assert raised.value.reason == "kernel_outdated"


async def test_ungranted_profile_without_consent_is_refused(tmp_path):
    profile = _profile(tmp_path, ungranted=("protected_location",))
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, PinnedMonitor()).acquire(ExecutionGrant("landlock"), profile)


async def test_executor_level_gets_the_executor_runner(tmp_path):
    """C2-2 翻轉（原 ``test_executor_level_has_no_runner_yet``）.

    政策是 executor、這次在執行器範圍內就給執行器 runner；固定 profile（不在範圍內）照樣拒絕、
    不退回 Landlock。
    """
    layout = replace(_layout(tmp_path), executor_dir=tmp_path / "ipc")
    snap = replace(pinned_snapshot("executor"), executor=EXECUTOR_READY)
    broker = RunnerBroker(
        monitor=PinnedMonitor(snap=snap), layout_provider=lambda: layout,
        interpreter=fixed_interpreter,
    )

    async def _executor_profile(location: str, state: str | None) -> SandboxProfile:
        assert (location, state) == ("executor", EXECUTOR_READY.state_root)
        return _profile(tmp_path, owner="Alice")

    source = ProfileSource(_executor_profile, None)
    acquired = await broker.acquire(ExecutionGrant("executor"), source)
    assert isinstance(acquired.runner, ExecutorRunner)
    with pytest.raises(IsolationUnavailableError):
        await broker.acquire(ExecutionGrant("executor"), ProfileSource.fixed(_profile(tmp_path)))


async def test_consent_landlock_uses_local_landlock(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    runner = await _broker(tmp_path, monitor).acquire(
        ExecutionGrant("none", CONSENT_LANDLOCK), _profile(tmp_path)
    )
    assert isinstance(runner, LandlockRunner)


async def test_consent_landlock_refused_when_landlock_gone(tmp_path):
    snap = pinned_snapshot("none", local_status="unavailable", reason="fs_9p")
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, PinnedMonitor(snap=snap)).acquire(
            ExecutionGrant("none", CONSENT_LANDLOCK), _profile(tmp_path)
        )


async def test_consent_landlock_refused_when_state_unconfigured(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none"), state_ok=False)
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire(
            ExecutionGrant("none", CONSENT_LANDLOCK), _profile(tmp_path)
        )


async def test_consent_direct_uses_strongest_available(tmp_path):
    runner = await _broker(tmp_path, PinnedMonitor(snap=pinned_snapshot("none"))).acquire(
        ExecutionGrant("none", CONSENT_DIRECT), _profile(tmp_path)
    )
    assert isinstance(runner, LandlockRunner)


async def test_consent_direct_falls_back_to_direct(tmp_path):
    snap = pinned_snapshot("none", local_status="unavailable", reason="kernel_not_installed")
    runner = await _broker(tmp_path, PinnedMonitor(snap=snap)).acquire(
        ExecutionGrant("none", CONSENT_DIRECT), _profile(tmp_path)
    )
    assert isinstance(runner, DirectRunner)


async def test_consent_direct_with_ungranted_project_stays_direct(tmp_path):
    profile = _profile(tmp_path, ungranted=("scan_limit",))
    runner = await _broker(tmp_path, PinnedMonitor(snap=pinned_snapshot("none"))).acquire(
        ExecutionGrant("none", CONSENT_DIRECT), profile
    )
    assert isinstance(runner, DirectRunner)


async def test_native_kernel_change_since_check_is_refused(tmp_path):
    layout = _layout(tmp_path, in_container=False)
    monitor = PinnedMonitor(checked_kernel_digest=kernel_digest(layout.kernel_dir))
    broker = FixedBroker(RunnerBroker(monitor=monitor, layout_provider=lambda: layout))
    assert isinstance(await broker.acquire(ExecutionGrant("landlock"), _profile(tmp_path)),
                      LandlockRunner)
    (layout.kernel_dir / "launcher.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(IsolationUnavailableError):
        await broker.acquire(ExecutionGrant("landlock"), _profile(tmp_path))
    assert monitor.failures == ["kernel_changed"]


# --- 可信直譯器（C2-2 §2.9） --------------------------------------------------------------


async def test_broker_runs_the_launcher_with_the_trusted_interpreter(tmp_path, monkeypatch):
    """不用 venv 的 ``sys.executable``；容器裡（版面 in_container）整條擁有者鏈要 root."""
    owned: list[bool] = []

    def _trusted(*, require_root_owned: bool) -> str:
        owned.append(require_root_owned)
        return "/trusted/python3"

    monkeypatch.setattr(runners_module, "trusted_interpreter", _trusted)
    for in_container in (True, False):
        layout = _layout(tmp_path, in_container=in_container)
        monitor = PinnedMonitor(checked_kernel_digest=kernel_digest(layout.kernel_dir))
        broker = FixedBroker(
            RunnerBroker(monitor=monitor, layout_provider=lambda layout=layout: layout)
        )
        runner = await broker.acquire(ExecutionGrant("landlock"), _profile(tmp_path))
        plan = runner.plan(_request(tmp_path))
        plan.release()
        plan.read_status()
        assert plan.argv[:3] == ("/trusted/python3", "-I", "-S")
    assert owned == [True, False]


async def test_untrusted_interpreter_is_refused_not_replaced(tmp_path, monkeypatch):
    def _untrusted(*, require_root_owned: bool) -> str:
        raise InterpreterUntrustedError(str(require_root_owned))

    monkeypatch.setattr(runners_module, "trusted_interpreter", _untrusted)
    broker = FixedBroker(
        RunnerBroker(monitor=PinnedMonitor(), layout_provider=lambda: _layout(tmp_path))
    )
    with pytest.raises(IsolationUnavailableError) as raised:
        await broker.acquire(ExecutionGrant("landlock"), _profile(tmp_path))
    assert raised.value.reason == INTERPRETER_UNTRUSTED


# --- real_isolation 只限 Linux 隔離模組 --------------------------------------------------


def test_real_isolation_fixture_only_used_by_linux_module():
    allowed = {
        PROJECT_ROOT / "tests" / "conftest.py",
        PROJECT_ROOT / "tests" / "support" / "isolation.py",
        PROJECT_ROOT / "tests" / "integration" / "test_isolation_linux.py",
        Path(__file__).resolve(),
    }
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests").rglob("*.py")
        if path.resolve() not in allowed and "real_isolation" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


async def test_ordinary_tests_get_pinned_isolation_doubles(tmp_path):
    assert isolation_monitor.snapshot().level == "landlock"
    assert (await isolation_monitor.ensure_ready()).level == "landlock"
    fixed = ProfileSource.fixed(_profile(tmp_path))
    runner = (await runner_broker.acquire(ExecutionGrant("landlock"), fixed)).runner
    assert isinstance(runner, PassthroughRunner)
    plan = runner.plan(_request(tmp_path))
    assert plan.argv == ("bash", "-c", "pytest -q")


def test_pytest_ini_registers_isolation_linux_marker():
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / "pytest.ini", encoding="utf-8")
    markers = [line.strip() for line in parser["pytest"]["markers"].splitlines() if line.strip()]
    assert any(line.startswith("isolation_linux:") for line in markers)


def test_real_isolation_fixture_opts_out_of_doubles(real_isolation):
    monitor, broker = real_isolation
    assert monitor is isolation_monitor and broker is runner_broker
    assert monitor.snapshot.__func__ is IsolationMonitor.snapshot
    assert broker.acquire.__func__ is RunnerBroker.acquire
