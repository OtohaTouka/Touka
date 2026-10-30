"""隔離層級 SSOT 與 IsolationMonitor（iso-c2 U1c）.

純函式（層級正規化、隔離必要清單、ABI 遮罩）與 ``IsolationMonitor`` 的兩個狀態、
時序、快取、9p 自測與原因碼。全部用注入的假探測與假時鐘，不碰真核心、不 spawn。
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import threading
from pathlib import Path

import pytest
from src import config as config_module
from src.config import Settings, is_container_runtime
from src.models.identity import AgentIdentity
from src.sandbox_kernel import framing
from src.sandbox_kernel import launcher as kernel_launcher
from src.sandbox_kernel import spec as kernel_spec
from src.services import approval_flow
from src.services.isolation import profiles as profiles_module
from src.services.isolation import runners as runners_module
from src.services.isolation import status as status_module
from src.services.isolation.consent import Consent, ConsentKey, ExecutionGrant, consent_ledger
from src.services.isolation.executor_client import ExecutorHealth, ExecutorReady, ExecutorRoot
from src.services.isolation.executor_server import EXECUTOR_CODE_FILES, executor_code_digest
from src.services.isolation.mcp_launch import McpLaunchBook, McpLaunchInfo
from src.services.isolation.mcp_profiles import MCP_PROXY_STATE_KINDS
from src.services.isolation.profiles import SandboxLayout, SandboxProfile
from src.services.isolation.runners import ExecRequest, RunnerBroker
from src.services.isolation.status import (
    HEALTH_CACHE_OK_SECONDS,
    SELFTEST_RETRY_SECONDS,
    FallbackPlan,
    IsolationMonitor,
    LevelDecision,
    LocalDecision,
    SelfTestResult,
    parse_selftest_output,
)
from src.services.resource_manager import resource_manager
from src.utils import isolation_policy as policy

from tests.support.isolation import (
    FixedBroker,
    PinnedMonitor,
    fixed_interpreter,
    pinned_snapshot,
)
from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_roots import point_zone_roots


@pytest.fixture(autouse=True)
def _zone_roots_match_layout(tmp_path, monkeypatch):
    """RM 的 data 與 ST 根指向 ``_status_layout`` 同一處（ADR-0293 P7：版面與 RM 的 ST 要相同）."""
    point_zone_roots(monkeypatch, tmp_path / "data", tmp_path / "state")


# --- 純函式：isolation_policy ---------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "LANDLOCK", "direct", 3, "executor ", object()])
def test_normalize_isolation_unknown_values_are_none(value):
    assert policy.normalize_isolation(value) == "none"


@pytest.mark.parametrize("value", ["none", "landlock", "executor"])
def test_normalize_isolation_keeps_known_levels(value):
    assert policy.normalize_isolation(value) == value


def test_isolation_required_tools_are_the_five_constants():
    assert frozenset(
        {
            "evolution_implement_and_verify",
            "write_mcp_implementation",
            "verify_mcp_locally",
            "create_mcp",
            "scaffold_mcp",
        }
    ) == policy.ISOLATION_REQUIRED_TOOLS


def test_requires_isolation_terminal_and_listed_tools_only():
    assert policy.requires_isolation("terminal_execute", "ls -la")
    assert policy.requires_isolation("tool_call", "create_mcp")
    assert not policy.requires_isolation("tool_call", "vault_read")
    assert not policy.requires_isolation("tool_call", "terminal_execute")


def test_public_config_names_are_the_nine_casefolded_files():
    assert frozenset(
        {
            "pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            "ruff.toml",
            ".ruff.toml",
            ".python-version",
            "uv.lock",
            "package.json",
        }
    ) == policy.PUBLIC_CONFIG_NAMES
    assert not any(name.startswith(".env") for name in policy.PUBLIC_CONFIG_NAMES)
    assert policy.is_public_config_name("PyTest.INI")
    assert not policy.is_public_config_name(".env")


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (status_module, "kernel_digest"),
        (status_module, "parse_selftest_output"),
        (status_module, "SelfTestResult"),
        (policy, "PUBLIC_CONFIG_NAMES"),
        (profiles_module, "PUBLIC_CONFIG_NAMES"),
        (runners_module, "kernel_digest"),
        (runners_module, "MAX_SPEC_BYTES"),
        (runners_module, "parse_status"),
    ],
)
def test_kernel_ssot_is_the_sandbox_kernel_spec_object(module, name):
    """kernel 雜湊、自測輸出解析、公開設定檔名與 spec 上限只有 sandbox_kernel/spec.py 一份。"""
    assert getattr(module, name) is getattr(kernel_spec, name)


@pytest.mark.parametrize(
    ("abi", "features"),
    [
        (2, set()),
        (3, {"fs"}),
        (4, {"fs", "tcp"}),
        (5, {"fs", "tcp", "ioctl"}),
        (6, {"fs", "tcp", "ioctl", "signal", "abstract_unix"}),
        (7, {"fs", "tcp", "ioctl", "signal", "abstract_unix"}),
        (12, {"fs", "tcp", "ioctl", "signal", "abstract_unix"}),
    ],
)
def test_landlock_features_follow_abi_table(abi, features):
    assert policy.landlock_features(abi) == frozenset(features)


def test_effective_abi_is_clamped_to_verified_max():
    assert policy.effective_abi(12) == 7
    assert policy.effective_abi(4) == 4


def test_abi_thresholds_are_pinned():
    assert policy.VERIFIED_ABI_MAX == 7
    assert policy.LANDLOCK_LAYER_MIN_ABI == 4
    assert policy.EXECUTOR_LAYER_MIN_ABI == 3


@pytest.mark.parametrize("segment", ["", ".", "..", "a/b", "a\\b", "a\x00b", " "])
def test_unsafe_segments_rejected(segment):
    assert not policy.is_safe_segment(segment)


def test_safe_segment_accepted():
    assert policy.is_safe_segment("Alice")


# --- config 的設定鍵與容器判斷 ---------------------------------------------------------


def test_isolation_settings_default_empty_strings():
    for key in (
        "TOUKA_SANDBOX_STATE_DIR",
        "TOUKA_SANDBOX_KERNEL_DIR",
        "TOUKA_EXECUTOR_DIR",
        "PROXY_PORT",
        "HEARTBEAT_PORT",
        "CHAT_PORT",
    ):
        assert Settings.model_fields[key].default == ""


def test_empty_port_value_in_env_does_not_crash_settings(monkeypatch):
    monkeypatch.setenv("PROXY_PORT", "")
    monkeypatch.setenv("CHAT_PORT", "not-a-port")
    assert Settings(_env_file=None).CHAT_PORT == "not-a-port"


def test_container_runtime_is_single_shared_check(monkeypatch):
    seen: list[str] = []

    class _Marker:
        def __init__(self, value: str) -> None:
            seen.append(value)

        def exists(self) -> bool:
            return True

        def is_absolute(self) -> bool:
            return False

    monkeypatch.setattr(config_module, "Path", _Marker)
    assert is_container_runtime() is True
    assert seen == ["/.dockerenv"]


def test_get_app_data_dir_uses_shared_container_check(monkeypatch):
    monkeypatch.setattr(config_module, "is_container_runtime", lambda: True)
    settings = Settings(_env_file=None, MEMORY_APP_DATA_DIR="data")
    container_dir = Path(settings.get_app_data_dir())
    assert container_dir.as_posix().startswith("/") and container_dir.name == "data"
    monkeypatch.setattr(config_module, "is_container_runtime", lambda: False)
    assert settings.get_app_data_dir() == "data"


# --- IsolationMonitor ------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _SelfTest:
    """假的啟動器自測：依位置（資料所在檔案系統／ST）回結果，記錄呼叫。"""

    def __init__(
        self,
        abi: int = 7,
        *,
        data_ok: bool = True,
        state_ok: bool = True,
        error: str = "check_failed",
        net: str | None = None,
    ) -> None:
        self.abi = abi
        self.data_ok = data_ok
        self.state_ok = state_ok
        self.error = error
        self.net = net  # 自測另外回報的網路設定問題（和檔案檢查無關）
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, kernel_dir: Path, test_dir: Path) -> SelfTestResult:
        location = "data" if test_dir.parent.name == "mcp_servers" else "state"
        # ST 的自測在 ``ST/proxy/selftest/<一次一個>``（ADR-0293 PR 2）
        assert location == "data" or test_dir.parent.parts[-2:] == ("proxy", "selftest")
        self.calls.append(location)
        if self.gate is not None:
            await self.gate.wait()
        ok = self.data_ok if location == "data" else self.state_ok
        return SelfTestResult(ok=ok, abi=self.abi, error=None if ok else self.error, net=self.net)


def _status_layout(tmp_path: Path, **overrides) -> SandboxLayout:
    data = tmp_path / "data"
    (data / "mcp_servers").mkdir(parents=True, exist_ok=True)
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    proxy = tmp_path / "ToukaProxy"
    (proxy / "src" / "sandbox_kernel").mkdir(parents=True, exist_ok=True)
    (proxy / "src" / "sandbox_kernel" / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    values = {
        "data_root": data,
        "config_dir": proxy / "config",
        "install_root": tmp_path,
        "proxy_root": proxy,
        "env_file": tmp_path / ".env",
        "state_root": state,
        "kernel_dir": kernel,
        "executor_dir": None,
        "mirror_roots": (),
        "in_container": True,
    }
    values.update(overrides)
    return SandboxLayout(**values)


def _pointed_layout(monkeypatch, base: Path, **overrides) -> SandboxLayout:
    """在 ``base`` 另建一個版面，RM 的 data 與 ST 根一起指過去（一個測試裡輪流用好幾個版面時）."""
    point_zone_roots(monkeypatch, base / "data", base / "state")
    return _status_layout(base, **overrides)


def _monitor(layout: SandboxLayout, selftest=None, *, clock=None, platform="linux", **kwargs):
    return IsolationMonitor(
        layout_provider=lambda: layout,
        selftest=selftest or _SelfTest(),
        clock=clock or _Clock(),
        platform=platform,
        start_background=False,
        **kwargs,
    )


class _Executor:
    """假的執行器健康檢查：回固定結果，記錄連到哪裡、送了哪一份 B。"""

    def __init__(self, ready: ExecutorReady | None = None, reason: str | None = None) -> None:
        self.health = ExecutorHealth(ready, reason)
        self.calls: list[tuple[Path, tuple[int, ...]]] = []

    async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        self.calls.append((ipc_dir, tuple(block_ports)))
        return self.health


SERVER_DIGEST = "e" * 64


def _ports_digest(ports) -> str:
    return "p" + ",".join(map(str, ports))


class _Digests:
    """接線後的執行器伺服器 SSOT 的替身（封鎖埠雜湊照實依 B 算）。"""

    def __init__(self, server: str | None = SERVER_DIGEST) -> None:
        self.server = server

    def server_digest(self, _proxy_root: Path) -> str | None:
        return self.server

    def block_ports_digest(self, block_ports: tuple[int, ...]) -> str | None:
        return _ports_digest(block_ports)


def _ready_for(layout: SandboxLayout, ports=None, **overrides) -> ExecutorReady:
    """和這個版面完全相符的 READY（kernel 是 live src、roots 是三個預期的位置）."""
    data = layout.data_root
    roots = (
        ExecutorRoot(str(data / "personal_drive"), "rw"),
        ExecutorRoot(str(data / "mcp_servers" / "managed"), "rw"),
        ExecutorRoot(str(layout.config_dir / "skills"), "ro"),
    )
    values = {
        "version": 1, "abi": 6, "enforced": True, "net": None,
        "features": ("fs", "tcp"), "roots": roots, "state_root": "/x/executor_state",
        "kernel_digest": kernel_spec.kernel_digest(layout.proxy_root / "src" / "sandbox_kernel"),
        "server_digest": SERVER_DIGEST, "boot_id": "b" * 32,
        "block_ports_digest": _ports_digest(ports) if ports is not None else "",
        "uid": 1001,
    }
    values.update(overrides)
    return ExecutorReady(**values)


class _MatchingExecutor(_Executor):
    """READY 的封鎖埠雜湊照這次 HELLO 的 B 算（真的伺服器就是這樣）。"""

    def __init__(self, layout: SandboxLayout, **overrides) -> None:
        super().__init__()
        self.layout = layout
        self.overrides = overrides

    async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        self.calls.append((ipc_dir, tuple(block_ports)))
        if self.health.reason:
            return self.health
        return ExecutorHealth(_ready_for(self.layout, block_ports, **self.overrides))


def _executor_monitor(tmp_path: Path, *, selftest=None, clock=None, executor=None, **layout_kw):
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec", **layout_kw)
    executor = executor or _MatchingExecutor(layout)
    monitor = _monitor(
        layout, selftest, clock=clock, executor_health=executor, executor_digests=_Digests()
    )
    return monitor, executor, layout


async def test_native_windows_is_none_without_probing(tmp_path):
    probe = _SelfTest()
    monitor = _monitor(_status_layout(tmp_path), probe, platform="win32")
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("none", "none", "native_platform")
    assert probe.calls == []


async def test_windows_with_executor_dir_is_executor_unreachable(tmp_path):
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    snap = await _monitor(layout, platform="win32").ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("none", "executor", "executor_unreachable")


async def test_linux_landlock_when_selftest_state_and_kernel_ok(tmp_path):
    probe = _SelfTest(abi=7)
    snap = await _monitor(_status_layout(tmp_path), probe).ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("landlock", "landlock", None)
    assert snap.local.status == "available" and snap.local.abi == 7
    assert "tcp" in snap.features and "signal" in snap.features
    assert probe.calls == ["data"]


async def test_ensure_ready_timeout_keeps_local_pending(tmp_path):
    probe = _SelfTest()
    probe.gate = asyncio.Event()
    monitor = _monitor(_status_layout(tmp_path), probe, ready_timeout=0.05)
    snap = await monitor.ensure_ready()
    assert snap.local.status == "pending"
    assert (snap.level, snap.reason) == ("none", "probe_pending")
    probe.gate.set()
    await asyncio.sleep(0.05)
    assert monitor.snapshot().level == "landlock"


async def test_abi_3_is_available_locally_but_not_landlock_level(tmp_path):
    snap = await _monitor(_status_layout(tmp_path), _SelfTest(abi=3)).ensure_ready()
    assert snap.local.status == "available"
    assert (snap.level, snap.reason) == ("none", "landlock_abi_insufficient")


async def test_abi_2_is_unavailable(tmp_path):
    snap = await _monitor(_status_layout(tmp_path), _SelfTest(abi=2)).ensure_ready()
    assert (snap.local.status, snap.local.reason) == ("unavailable", "landlock_abi_too_old")
    assert snap.level == "none"


async def test_kernel_outdated_does_not_affect_local_landlock(tmp_path):
    layout = _status_layout(tmp_path)
    (layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py").write_text(
        "# edited live src\n", encoding="utf-8"
    )
    snap = await _monitor(layout).ensure_ready()
    assert snap.local.status == "available"
    assert (snap.level, snap.reason) == ("none", "kernel_outdated")


async def test_container_without_kernel_is_kernel_not_installed(tmp_path):
    probe = _SelfTest()
    layout = _status_layout(tmp_path, kernel_dir=None)
    snap = await _monitor(layout, probe).ensure_ready()
    assert (snap.local.status, snap.local.reason) == ("unavailable", "kernel_not_installed")
    assert (snap.level, snap.reason) == ("none", "kernel_not_installed")
    assert probe.calls == []


async def test_kernel_dir_without_launcher_is_kernel_not_installed(tmp_path):
    probe = _SelfTest()
    layout = _status_layout(tmp_path)
    (layout.kernel_dir / "launcher.py").unlink()
    snap = await _monitor(layout, probe).ensure_ready()
    assert (snap.local.status, snap.local.reason) == ("unavailable", "kernel_not_installed")
    assert probe.calls == []


async def test_sandbox_state_unset_is_unconfigured(tmp_path):
    snap = await _monitor(_status_layout(tmp_path, state_root=None)).ensure_ready()
    assert (snap.level, snap.reason, snap.detail) == ("none", "sandbox_state_unconfigured", "unset")


async def test_sandbox_state_not_writable_is_unconfigured_without_crash(tmp_path, monkeypatch):
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("", encoding="utf-8")
    point_zone_roots(monkeypatch, tmp_path / "data", blocker)  # RM 的 ST 也是這個檔案
    snap = await _monitor(_status_layout(tmp_path, state_root=blocker)).ensure_ready()
    assert (snap.level, snap.reason, snap.detail) == (
        "none",
        "sandbox_state_unconfigured",
        "not_writable",
    )
    assert snap.local.status == "available"


async def test_9p_failure_recovers_only_through_9p_selftest(tmp_path):
    probe = _SelfTest(data_ok=False, state_ok=True)
    monitor = _monitor(_status_layout(tmp_path), probe)
    changes: list[str] = []

    async def _record(snapshot):
        changes.append(snapshot.level)

    monitor.on_level_change(_record)
    snap = await monitor.ensure_ready()
    assert (snap.local.status, snap.local.reason) == ("unavailable", "fs_9p")
    await monitor.run_selftest("state")
    assert monitor.snapshot().local.reason == "fs_9p"
    probe.data_ok = True
    await monitor.run_selftest("data")
    await asyncio.sleep(0)
    assert monitor.snapshot().local.status == "available"
    assert monitor.snapshot().level == "landlock"
    # C2-3 U3d：本機自測有結果（9p 失敗）也通知，等本機判定的第三方 MCP 才會啟動
    assert changes == ["none", "landlock"]


async def test_background_keeps_9p_selftest_after_fs_9p(tmp_path):
    probe = _SelfTest(data_ok=False)
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    await monitor.ensure_ready()
    clock.now += 599
    await monitor.run_due_checks()
    assert probe.calls == ["data"]
    clock.now += 2
    await monitor.run_due_checks()
    assert probe.calls == ["data", "data"]


async def test_later_reprobe_keeps_completed_result_and_suspended_timer(tmp_path):
    clock = _Clock()
    book = McpLaunchBook(clock=clock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(_kernel, _directory):
        entered.set()
        await release.wait()
        return SelfTestResult(ok=False, error="check_failed")

    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)

    def record():
        decision = monitor.local_decision()
        book.record("s", McpLaunchInfo(
            "third_party", "manual", "ok", "waiting_probe", "none", reason=decision.reason,
        ))
        return decision

    initial = asyncio.create_task(monitor.ensure_ready())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert record().reason == "probe_pending"
    clock.now += 700
    assert not book.suspended("s")
    release.set()
    await initial
    assert record().reason == "selftest_failed"
    release.clear()
    entered.clear()
    clock.now += SELFTEST_RETRY_SECONDS
    recheck = asyncio.create_task(monitor.run_due_checks())
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        assert record().reason == "selftest_failed"
        clock.now += 60
        assert book.suspended("s")
    finally:
        release.set()
        await recheck
    assert record().reason == "selftest_failed"
    assert book.suspended("s")


@pytest.mark.parametrize("code", ["net_tcp_fastopen", "net_mptcp"])
async def test_unsafe_net_sysctl_keeps_file_isolation_without_tcp(tmp_path, code):
    """網路設定讓 TCP 規則擋不住連線（比照 ABI 3）：本機 Landlock 仍可用、features 不含 tcp；
    層級 none、原因照實寫網路設定（Auto／YOLO 照舊要人核准），核准後走「隔離降級」."""
    snap = await _monitor(_status_layout(tmp_path), _SelfTest(abi=7, net=code)).ensure_ready()
    assert (snap.local.status, snap.local.abi, snap.local.reason) == ("available", 7, None)
    assert snap.local.net == code
    assert (snap.level, snap.expected, snap.reason) == ("none", "landlock", code)
    assert "tcp" not in snap.features
    assert snap.features == policy.landlock_features(7) - {"tcp"}


@pytest.mark.parametrize("code", ["net_tcp_fastopen", "net_mptcp"])
async def test_unsafe_net_sysctl_fallback_is_landlock_without_tcp(tmp_path, code):
    monitor = _monitor(_status_layout(tmp_path), _SelfTest(abi=7, net=code))
    await monitor.ensure_ready()
    plan = monitor.fallback_for(profile_complete=True)
    assert plan == FallbackPlan("landlock", policy.landlock_features(7) - {"tcp"})
    assert monitor.fallback_for(profile_complete=False).kind == "direct"


async def test_unsafe_net_sysctl_is_retried_on_state_and_recovers_to_landlock(tmp_path):
    """設定改好之後背景重探自動回到 landlock；網路原因不走「只認 9p 自測」的回復路徑。"""
    probe = _SelfTest(net="net_tcp_fastopen")
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    assert (await monitor.ensure_ready()).reason == "net_tcp_fastopen"
    clock.now += SELFTEST_RETRY_SECONDS - 1
    await monitor.run_due_checks()
    assert probe.calls == ["data"]  # 還沒到 10 分鐘
    clock.now += 2
    await monitor.run_due_checks()
    assert probe.calls == ["data", "state"]
    assert monitor.snapshot().reason == "net_tcp_fastopen"  # 設定還沒改：照舊
    probe.net = None  # 例如原生 Linux 在主機改好 sysctl（不用重啟）
    clock.now += SELFTEST_RETRY_SECONDS + 1
    await monitor.run_due_checks()
    snap = monitor.snapshot()
    assert probe.calls == ["data", "state", "state"]
    assert (snap.level, snap.reason, snap.local.net) == ("landlock", None, None)
    assert "tcp" in snap.features


async def test_net_issue_with_a_9p_failure_recovers_only_through_9p(tmp_path):
    """data 位置的檔案檢查失敗是 fs_9p（網路設定同時不安全也一樣）；9p 自測通過後才換成網路原因。"""
    probe = _SelfTest(data_ok=False, state_ok=True, net="net_mptcp")
    monitor = _monitor(_status_layout(tmp_path), probe)
    snap = await monitor.ensure_ready()
    assert (snap.local.status, snap.reason) == ("unavailable", "fs_9p")
    await monitor.run_selftest("state")
    assert monitor.snapshot().reason == "fs_9p"
    probe.data_ok = True
    await monitor.run_selftest("data")
    snap = monitor.snapshot()
    assert (snap.local.status, snap.level, snap.reason) == ("available", "none", "net_mptcp")


async def test_consented_fallback_without_tcp_still_blocks_the_ports(tmp_path):
    """核准後的「隔離降級」features 不含 tcp，但啟動器拿到的 spec 照樣有封鎖埠（有比沒有好）。"""
    layout = _status_layout(tmp_path)
    monitor = _monitor(layout, _SelfTest(abi=7, net="net_tcp_fastopen"))
    await monitor.ensure_ready()
    broker = FixedBroker(
        RunnerBroker(monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter)
    )
    profile = SandboxProfile(name="agent_command", cwd=tmp_path, rules=(), block_ports=(8000, 2375))
    consent = ExecutionGrant("none", Consent(subject="s", fallback="landlock"))
    runner = await broker.acquire(consent, profile)
    plan = runner.plan(ExecRequest(argv=("true",), env={}, profile=profile))
    try:
        chunks = []
        while chunk := os.read(plan.pass_fds[0], 65536):
            chunks.append(chunk)
    finally:
        plan.release()
    assert (plan.level, "tcp" in plan.features) == ("landlock", False)
    assert json.loads(b"".join(chunks))["block_ports"] == [8000, 2375]


@pytest.mark.parametrize("broken", ["outdated", "unreadable"])
async def test_kernel_issue_makes_the_fallback_direct(tmp_path, monkeypatch, broken):
    """kernel 和原始碼不一致（或讀不到）：/opt 的舊啟動器會拒絕新的 spec 鍵，核准後必定
    spec_invalid；卡片照實寫「未隔離」（原因那行寫要重建），不寫「隔離降級」."""
    layout = _status_layout(tmp_path)
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    assert monitor.fallback_for(profile_complete=True).kind == "landlock"
    if broken == "outdated":
        live = layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py"
        live.write_text("# edited live src\n", encoding="utf-8")
    else:

        def _eio(_kernel_dir):
            raise OSError(errno.EIO, "9p hiccup")

        monkeypatch.setattr(status_module, "kernel_digest", _eio)
    snap = await monitor.refresh()
    reason = {"outdated": "kernel_outdated", "unreadable": "isolation_check_failed"}[broken]
    assert (snap.local.status, snap.level, snap.reason) == ("available", "none", reason)
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan("direct", frozenset())


async def test_kernel_outdated_card_says_unisolated_and_names_the_rebuild(tmp_path, monkeypatch):
    """審批卡照實寫「未隔離」（direct、沒有 features），原因那行是 kernel_outdated（要重建）。"""
    layout = _status_layout(tmp_path)
    live = layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py"
    live.write_text("# edited live src\n", encoding="utf-8")
    monitor = _monitor(layout)
    assert (await monitor.ensure_ready()).reason == "kernel_outdated"
    monkeypatch.setattr(approval_flow, "isolation_monitor", monitor)
    monkeypatch.setattr(approval_flow, "current_layout", lambda: layout)
    key = ConsentKey("Alice", "s1", "c1")
    consent_ledger.record_policy(key, level="none", subject="x", reason="kernel_outdated")
    card = await approval_flow.card_isolation(key, "execute_command")
    assert card is not None
    assert (card.fallback, card.features, card.reason) == ("direct", frozenset(), "kernel_outdated")


async def test_background_retries_on_state_for_other_reasons(tmp_path):
    probe = _SelfTest(abi=2)
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    await monitor.ensure_ready()
    clock.now += 601
    await monitor.run_due_checks()
    assert probe.calls == ["data", "state"]


async def test_failure_marker_triggers_9p_selftest_rate_limited(tmp_path):
    probe = _SelfTest()
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    await monitor.ensure_ready()
    monitor.report_failure("landlock_restrict_failed")
    monitor.report_failure("landlock_restrict_failed")
    await asyncio.sleep(0.01)
    assert probe.calls == ["data", "data"]
    clock.now += 61
    monitor.report_failure("landlock_restrict_failed")
    await asyncio.sleep(0.01)
    assert probe.calls == ["data", "data", "data"]


async def test_health_cache_ten_seconds_on_success_one_on_failure(tmp_path, monkeypatch):
    clock = _Clock()
    checks: list[float] = []
    real_state_issue = status_module._state_issue

    def _counting_state_issue(layout):
        checks.append(clock.now)
        return real_state_issue(layout)

    monkeypatch.setattr(status_module, "_state_issue", _counting_state_issue)
    layouts = {"current": _status_layout(tmp_path)}
    monitor = IsolationMonitor(
        layout_provider=lambda: layouts["current"],
        selftest=_SelfTest(),
        clock=clock,
        platform="linux",
        start_background=False,
    )
    assert (await monitor.ensure_ready()).level == "landlock"
    assert len(checks) == 1
    clock.now += 9
    await monitor.ensure_ready()
    assert len(checks) == 1
    clock.now += 2
    await monitor.ensure_ready()
    assert len(checks) == 2
    layouts["current"] = _status_layout(tmp_path, state_root=None)
    clock.now += 11
    assert (await monitor.ensure_ready()).level == "none"
    assert len(checks) == 3
    clock.now += 1.5
    await monitor.ensure_ready()
    assert len(checks) == 4


async def test_executor_dir_is_judged_by_the_health_check(tmp_path):
    """設了執行器目錄：照健康檢查判定（C2-1 是一律 none）；連不上就是 none／unreachable."""
    probe = _SelfTest(abi=7)
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    executor = _MatchingExecutor(layout)
    executor.health = ExecutorHealth(None, "executor_unreachable")
    clock = _Clock()
    monitor = _monitor(
        layout, probe, clock=clock, executor_health=executor, executor_digests=_Digests()
    )
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("none", "executor", "executor_unreachable")
    # 第三方 MCP 的包裝與人核准的降級仍看本機 Landlock：照樣自測
    assert snap.local.status == "available" and probe.calls == ["data"]
    assert [ipc for ipc, _ in executor.calls] == [layout.executor_dir]
    executor.health = ExecutorHealth(None)  # 執行器起來了
    clock.now += 2
    assert (await monitor.ensure_ready()).level == "executor"


async def test_on_level_change_fires_when_local_landlock_becomes_available(tmp_path):
    probe = _SelfTest(data_ok=False)
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    monitor = _monitor(layout, probe)
    seen: list[str] = []

    async def _record(snapshot):
        seen.append(snapshot.local.status)

    monitor.on_level_change(_record)
    await monitor.ensure_ready()
    probe.data_ok = True
    await monitor.run_selftest("data")
    await asyncio.sleep(0)
    assert monitor.snapshot().level == "none"
    # C2-3 U3d：第一次本機自測有結果（不可用）也通知
    assert seen == ["unavailable", "available"]


async def test_schedule_intervals(tmp_path):
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path, state_root=None), clock=clock)
    await monitor.ensure_ready()
    assert monitor.next_interval() == 2
    clock.now += 61
    assert monitor.next_interval() == 30
    healthy = _monitor(_status_layout(tmp_path), clock=clock)
    await healthy.ensure_ready()
    clock.now += 61
    assert healthy.next_interval() == 60


async def test_background_loop_starts_only_when_isolation_expected(tmp_path):
    started: list[str] = []

    async def _never(_seconds: float) -> None:
        started.append("loop")
        await asyncio.Event().wait()

    def _build(platform: str) -> IsolationMonitor:
        return IsolationMonitor(
            layout_provider=lambda: _status_layout(tmp_path),
            selftest=_SelfTest(),
            clock=_Clock(),
            platform=platform,
            sleep=_never,
        )

    linux = _build("linux")
    await linux.ensure_ready()
    await asyncio.sleep(0)
    assert started == ["loop"]
    linux.stop()
    windows = _build("win32")
    await windows.ensure_ready()
    await asyncio.sleep(0)
    assert started == ["loop"]


async def test_background_loop_survives_a_failed_round_and_stops_on_cancel(
    tmp_path, monkeypatch
):
    """一輪出錯（例如 9p 暫時 EIO）只記 log，下一輪照常；取消照常讓迴圈結束。"""
    rounds: list[int] = []
    in_third_round = asyncio.Event()
    real_checks = IsolationMonitor.run_due_checks

    async def _flaky(self: IsolationMonitor) -> None:
        rounds.append(len(rounds))
        if len(rounds) == 1:
            raise OSError(errno.EIO, "9p hiccup")
        await real_checks(self)
        if len(rounds) == 3:
            in_third_round.set()
            await asyncio.Event().wait()  # 停在這一輪裡：取消落在 try 之內

    async def _tick(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(IsolationMonitor, "run_due_checks", _flaky)
    monitor = IsolationMonitor(
        layout_provider=lambda: _status_layout(tmp_path),
        selftest=_SelfTest(),
        clock=_Clock(),
        platform="linux",
        sleep=_tick,
    )
    await monitor.ensure_ready()
    await asyncio.wait_for(in_third_round.wait(), 5)
    monitor.stop()
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(rounds) == 3


async def test_unreadable_kernel_is_check_failed_not_an_exception(tmp_path, monkeypatch):
    """讀 kernel（其中一份在 9p 的 live src）出錯時判 none（判定出錯），恢復後回到 landlock。"""
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), clock=clock)
    assert (await monitor.ensure_ready()).level == "landlock"
    real_digest = status_module.kernel_digest

    def _eio(_kernel_dir):
        raise OSError(errno.EIO, "9p hiccup")

    monkeypatch.setattr(status_module, "kernel_digest", _eio)
    snap = await monitor.refresh()
    assert (snap.level, snap.reason) == ("none", "isolation_check_failed")
    assert monitor.checked_kernel_digest is None
    monkeypatch.setattr(status_module, "kernel_digest", real_digest)
    clock.now += 2
    assert (await monitor.ensure_ready()).level == "landlock"


async def test_level_for_denied_project_is_none_with_reason(tmp_path, monkeypatch):
    async def _deny(_path, _layout):
        return "scan_limit"

    monkeypatch.setattr(profiles_module, "touka_directory_verdict", _deny)
    layout = _status_layout(tmp_path, in_container=False)
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    alice = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True)
    assert await monitor.level_for(alice, None) == LevelDecision("landlock")
    assert await monitor.level_for(alice, project) == LevelDecision("none", "scan_limit")


async def test_level_for_grants_only_the_callers_own_drive(tmp_path):
    """G 依身分：自己資料夾裡的專案是 landlock，別的代理人資料夾裡的一律 none。"""
    layout = _status_layout(tmp_path, in_container=False)
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    alice = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    drives = layout.data_root / "personal_drive"
    for owner in ("Alice", "Bob"):
        (drives / owner / "proj").mkdir(parents=True)
    assert await monitor.level_for(alice, drives / "Alice" / "proj") == LevelDecision("landlock")
    denied = LevelDecision("none", "protected_location")
    assert await monitor.level_for(alice, drives / "Bob" / "proj") == denied


async def test_fallback_for_prefers_landlock_only_when_complete(tmp_path):
    monitor = _monitor(_status_layout(tmp_path), _SelfTest(abi=3))
    await monitor.ensure_ready()
    assert monitor.fallback_for(profile_complete=True).kind == "landlock"
    assert monitor.fallback_for(profile_complete=True).features == frozenset({"fs"})
    assert monitor.fallback_for(profile_complete=False).kind == "direct"
    unset = _monitor(_status_layout(tmp_path, state_root=None))
    await unset.ensure_ready()
    assert unset.fallback_for(profile_complete=True).kind == "direct"


# --- 測試替身（PinnedMonitor）和真的 monitor 一致 ----------------------------------------

# 各情況下測試會寫的替身；ST 與 kernel 的問題可能被更前面的原因蓋住，所以設在 monitor 上
_DOUBLES = {
    "available": PinnedMonitor,
    "kernel_outdated": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="kernel_outdated"), kernel_issue="kernel_outdated"
    ),
    "kernel_unreadable": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="isolation_check_failed"),
        kernel_issue="isolation_check_failed",
    ),
    "net_unsafe": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="net_tcp_fastopen", net="net_tcp_fastopen")
    ),
    "state_unset": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="sandbox_state_unconfigured"), state_ok=False
    ),
    "pending": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", local_status="pending", abi=0, reason="probe_pending")
    ),
    "unavailable": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", local_status="unavailable", abi=0, reason="selftest_failed")
    ),
    "abi_3": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", abi=3, reason="landlock_abi_insufficient")
    ),
    "executor_unreachable": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="executor_unreachable", expected="executor")
    ),
    "executor_net_unsafe": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="net_tcp_fastopen", expected="executor"),
        executor_features=policy.landlock_features(6) - {"tcp"},
    ),
    "block_ports_incomplete": lambda: PinnedMonitor(
        snap=pinned_snapshot(
            "none", reason="block_ports_incomplete", block_ports_issue="block_ports_incomplete"
        )
    ),
    "executor_block_ports_incomplete": lambda: PinnedMonitor(
        snap=pinned_snapshot(
            "none",
            reason="block_ports_incomplete",
            expected="executor",
            block_ports_issue="block_ports_incomplete",
        ),
        executor_features=policy.landlock_features(6) - {"tcp"},
    ),
}


def _unreadable_kernel(_kernel_dir: Path) -> str | None:
    raise OSError(errno.EIO, "9p hiccup")


async def _real_monitor(case: str, tmp_path: Path, monkeypatch) -> IsolationMonitor:
    """同一個情況的真 monitor：注入版面與自測結果（pending 是第一次自測還沒回來）。"""
    layouts = {
        "state_unset": {"state_root": None},
        "executor_unreachable": {"executor_dir": tmp_path / "exec"},
        "executor_net_unsafe": {"executor_dir": tmp_path / "exec"},
        "executor_block_ports_incomplete": {"executor_dir": tmp_path / "exec"},
    }
    layout = _status_layout(tmp_path, **layouts.get(case, {}))
    if case.endswith("block_ports_incomplete"):  # 設定檔讀不到（結構不對）
        layout.config_dir.mkdir(parents=True, exist_ok=True)
        (layout.config_dir / "connections.json").write_text("[]", encoding="utf-8")
    executors = {"executor_net_unsafe": {"net": "net_tcp_fastopen"}}
    probes = {
        "net_unsafe": _SelfTest(net="net_tcp_fastopen"),
        "unavailable": _SelfTest(abi=0, data_ok=False),
        "abi_3": _SelfTest(abi=3),
    }
    if case == "kernel_outdated":
        live = layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py"
        live.write_text("# edited live src\n", encoding="utf-8")
    elif case == "kernel_unreadable":
        monkeypatch.setattr(status_module, "kernel_digest", _unreadable_kernel)
    if case in executors or case.startswith("executor_block"):
        executor = _MatchingExecutor(layout, **executors.get(case, {}))
        monitor = _monitor(
            layout, probes.get(case), executor_health=executor, executor_digests=_Digests()
        )
    else:
        monitor = _monitor(layout, probes.get(case))
    if case != "pending":
        await monitor.ensure_ready()
    return monitor


def _judged(snap) -> tuple:
    return (snap.level, snap.expected, snap.reason, snap.local, snap.features)


@pytest.mark.parametrize("case", list(_DOUBLES))
async def test_pinned_monitor_fallback_matches_the_real_monitor(tmp_path, monkeypatch, case):
    """同一個情況下，替身和真的 monitor 的判定與降級去處相同.

    卡片、同意、執行的主流程靠替身守；替身和真的不一致時，C2-2 重寫 ``fallback_for`` 的回歸會漏掉。
    """
    real = await _real_monitor(case, tmp_path, monkeypatch)
    double = _DOUBLES[case]()
    assert _judged(double.snapshot()) == _judged(real.snapshot())
    for complete in (True, False):
        for eligible in (True, False):
            expected = real.fallback_for(profile_complete=complete, executor_eligible=eligible)
            got = double.fallback_for(profile_complete=complete, executor_eligible=eligible)
            assert got == expected, (complete, eligible)


_TOO_OLD = SelfTestResult(ok=False, abi=2, error="abi_too_old")


@pytest.mark.parametrize(
    ("code", "stdout", "expected"),
    [
        (0, b'noise\n{"ok": true, "abi": 7}\n', SelfTestResult(ok=True, abi=7)),
        (1, b'{"ok": true, "abi": 7}\n', SelfTestResult(ok=False, abi=7, error="exit_1")),
        (0, b"not json\n", SelfTestResult(ok=False, error="bad_output")),
        (0, b'{"ok": false, "abi": 2, "error": "abi_too_old"}', _TOO_OLD),
        # 自相矛盾的報告（ok 卻帶 error）不算通過
        (
            0,
            b'{"ok": true, "abi": 7, "error": "check_failed"}',
            SelfTestResult(ok=False, abi=7, error="check_failed"),
        ),
    ],
)
def test_parse_selftest_output(code, stdout, expected):
    assert parse_selftest_output(code, stdout) == expected


# --- 自測介面：用啟動器自己的 run_self_test 產生真實輸出 ---------------------------------------


def _abi_too_old():
    raise kernel_launcher.LaunchError("abi_too_old", "2")


def _no_landlock():
    raise kernel_launcher.LaunchError("landlock_unavailable")


def _abi_3():
    return None, 3


def _abi_5():
    return None, 5


class _FakeListener:
    """自測的 127.0.0.1 listener 替身（ABI≥4 時建兩個；不建立真的 socket）。"""

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 40001)

    def close(self) -> None:
        return None


# 自測讀到的網路設定（Fast Open, MPTCP）；沒列的案例是安全值（MPTCP 不存在）
_SYSCTLS = {"net_tcp_fastopen": ("1\n", None), "net_mptcp": ("0\n", "1\n")}
_PASSED = b'{"checks": {}, "failed": []}'

# 案例：(Landlock 查詢, 第二段子行程的輸出, 啟動器 exit code, 解析結果)
_LAUNCHER_CASES = {
    "pass": (_abi_3, b'{"checks": {}, "failed": []}', 0, SelfTestResult(ok=True, abi=3)),
    "check_failed": (
        _abi_3,
        b'{"checks": {}, "failed": ["rw_write:EACCES"]}',
        1,
        SelfTestResult(ok=False, abi=3, error="check_failed"),
    ),
    "abi_too_old": (_abi_too_old, b"", 125, SelfTestResult(ok=False, abi=2, error="abi_too_old")),
    "landlock_unavailable": (
        _no_landlock,
        b"",
        125,
        SelfTestResult(ok=False, abi=0, error="landlock_unavailable"),
    ),
    # 網路設定不安全：檔案與一般 TCP 的檢查照跑、自測通過，另外回報 net
    "net_tcp_fastopen": (
        _abi_5, _PASSED, 0, SelfTestResult(ok=True, abi=5, net="net_tcp_fastopen")
    ),
    "net_mptcp": (_abi_5, _PASSED, 0, SelfTestResult(ok=True, abi=5, net="net_mptcp")),
}


def _launcher_output(monkeypatch, capsys, tmp_path, case: str) -> tuple[int, bytes]:
    """啟動器 run_self_test 實際寫出的 exit code 與 stdout；只換掉 Landlock 查詢與第二段子行程。"""
    landlock, stage2, _code, _result = _LAUNCHER_CASES[case]
    sysctls = zip(kernel_launcher.NET_SYSCTL_PATHS, _SYSCTLS.get(case, ("0\n", None)), strict=True)
    monkeypatch.setattr(kernel_launcher, "_read_sysctl", dict(sysctls).get)
    monkeypatch.setattr(kernel_launcher, "_listener", lambda *_args: _FakeListener())
    monkeypatch.setattr(kernel_launcher, "_linux_landlock", landlock)
    monkeypatch.setattr(kernel_launcher, "_stage2_checks", lambda _config: [])
    monkeypatch.setattr(kernel_launcher, "_spawn_launcher", lambda *_args: (0, stage2))
    code = kernel_launcher.run_self_test(str(tmp_path / "st"), pin=True)
    return code, capsys.readouterr().out.encode("utf-8")


def test_launcher_selftest_tolerates_a_leftover_fixture(monkeypatch, capsys, tmp_path):
    """同一個 --dir 裡留著上次被打斷的 fixture：自測照樣通過，也不刪不是這次建的東西。"""
    (tmp_path / "st" / "selftest" / "rw").mkdir(parents=True)
    code, stdout = _launcher_output(monkeypatch, capsys, tmp_path, "pass")
    assert parse_selftest_output(code, stdout) == SelfTestResult(ok=True, abi=3)
    assert [p.name for p in (tmp_path / "st").iterdir()] == ["selftest"]


async def test_selftest_dirs_are_fresh_and_stale_ones_are_cleared(tmp_path):
    """容器裡 PID 常是 1：上次留下的 .touka-selftest-* 先清掉，每次自測用新的隨機名稱。"""
    layout = _status_layout(tmp_path)
    base = layout.data_root / "mcp_servers"
    stale = base / f".touka-selftest-{os.getpid()}"
    (stale / "selftest" / "rw").mkdir(parents=True)
    (base / ".touka-selftest-file").write_text("not a directory\n", encoding="utf-8")
    outside = layout.data_root / ".touka-selftest-outside"  # 不在固定的父資料夾
    nested = base / "some_server" / ".touka-selftest-own"  # 別的 MCP 伺服器資料夾裡的
    other = base / "keep-me"
    for folder in (outside, nested, other):
        folder.mkdir(parents=True)
    seen: list[tuple[Path, bool, bool]] = []

    async def _probe(_kernel_dir: Path, test_dir: Path) -> SelfTestResult:
        seen.append((test_dir, test_dir.exists(), stale.exists()))
        return SelfTestResult(ok=True, abi=7)

    monitor = _monitor(layout, _probe)
    await monitor.run_selftest("data", notify=False)
    await monitor.run_selftest("data", notify=False)
    assert monitor.snapshot().local.status == "available"
    (first, first_existed, stale_left), (second, _, _) = seen
    assert not first_existed and not stale_left
    assert first != second
    for test_dir in (first, second):
        assert test_dir.parent == base and test_dir.name.startswith(".touka-selftest-")
    assert outside.is_dir() and nested.is_dir() and other.is_dir()


@pytest.mark.parametrize("case", list(_LAUNCHER_CASES))
def test_parse_keeps_the_launcher_selftest_error(case, monkeypatch, capsys, tmp_path):
    code, stdout = _launcher_output(monkeypatch, capsys, tmp_path, case)
    _landlock, _stage2, expected_code, expected = _LAUNCHER_CASES[case]
    assert code == expected_code
    assert parse_selftest_output(code, stdout) == expected


@pytest.mark.parametrize(
    ("case", "location", "status", "reason"),
    [
        ("pass", "data", "available", None),
        ("check_failed", "data", "unavailable", "fs_9p"),
        ("check_failed", "state", "unavailable", "selftest_failed"),
        ("abi_too_old", "data", "unavailable", "landlock_abi_too_old"),
        # 核心沒有 Landlock 是獨立原因（卡片要分得出「核心沒有」和「自測失敗」）
        ("landlock_unavailable", "data", "unavailable", "landlock_unavailable"),
        ("landlock_unavailable", "state", "unavailable", "landlock_unavailable"),
        # 網路設定讓 TCP 規則擋不住連線：本機 Landlock 仍可用（只有檔案隔離），和磁碟無關
        ("net_tcp_fastopen", "data", "available", None),
        ("net_mptcp", "data", "available", None),
        ("net_mptcp", "state", "available", None),
    ],
)
async def test_monitor_reads_real_launcher_selftest_outcomes(
    case, location, status, reason, monkeypatch, capsys, tmp_path
):
    result = parse_selftest_output(*_launcher_output(monkeypatch, capsys, tmp_path, case))

    async def _probe(_kernel_dir: Path, _test_dir: Path) -> SelfTestResult:
        return result

    monitor = _monitor(_status_layout(tmp_path), _probe)
    await monitor.run_selftest(location, notify=False)
    snap = monitor.snapshot()
    assert (snap.local.status, snap.local.reason) == (status, reason)
    net = case if case in kernel_spec.NET_SYSCTL_CODES else None
    assert snap.local.net == net
    if net:  # 層級 none、原因是網路設定（不是 fs_9p），features 不含 tcp
        assert (snap.level, snap.reason, "tcp" in snap.features) == ("none", net, False)


async def test_default_selftest_runs_isolated_launcher_in_pin_mode(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"ok": true, "abi": 7}', b""

    async def _spawn(*argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(status_module, "spawn_exec", _spawn)
    result = await status_module.launcher_selftest(tmp_path / "kernel", tmp_path / "t")
    assert result == SelfTestResult(ok=True, abi=7)
    argv = seen["argv"]
    assert argv[1:4] == ("-I", "-S", str(tmp_path / "kernel" / "launcher.py"))
    assert argv[4:] == ("--self-test", "--pin", "--dir", str(tmp_path / "t"))
    assert not any("TOKEN" in key or "SECRET" in key for key in seen["env"])


async def test_cancelled_selftest_kills_and_reaps_the_launcher_first(tmp_path, monkeypatch):
    """關機或 reload 取消自測：先殺掉啟動器並等它結束，才把取消往外傳（_probe 之後才清資料夾）。"""
    events: list[str] = []
    reading = asyncio.Event()

    class _Proc:
        returncode = None

        async def communicate(self):
            reading.set()
            await asyncio.Event().wait()

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    async def _spawn(*_argv, **_kwargs):
        return _Proc()

    monkeypatch.setattr(status_module, "spawn_exec", _spawn)
    task = asyncio.create_task(
        status_module.launcher_selftest(tmp_path / "kernel", tmp_path / "t")
    )
    await asyncio.wait_for(reading.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["kill", "wait"]


# --- C2-2 U2d：refresh 分兩段、版面快取、/opt 雜湊、開機例外、原因排序 ---------------------


def _on_loop_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


async def test_refresh_does_its_disk_work_off_the_event_loop(tmp_path, monkeypatch):
    """版面、kernel 雜湊、ST 檢查都在執行緒裡做；之後事件迴圈上只用快取的版面."""
    layout = _status_layout(tmp_path)
    seen: list[tuple[str, bool]] = []
    real_digest, real_state = status_module.kernel_digest, status_module._state_issue

    def _layout():
        seen.append(("layout", _on_loop_thread()))
        return layout

    def _digest(path):
        seen.append(("digest", _on_loop_thread()))
        return real_digest(path)

    def _state(arg):
        seen.append(("state", _on_loop_thread()))
        return real_state(arg)

    monkeypatch.setattr(status_module, "kernel_digest", _digest)
    monkeypatch.setattr(status_module, "_state_issue", _state)
    monitor = IsolationMonitor(
        layout_provider=_layout, selftest=_SelfTest(), clock=_Clock(), platform="linux",
        start_background=False,
    )
    assert (await monitor.ensure_ready()).level == "landlock"
    seen.clear()
    await monitor.refresh()
    assert {name for name, _ in seen} == {"layout", "digest", "state"}
    assert not any(on_loop for _, on_loop in seen), seen
    seen.clear()
    alice = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    monitor.snapshot()
    monitor.fallback_for(profile_complete=True)
    await monitor.level_for(alice, None)
    monitor.next_interval()
    assert seen == []


async def test_the_image_kernel_is_hashed_once_and_live_src_every_refresh(tmp_path, monkeypatch):
    layout = _status_layout(tmp_path)
    hashed: list[Path] = []
    real_digest = status_module.kernel_digest

    def _digest(path):
        hashed.append(path)
        return real_digest(path)

    monkeypatch.setattr(status_module, "kernel_digest", _digest)
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    for _ in range(3):
        await monitor.refresh()
    live = layout.proxy_root / "src" / "sandbox_kernel"
    assert hashed.count(layout.kernel_dir) == 1
    assert hashed.count(live) == 4
    live.joinpath("launcher.py").write_text("# rebuilt\n", encoding="utf-8")
    assert (await monitor.refresh()).reason == "kernel_outdated"


async def test_native_kernel_dir_is_the_live_src_and_hashed_every_refresh(tmp_path, monkeypatch):
    live = tmp_path / "ToukaProxy" / "src" / "sandbox_kernel"
    layout = _status_layout(tmp_path, in_container=False, kernel_dir=live)
    hashed: list[Path] = []
    real_digest = status_module.kernel_digest
    monkeypatch.setattr(
        status_module, "kernel_digest", lambda p: hashed.append(p) or real_digest(p)
    )
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    await monitor.refresh()
    assert hashed == [live, live]
    assert monitor.checked_kernel_digest == real_digest(live)


async def test_concurrent_refreshes_share_one_disk_check(tmp_path):
    layout = _status_layout(tmp_path)
    calls: list[int] = []
    monitor = _monitor(layout)
    monitor._layout_provider = lambda: calls.append(1) or layout
    await asyncio.gather(monitor.refresh(), monitor.refresh(), monitor.refresh())
    assert len(calls) == 1


class _ExplodingSelfTest(_SelfTest):
    """第一次自測丟出非 OSError 的例外（例如啟動器輸出解析出錯）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.explode = True

    async def __call__(self, kernel_dir: Path, test_dir: Path) -> SelfTestResult:
        if self.explode:
            self.explode = False
            self.calls.append("boom")
            raise RuntimeError("unexpected")
        return await super().__call__(kernel_dir, test_dir)


