"""隔離自我檢查的濫用防護與子行程 stdin（iso-c2 對抗式第 2 輪修正 FD）.

- 啟動器失敗標記只認 ``FAILURE_CODES``：指令自己印出的假代碼不算；重探不分代碼、60 秒內最多
  一次，所以就算指令印出真代碼也不能無限觸發自測。
- 代理人指令（終端、互審驗證指令、演化）與啟動器自測的子行程 stdin 一律是 DEVNULL，不繼承
  proxy 的 stdin（原生 Linux 從 TTY 啟動時，TIOCSTI 可以往那個終端注入指令）。
- spec 帶 spawn 端的 pid（``parent_pid``）：啟動器設好 PDEATHSIG 後父行程不是它，就不執行指令
  （父行程在 PDEATHSIG 生效前就結束的競態；真核心案例在 Linux 模組）。

全部用替身：不真的 spawn、不碰真核心、不打網路。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import types
from typing import Any

import pytest
from src.sandbox_kernel import launcher as kernel_launcher
from src.sandbox_kernel.spec import (
    EXECUTOR_ENV_ALLOWED,
    FAILURE_CODES,
    FAILURE_MARKER,
    INTERPRETER_UNTRUSTED,
    SPEC_VERSION,
    InterpreterUntrustedError,
    SelfTestResult,
    SpecError,
    parse_spec,
)
from src.services import evolution_security
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import status as status_module
from src.services.isolation.profiles import SandboxProfile
from src.services.isolation.runners import LandlockRunner, launcher_spec
from src.services.isolation.status import MARKER_SELFTEST_MIN_GAP_SECONDS, IsolationMonitor
from src.services.review import evidence
from src.services.review.evidence import collect_evidence
from src.tools import terminal

from tests.support.isolation import fake_kernel_layout, passthrough_launch

# --- 失敗標記與重探頻率 -----------------------------------------------------------------


class _CountingSelfTest:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _kernel_dir: Any, _test_dir: Any) -> SelfTestResult:
        self.calls += 1
        return SelfTestResult(ok=True, abi=7)


async def test_failure_markers_share_one_global_rate_limit(tmp_path):
    now = [100.0]
    probe = _CountingSelfTest()
    layout = fake_kernel_layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout,
        selftest=probe,
        clock=lambda: now[0],
        platform="linux",
        start_background=False,
    )
    for code in sorted(FAILURE_CODES):
        monitor.report_failure(code)
    await asyncio.sleep(0.05)
    assert probe.calls == 1
    now[0] += MARKER_SELFTEST_MIN_GAP_SECONDS - 1
    monitor.report_failure("kernel_changed")
    await asyncio.sleep(0.05)
    assert probe.calls == 1
    now[0] += 1
    monitor.report_failure("path_open")
    await asyncio.sleep(0.05)
    assert probe.calls == 2


def test_runner_reports_only_known_launcher_codes(tmp_path):
    """狀態 fd 只認啟動器的代碼表；指令印在輸出裡的標記（真代碼也一樣）runner 根本不看."""
    runner = LandlockRunner(tmp_path, frozenset(), python=sys.executable)
    real = runner.failure(b"path_open\n")
    assert real is not None and real.code == "path_open"
    for forged in ("restrict_self_failed", "made_up_code", "$(rm -rf /)", ""):
        assert runner.failure(f"{forged}\n".encode()) is None, forged
    assert runner.failure(f"{FAILURE_MARKER}path_open\n".encode()) is None


# --- 子行程 stdin -------------------------------------------------------------------------


class _DoneProc:
    """馬上結束、沒有輸出的子行程替身。"""

    returncode = 0
    pid = None

    def __init__(self) -> None:
        self.stdout = self

    async def readline(self) -> bytes:
        return b""

    async def communicate(self) -> tuple[bytes, None]:
        return b"", None

    async def wait(self) -> int:
        return 0


def _spawn_recorder(seen: dict[str, Any]):
    async def _spawn(*_argv: Any, **kwargs: Any) -> _DoneProc:
        seen.update(kwargs)
        return _DoneProc()

    return _spawn


async def test_terminal_command_stdin_is_devnull(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(terminal, "spawn_exec", _spawn_recorder(seen))
    await terminal._execute_core("echo hi", "powershell", tmp_path, "fd-stdin")
    assert seen["stdin"] == asyncio.subprocess.DEVNULL


async def test_verification_command_stdin_is_devnull(zone_drive, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(evidence, "spawn_exec", _spawn_recorder(seen))

    async def _allow_all(_cmd: str) -> str | None:
        return None

    await collect_evidence(
        artifact_refs=[],
        verification_cmd="pytest -q",
        test_log=None,
        artifact_root=zone_drive,
        timeout_seconds=30,
        command_gate=_allow_all,
    )
    assert seen["stdin"] == asyncio.subprocess.DEVNULL


def test_evolution_command_stdin_is_devnull(tmp_path, monkeypatch):
    monkeypatch.setattr(EvolutionPathGuard, "MANAGED_BASE_DIR", tmp_path)
    EvolutionPathGuard.mkdir_tool("skill_alpha")
    seen: dict[str, Any] = {}

    def _run(_argv: Any, **kwargs: Any) -> types.SimpleNamespace:
        seen.update(kwargs)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(evolution_security, "run_sync", _run)
    EvolutionPathGuard.run_sandbox_command(
        "skill_alpha", ["python", "x.py"], launch=passthrough_launch("skill_alpha")
    )
    assert seen["stdin"] == subprocess.DEVNULL


async def test_launcher_selftest_stdin_is_devnull(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(status_module, "spawn_exec", _spawn_recorder(seen))
    await status_module.launcher_selftest(tmp_path / "kernel", tmp_path / "t")
    assert seen["stdin"] == asyncio.subprocess.DEVNULL


# --- 自測的直譯器與環境（C2-2 §2.9） -----------------------------------


@pytest.mark.parametrize("in_container", [True, False])
async def test_launcher_selftest_uses_the_trusted_interpreter_and_the_allowlist(
    tmp_path, monkeypatch, in_container
):
    """自測和執行指令同一份允許清單（不帶 proxy 的環境）；直譯器是可信直譯器，容器裡要 root 擁有."""
    seen: dict[str, Any] = {}
    owned: list[bool] = []

    async def _spawn(*argv: Any, **kwargs: Any) -> _DoneProc:
        seen.update(kwargs, argv=argv)
        return _DoneProc()

    def _trusted(*, require_root_owned: bool) -> str:
        owned.append(require_root_owned)
        return "/trusted/python3"

    monkeypatch.setenv("TOUKA_SELFTEST_PROBE_SECRET_KEY", "leak")
    monkeypatch.setattr(status_module, "spawn_exec", _spawn)
    monkeypatch.setattr(status_module, "trusted_interpreter", _trusted)
    monkeypatch.setattr(status_module, "is_container_runtime", lambda: in_container)
    await status_module.launcher_selftest(tmp_path / "kernel", tmp_path / "t")
    assert seen["argv"][:3] == ("/trusted/python3", "-I", "-S")
    assert owned == [in_container]
    assert set(seen["env"]) <= EXECUTOR_ENV_ALLOWED
    assert "leak" not in seen["env"].values()


async def test_untrusted_interpreter_fails_the_selftest_with_its_own_reason(tmp_path, monkeypatch):
    spawned: list[Any] = []

    async def _spawn(*argv: Any, **_kwargs: Any) -> _DoneProc:
        spawned.append(argv)
        return _DoneProc()

    def _untrusted(*, require_root_owned: bool) -> str:
        raise InterpreterUntrustedError(str(require_root_owned))

    monkeypatch.setattr(status_module, "spawn_exec", _spawn)
    monkeypatch.setattr(status_module, "trusted_interpreter", _untrusted)
    result = await status_module.launcher_selftest(tmp_path / "kernel", tmp_path / "t")
    assert (result.ok, result.error, spawned) == (False, INTERPRETER_UNTRUSTED, [])
    layout = fake_kernel_layout(tmp_path)

    async def _probe(_kernel: Any, _dir: Any) -> SelfTestResult:
        return result

    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_probe, platform="linux", start_background=False
    )
    await monitor.run_selftest("data", notify=False)
    snap = monitor.snapshot()
    assert (snap.local.status, snap.level, snap.reason) == (
        "unavailable", "none", INTERPRETER_UNTRUSTED
    )
    assert monitor.fallback_for(profile_complete=True).kind == "direct"


# --- 啟動器的預期父行程（PDEATHSIG 競態） -------------------------------------------------


def _spec_with_parent(parent_pid: object) -> bytes:
    return json.dumps({"version": SPEC_VERSION, "parent_pid": parent_pid}).encode("utf-8")


def test_spec_parent_pid_is_an_optional_non_negative_int():
    assert parse_spec(_spec_with_parent(4242)).parent_pid == 4242
    assert parse_spec(json.dumps({"version": SPEC_VERSION}).encode()).parent_pid == 0
    for bad in (-1, "4242", True, 1.5, None):
        with pytest.raises(SpecError):
            parse_spec(_spec_with_parent(bad))


def test_runner_spec_names_the_spawning_process_as_parent(tmp_path):
    profile = SandboxProfile(name="agent_command", cwd=tmp_path, rules=(), block_ports=())
    assert json.loads(launcher_spec(profile))["parent_pid"] == os.getpid()


class _Reached(Exception):
    """走到套用規則這一步（替身：之後的步驟會動到這個行程的 fd，不能真的跑）。"""


def test_launch_stops_before_the_sandbox_when_the_parent_is_not_the_spawner(monkeypatch):
    current = {"spec": b""}

    def _apply(*_args: Any) -> list[int]:
        raise _Reached

    monkeypatch.setattr(kernel_launcher, "_read_spec", lambda _fd: current["spec"])
    monkeypatch.setattr(kernel_launcher, "_linux_landlock", lambda: (None, 7))
    monkeypatch.setattr(kernel_launcher, "_set_process_attributes", lambda _api: None)
    monkeypatch.setattr(kernel_launcher, "apply_sandbox", _apply)
    command = kernel_launcher.parse_cli(["--spec-fd", "9", "--", "true"])
    current["spec"] = _spec_with_parent(os.getppid())
    with pytest.raises(_Reached):
        kernel_launcher.launch(command)
    current["spec"] = _spec_with_parent(os.getppid() + 1)
    with pytest.raises(kernel_launcher.LaunchError) as raised:
        kernel_launcher.launch(command)
    assert raised.value.code == "process_setup"
