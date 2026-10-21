"""演化（建 venv、pip、本機驗證）經隔離 runner（iso-c2 U1h）.

- profile：讀寫只有 ``managed/<skill>`` 與 ST 底下自己的 ``evolution/<skill>/{home,tmp}``；
  系統路徑與 ``config/skills`` 唯讀＋執行；別的代理人與 Touka 的資料不授權。rlimit 由每條
  指令帶入（驗證同現況：FSIZE、CPU、AS；建置只有 CPU）。
- 接線：工具開頭取一次 grant（同意帳本、正規化 subject），同一次呼叫的 venv、pip、驗證共用；
  spawn 前才 acquire；argv／env／pass_fds 用 runner 的 plan，spawn 後 release；拿不到 runner
  就不執行（``upstream_error``／``isolation_unavailable``）。兩個舊別名用自己的 subject。
monitor 用 ``PinnedMonitor``、broker 用真的 ``RunnerBroker``，``run_sync`` 換成記錄替身；
全部用 tmp_path 建的假版面；不建 venv、不連網。
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import PureWindowsPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from mcp import types
from src.models.identity import AgentIdentity
from src.sandbox_kernel import spec as kernel_spec
from src.services import evolution_security, tool_errors
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import evolution_command
from src.services.isolation.consent import ConsentKey, consent_ledger
from src.services.isolation.executor_client import ExecutorReady
from src.services.isolation.policy import policy_level
from src.services.isolation.profiles import (
    ProfileError,
    SandboxLayout,
    blocked_ports,
    build_evolution_profile,
    launcher_spec_fields,
)
from src.tools.builtin import evolution

from tests.support.isolation import (
    REAL_EVOLUTION_PROFILE,
    fake_kernel_layout,
    install_real_broker,
    pinned_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

SKILL = "probe_skill"
MB = 1024 * 1024


def _layout(root: Path) -> SandboxLayout:
    install = root / "install"
    data = install / "data"
    for sub in ("personal_drive/Alice", "personal_drive/Bob", "soul", "mcp_servers/managed/other"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (data / "mcp_servers" / "managed" / SKILL).mkdir(parents=True)
    for sub in ("ToukaProxy/config/skills", "host_root", "st"):
        (install / sub).mkdir(parents=True, exist_ok=True)
    env_file = install / "host_root" / ".env"
    env_file.write_text("", encoding="utf-8")
    return SandboxLayout(
        data_root=data,
        config_dir=install / "ToukaProxy" / "config",
        install_root=install,
        proxy_root=install / "ToukaProxy",
        env_file=env_file,
        state_root=install / "st",
        kernel_dir=install / "kernel",
        executor_dir=None,
        mirror_roots=(install / "host_root",),
        in_container=False,
    )


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    return _layout(tmp_path)


def _skill_root(layout: SandboxLayout, skill: str = SKILL) -> Path:
    return layout.data_root / "mcp_servers" / "managed" / skill


def _launcher_paths(fields: dict) -> dict:
    """Linux 原樣驗；Windows 的路徑帶磁碟代號、不是 POSIX 形狀，只去掉代號再驗。"""
    if os.name != "nt":
        return fields
    rules = [
        {**rule, "path": "/" + "/".join(PureWindowsPath(rule["path"]).parts[1:])}
        for rule in fields["rules"]
    ]
    return {**fields, "rules": rules}


# ── profile ─────────────────────────────────────────────────────────────────────


def test_evolution_profile_grants_only_its_own_sandbox_and_state(layout):
    root = _skill_root(layout).resolve()
    profile = build_evolution_profile(SKILL, _skill_root(layout), layout=layout)
    rules = {rule.path: rule.access for rule in profile.rules}
    state = (layout.state_root / "proxy" / "evolution" / SKILL).resolve()
    assert {path for path, access in rules.items() if access == "rwx"} == {
        root,
        state / "home",
        state / "tmp",
    }
    assert (state / "home").is_dir() and (state / "tmp").is_dir()
    assert rules[(layout.config_dir / "skills").resolve()] == "rox"
    data = layout.data_root.resolve()
    # 資料根底下只授權自己的 managed/<skill>；資料根與它的上層都不授權（別的代理人、soul 讀不到）
    assert {path for path in rules if path.is_relative_to(data)} == {root}
    assert not any(data.is_relative_to(path) for path in rules)
    assert profile.cwd == root
    assert profile.name == "evolution"
    assert profile.ungranted == ()
    assert dict(profile.rlimits) == {}  # 每條指令自己帶（建置只有 CPU，驗證 FSIZE、CPU、AS）
    assert profile.block_ports == blocked_ports("proxy")


def test_evolution_profile_redirects_home_tmp_and_git_ceiling_into_state(layout):
    root = _skill_root(layout).resolve()
    profile = build_evolution_profile(SKILL, _skill_root(layout), layout=layout)
    state = (layout.state_root / "proxy" / "evolution" / SKILL).resolve()
    env = profile.env_set
    assert env["HOME"] == str(state / "home")
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(state / "tmp")
    assert env["XDG_CACHE_HOME"] == str(state / "home" / ".cache")
    assert env["GIT_CEILING_DIRECTORIES"] == str(root.parent)
    assert {"PIP_CACHE_DIR", "UV_CACHE_DIR", "HF_HOME"} <= profile.env_unset


@pytest.mark.parametrize(
    "relative",
    ["mcp_servers/managed/other", "mcp_servers/managed", "personal_drive/Alice", "soul", "."],
)
def test_evolution_profile_rejects_a_root_that_is_not_the_skill_sandbox(layout, relative):
    with pytest.raises(ProfileError):
        build_evolution_profile(SKILL, layout.data_root / relative, layout=layout)


@pytest.mark.parametrize("skill", ["..", "a/b", "a\\b", " x", ""])
def test_evolution_profile_rejects_unsafe_skill_ids(layout, skill):
    with pytest.raises(ProfileError):
        build_evolution_profile(skill, _skill_root(layout) / ".." / skill, layout=layout)


def test_evolution_profile_without_state_root_is_profile_error(layout):
    with pytest.raises(ProfileError):
        build_evolution_profile(
            SKILL, _skill_root(layout), layout=replace(layout, state_root=None)
        )


def test_evolution_spec_with_verify_rlimits_passes_launcher_validation(layout):
    limits = {"fsize": 50 * MB, "cpu": 30, "as": 256 * MB}
    profile = replace(
        build_evolution_profile(SKILL, _skill_root(layout), layout=layout), rlimits=limits
    )
    fields = _launcher_paths(launcher_spec_fields(profile))
    parsed = kernel_spec.parse_spec(json.dumps(fields).encode("utf-8"))
    assert parsed.profile == "evolution"
    assert dict(parsed.rlimits) == limits
    assert [r.access for r in parsed.rules] == [r.access for r in profile.rules]


# ── 接線：一次 grant、spawn 前才 acquire、argv／env／pass_fds 用 plan、spawn 後 release ──────────

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
CODE = "class _M:\n    async def call_tool(self, name, args):\n        return 'pong'\nmcp = _M()\n"
DEPS = ["example-package==1.0"]
MERGED_ARGS: dict[str, Any] = {
    "skill_id": SKILL,
    "code": CODE,
    "test_tool_name": "ping",
    "test_arguments": {"x": 1},
    "dependencies": DEPS,
}
KEY = ConsentKey("Alice", "s1", "c1")
LIMITS = {"execution_timeout_sec": 17, "max_storage_mb": 5, "max_memory_mb": 64}
VERIFY_OUT = '{"status": "success", "data": "pong"}'
LANDLOCK = pinned_snapshot("landlock")
# 執行器連不上、本機 Landlock 仍可用（ABI 7）：真的 monitor 降級給 landlock（有比對測試）
NONE = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
NONE_WITHOUT_LANDLOCK = pinned_snapshot(
    "none", local_status="unavailable", abi=0, reason="selftest_failed"
)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _fake_resource(calls: list) -> SimpleNamespace:
    return SimpleNamespace(
        RLIMIT_FSIZE="FSIZE",
        RLIMIT_CPU="CPU",
        RLIMIT_AS="AS",
        setrlimit=lambda res, limits: calls.append((res, limits)),
    )


@dataclass
class _Spawned:
    argv: list[str]
    kwargs: dict[str, Any]
    thread: int
    spec: dict[str, Any] | None = None
    spec_fd: int | None = None
    status_fd: int | None = None


@dataclass
class _Recorder:
    """``run_sync`` 的替身：記下 argv／kwargs／執行緒，從繼承的 pipe 讀出啟動器 spec.

    ``status`` 有值時當成啟動器失敗：寫進狀態 fd（只有啟動器寫得到）。
    """

    calls: list[_Spawned] = field(default_factory=list)
    fail: bool = False
    after: Any = None  # 每次 spawn 之後呼叫（例如中途把層級改掉）
    status: bytes = b""

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        call = _Spawned(list(argv), kwargs, threading.get_ident())
        if "--request-fd" in argv:  # 執行器的 relay：請求 {parent_pid, start}
            call.spec = json.loads(os.read(int(argv[argv.index("--request-fd") + 1]), 1 << 20))
        if "--spec-fd" in argv:
            call.spec_fd = int(argv[argv.index("--spec-fd") + 1])
            call.spec = json.loads(os.read(call.spec_fd, 1 << 20))
            call.status_fd = int(argv[argv.index("--status-fd") + 1])
            if self.status:
                os.write(call.status_fd, self.status)
        self.calls.append(call)
        if self.after is not None:
            self.after(len(self.calls))
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(returncode=0, stdout=VERIFY_OUT, stderr="")

    def original(self, index: int) -> list[str]:
        argv = self.calls[index].argv
        return argv[argv.index("--") + 1 :] if "--" in argv else argv


@pytest.fixture
def evolution_env(monkeypatch, tmp_path) -> Path:
    """沙盒根目錄指到 tmp；已提案（有 spec.json）；配額固定；回傳 managed 根目錄。"""
    managed = tmp_path / "managed"
    monkeypatch.setattr(EvolutionPathGuard, "MANAGED_BASE_DIR", managed)
    EvolutionPathGuard.write_tool_file(SKILL, "spec.json", "{}")
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": LIMITS})
    return managed


@pytest.fixture
def spawned(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(evolution_security, "run_sync", recorder)
    monkeypatch.setattr(EvolutionPathGuard, "is_windows", lambda: False)

    async def connect(_config, *, isolated, **_kwargs):
        assert isolated.on_eof == "close"
        request = isolated.launch.request(EvolutionPathGuard.get_clean_env())
        assert request.io == "stdio"
        runner = isolated.launch.acquired.runner
        plan = runner.plan(request)
        try:
            recorder(list(plan.argv), cwd=str(plan.cwd), env=plan.env,
                     preexec_fn=plan.preexec_fn, pass_fds=plan.pass_fds, close_fds=True)
        finally:
            plan.release()
            failure = runner.failure(plan.read_status())
            if failure is not None:
                isolated.report_failure(failure.code)
        session = SimpleNamespace(
            call_tool=AsyncMock(return_value=types.CallToolResult(content=[])),
        )
        return SimpleNamespace(session=session), AsyncExitStack()

    monkeypatch.setattr(evolution, "connect_mcp_server", connect)
    return recorder


def _isolation(monkeypatch, tmp_path: Path, snap) -> Any:
    return install_real_broker(monkeypatch, snap, fake_kernel_layout(tmp_path))


def _launcher(tmp_path: Path) -> str:
    return str(fake_kernel_layout(tmp_path).kernel_dir / "launcher.py")


def _venv_python() -> str:
    return evolution._venv_python(SKILL)


async def _merged(**overrides: Any) -> str:
    values: dict[str, Any] = {
        **MERGED_ARGS,
        "identity": ALICE,
        "session_id": "s1",
        "_call_id": "c1",
    }
    values.update(overrides)
    return await evolution.evolution_implement_and_verify(**values)


async def _record_policy(tool: str, args: dict[str, Any], *, fallback: str | None) -> None:
    """政策評估時記下層級與 subject（tool_executor 的接線方式）；``fallback`` 是人核准的卡片。"""
    await policy_level(ALICE, {"identity": ALICE}, "s1", "c1", tool=tool, args=args)
    if fallback is not None:
        assert consent_ledger.grant(KEY, fallback=fallback)


def _assert_isolation_error(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    assert payload["error_class"] == "upstream_error", payload
    assert payload["reason"] == "isolation_unavailable", payload
    return payload


async def test_build_and_verify_spawn_through_the_launcher(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, LANDLOCK)
    clean = {"PATH": "/usr/bin", "HF_HOME": "/cache/hf", "PIP_CACHE_DIR": "/cache/pip"}
    monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: dict(clean))
    result = await _merged()
    assert '"status": "success"' in result
    launcher = _launcher(tmp_path)
    assert [spawned.original(i) for i in range(3)] == [
        [sys.executable, "-m", "venv", ".venv"],
        [_venv_python(), "-m", "pip", "install", "-r", "requirements.txt"],
        [_venv_python(), str(EvolutionPathGuard.get_tool_root(SKILL) / "test_wrapper.py")],
    ]
    root = str(EvolutionPathGuard.get_tool_root(SKILL))
    loop_thread = threading.get_ident()
    for call in spawned.calls:
        assert call.argv[:4] == [sys.executable, "-I", "-S", launcher]
        assert call.kwargs["pass_fds"] == (call.spec_fd, call.status_fd)
        assert call.kwargs.get("close_fds", True) is True  # 只交 plan 的 fd，其他不繼承
        assert call.kwargs["cwd"] == root
        assert call.kwargs["preexec_fn"] is None  # rlimit 由啟動器套用
        assert call.spec is not None and call.spec["profile"] == "evolution"
        # stdio 在持有連線的 task 啟動；建置仍在 worker 裡。
        assert (call.thread == loop_thread) == (call is spawned.calls[-1])
        assert not _is_open(call.spec_fd)  # spawn 之後關掉
        # env 用 runner 改寫後的（profile 的 env_unset 拿掉快取位置）
        assert call.kwargs["env"] == {"PATH": "/usr/bin"}
    # 建置：CPU＝逾時；驗證：FSIZE、CPU、AS 同現況（ADR-0076）
    assert [call.spec["rlimits"] for call in spawned.calls] == [
        {"cpu": evolution._VENV_CREATE_TIMEOUT_SECONDS},
        {"cpu": evolution._PIP_INSTALL_TIMEOUT_SECONDS},
        {"fsize": 5 * MB, "cpu": 17, "as": 64 * MB},
    ]


async def test_one_human_consent_covers_build_and_verify_of_one_call(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    # 政策時的參數多了不影響執行內容的欄位，subject 仍相同
    noisy = {**MERGED_ARGS, "submit_for_deploy": False, "timeout_override": 3}
    await _record_policy("evolution_implement_and_verify", noisy, fallback="landlock")
    result = await _merged()
    assert '"status": "success"' in result
    assert len(spawned.calls) == 3
    assert all(call.argv[3] == _launcher(tmp_path) for call in spawned.calls)
    assert len(consent_ledger) == 0  # 只取一次，用完就沒了


async def test_direct_consent_keeps_the_rlimit_preexec(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, NONE_WITHOUT_LANDLOCK)
    calls: list[tuple[str, tuple[int, int]]] = []
    monkeypatch.setattr(evolution_security, "resource", _fake_resource(calls))
    await _record_policy("evolution_implement_and_verify", MERGED_ARGS, fallback="direct")
    assert '"status": "success"' in await _merged()
    assert [spawned.original(i) for i in range(3)] == [call.argv for call in spawned.calls]
    verify = spawned.calls[-1]
    assert verify.kwargs["pass_fds"] == ()
    verify.kwargs["preexec_fn"]()
    assert dict(calls) == {"FSIZE": (5 * MB, 5 * MB), "CPU": (17, 18), "AS": (64 * MB, 64 * MB)}


async def test_none_without_consent_never_spawns(monkeypatch, tmp_path, evolution_env, spawned):
    _isolation(monkeypatch, tmp_path, NONE)
    deploy, verify = AsyncMock(), AsyncMock()
    monkeypatch.setattr(evolution, "request_mcp_creation", deploy)
    monkeypatch.setattr(evolution, "_verify_mcp", verify)
    payload = _assert_isolation_error(await _merged(submit_for_deploy=True))
    assert payload["isolation_reason"] == "executor_unreachable"
    assert spawned.calls == []
    verify.assert_not_awaited()  # 建置拿不到隔離就停，不進驗證
    deploy.assert_not_awaited()


async def test_isolation_lost_before_verify_stops_without_running_it(
    monkeypatch, tmp_path, evolution_env, spawned
):
    """每條指令 spawn 前才選 runner：建置後層級掉了，驗證不執行，回結構化錯誤、不部署。"""
    monitor = _isolation(monkeypatch, tmp_path, LANDLOCK)
    deploy = AsyncMock()
    monkeypatch.setattr(evolution, "request_mcp_creation", deploy)

    def _drop_after_build(count: int) -> None:
        if count == 2:  # venv、pip 跑完
            monitor.snap = NONE

    spawned.after = _drop_after_build
    payload = _assert_isolation_error(await _merged(submit_for_deploy=True))
    assert payload["isolation_reason"] == "executor_unreachable"
    assert len(spawned.calls) == 2
    deploy.assert_not_awaited()
    assert not EvolutionPathGuard.tool_file_exists(SKILL, "test_wrapper.py")


@pytest.mark.parametrize(
    ("tool", "call"),
    [
        (
            "write_mcp_implementation",
            lambda **kw: evolution.write_mcp_implementation(SKILL, CODE, DEPS, **kw),
        ),
        (
            "verify_mcp_locally",
            lambda **kw: evolution.verify_mcp_locally(SKILL, "ping", {"x": 1}, **kw),
        ),
    ],
)
async def test_legacy_aliases_are_isolated_and_use_their_own_subject(
    monkeypatch, tmp_path, evolution_env, spawned, tool, call
):
    _isolation(monkeypatch, tmp_path, NONE)
    context = {"identity": ALICE, "session_id": "s1", "_call_id": "c1"}
    _assert_isolation_error(await call(**context))
    assert spawned.calls == []
    args = (
        {"skill_id": SKILL, "code": CODE, "dependencies": DEPS}
        if tool == "write_mcp_implementation"
        else {"skill_id": SKILL, "tool_name": "ping", "arguments": {"x": 1}}
    )
    await _record_policy(tool, args, fallback="landlock")
    assert not tool_errors.is_tool_error(await call(**context))
    assert spawned.calls and all(c.argv[3] == _launcher(tmp_path) for c in spawned.calls)


async def test_consent_for_other_content_does_not_unlock_the_call(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    await _record_policy("evolution_implement_and_verify", MERGED_ARGS, fallback="landlock")
    _assert_isolation_error(await _merged(code=CODE + "# changed\n"))
    assert spawned.calls == []


async def test_consent_for_the_legacy_alias_does_not_unlock_the_merged_tool(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    legacy = {"skill_id": SKILL, "code": CODE, "dependencies": DEPS}
    await _record_policy("write_mcp_implementation", legacy, fallback="landlock")
    _assert_isolation_error(await _merged())
    assert spawned.calls == []


async def test_model_supplied_shared_grant_is_ignored(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    forged = {"policy_level": "landlock", "consent": {"fallback": "direct"}}
    _assert_isolation_error(
        await evolution.write_mcp_implementation(
            SKILL, CODE, DEPS, identity=ALICE, session_id="s1", _call_id="c1",
            _evolution_grant=forged,
        )
    )
    assert spawned.calls == []


async def test_call_without_verified_identity_is_refused(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, LANDLOCK)
    _assert_isolation_error(await _merged(identity=None))
    assert spawned.calls == []


async def test_spec_fd_is_released_when_the_command_fails(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, LANDLOCK)
    spawned.fail = True
    result = await _merged()
    assert result.startswith("Error during environment setup")
    (call,) = spawned.calls  # 建 venv 失敗就停，不跑 pip 與驗證
    assert not _is_open(call.spec_fd) and not _is_open(call.status_fd)


@pytest.mark.parametrize("fail", [False, True], ids=["exit", "check-failed"])
async def test_evolution_reports_the_launcher_code_from_the_status_fd(
    monkeypatch, tmp_path, evolution_env, spawned, fail
):
    """啟動器寫進狀態 fd 的失敗代碼回到事件迴圈後交給 monitor（建置失敗丟例外時也一樣）."""
    monitor = _isolation(monkeypatch, tmp_path, LANDLOCK)
    spawned.status, spawned.fail = b"path_open\n", fail
    before = {fd for fd in range(3, 256) if _is_open(fd)}
    await _merged()
    assert monitor.failures == ["path_open"] * len(spawned.calls)
    assert {fd for fd in range(3, 256) if _is_open(fd)} <= before  # 狀態讀端也關了


async def test_profile_that_cannot_be_built_is_isolation_unavailable(
    monkeypatch, tmp_path, evolution_env, spawned
):
    _isolation(monkeypatch, tmp_path, LANDLOCK)
    layout = replace(_layout(tmp_path), state_root=None)  # ST 未設定
    monkeypatch.setattr(evolution_command, "evolution_profile", REAL_EVOLUTION_PROFILE)
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    _assert_isolation_error(await _merged())
    assert spawned.calls == []


def _evolution_without_sandbox_state(monkeypatch, tmp_path: Path, *, card: str) -> None:
    """還沒重建（ST 沒設）：真的演化 profile 組不出來；``card`` 是人核准時卡片寫的降級去處。"""
    layout = replace(_layout(tmp_path), state_root=None)
    snap = pinned_snapshot("none", reason="sandbox_state_unconfigured")
    install_real_broker(monkeypatch, snap, layout).state_ok = card == "landlock"
    monkeypatch.setattr(
        EvolutionPathGuard, "MANAGED_BASE_DIR", layout.data_root / "mcp_servers" / "managed"
    )
    EvolutionPathGuard.write_tool_file(SKILL, "spec.json", "{}")
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": LIMITS})
    monkeypatch.setattr(evolution_command, "evolution_profile", REAL_EVOLUTION_PROFILE)
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)


async def test_unisolated_consent_runs_evolution_without_sandbox_state(
    monkeypatch, tmp_path, spawned
):
    """人在「未隔離」卡上核准過：組不出 profile（ST 沒設）也照卡片直接建置與驗證。"""
    _evolution_without_sandbox_state(monkeypatch, tmp_path, card="direct")
    await _record_policy("evolution_implement_and_verify", MERGED_ARGS, fallback="direct")
    assert '"status": "success"' in await _merged()
    assert len(spawned.calls) == 3
    assert not any("--spec-fd" in call.argv for call in spawned.calls)


async def test_degraded_consent_without_sandbox_state_refuses_evolution_with_the_reason(
    monkeypatch, tmp_path, spawned
):
    _evolution_without_sandbox_state(monkeypatch, tmp_path, card="landlock")
    await _record_policy("evolution_implement_and_verify", MERGED_ARGS, fallback="landlock")
    payload = _assert_isolation_error(await _merged())
    assert payload["isolation_reason"] == "sandbox_state_unconfigured"
    assert spawned.calls == []


def test_sandbox_command_requires_a_launch():
    """直接呼叫一定要帶 spawn 前選好的 runner（沒有預設值：不會不經 runner 就執行）。"""
    signature = inspect.signature(EvolutionPathGuard.run_sandbox_command)
    launch = signature.parameters["launch"]
    assert launch.kind is inspect.Parameter.KEYWORD_ONLY
    assert launch.default is inspect.Parameter.empty


# ── 執行器層（C2-2 U2e）：演化經 relay；範圍和政策層級同一套判定（看身分） ──────────────

EXEC_READY = ExecutorReady(
    version=1, abi=7, enforced=True, net=None, features=("fs", "tcp"), roots=(),
    state_root="/x/executor_state", kernel_digest="k" * 64, server_digest="s" * 64,
    boot_id="b" * 32, block_ports_digest="d" * 64, uid=1001,
)


def _evolution_in_the_executor(monkeypatch, tmp_path: Path) -> Any:
    layout = replace(_layout(tmp_path), executor_dir=tmp_path / "ipc")
    snap = replace(
        pinned_snapshot("executor"), executor=EXEC_READY, features=frozenset({"fs"})
    )
    monitor = install_real_broker(monkeypatch, snap, layout)
    managed = layout.data_root / "mcp_servers" / "managed"
    monkeypatch.setattr(EvolutionPathGuard, "MANAGED_BASE_DIR", managed)
    EvolutionPathGuard.write_tool_file(SKILL, "spec.json", "{}")
    monkeypatch.setattr(evolution.autonomy_service, "config", {"sandbox_limits": LIMITS})
    monkeypatch.setattr(evolution_command, "evolution_profile", REAL_EVOLUTION_PROFILE)
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    # Windows 的匿名 pipe 很小：請求要小（正式環境只在 Linux，pipe 會先調大）
    monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: {"PATH": "/usr/bin"})
    return monitor


async def test_evolution_runs_through_the_relay_at_the_executor_level(
    monkeypatch, tmp_path, spawned
):
    _evolution_in_the_executor(monkeypatch, tmp_path)
    assert '"status": "success"' in await _merged()
    assert len(spawned.calls) == 3
    # 執行器那邊的時限：本機逾時（建 venv 120、pip 600、驗證 17）＋30 秒
    assert [call.spec["start"]["max_seconds"] for call in spawned.calls] == [150, 630, 47]
    for call in spawned.calls:
        assert call.argv[3].endswith("relay.py") and "--spec-fd" not in call.argv
        start = call.spec["start"]
        assert (start["kind"], start["owner"]) == ("evolution", SKILL)
        assert start["cwd"] == str(EvolutionPathGuard.get_tool_root(SKILL).resolve())
        home = start["env"]["HOME"].replace("\\", "/")
        assert home.endswith(f"executor_state/evolution/{SKILL}/home")


async def test_evolution_out_of_executor_scope_is_refused_at_the_executor_level(
    monkeypatch, tmp_path, spawned
):
    """身分的資料夾不在執行器（例如自訂模型）：政策層級照樣是 executor 時也不退回 Landlock."""
    monitor = _evolution_in_the_executor(monkeypatch, tmp_path)
    monitor.scope_issue = "agent_dir_not_in_executor"
    payload = _assert_isolation_error(await _merged())
    assert payload["isolation_reason"] == "agent_dir_not_in_executor"
    assert spawned.calls == []