async def test_boot_selftest_exception_needs_a_9p_selftest_to_recover(tmp_path):
    """開機自測丟出任何例外都算失敗；9p 沒驗證過之前，背景只做 9p 自測（ST 通過不算恢復）."""
    probe = _ExplodingSelfTest(data_ok=False, state_ok=True)
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    snap = await monitor.ensure_ready()
    assert (snap.local.status, snap.level) == ("unavailable", "none")
    clock.now += SELFTEST_RETRY_SECONDS + 1
    await monitor.run_due_checks()
    assert probe.calls == ["boom", "data"]
    assert monitor.snapshot().local.status == "unavailable"
    probe.data_ok = True
    clock.now += SELFTEST_RETRY_SECONDS + 1
    await monitor.run_due_checks()
    assert probe.calls == ["boom", "data", "data"]
    assert monitor.snapshot().level == "landlock"


async def test_kernel_verdicts_keep_retrying_on_state(tmp_path):
    """ABI 太舊這類核心層的結論和 9p 無關：照舊在 ST 上重試。"""
    probe = _SelfTest(abi=2)
    clock = _Clock()
    monitor = _monitor(_status_layout(tmp_path), probe, clock=clock)
    await monitor.ensure_ready()
    clock.now += SELFTEST_RETRY_SECONDS + 1
    await monitor.run_due_checks()
    assert probe.calls == ["data", "state"]


