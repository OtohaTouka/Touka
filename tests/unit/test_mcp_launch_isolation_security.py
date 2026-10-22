"""MCP 伺服器的啟動判定與 broker（iso-c2 C2-3 U3d；設計 §2.1、§2.4、§2.6～§2.8，v2、v2.1）.

- runner：條目自己的 env 走 spec 的 ``exec_env``（啟動器套完限制才給目標），啟動器自己的環境
  沒有；profile 管的名稱與禁令 19 的變數先濾掉並記警告（只記名稱，值不進 log）。
全部用替身 monitor 與 tmp_path 的假版面；不 spawn、不連線、不碰真的資料夾。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from src.models.mcp_isolation import SandboxPathGrant
from src.routers import mcp as mcp_router
from src.sandbox_kernel.spec import parse_spec
from src.services.isolation.consent import Consent, ExecutionGrant
from src.services.isolation.executor_client import ExecutorReady
from src.services.isolation.mcp_launch import (
    McpLaunchBook,
    McpLaunchCounts,
    McpLauncher,
    McpLaunchInfo,
    McpLaunchWaiting,
    McpManualStart,
    entry_identity,
    manual_start_subject,
    mcp_launcher,
)
from src.services.isolation.mcp_profiles import mcp_state_dirs
from src.services.isolation.profiles import PathRule, SandboxProfile
from src.services.isolation.runners import (
    DirectRunner,
    ExecRequest,
    ExecutorRunner,
    IsolationPendingError,
    IsolationUnavailableError,
    LandlockRunner,
    ProfileSource,
    RunnerBroker,
    ThirdPartyAcquired,
    launcher_spec,
)
from src.services.isolation.status import FallbackPlan, LocalDecision
from src.services.mcp_isolation_store import IsolationRecord, bound_of
from src.services.mcp_isolation_store import IsolationSnapshot as StoreSnapshot
from src.utils.isolation_policy import landlock_features
from src.utils.mcp_origin import UNREADABLE_RECORD, RawMcpEntry
from src.utils.mcp_sentinels import APPDATA_SENTINEL as APPDATA
from src.utils.mcp_sentinels import resolve_env_sentinels

from tests.support.isolation import (
    PinnedMonitor,
    fake_kernel_layout,
    fixed_interpreter,
    pinned_snapshot,
)

PRIVATE = "priv-value-9f3c2a7b"  # 條目作者交給伺服器的憑證（值不能出現在任何 log）
BOOT = "b" * 32
if TYPE_CHECKING:
    from pathlib import Path

    from src.services.isolation.profiles import SandboxLayout
    from src.services.isolation.runners import Acquired
    from src.services.mcp_isolation_store import RecordView
    from src.utils.mcp_origin import OriginRecord

FS_TCP = frozenset({"fs", "tcp"})
SEG = "m" + "0" * 24
EXECUTOR_READY = ExecutorReady(
    version=1, abi=7, enforced=True, net=None, features=("fs", "tcp"), roots=(),
    state_root="/x/executor_state", kernel_digest="k" * 64, server_digest="s" * 64,
    boot_id=BOOT, block_ports_digest="d" * 64, uid=1001,
)  # fmt: skip


def _read_fd(fd: int) -> bytes:
    """讀到 EOF（寫端在 runner 裡已關）；不關 fd，留給 ``plan.release``。"""
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _profile(tmp_path: Path, name: str = "mcp_third_party", **overrides) -> SandboxProfile:
    values = {
        "name": name,
        "cwd": tmp_path / "home",
        "rules": (PathRule(tmp_path / "home", "rwx"),),
        "block_ports": (2375, 8000),
        "env_set": {"HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp")},
        "owner": SEG,
    }
    values.update(overrides)
    return SandboxProfile(**values)


def _request(tmp_path: Path, profile: SandboxProfile | None = None, **overrides) -> ExecRequest:
    values = {
        "argv": ("npx", "-y", "@scope/server"),
        "env": {"PATH": "/usr/bin", "LANG": "C.UTF-8"},
        "profile": profile or _profile(tmp_path),
        "io": "stdio",
        "exec_env": {"NOTION_TOKEN": PRIVATE, "LOG_LEVEL": "info"},
    }
    values.update(overrides)
    return ExecRequest(**values)


def _landlock(tmp_path: Path) -> LandlockRunner:
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    return LandlockRunner(kernel, FS_TCP, python=sys.executable)


def _kernel_accepts(spec: dict) -> dict[str, str]:
    """kernel 的同一份驗證收得下；規則換成空的（Windows 的 tmp 路徑不是 spec 收的 POSIX 形狀）."""
    return dict(parse_spec(json.dumps({**spec, "rules": []}).encode("utf-8")).exec_env)


def _landlock_spec(tmp_path: Path, request: ExecRequest) -> tuple[dict, dict[str, str]]:
    plan = _landlock(tmp_path).plan(request)
    try:
        raw = _read_fd(int(plan.argv[plan.argv.index("--spec-fd") + 1]))
    finally:
        plan.release()
    return json.loads(raw), plan.env


# --- runner：exec_env 只進 spec，不進啟動器自己的環境 --------------------------------------


def test_landlock_runner_puts_exec_env_in_the_spec_not_in_the_launcher_env(tmp_path):
    spec, env = _landlock_spec(tmp_path, _request(tmp_path))
    assert spec["exec_env"] == {"NOTION_TOKEN": PRIVATE, "LOG_LEVEL": "info"}
    assert "NOTION_TOKEN" not in env and PRIVATE not in json.dumps(env)
    assert env["HOME"] == str(tmp_path / "home")  # 啟動器的 HOME 仍是 profile 的
    # kernel 收得下（同一份驗證，啟動器才會在限制之後交給目標）
    assert _kernel_accepts(spec) == {"NOTION_TOKEN": PRIVATE, "LOG_LEVEL": "info"}


def test_spec_without_exec_env_has_no_exec_env_key(tmp_path):
    """沒有 exec_env 時不帶這個鍵：舊啟動器（/opt 沒重建）不認得它，代理人指令不受影響."""
    spec, _env = _landlock_spec(tmp_path, _request(tmp_path, exec_env={}))
    assert "exec_env" not in spec
    assert "exec_env" not in json.loads(launcher_spec(_profile(tmp_path)))
    assert ExecRequest(argv=("x",), env={}, profile=_profile(tmp_path)).exec_env == {}


@pytest.mark.parametrize(
    "name",
    ["HOME", "home", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "xdg_config_home",
     "GIT_CONFIG_GLOBAL", "PYTEST_ADDOPTS", "TOUKA_EXEC_ID", "UV_CACHE_DIR", "npm_config_cache",
     "LD_PRELOAD", "LD_AUDIT", "PYTHONPATH", "NODE_OPTIONS", "DYLD_INSERT_LIBRARIES"],
)  # fmt: skip
def test_profile_owned_and_loader_names_are_dropped_with_a_name_only_warning(
    tmp_path, caplog, name
):
    """LOW 8／禁令 19：proxy 先濾掉（kernel 的拒收是第二道）；警告只有名稱，值不進 log."""
    caplog.set_level(logging.DEBUG)
    value = f"/agent/{PRIVATE}"
    exec_env = {name: value, "API_KEY": PRIVATE}
    spec, env = _landlock_spec(tmp_path, _request(tmp_path, exec_env=exec_env))
    assert spec["exec_env"] == {"API_KEY": PRIVATE}
    assert value not in json.dumps(env)
    assert _kernel_accepts(spec) == {"API_KEY": PRIVATE}  # 保留名稱已經不在
    assert name in caplog.text
    assert PRIVATE not in caplog.text


def test_executor_start_spec_carries_exec_env_and_the_start_env_does_not(tmp_path):
    runner = ExecutorRunner(
        tmp_path / "kernel", tmp_path / "ipc", BOOT, FS_TCP, python=sys.executable
    )
    request = _request(tmp_path, _profile(tmp_path, name="mcp_self"))
    plan = runner.plan(request)
    try:
        body = json.loads(_read_fd(int(plan.argv[plan.argv.index("--request-fd") + 1])))
    finally:
        plan.release()
    start = body["start"]
    assert start["spec"]["exec_env"] == {"NOTION_TOKEN": PRIVATE, "LOG_LEVEL": "info"}
    assert "NOTION_TOKEN" not in start["env"] and plan.env == {}
    assert (start["kind"], start["io"], start["max_seconds"]) == ("mcp", "stdio", 0)


def test_direct_runner_merges_exec_env_into_the_environment(tmp_path):
    """直接執行沒有啟動器：exec_env 直接併進 env（只在人核准的未隔離時用）."""
    plan = DirectRunner().plan(_request(tmp_path))
    assert plan.env == {
        "PATH": "/usr/bin", "LANG": "C.UTF-8", "NOTION_TOKEN": PRIVATE, "LOG_LEVEL": "info"
    }


# --- broker：第三方看本機判定、scaffold 試跑只用本機 Landlock --------------------------------


class _Source:
    """記錄 profile 建了幾次、在哪裡建（第三方未隔離時不能建：不碰 ST，MEDIUM-7）."""

    def __init__(self, tmp_path: Path, **overrides) -> None:
        self.profile = _profile(tmp_path, **overrides)
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, location: str, executor_state: str | None) -> SandboxProfile:
        self.calls.append((location, executor_state))
        return self.profile


def _broker(tmp_path: Path, monitor: PinnedMonitor) -> RunnerBroker:
    """有執行器的版面（ipc 資料夾在）：拒絕執行器不是因為版面沒有它."""
    layout = replace(fake_kernel_layout(tmp_path), executor_dir=tmp_path / "ipc")
    return RunnerBroker(
        monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter
    )


def _executor_ready_monitor(**kwargs) -> PinnedMonitor:
    """執行器可用（層級 executor、可當降級去處），本機照 ``kwargs``."""
    snap = replace(pinned_snapshot("executor", **kwargs), executor=EXECUTOR_READY)
    return PinnedMonitor(snap=snap, executor_features=landlock_features(7))


def _unavailable(reason: str) -> PinnedMonitor:
    return PinnedMonitor(snap=pinned_snapshot("none", local_status="unavailable", reason=reason))


async def test_third_party_is_wrapped_by_local_landlock_with_the_local_features(tmp_path):
    """層級是執行器也一樣看本機；B 擋不全時照樣包、features 不含 tcp；沒授權到的路徑照樣啟動."""
    incomplete = pinned_snapshot("none", block_ports_issue="block_ports_incomplete")
    for monitor, features in (
        (_executor_ready_monitor(), landlock_features(7)),
        (PinnedMonitor(snap=incomplete), landlock_features(7) - {"tcp"}),
    ):
        source = _Source(tmp_path, ungranted=("protected_location",))
        got = await _broker(tmp_path, monitor).acquire_third_party(ProfileSource(source, None))
        assert got.reason is None and got.acquired is not None
        assert isinstance(got.acquired.runner, LandlockRunner)
        plan = got.acquired.runner.plan(_request(tmp_path))
        plan.release()
        assert plan.features == features
        assert source.calls == [("proxy", None)]


@pytest.mark.parametrize(
    ("monitor", "reason"),
    [
        (
            PinnedMonitor(snap=pinned_snapshot("none", local_status="pending", abi=0)),
            "probe_pending",
        ),
        (_unavailable("fs_9p"), "fs_9p"),
        (_unavailable("selftest_failed"), "selftest_failed"),
        (
            PinnedMonitor(
                snap=pinned_snapshot("none"), state_ok=False, state_detail="not_writable"
            ),
            "sandbox_state_unconfigured",
        ),
        (
            PinnedMonitor(snap=pinned_snapshot("none"), kernel_issue="isolation_check_failed"),
            "isolation_check_failed",
        ),
        (_unavailable("odd_new_reason"), "odd_new_reason"),
    ],
)
async def test_third_party_waits_while_pending_or_on_a_temporary_reason(tmp_path, monitor, reason):
    """v3 E／MEDIUM-5：還在檢查或暫時有問題時不先未隔離啟動；看不懂的原因也等（fail-closed）."""
    source = _Source(tmp_path)
    with pytest.raises(IsolationPendingError) as raised:
        await _broker(tmp_path, monitor).acquire_third_party(ProfileSource(source, None))
    assert raised.value.reason == reason
    assert isinstance(raised.value, IsolationUnavailableError)  # 不分辨的呼叫端照樣當拿不到
    assert source.calls == []


@pytest.mark.parametrize(
    ("monitor", "reason"),
    [
        (PinnedMonitor(snap=pinned_snapshot("none", expected="none")), "native_platform"),
        (
            PinnedMonitor(snap=pinned_snapshot("none"), kernel_issue="kernel_outdated"),
            "kernel_outdated",
        ),
        (_unavailable("kernel_not_installed"), "kernel_not_installed"),
        (
            PinnedMonitor(snap=pinned_snapshot("none"), state_ok=False, state_detail="unset"),
            "sandbox_state_unconfigured",
        ),
    ],
)
async def test_third_party_on_a_stable_reason_starts_the_way_it_does_today(
    tmp_path, monitor, reason
):
    """ADR-0275＋MEDIUM-7：穩定原因照今天的方式未隔離啟動（不建 profile、不碰 ST），原因照實."""
    source = _Source(tmp_path)
    got = await _broker(tmp_path, monitor).acquire_third_party(ProfileSource(source, None))
    assert got == ThirdPartyAcquired(None, reason)
    assert source.calls == []


async def test_acquire_local_gives_local_landlock_only_when_the_local_level_is_landlock(tmp_path):
    source = _Source(tmp_path, name="mcp_scaffold")
    got = await _broker(tmp_path, PinnedMonitor()).acquire_local(
        ExecutionGrant("landlock"), ProfileSource(source, None)
    )
    assert isinstance(got.runner, LandlockRunner) and source.calls == [("proxy", None)]


@pytest.mark.parametrize(
    ("grant", "monitor"),
    [
        # 本機層級掉到 none（ABI 3）：執行器可用也不退回執行器
        (ExecutionGrant("landlock"), _executor_ready_monitor(abi=3)),
        # 政策是 executor（全域層級）：本機試跑不收
        (ExecutionGrant("executor", executor_eligible=True), _executor_ready_monitor()),
        (ExecutionGrant("none"), PinnedMonitor()),
    ],
)
async def test_acquire_local_refuses_instead_of_using_the_executor(tmp_path, grant, monitor):
    source = _Source(tmp_path, name="mcp_scaffold")
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, monitor).acquire_local(grant, ProfileSource(source, None))
    assert all(location == "proxy" for location, _state in source.calls)


async def test_acquire_local_refuses_an_incomplete_profile_without_consent(tmp_path):
    source = _Source(tmp_path, name="mcp_scaffold", ungranted=("protected_location",))
    with pytest.raises(IsolationUnavailableError):
        await _broker(tmp_path, PinnedMonitor()).acquire_local(
            ExecutionGrant("landlock"), ProfileSource(source, None)
        )


@pytest.mark.parametrize(
    ("fallback", "monitor", "runner"),
    [
        ("landlock", _executor_ready_monitor(), LandlockRunner),
        ("direct", _executor_ready_monitor(), LandlockRunner),
        (
            "direct",
            PinnedMonitor(
                snap=pinned_snapshot("none", local_status="unavailable", reason="fs_9p"),
                executor_features=landlock_features(7),
            ),
            DirectRunner,
        ),
    ],
)
async def test_acquire_local_with_consent_is_landlock_or_direct_never_the_executor(
    tmp_path, fallback, monitor, runner
):
    source = _Source(tmp_path, name="mcp_scaffold")
    grant = ExecutionGrant("none", Consent("s", fallback))
    got = await _broker(tmp_path, monitor).acquire_local(grant, ProfileSource(source, None))
    assert type(got.runner) is runner
    assert source.calls == [("proxy", None)]


# --- 啟動判定：§2.1 整張表 --------------------------------------------------------------

URL = "http://localhost:8000"
SELF = RawMcpEntry("npx", ("-y", "agent-built"), {"API_KEY": PRIVATE, "DATA": APPDATA}, None)
THIRD = RawMcpEntry("uvx", ("mcp-server-fetch",), {"NOTION_TOKEN": PRIVATE}, None)
REMOTE = RawMcpEntry(None, (), None, "https://example.invalid/mcp")
ORIGINS = {"self": "creation", "third": "manual", "scaffold": "scaffold", "remote": "hub"}


class _Reader:
    """隔離設定檔的替身：用 U3f 的紀錄模型（``bound`` 照實核對）."""

    def __init__(
        self,
        origin: str | None,
        *,
        unreadable: bool = False,
        stale: bool = False,
        sandbox_paths: tuple[SandboxPathGrant, ...] = (),
    ) -> None:
        self.origin, self.unreadable, self.stale = origin, unreadable, stale
        self.sandbox_paths = sandbox_paths

    def record_for(self, name: str, entry: dict) -> RecordView:
        if self.unreadable:
            return StoreSnapshot("unreadable", {}).view(name, entry)
        bound = "0" * 64 if self.stale else bound_of(entry)
        record = IsolationRecord(origin=self.origin, sandbox_paths=self.sandbox_paths, bound=bound)
        return StoreSnapshot("ok", {name: record} if self.origin else {}).view(name, entry)


def _layout(tmp_path: Path) -> SandboxLayout:
    """Touka 自己的資料夾都在 ``touka/`` 底下（``tmp_path`` 其他地方可以當額外允許的資料夾）."""
    (tmp_path / "touka").mkdir(exist_ok=True)
    return replace(fake_kernel_layout(tmp_path / "touka"), executor_dir=tmp_path / "ipc")


def _scaffold_entry(layout: SandboxLayout) -> RawMcpEntry:
    script = layout.config_dir / "mcp_servers" / "stub" / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")
    return RawMcpEntry("python", (str(script),), {"OUT": "x"}, None)


def _launcher(tmp_path: Path, monitor: PinnedMonitor) -> tuple[McpLauncher, McpLaunchBook]:
    layout = _layout(tmp_path)
    broker = RunnerBroker(
        monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter
    )
    book = McpLaunchBook()
    launcher = McpLauncher(
        monitor=monitor, broker=broker, layout_provider=lambda: layout,
        base_dir=lambda: tmp_path / "cwd", book=book,
    )  # fmt: skip
    return launcher, book


MONITORS = {
    "executor": _executor_ready_monitor,
    "landlock": PinnedMonitor,
    "pending": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", local_status="pending", abi=0, reason="probe_pending")
    ),
    "kernel_outdated": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="kernel_outdated"), kernel_issue="kernel_outdated"
    ),
    "temporary": lambda: _unavailable("fs_9p"),
    "native": lambda: PinnedMonitor(
        snap=pinned_snapshot("none", reason="native_platform", expected="none")
    ),
}
# (誰, 本機與層級) → (state, level, runner, profile, reason)；waiting 的 reason 是 snap／本機原因
TABLE = {
    ("self", "executor"): ("launched", "executor", ExecutorRunner, "mcp_self", None),
    ("self", "landlock"): ("launched", "landlock", LandlockRunner, "mcp_self", None),
    ("self", "pending"): ("waiting_isolation", "none", None, None, "probe_pending"),
    ("self", "kernel_outdated"): ("waiting_isolation", "none", None, None, "kernel_outdated"),
    ("self", "temporary"): ("waiting_isolation", "none", None, None, "fs_9p"),
    ("self", "native"): ("waiting_isolation", "none", None, None, "native_platform"),
    ("third", "executor"): ("launched", "landlock", LandlockRunner, "mcp_third_party", None),
    ("third", "landlock"): ("launched", "landlock", LandlockRunner, "mcp_third_party", None),
    ("third", "pending"): ("waiting_probe", "none", None, None, "probe_pending"),
    ("third", "kernel_outdated"): ("launched", "none", None, None, "kernel_outdated"),
    ("third", "temporary"): ("waiting_probe", "none", None, None, "fs_9p"),
    ("third", "native"): ("launched", "none", None, None, "native_platform"),
    ("scaffold", "executor"): ("launched", "landlock", LandlockRunner, "mcp_scaffold", None),
    ("scaffold", "landlock"): ("launched", "landlock", LandlockRunner, "mcp_scaffold", None),
    ("scaffold", "pending"): ("waiting_probe", "none", None, None, "probe_pending"),
    ("scaffold", "kernel_outdated"): ("launched", "none", None, None, "kernel_outdated"),
    ("scaffold", "temporary"): ("waiting_probe", "none", None, None, "fs_9p"),
    ("scaffold", "native"): ("launched", "none", None, None, "native_platform"),
    **{("remote", m): ("launched", "none", None, None, None) for m in MONITORS},
}


@pytest.mark.parametrize(("who", "where"), list(TABLE))
async def test_every_cell_of_the_launch_table(tmp_path, who, where):
    """§2.1（v2 改第三方與 scaffold）：自建看層級、第三方與 scaffold 看本機、遠端不判定."""
    state, level, runner, profile, reason = TABLE[(who, where)]
    launcher, book = _launcher(tmp_path, MONITORS[where]())
    layout = _layout(tmp_path)
    entry = {"self": SELF, "third": THIRD, "remote": REMOTE}.get(who) or _scaffold_entry(layout)
    reader = _Reader(ORIGINS[who])
    name = "stub" if who == "scaffold" else who  # scaffold 的腳本在 config/mcp_servers/<名稱>/
    if state != "launched":
        with pytest.raises(McpLaunchWaiting) as waiting:
            await launcher.plan_server_launch(name, entry, reader, tool_base_url=URL)
        assert (waiting.value.state, waiting.value.reason) == (state, reason)
        info = book.get(name)
        assert (info.state, info.level, info.reason) == (state, "none", reason)
        assert not (layout.state_root / "proxy").exists()  # 等待時不建 profile、不碰 ST
        return
    launch = await launcher.plan_server_launch(name, entry, reader, tool_base_url=URL)
    assert book.get(name) == launch.info
    assert (launch.info.state, launch.info.level, launch.info.reason) == (state, level, reason)
    assert launch.info.mcp_class == {"third": "third_party"}.get(who, who)
    if runner is None:  # 照今天的方式（SDK 的 stdio_client）：不建 profile、不碰 ST
        assert launch.acquired is None
        assert not (layout.state_root / "proxy").exists()
        return
    assert type(launch.acquired.runner) is runner
    assert launch.acquired.profile.name == profile
    assert launch.exec_env["TOOL_BASE_URL"] == URL
    if runner is ExecutorRunner:
        assert launch.info.boot_id == BOOT
        assert launch.info.features == landlock_features(7)


async def test_self_built_argv_and_exec_env_resolve_sentinels_and_add_the_tool_url(tmp_path):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.plan_server_launch("self", SELF, _Reader("creation"), tool_base_url=URL)
    assert launch.argv == ("npx", "-y", "agent-built")
    assert launch.exec_env == {
        **resolve_env_sentinels(dict(SELF.env)), "TOOL_BASE_URL": URL
    }
    request = launch.request({"PATH": "/usr/bin"})
    assert (request.io, request.env, request.profile) == ("stdio", {"PATH": "/usr/bin"},
                                                          launch.acquired.profile)  # fmt: skip


@pytest.mark.parametrize("where", ["command", "arg", "env"])
@pytest.mark.parametrize("relative", ["./program", "program"])
async def test_third_party_checks_references_at_the_actual_launch_cwd(tmp_path, where, relative):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    layout = _layout(tmp_path)
    home, _tmp = mcp_state_dirs("third", "relative", layout=layout, location="proxy")
    (home / "program").write_text("pass", encoding="utf-8")
    entry = RawMcpEntry(
        relative if where == "command" else "wrapper",
        (relative,) if where == "arg" else (),
        {"PROGRAM": relative} if where == "env" else {}, None,
    )
    launch = await launcher.plan_server_launch("relative", entry, _Reader("hub"), tool_base_url=URL)
    assert launch.info.mcp_class == "self"
    assert launch.acquired.profile.name == "mcp_self"
    assert launch.argv == (entry.command, *entry.args)


@pytest.mark.parametrize("where", ["command", "arg", "env"])
@pytest.mark.parametrize("relative", ["./program.py", "program.py"])
async def test_relative_classification_does_not_create_state_while_waiting(
    tmp_path, where, relative,
):
    launcher, book = _launcher(tmp_path, MONITORS["native"]())
    entry = RawMcpEntry(
        relative if where == "command" else "python",
        (relative,) if where == "arg" else (),
        {"PROGRAM": relative} if where == "env" else {}, None,
    )
    with pytest.raises(McpLaunchWaiting):
        await launcher.plan_server_launch("relative", entry, _Reader("hub"), tool_base_url=URL)
    assert book.get("relative").mcp_class == "self"
    assert not (_layout(tmp_path).state_root / "proxy").exists()


@pytest.mark.parametrize("command", ["npx", "uvx"])
@pytest.mark.parametrize("package", ["@scope/server", "package.name"])
async def test_scoped_package_name_is_not_rewritten_as_a_relative_path(tmp_path, command, package):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    entry = RawMcpEntry(command, (package,), {}, None)
    launch = await launcher.plan_server_launch("package", entry, _Reader("hub"), tool_base_url=URL)
    assert launch.argv == (command, package)


async def test_relative_command_and_script_reach_the_runner_unchanged(tmp_path):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    entry = RawMcpEntry("./bin/python", ("../external/server.py",), {}, None)
    launch = await launcher.plan_server_launch("relative", entry, _Reader("hub"), tool_base_url=URL)
    assert launch.argv == (entry.command, *entry.args)


@pytest.mark.parametrize("origin", ["hub", "creation"])
@pytest.mark.parametrize(("command", "args"), [
    ("docker", ("run", "-i", "--rm", "mcp/fetch")),
    ("deno", ("run", "npm:@scope/pkg")), ("deno", ("run", "jsr:@std/path")),
    ("bun", ("x", "@scope/pkg")), ("pnpm", ("dlx", "@scope/pkg")),
    ("uvx", ("--python", "3.12", "tool")),
    ("uvx", ("--from", "pkg==1.2.0", "tool")),
    ("uvx", ("--from=pkg==1.0", "tool")),
    ("uvx", ("--with", "pkg==1.2.0", "tool")),
    ("npx", ("-p", "pkg@1.2.3", "bin")),
    ("python", ("-m", "package.module")), ("python", ("-c", "package.function()")),
    ("python", ("-W", "ignore::package.Warning", "-m", "package.module")),
])
async def test_isolated_launch_preserves_configured_argv(tmp_path, origin, command, args):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    entry = RawMcpEntry(command, args, {}, None)
    launch = await launcher.plan_server_launch("pkg", entry, _Reader(origin), tool_base_url=URL)
    assert launch.argv == (command, *args)
    assert launch.request({}).argv == (command, *args)


@pytest.mark.parametrize("origin", ["hub", "creation"])
async def test_docker_volume_argument_is_preserved(tmp_path, origin):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    volume = str(tmp_path / "host") + ":/app"
    entry = RawMcpEntry("docker", ("run", "-v", volume, "image"), {}, None)
    launch = await launcher.plan_server_launch("docker", entry, _Reader(origin), tool_base_url=URL)
    assert launch.argv == (entry.command, *entry.args)


async def test_appdata_program_reference_uses_the_self_built_launch(tmp_path):
    """可載入代理人程式的根從嚴分類，套件名稱與來源紀錄均不能豁免。"""
    entry = RawMcpEntry("npx", ("-y", "fs-server", APPDATA), {"ROOT": APPDATA}, None)
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.plan_server_launch("fs", entry, _Reader("hub"), tool_base_url=URL)
    assert launch.info.mcp_class == "self"
    assert launch.argv == ("npx", "-y", "fs-server", resolve_env_sentinels({"X": APPDATA})["X"])
    assert launch.exec_env == {**resolve_env_sentinels({"ROOT": APPDATA}), "TOOL_BASE_URL": URL}


@pytest.mark.parametrize(
    ("reader", "mcp_class", "record_state"),
    [
        (_Reader("manual", stale=True), "third_party", "stale"),  # 第三方類 bound 不符：沒有紀錄
        (_Reader("creation", stale=True), "self", "stale"),  # 自建類 bound 不符：照舊自建
        (_Reader(None, unreadable=True), "self", "unreadable"),  # 讀不到：全部當自建
        (_Reader("made_up"), "self", "ok"),  # 看不懂的 origin：自建
        (_Reader(None), "third_party", "none"),  # 沒有紀錄、啟發式沒命中：第三方
    ],
)
async def test_the_record_view_decides_the_class(tmp_path, reader, mcp_class, record_state):
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.plan_server_launch("x", THIRD, reader, tool_base_url=URL)
    assert (launch.info.mcp_class, launch.info.record_state) == (mcp_class, record_state)
    expected = "mcp_self" if mcp_class == "self" else "mcp_third_party"
    assert launch.acquired.profile.name == expected


@pytest.mark.parametrize(
    ("reader", "granted"),
    [
        ("third_ok", True),
        ("third_stale", False),  # 程式變了：人給的資料夾不套到另一個程式上
        ("self_ok", False),  # 自建不收額外資料夾
        ("unreadable", False),
    ],
)
async def test_sandbox_paths_reach_only_matching_third_party_records(tmp_path, reader, granted):
    extra = tmp_path / "extra"
    extra.mkdir()
    grants = (SandboxPathGrant(path=str(extra), access="ro"),)
    readers = {
        "third_ok": _Reader("manual", sandbox_paths=grants),
        "third_stale": _Reader("manual", stale=True, sandbox_paths=grants),
        "self_ok": _Reader("creation", sandbox_paths=grants),
        "unreadable": _Reader(None, unreadable=True),
    }
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.plan_server_launch("x", THIRD, readers[reader], tool_base_url=URL)
    rules = {(rule.path, rule.access) for rule in launch.acquired.profile.rules}
    assert ((extra, "ro") in rules) is granted


async def test_exec_env_values_never_reach_logs_the_book_or_the_api(tmp_path, caplog):
    """條目 env 的值不進 log、``/mcp/list``、``/isolation``、``repr``；濾掉的只記名稱."""
    caplog.set_level(logging.DEBUG)
    env = {"HOME": PRIVATE, "LD_PRELOAD": PRIVATE, "K": PRIVATE}
    entry = RawMcpEntry("uvx", ("srv",), env, None)
    launcher, book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.plan_server_launch("leaky", entry, _Reader("manual"), tool_base_url=URL)
    assert launch.exec_env == {"K": PRIVATE, "TOOL_BASE_URL": URL}
    assert "leaky" in caplog.text and "HOME" in caplog.text and "LD_PRELOAD" in caplog.text
    request = launch.request({"PATH": "/usr/bin"})
    for text in (
        caplog.text, repr(launch), repr(request), repr(book.get("leaky")),
        json.dumps(launch.info.public(connected=True)), repr(book.counts()),
    ):  # fmt: skip
        assert PRIVATE not in text


# --- 人同意的手動啟動（自建、層級 none）---------------------------------------------------


async def test_consent_starts_a_waiting_self_built_server_once(tmp_path):
    """同意只用這一次：之後沒有同意的判定照樣等隔離（不留著給重連）."""
    snap = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
    launcher, book = _launcher(tmp_path, PinnedMonitor(snap=snap))
    reader = _Reader("creation")
    with pytest.raises(McpLaunchWaiting):
        await launcher.plan_server_launch("self", SELF, reader, tool_base_url=URL)
    consent = Consent("s", "landlock", landlock_features(7))
    launch = await launcher.plan_server_launch(
        "self", SELF, reader, tool_base_url=URL, consent=consent
    )
    assert isinstance(launch.acquired.runner, LandlockRunner)
    assert (book.get("self").state, book.get("self").level) == ("launched", "landlock")
    with pytest.raises(McpLaunchWaiting):
        await launcher.plan_server_launch("self", SELF, reader, tool_base_url=URL)
    assert book.get("self").state == "waiting_isolation"


async def test_unisolated_consent_starts_the_way_it_does_today_without_the_state(tmp_path):
    """去處是 direct：照今天的方式（不依賴 ST；ST 壞掉也起得來）."""
    snap = pinned_snapshot("none", local_status="unavailable", reason="kernel_not_installed")
    layout = _layout(tmp_path)
    launcher, book = _launcher(tmp_path, PinnedMonitor(snap=snap))
    layout.state_root.write_text("", encoding="utf-8")  # ST 是檔案：profile 組不出來
    launch = await launcher.plan_server_launch(
        "self", SELF, _Reader("creation"), tool_base_url=URL, consent=Consent("s", "direct")
    )
    assert launch.acquired is None
    assert (book.get("self").state, book.get("self").level) == ("launched", "none")


async def test_consent_does_not_change_the_third_party_rules(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", local_status="pending", abi=0))
    launcher, _book = _launcher(tmp_path, monitor)
    with pytest.raises(McpLaunchWaiting) as waiting:
        await launcher.plan_server_launch(
            "t", THIRD, _Reader("manual"), tool_base_url=URL, consent=Consent("s", "direct")
        )
    assert waiting.value.state == "waiting_probe"


class _Host:
    """manager 的替身：條目、generation，記下帶同意的重新啟動."""

    def __init__(self, entry: RawMcpEntry, generation: int = 3) -> None:
        self.entry, self.generation = entry, generation
        self.calls: list[tuple[str, Consent]] = []
        self.gate: asyncio.Event | None = None

    def server_entry(self, _name: str) -> RawMcpEntry:
        return self.entry

    def get_installation_generation(self, _name: str) -> int:
        return self.generation

    async def relaunch_with_consent(self, name: str, consent: Consent) -> bool:
        self.calls.append((name, consent))
        if self.gate is not None:
            await self.gate.wait()
        return True


def _subject(entry: RawMcpEntry = SELF, generation: int = 3, name: str = "self") -> str:
    return manual_start_subject(name, generation, bound_of(entry_identity(entry)))


async def _waiting_starter(tmp_path: Path, monitor: PinnedMonitor) -> tuple[McpManualStart, _Host]:
    launcher, book = _launcher(tmp_path, monitor)
    with pytest.raises(McpLaunchWaiting):
        await launcher.plan_server_launch("self", SELF, _Reader("creation"), tool_base_url=URL)
    host = _Host(SELF)
    return McpManualStart(host, monitor=monitor, book=book), host


async def test_manual_start_plan_is_only_for_waiting_self_built_servers(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    starter, _host = await _waiting_starter(tmp_path, monitor)
    assert starter.manual_start_plan("self") == FallbackPlan("landlock", landlock_features(7))
    assert starter.manual_start_plan("unknown") is None
    third = McpLaunchBook()
    third.record("t", McpLaunchInfo("third_party", "manual", "ok", "waiting_probe", "none"))
    third.record("s", McpLaunchInfo("self", "creation", "ok", "launched", "landlock"))
    other = McpManualStart(_Host(SELF), monitor=monitor, book=third)
    assert other.manual_start_plan("t") is None and other.manual_start_plan("s") is None


async def test_manual_start_with_the_right_ack_generation_and_bound_starts_once(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    starter, host = await _waiting_starter(tmp_path, monitor)
    consent = Consent(_subject(), "landlock", landlock_features(7))
    assert await starter.start_with_consent("self", consent) is True
    assert host.calls == [("self", consent)]


@pytest.mark.parametrize(
    "consent",
    [
        Consent(_subject(), "direct"),  # ack 和現在的去處不符
        Consent(_subject(), "executor"),
        Consent(_subject(generation=2), "landlock"),  # 設定已改變（generation）
        Consent(_subject(entry=THIRD), "landlock"),  # 條目的程式改了（bound）
        Consent(_subject(name="other"), "landlock"),
    ],
)
async def test_manual_start_refuses_a_stale_or_mismatched_consent(tmp_path, consent, caplog):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    starter, host = await _waiting_starter(tmp_path, monitor)
    assert await starter.start_with_consent("self", consent) is False
    assert host.calls == []
    assert "AUDIT" in caplog.text


async def test_manual_start_cannot_run_twice_at_once(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    starter, host = await _waiting_starter(tmp_path, monitor)
    host.gate = asyncio.Event()
    consent = Consent(_subject(), "landlock", landlock_features(7))
    first = asyncio.create_task(starter.start_with_consent("self", consent))
    await asyncio.sleep(0)
    second = starter.start_with_consent("self", consent)
    assert await asyncio.wait_for(second, 1) is False  # 不等第一個：直接拒絕
    host.gate.set()
    assert await first is True
    assert len(host.calls) == 1


def test_the_router_builds_subjects_and_records_only_through_mcp_launch():
    """SSOT：路由不自己組手動啟動的 subject、不自己把紀錄對映成 ``OriginRecord``."""
    tree = ast.parse(inspect.getsource(mcp_router))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"manual_start_subject", "origin_record"} <= called
    assert "consent_subject" not in names
    assert "OriginRecord" not in called
    assert not {"NO_RECORD", "UNREADABLE_RECORD"} & names


async def test_the_router_maps_records_through_origin_record(monkeypatch):
    """換掉 ``origin_record`` 的那份，路由的分類就照它給的紀錄（證明路由沒有自己對映）."""
    seen: list[tuple[str | None, bool]] = []

    def _spy(origin: str | None, *, records_unreadable: bool) -> OriginRecord:
        seen.append((origin, records_unreadable))
        return UNREADABLE_RECORD

    monkeypatch.setattr(mcp_router, "origin_record", _spy)
    entry = {"command": "uvx", "args": ["mcp-server-fetch"]}
    kind = await mcp_router._classify_entry("fetch", entry, "manual", records_unreadable=False)
    assert seen == [("manual", False)]
    assert kind == "self"  # 讀不到的紀錄一律自建；路由自己對映的話會是第三方


# --- 帳本：重連的對象與計數 --------------------------------------------------------------


def _book() -> McpLaunchBook:
    book = McpLaunchBook()
    rows = {
        "wait_self": McpLaunchInfo("self", "creation", "ok", "waiting_isolation", "none"),
        "wait_third": McpLaunchInfo("third_party", "hub", "ok", "waiting_probe", "none"),
        "third_direct": McpLaunchInfo("third_party", "hub", "ok", "launched", "none"),
        "scaffold_direct": McpLaunchInfo("scaffold", "scaffold", "ok", "launched", "none"),
        "third_wrapped": McpLaunchInfo("third_party", "hub", "ok", "launched", "landlock"),
        "exec_same": McpLaunchInfo("self", "agent", "ok", "launched", "executor", boot_id=BOOT),
        "exec_old": McpLaunchInfo("self", "agent", "ok", "launched", "executor", boot_id="9" * 32),
        "self_direct": McpLaunchInfo("self", "agent", "ok", "launched", "none"),
        "remote": McpLaunchInfo("remote", None, "none", "launched", "none"),
    }
    for name, info in rows.items():
        book.record(name, info)
    return book


def test_relaunch_targets_follow_the_three_conditions():
    snap = replace(pinned_snapshot("executor"), executor=EXECUTOR_READY)
    available = LocalDecision("available", features=landlock_features(7))
    assert _book().relaunch_targets(snap, available) == (
        "wait_self", "wait_third", "third_direct", "scaffold_direct", "exec_old"
    )
    unavailable = LocalDecision("unavailable", "kernel_outdated")
    no_ready = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
    assert _book().relaunch_targets(no_ready, unavailable) == ("wait_self", "wait_third")


def _recorded_grants(launcher: McpLauncher) -> list[ExecutionGrant]:
    """記下 broker 收到的每個 grant（看重新判定時有沒有帶同意）."""
    grants: list[ExecutionGrant] = []
    broker = launcher._broker
    original = broker.acquire

    async def _acquire(grant: ExecutionGrant, source: ProfileSource) -> Acquired:
        grants.append(grant)
        return await original(grant, source)

    broker.acquire = _acquire
    return grants


@pytest.mark.parametrize("level", ["landlock", "executor"])
async def test_a_consented_unisolated_start_is_relaunched_once_isolation_returns(tmp_path, level):
    """（U3d-2）人同意的是「現在沒有隔離，先不隔離跑」；隔離恢復就不帶同意重新判定."""
    monitor = _unavailable("kernel_not_installed")  # 層級 none、本機也不能用：只剩未隔離
    launcher, book = _launcher(tmp_path, monitor)
    reader = _Reader("creation")
    consent = Consent("s", "direct")
    launch = await launcher.plan_server_launch(
        "self", SELF, reader, tool_base_url=URL, consent=consent
    )
    assert launch.acquired is None and book.get("self").consented_direct
    assert book.relaunch_targets(monitor.snapshot(), monitor.local_decision()) == ()
    restored = _executor_ready_monitor() if level == "executor" else PinnedMonitor()
    monitor.snap, monitor.executor_features = restored.snap, restored.executor_features
    assert book.relaunch_targets(monitor.snapshot(), monitor.local_decision()) == ("self",)
    grants = _recorded_grants(launcher)
    relaunch = await launcher.plan_server_launch("self", SELF, reader, tool_base_url=URL)
    assert [(grant.policy_level, grant.consent) for grant in grants] == [(level, None)]
    assert relaunch.acquired is not None
    assert (book.get("self").level, book.get("self").consented_direct) == (level, False)
    assert book.relaunch_targets(monitor.snapshot(), monitor.local_decision()) == ()


def test_consented_unisolated_rows_wait_for_a_level_that_is_not_none():
    """只記事實（``consented_direct``），不留同意本身；層級仍是 none 時不列入."""
    book = McpLaunchBook()
    info = McpLaunchInfo("self", "agent", "ok", "launched", "none", consented_direct=True)
    book.record("manual", info)
    available = LocalDecision("available", features=landlock_features(7))
    none = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
    assert book.relaunch_targets(none, available) == ()
    executor = replace(pinned_snapshot("executor"), executor=EXECUTOR_READY)
    for snap in (pinned_snapshot("landlock"), executor):
        assert book.relaunch_targets(snap, available) == ("manual",)
    assert "consented_direct" not in info.public(connected=True)


def test_counts_and_retain():
    book = _book()
    assert book.counts() == McpLaunchCounts(unisolated=2, waiting=2, suspended=0)
    book.retain(["wait_self", "third_direct"])
    assert book.counts() == McpLaunchCounts(unisolated=1, waiting=1, suspended=0)
    assert book.get("remote") is None


def test_public_isolation_has_codes_only():
    features = landlock_features(6)
    info = McpLaunchInfo("self", "agent", "ok", "launched", "executor", features, None, BOOT)
    assert info.public(connected=True) == {
        "class": "self", "origin": "agent", "record_state": "ok", "state": "running",
        "level": "executor", "features": sorted(landlock_features(6)), "reason": None,
    }  # fmt: skip
    assert info.public(connected=False)["state"] == "not_started"
    waiting = McpLaunchInfo("third_party", None, "none", "waiting_probe", "none", reason="fs_9p")
    assert waiting.public(connected=False)["state"] == "waiting_probe"


# --- 創建試跑（create_mcp、scaffold_mcp）---------------------------------------------------


async def test_create_candidate_uses_the_self_profile_and_the_clean_env(tmp_path):
    """試跑和正式同一個 profile；env 照 clean 的語意（哨兵不解析、沒有 TOOL_BASE_URL）、不記帳本."""
    launcher, book = _launcher(tmp_path, PinnedMonitor())
    launch = await launcher.candidate_launch(ExecutionGrant("landlock"), "new", SELF, kind="self")
    assert isinstance(launch.acquired.runner, LandlockRunner)
    assert launch.acquired.profile.name == "mcp_self"
    assert launch.exec_env == {"API_KEY": PRIVATE, "DATA": APPDATA}
    assert book.get("new") is None


async def test_create_candidate_without_isolation_or_consent_is_refused(tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", reason="executor_unreachable"))
    launcher, _book = _launcher(tmp_path, monitor)
    with pytest.raises(IsolationUnavailableError):
        await launcher.candidate_launch(ExecutionGrant("none"), "new", SELF, kind="self")


async def test_scaffold_candidate_uses_local_landlock_and_never_the_executor(tmp_path):
    entry = _scaffold_entry(_layout(tmp_path))
    launcher, _book = _launcher(tmp_path, PinnedMonitor())
    grant = ExecutionGrant("landlock")
    launch = await launcher.candidate_launch(grant, "stub", entry, kind="scaffold")
    assert (type(launch.acquired.runner), launch.acquired.profile.name) == (
        LandlockRunner, "mcp_scaffold"
    )
    refused, _book = _launcher(tmp_path, _executor_ready_monitor(abi=3))
    with pytest.raises(IsolationUnavailableError):
        await refused.candidate_launch(ExecutionGrant("landlock"), "stub", entry, kind="scaffold")


async def test_scaffold_candidate_never_uses_the_executor_even_with_consent(tmp_path):
    """腳本不在自己資料夾的 scaffold 當自建：試跑有同意也只有本機 Landlock 或未隔離."""
    layout = _layout(tmp_path)
    script = layout.config_dir / "mcp_servers" / "other" / "server.py"
    script.parent.mkdir(parents=True)
    script.write_text("# other\n", encoding="utf-8")
    entry = RawMcpEntry("python", (str(script),), None, None)
    launcher, _book = _launcher(tmp_path, _executor_ready_monitor())
    grant = ExecutionGrant("none", Consent("s", "direct"))
    launch = await launcher.candidate_launch(grant, "stub", entry, kind="scaffold")
    assert launch.info.mcp_class == "self"
    assert type(launch.acquired.runner) is LandlockRunner


# --- 測試替身：一般測試的 MCP 直通 -------------------------------------------------------


async def test_ordinary_tests_get_the_mcp_passthrough(tmp_path):
    """autouse 替身：既有 MCP 測試照今天的方式啟動（SDK 傳輸），不判定、不碰 ST."""
    reader = _Reader("creation")
    launch = await mcp_launcher.plan_server_launch("x", SELF, reader, tool_base_url=URL)
    assert launch.acquired is None and launch.info.state == "launched"
    trial = await mcp_launcher.candidate_launch(ExecutionGrant("none"), "x", SELF, kind="self")
    assert trial.acquired is None