@pytest.mark.parametrize(
    ("broken", "reason", "detail"),
    [
        ("kernel", "kernel_outdated", None),
        ("state", "sandbox_state_unconfigured", "unset"),
    ],
)
async def test_fallback_deciding_reasons_come_before_the_network(tmp_path, broken, reason, detail):
    """網路設定不安全又同時有 kernel／ST 的問題：原因寫決定了降級去處（未隔離）的那個.

    FE 未解 2：原因與降級去處對不上。
    """
    layout = _status_layout(tmp_path, **({"state_root": None} if broken == "state" else {}))
    if broken == "kernel":
        live = layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py"
        live.write_text("# edited live src\n", encoding="utf-8")
    monitor = _monitor(layout, _SelfTest(abi=7, net="net_mptcp"))
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason, snap.detail) == ("none", reason, detail)
    assert snap.local_issue == reason
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan("direct", frozenset())


async def test_abi_3_with_a_kernel_issue_names_the_kernel(tmp_path):
    layout = _status_layout(tmp_path)
    (layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py").write_text("# x\n", "utf-8")
    snap = await _monitor(layout, _SelfTest(abi=3)).ensure_ready()
    assert snap.reason == "kernel_outdated"


# --- C2-2 U2d：執行器層的判定 ---------------------------------------------------------------


async def test_executor_level_when_the_ready_matches_everything(tmp_path):
    monitor, executor, layout = _executor_monitor(tmp_path)
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("executor", "executor", None)
    assert snap.features == policy.landlock_features(6)
    assert snap.executor is not None and snap.executor.boot_id == "b" * 32
    (ipc, _ports), = executor.calls
    assert ipc == layout.executor_dir


class _GatedExecutor(_MatchingExecutor):
    """健康檢查卡住，直到 ``gate`` 打開（開機後執行器還沒回應的那段時間）。"""

    def __init__(self, layout: SandboxLayout) -> None:
        super().__init__(layout)
        self.gate = asyncio.Event()

    async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        await self.gate.wait()
        return await super().__call__(ipc_dir, block_ports)


async def test_executor_is_probe_pending_until_its_first_health_check_returns(tmp_path):
    """第一次健康檢查回來之前是 none／``probe_pending``：Auto／YOLO 要人核准，不是 executor."""
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    executor = _GatedExecutor(layout)
    monitor = _monitor(
        layout, executor_health=executor, executor_digests=_Digests(), health_timeout=0.05
    )
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("none", "executor", "probe_pending")
    executor.gate.set()
    await monitor.refresh()
    assert monitor.snapshot().level == "executor"


async def test_unwired_digests_are_a_version_mismatch(tmp_path):
    """執行器伺服器的雜湊 SSOT 還沒接上（預設）：一律版本不符，不會判成執行器層."""
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    monitor = _monitor(layout, executor_health=_MatchingExecutor(layout))
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason) == ("none", "executor_version_mismatch")


_EXECUTOR_CASES = {
    "unreachable": ({"reason": "executor_unreachable"}, "executor_unreachable"),
    "auth_failed": ({"reason": "executor_auth_failed"}, "executor_auth_failed"),
    "version": ({"reason": "executor_version_mismatch"}, "executor_version_mismatch"),
    "protocol_v": ({"version": 2}, "executor_version_mismatch"),
    "kernel": ({"kernel_digest": "0" * 64}, "kernel_outdated"),
    "server": ({"server_digest": "0" * 64}, "executor_version_mismatch"),
    "block_ports": ({"block_ports_digest": "p1"}, "executor_version_mismatch"),
    "roots_extra": ({"roots": "extra"}, "executor_roots_mismatch"),
    "not_enforced": ({"enforced": False}, "executor_selftest_failed"),
    "abi_2": ({"abi": 2}, "landlock_abi_too_old"),
}


def _case_executor(layout: SandboxLayout, case: str) -> _Executor:
    overrides, _reason = _EXECUTOR_CASES[case]
    if "reason" in overrides:
        return _Executor(reason=overrides["reason"])
    if overrides.get("roots") == "extra":
        extra = ExecutorRoot(str(layout.install_root / "workspace"), "rw")
        roots = (*_ready_for(layout).roots, extra)
        return _MatchingExecutor(layout, roots=roots)
    if "block_ports_digest" in overrides:
        return _Executor(_ready_for(layout, **overrides))
    return _MatchingExecutor(layout, **overrides)


@pytest.mark.parametrize("case", list(_EXECUTOR_CASES))
async def test_executor_is_only_judged_executor_when_every_check_passes(tmp_path, case):
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    executor = _case_executor(layout, case)
    monitor = _monitor(layout, executor_health=executor, executor_digests=_Digests())
    snap = await monitor.ensure_ready()
    reason = _EXECUTOR_CASES[case][1]
    assert (snap.level, snap.expected, snap.reason) == ("none", "executor", reason)
    assert snap.executor is None
    # 執行器不能用：降級去處是本機 Landlock（合格也一樣），features 是本機的
    plan = monitor.fallback_for(profile_complete=True, executor_eligible=True)
    assert plan == FallbackPlan("landlock", policy.landlock_features(7))


async def test_unreadable_live_kernel_is_check_failed_for_the_executor(tmp_path, monkeypatch):
    monitor, _executor, _layout = _executor_monitor(tmp_path)
    assert (await monitor.ensure_ready()).level == "executor"
    monkeypatch.setattr(status_module, "kernel_digest", _unreadable_kernel)
    snap = await monitor.refresh()
    assert (snap.level, snap.reason) == ("none", "isolation_check_failed")


async def test_executor_abi_3_stays_executor_with_file_features_only(tmp_path):
    """ABI 3：仍判執行器層（只有升級核心才修得好，已告知），features 只有 fs、沒有 tcp."""
    for net in (None, "net_tcp_fastopen"):
        layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
        executor = _MatchingExecutor(layout, abi=3, net=net)
        monitor = _monitor(layout, executor_health=executor, executor_digests=_Digests())
        snap = await monitor.ensure_ready()
        assert (snap.level, snap.reason, snap.features) == ("executor", None, frozenset({"fs"}))


@pytest.mark.parametrize("code", ["net_tcp_fastopen", "net_mptcp"])
async def test_unsafe_net_on_the_executor_is_none_and_falls_back_to_it(tmp_path, code):
    """網路設定不安全（ABI≥4）：判 none；核准後的去處是執行器，features 不含 tcp.

    runner 依 features 送 ``net_strict``（不含 tcp 就是 false）。
    """
    monitor, executor, _layout = _executor_monitor(tmp_path)
    executor.overrides = {"net": code}
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.expected, snap.reason) == ("none", "executor", code)
    assert snap.executor is not None
    plan = monitor.fallback_for(profile_complete=True, executor_eligible=True)
    assert plan == FallbackPlan("executor", policy.landlock_features(6) - {"tcp"})
    assert "tcp" not in plan.features  # runner 依 features 送 net_strict=false
    # 這次不在執行器範圍（專案、custom 資料夾）：本機 Landlock，原本的 features
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan(
        "landlock", policy.landlock_features(7)
    )


async def test_executor_fallback_with_a_broken_local_landlock_is_direct_when_not_eligible(
    tmp_path,
):
    monitor, executor, _layout = _executor_monitor(tmp_path, state_root=None)
    executor.overrides = {"net": "net_mptcp"}
    snap = await monitor.ensure_ready()
    assert (snap.reason, snap.local_issue) == ("net_mptcp", "sandbox_state_unconfigured")
    assert monitor.fallback_for(profile_complete=True).kind == "direct"
    assert monitor.fallback_for(profile_complete=True, executor_eligible=True).kind == "executor"


async def test_executor_boot_id_change_notifies_listeners(tmp_path):
    clock = _Clock()
    monitor, executor, _layout = _executor_monitor(tmp_path, clock=clock)
    seen: list[str | None] = []

    async def _record(snapshot):
        seen.append(snapshot.executor.boot_id if snapshot.executor else None)

    monitor.on_level_change(_record)
    await monitor.ensure_ready()
    await monitor.refresh()  # 同一個 boot_id：不通知
    executor.overrides = {"boot_id": "9" * 32}  # 執行器重啟過
    await monitor.refresh()
    await monitor.refresh()
    await asyncio.sleep(0)
    assert seen == ["b" * 32, "9" * 32]


async def test_executor_health_is_cached_ten_seconds_on_success_one_on_failure(tmp_path):
    clock = _Clock()
    monitor, executor, _layout = _executor_monitor(tmp_path, clock=clock)
    await monitor.ensure_ready()
    clock.now += HEALTH_CACHE_OK_SECONDS - 1
    await monitor.ensure_ready()
    assert len(executor.calls) == 1
    clock.now += 2
    await monitor.ensure_ready()
    assert len(executor.calls) == 2
    executor.health = ExecutorHealth(None, "executor_unreachable")
    clock.now += HEALTH_CACHE_OK_SECONDS + 1
    assert (await monitor.ensure_ready()).level == "none"
    clock.now += 0.5
    await monitor.ensure_ready()
    assert len(executor.calls) == 3
    clock.now += 0.6
    await monitor.ensure_ready()
    assert len(executor.calls) == 4


async def test_level_for_on_the_executor_layer(tmp_path):
    """執行器層：靈魂自己的 personal_drive 才行；custom 資料夾或綁了專案是 none（兩種原因）."""
    data = Path(resource_manager.get_base_path()).resolve()
    monitor, _executor, _layout = _executor_monitor(tmp_path, data_root=data)
    assert (await monitor.ensure_ready()).level == "executor"
    soul = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    custom = AgentIdentity.for_role("gpt-x", "Base", capabilities=[])
    project = tmp_path / "work"
    project.mkdir()
    assert await monitor.level_for(soul, None) == LevelDecision("executor")
    assert await monitor.level_for(soul, project) == LevelDecision(
        "none", "project_not_in_executor"
    )
    assert await monitor.level_for(custom, None) == LevelDecision(
        "none", "agent_dir_not_in_executor"
    )
    assert monitor.executor_scope_issue(soul, None) is None
    assert monitor.executor_scope_issue(custom, project) == "agent_dir_not_in_executor"


@pytest.mark.parametrize(
    ("kind", "issue"),
    [
        ("dir", None),
        ("missing", None),
        ("link", "agent_dir_not_in_executor"),
        ("file", "agent_dir_not_in_executor"),
        ("other", "agent_dir_not_in_executor"),
    ],
)
async def test_executor_scope_classifies_the_drive_without_resolving_it(
    tmp_path, monkeypatch, kind, issue
):
    """範圍看個人空間區的分類（``zone_kind``，不跟隨），不解析路徑（ADR-0293 R7）."""
    data = Path(resource_manager.get_base_path()).resolve()
    monitor, _executor, _layout = _executor_monitor(tmp_path, data_root=data)
    soul = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    seen: list[object] = []

    def _kind(zp):
        seen.append(zp)
        return kind

    monkeypatch.setattr(resource_manager, "zone_kind", _kind)
    assert monitor.executor_scope_issue(soul, None) == issue
    assert seen == [resource_manager.agent_drive(soul)]


async def test_executor_scope_is_out_when_the_drive_cannot_be_read(tmp_path, monkeypatch):
    data = Path(resource_manager.get_base_path()).resolve()
    monitor, _executor, _layout = _executor_monitor(tmp_path, data_root=data)
    soul = AgentIdentity.for_role("Alice", "Owner", capabilities=[])

    def _refuse(_zp):
        raise PermissionError("denied")

    monkeypatch.setattr(resource_manager, "zone_kind", _refuse)
    assert monitor.executor_scope_issue(soul, None) == "agent_dir_not_in_executor"


async def test_a_linked_soul_drive_is_not_in_the_executor(tmp_path, zone_data_root):
    """靈魂的個人空間是連結（即使指到 personal_drive 裡別的資料夾）：不在執行器範圍."""
    monitor, _executor, _layout = _executor_monitor(tmp_path, data_root=zone_data_root)
    soul = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    drives = zone_data_root / "personal_drive"
    (drives / "Bob").mkdir(parents=True)
    make_dir_link(drives / "Alice", drives / "Bob")
    assert monitor.executor_scope_issue(soul, None) == "agent_dir_not_in_executor", LINK_FOLLOWED


@pytest.mark.parametrize("local_ok", [True, False], ids=["local-landlock", "local-broken"])
async def test_level_for_puts_the_executor_scope_before_net_reasons(tmp_path, local_ok):
    """快照是 none、只差網路設定時（§2.8 原因順序）：決定降級去處的原因排在網路原因前面.

    綁了專案或資料夾不在執行器：去處只能是本機 Landlock（或本機也壞了就是未隔離），帳本與卡片
    要寫範圍（或本機的原因），不是網路設定。
    """
    data = Path(resource_manager.get_base_path()).resolve()
    probe = _SelfTest(data_ok=local_ok, state_ok=local_ok)
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec", data_root=data)
    executor = _MatchingExecutor(layout, net="net_mptcp")
    monitor = _monitor(layout, probe, executor_health=executor, executor_digests=_Digests())
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason) == ("none", "net_mptcp")
    soul = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    custom = AgentIdentity.for_role("gpt-x", "Base", capabilities=[])
    project = tmp_path / "work"
    project.mkdir()
    local = None if local_ok else snap.local_issue
    assert await monitor.level_for(soul, None) == LevelDecision("none", "net_mptcp")
    assert await monitor.level_for(soul, project) == LevelDecision(
        "none", local or "project_not_in_executor"
    )
    assert await monitor.level_for(custom, None) == LevelDecision(
        "none", local or "agent_dir_not_in_executor"
    )


@pytest.mark.parametrize(
    "code",
    ["executor_unreachable", "executor_restarted", "executor_auth_failed", "executor_rejected"],
)
async def test_relay_failure_codes_mark_the_executor_until_the_next_health_check(tmp_path, code):
    """relay 經狀態 fd 回報的代碼（指令寫不進去）：執行器先照這個原因判 none，不重做本機自測."""
    clock = _Clock()
    probe = _SelfTest()
    monitor, executor, _layout = _executor_monitor(tmp_path, selftest=probe, clock=clock)
    assert (await monitor.ensure_ready()).level == "executor"
    monitor.report_failure(code)
    snap = monitor.snapshot()
    assert (snap.level, snap.reason) == ("none", code)
    await asyncio.sleep(0.01)
    assert probe.calls == ["data"]  # 不是本機的問題
    assert (await monitor.ensure_ready()).level == "executor"  # 下一次健康檢查就恢復
    assert len(executor.calls) == 2


# --- C2-2 U2d：proxy 開機時清自己的 tmp ------------------------------------------------------


def _proxy_state(layout: SandboxLayout) -> dict[str, Path]:
    """ST 底下的幾個位置：要清的 proxy tmp，和不該碰的東西."""
    proxy = layout.state_root / "proxy"
    paths = {
        "agent_tmp": proxy / "agents" / "Alice" / "tmp",
        "evolution_tmp": proxy / "evolution" / "skill_a" / "tmp",
        "agent_home": proxy / "agents" / "Alice" / "home",
        "nested_tmp": proxy / "agents" / "Alice" / "home" / "tmp",
        "leftover": proxy / "agents" / "Alice" / "tmp.trash-0123456789abcdef",
        "executor_tmp": layout.state_root / "executor" / "agents" / "Alice" / "tmp",
        "other_kind_tmp": proxy / "scratch" / "Alice" / "tmp",
        "data_tmp": layout.data_root / "personal_drive" / "Alice" / "tmp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
        (path / "f.txt").write_text("x", encoding="utf-8")
    return paths


async def _drain_background() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_first_ensure_ready_renames_proxy_tmp_before_the_selftest_and_deletes_it(tmp_path):
    layout = _status_layout(tmp_path)
    paths = _proxy_state(layout)
    seen_at_selftest: list[bool] = []

    async def _selftest(_kernel_dir: Path, _test_dir: Path) -> SelfTestResult:
        seen_at_selftest.append(paths["agent_tmp"].exists())
        return SelfTestResult(ok=True, abi=7)

    monitor = _monitor(layout, _selftest)
    await monitor.ensure_ready()
    await _drain_background()
    assert seen_at_selftest == [False]  # 先改名，自測與指令都在那之後
    for gone in ("agent_tmp", "evolution_tmp", "leftover"):
        assert not paths[gone].exists(), gone
    for kept in ("agent_home", "nested_tmp", "executor_tmp", "other_kind_tmp", "data_tmp"):
        assert (paths[kept] / "f.txt").is_file(), kept
    owner = paths["agent_tmp"].parent
    assert sorted(p.name for p in owner.iterdir()) == ["home"]


async def test_proxy_tmp_is_cleared_only_once(tmp_path):
    layout = _status_layout(tmp_path)
    paths = _proxy_state(layout)
    clock = _Clock()
    monitor = _monitor(layout, clock=clock)
    await monitor.ensure_ready()
    await _drain_background()
    paths["agent_tmp"].mkdir()  # 之後的指令建的
    clock.now += 61
    await monitor.ensure_ready()
    await monitor.run_due_checks()
    await _drain_background()
    assert paths["agent_tmp"].is_dir()


async def test_first_ensure_ready_also_clears_the_mcp_state_tmp(tmp_path):
    """C2-3：MCP 的 state（自建、第三方分開父目錄）開機一樣清 tmp、留 home."""
    layout = _status_layout(tmp_path)
    proxy = layout.state_root / "proxy"
    owners = [proxy / kind / "m0123456789abcdef01234567" for kind in MCP_PROXY_STATE_KINDS.values()]
    for owner in owners:
        for sub in ("home", "tmp"):
            (owner / sub).mkdir(parents=True)
            (owner / sub / "f.txt").write_text("x", encoding="utf-8")
    await _monitor(layout).ensure_ready()
    await _drain_background()
    for owner in owners:
        assert sorted(p.name for p in owner.iterdir()) == ["home"], owner.parent.name
        assert (owner / "home" / "f.txt").is_file()


async def test_proxy_tmp_that_is_not_a_real_directory_is_left_alone(tmp_path):
    layout = _status_layout(tmp_path)
    owner = layout.state_root / "proxy" / "agents" / "Alice"
    owner.mkdir(parents=True)
    (owner / "tmp").write_text("a file, not a folder", encoding="utf-8")
    await _monitor(layout).ensure_ready()
    await _drain_background()
    assert (owner / "tmp").read_text(encoding="utf-8") == "a file, not a folder"


async def test_native_windows_does_not_clear_proxy_tmp(tmp_path):
    layout = _status_layout(tmp_path)
    paths = _proxy_state(layout)
    await _monitor(layout, platform="win32").ensure_ready()
    await _drain_background()
    assert paths["agent_tmp"].is_dir()


async def test_a_crashing_health_probe_is_unreachable_not_an_exception(tmp_path, caplog):
    class _Crash(_Executor):
        async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
            raise RuntimeError

    monitor, _executor, _layout = _executor_monitor(tmp_path, executor=_Crash())
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason) == ("none", "executor_unreachable")
    assert "RuntimeError" in caplog.text


# --- 接線：monitor 預設就用執行器伺服器的 SSOT（U2c）算兩個雜湊 ---------------------


class _ServerReady(_Executor):
    """READY 的兩個雜湊照執行器伺服器的算法（``executor_code_digest``、``framing``）."""

    def __init__(self, layout: SandboxLayout) -> None:
        super().__init__()
        self.layout = layout

    async def __call__(self, ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        self.calls.append((ipc_dir, tuple(block_ports)))
        ready = _ready_for(
            self.layout,
            server_digest=executor_code_digest(self.layout.proxy_root),
            block_ports_digest=framing.block_ports_digest(block_ports),
        )
        return ExecutorHealth(ready)


async def test_monitor_checks_ready_with_the_executor_server_ssot_by_default(tmp_path):
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    for name in EXECUTOR_CODE_FILES:  # 伺服器程式清單（proxy 這邊的原始碼）
        path = layout.proxy_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n", encoding="utf-8")
    monitor = _monitor(layout, executor_health=_ServerReady(layout))  # 不注入 executor_digests
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason) == ("executor", None)
    (layout.proxy_root / EXECUTOR_CODE_FILES[0]).write_text("# changed\n", encoding="utf-8")
    monitor_after_edit = _monitor(layout, executor_health=_Executor(_ready_for(layout)))
    assert (await monitor_after_edit.ensure_ready()).reason == "executor_version_mismatch"


# --- C2-3 U3d：本機判定（第三方 MCP 與 scaffold 只看 proxy 本機 Landlock）--------------------


async def test_local_decision_on_a_non_linux_platform_is_native_platform_not_pending(tmp_path):
    """HIGH-2：原生 Windows 不自測、本機狀態永遠 pending；判定先看平台，``native_platform`` 穩定."""
    for layout in (
        _status_layout(tmp_path),
        _status_layout(tmp_path, executor_dir=tmp_path / "exec"),
    ):
        monitor = _monitor(layout, platform="win32")
        await monitor.ensure_ready()
        assert monitor.snapshot().local.status == "pending"
        decision = monitor.local_decision()
        assert decision == LocalDecision("unavailable", "native_platform")
        assert decision.stable
        assert monitor.local_level() == LevelDecision("none", "native_platform")


async def test_local_decision_is_pending_only_on_linux_before_the_first_selftest(tmp_path):
    probe = _SelfTest()
    probe.gate = asyncio.Event()
    monitor = _monitor(_status_layout(tmp_path), probe, ready_timeout=0.05)
    await monitor.ensure_ready()
    decision = monitor.local_decision()
    assert decision == LocalDecision("pending", "probe_pending")
    assert not decision.stable
    probe.gate.set()
    await asyncio.sleep(0.05)
    assert monitor.local_decision().status == "available"


async def test_local_decision_features_drop_tcp_when_the_net_or_the_ports_are_unsafe(
    tmp_path, monkeypatch
):
    """可用時照樣包（features 不含 tcp）；ABI 3 只有檔案隔離."""
    ok = _monitor(_pointed_layout(monkeypatch, tmp_path / "ok"))
    await ok.ensure_ready()
    assert ok.local_decision() == LocalDecision("available", features=policy.landlock_features(7))
    net_layout = _pointed_layout(monkeypatch, tmp_path / "net")
    net = _monitor(net_layout, _SelfTest(net="net_tcp_fastopen"))
    await net.ensure_ready()
    assert net.local_decision().features == policy.landlock_features(7) - {"tcp"}
    abi_3 = _monitor(_pointed_layout(monkeypatch, tmp_path / "abi3"), _SelfTest(abi=3))
    await abi_3.ensure_ready()
    assert abi_3.local_decision() == LocalDecision("available", features=frozenset({"fs"}))
    layout = _pointed_layout(monkeypatch, tmp_path / "ports")
    layout.config_dir.mkdir(parents=True)
    (layout.config_dir / "connections.json").write_text("[]", encoding="utf-8")  # 結構不對
    ports = _monitor(layout)
    await ports.ensure_ready()
    assert ports.snapshot().block_ports_issue == "block_ports_incomplete"
    assert ports.local_decision().features == policy.landlock_features(7) - {"tcp"}


def _local_case_monitor(case: str, tmp_path: Path, monkeypatch) -> IsolationMonitor:
    """本機原因的各種情況（真的 monitor，注入版面與自測結果）."""
    layouts = {
        "state_unset": {"state_root": None},
        "kernel_not_installed": {"kernel_dir": None},
        "executor_expected": {"executor_dir": tmp_path / "exec"},
    }
    if case == "state_not_writable":
        blocker = tmp_path / "state-is-a-file"
        blocker.write_text("", encoding="utf-8")
        layouts[case] = {"state_root": blocker}
    layout = _status_layout(tmp_path, **layouts.get(case, {}))
    probes = {
        "abi_too_old": _SelfTest(abi=2),
        "landlock_unavailable": _SelfTest(abi=0, data_ok=False, error="landlock_unavailable"),
        "interpreter_untrusted": _SelfTest(abi=0, data_ok=False, error="interpreter_untrusted"),
        "selftest_failed": _SelfTest(abi=0, data_ok=False),
        "fs_9p": _SelfTest(data_ok=False),
    }
    if case == "kernel_outdated":
        live = layout.proxy_root / "src" / "sandbox_kernel" / "launcher.py"
        live.write_text("# edited live src\n", encoding="utf-8")
    elif case == "kernel_unreadable":
        monkeypatch.setattr(status_module, "kernel_digest", _unreadable_kernel)
    return _monitor(layout, probes.get(case))


@pytest.mark.parametrize(
    ("case", "reason", "detail", "stable"),
    [
        ("kernel_not_installed", "kernel_not_installed", None, True),
        ("abi_too_old", "landlock_abi_too_old", None, True),
        ("landlock_unavailable", "landlock_unavailable", None, True),
        ("interpreter_untrusted", "interpreter_untrusted", None, True),
        ("kernel_outdated", "kernel_outdated", None, True),
        ("state_unset", "sandbox_state_unconfigured", "unset", True),
        ("state_not_writable", "sandbox_state_unconfigured", "not_writable", False),
        ("kernel_unreadable", "isolation_check_failed", None, False),
        ("selftest_failed", "selftest_failed", None, False),
        ("fs_9p", "fs_9p", None, False),
    ],
)
async def test_local_decision_reasons_are_stable_or_temporary(
    tmp_path, monkeypatch, case, reason, detail, stable
):
    """MEDIUM-5：只有映像、核心、kernel 版本、直譯器與 ST 沒設是穩定原因；其他都是暫時性."""
    monitor = _local_case_monitor(case, tmp_path, monkeypatch)
    await monitor.ensure_ready()
    decision = monitor.local_decision()
    assert decision == LocalDecision("unavailable", reason, detail)
    assert decision.stable is stable
    assert monitor.local_level() == LevelDecision("none", reason)


def test_unknown_or_missing_local_reasons_are_temporary():
    """看不懂的原因、沒有原因都當暫時性（等，不未隔離啟動：fail-closed）."""
    for decision in (
        LocalDecision("unavailable", "made_up_reason"),
        LocalDecision("unavailable"),
        LocalDecision("unavailable", "sandbox_state_unconfigured"),
        LocalDecision("pending", "kernel_not_installed"),
        LocalDecision("available", "native_platform"),
    ):
        assert not decision.stable, decision


async def test_local_level_is_landlock_only_with_abi_4_a_safe_net_and_complete_ports(
    tmp_path, monkeypatch
):
    """本機層級（scaffold 的試跑）：同 landlock 層的規則，但不看部署預期（執行器部署也看本機）."""
    executor = _monitor(
        _pointed_layout(monkeypatch, tmp_path / "exec", executor_dir=tmp_path / "ipc")
    )
    snap = await executor.ensure_ready()
    assert snap.expected == "executor" and snap.level == "none"
    assert executor.local_level() == LevelDecision("landlock")
    abi_3 = _monitor(_pointed_layout(monkeypatch, tmp_path / "abi3"), _SelfTest(abi=3))
    await abi_3.ensure_ready()
    assert abi_3.local_level() == LevelDecision("none", "landlock_abi_insufficient")
    net = _monitor(_pointed_layout(monkeypatch, tmp_path / "net"), _SelfTest(net="net_mptcp"))
    await net.ensure_ready()
    assert net.local_level() == LevelDecision("none", "net_mptcp")
    layout = _pointed_layout(monkeypatch, tmp_path / "ports")
    layout.config_dir.mkdir(parents=True)
    (layout.config_dir / "connections.json").write_text("[]", encoding="utf-8")
    ports = _monitor(layout)
    await ports.ensure_ready()
    assert ports.local_level() == LevelDecision("none", "block_ports_incomplete")


@pytest.mark.parametrize("case", list(_DOUBLES))
async def test_pinned_monitor_local_decision_matches_the_real_monitor(tmp_path, monkeypatch, case):
    """同一個情況下，替身和真的 monitor 的本機判定與本機層級相同（MCP 的測試靠替身守）."""
    real = await _real_monitor(case, tmp_path, monkeypatch)
    double = _DOUBLES[case]()
    assert double.local_decision() == real.local_decision()
    assert double.local_level() == real.local_level()


async def test_first_local_selftest_result_notifies_even_when_the_level_stays_none(tmp_path):
    """C2-3 §2.7：執行器預期但連不上、本機從 pending 變成不可用：層級一直是 none，照樣通知."""
    probe = _SelfTest(data_ok=False)
    probe.gate = asyncio.Event()
    layout = _status_layout(tmp_path, executor_dir=tmp_path / "exec")
    executor = _Executor(reason="executor_unreachable")
    monitor = _monitor(layout, probe, ready_timeout=0.05, executor_health=executor)
    seen: list[str | None] = []

    async def _record(snapshot):
        seen.append(snapshot.local_issue)

    monitor.on_level_change(_record)
    await monitor.ensure_ready()  # 自測還沒回來：第一次健康檢查不通知
    await asyncio.sleep(0)
    assert monitor.snapshot().local.status == "pending" and seen == []
    probe.gate.set()
    await asyncio.sleep(0.05)
    assert monitor.snapshot().level == "none"
    assert seen == ["fs_9p"]


async def test_local_issue_change_notifies_under_the_executor_level(tmp_path, monkeypatch):
    """MEDIUM-R3-2：執行器部署下 ST 恢復可寫，層級與本機狀態都不變，仍要通知（第三方才會重啟）."""
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("", encoding="utf-8")
    point_zone_roots(monkeypatch, tmp_path / "data", blocker)  # RM 的 ST 也是這個檔案
    monitor, _executor, _layout = _executor_monitor(tmp_path, state_root=blocker)
    seen: list[tuple[str, str | None]] = []

    async def _record(snapshot):
        seen.append((snapshot.level, snapshot.local_issue))

    monitor.on_level_change(_record)
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.local.status) == ("executor", "available")
    blocker.unlink()
    blocker.mkdir()  # ST 恢復可寫
    await monitor.refresh()
    await monitor.refresh()  # 沒有再變：不重複通知
    await asyncio.sleep(0)
    assert seen == [("executor", "sandbox_state_unconfigured"), ("executor", None)]
