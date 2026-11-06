"""ADR-0082：DispatchSupervisor 單元測試（proxy-spawn dispatch 子程序的生命週期）.

涵蓋：預設關閉 no-op、spawn 命令/env/行程群組參數、server.py 路徑解析、
崩潰後看門狗退避重生、stop() 終止語意、modern MCP 就緒探測逾時非致命。
生命週期測試 mock subprocess；fresh-child import 回歸只用 owned sandbox，
不啟動 dispatch server、不觸碰任何實體 agent 資料。
"""

# ruff: noqa: S105  # 測試用假 DISPATCH_TOKEN 值（非真密鑰），同 test_dispatch_hooks.py 慣例

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from src.config import PROXY_ROOT, WORKSPACE_ROOT, settings
from src.services import dispatch_supervisor as supervisor_module
from src.services.dispatch_supervisor import DispatchSupervisor

from tests.live_api_policy import PROVIDER_CREDENTIAL_ENV_VARS
from tests.support.zone_roots import point_zone_roots

_Q48_FACTORY_CHILD = r"""import json
import os
from pathlib import Path, PurePath
import sys

VERBATIM = '\\\\?\\'
def final(name):
    # Windows may report one file as both C:\... and \\?\C:\...; compare a single spelling.
    text = str(Path(os.fsdecode(name)).resolve())
    if text.startswith(VERBATIM) and text[5:7] == ':\\':
        text = text[len(VERBATIM):]
    return Path(text)
source = final(sys.argv[1])
owned = final(sys.argv[2])
# The repo root stays protected even when the venv lives outside the repo.
protected_roots = [source, final(sys.prefix).parent]
if sys.argv[4]:
    assert final(sys.argv[4]).is_relative_to(owned)
    protected_roots.append(final(sys.argv[4]))
probes = json.loads(sys.argv[5])
assert 'src' not in sys.modules and 'src.services.resource_manager' not in sys.modules
assert final(Path.cwd()) == source / 'ToukaDispatch'
base = final(sys.base_prefix)
allowed_reads = [source, owned, final(sys.prefix), base]
if sys.argv[3]:
    allowed_reads.append(final(sys.argv[3]))
# A packaged host (Claude desktop is MSIX) can keep the interpreter's files somewhere other
# than where its folder reports; also allow the folder that really holds the stdlib.
try:
    tail = PurePath(os.__file__).relative_to(sys.base_prefix).parts
except ValueError:
    tail = ()
physical = final(os.__file__).parts
if tail and PurePath(*physical[-len(tail):]) == PurePath(*tail):
    stdlib_root = Path(*physical[:-len(tail)])
    if stdlib_root != base:
        assert stdlib_root.name == base.name
        assert not any(
            root.is_relative_to(stdlib_root) for root in (*protected_roots, owned)
        )
        allowed_reads.append(stdlib_root)
def audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'subprocess.Popen', 'os.system'}:
        raise RuntimeError('FACTORY_CHILD_FORBIDDEN_OPERATION')
    if event == 'open':
        name = args[0]
        if isinstance(name, int):
            return
        path = final(name)
        if any(
            path == root / '.env' or path.is_relative_to(root / 'data') for root in protected_roots
        ):
            raise RuntimeError('FACTORY_CHILD_PROTECTED_PATH')
        mode, flags = args[1:3]
        writing = (
            isinstance(mode, str) and any(value in mode for value in 'wax+')
            or isinstance(flags, int)
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if writing and not path.is_relative_to(owned):
            raise RuntimeError('FACTORY_CHILD_WRITE_SCOPE')
        if not writing and not any(path.is_relative_to(root) for root in allowed_reads):
            raise RuntimeError('FACTORY_CHILD_READ_SCOPE')
    if event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'}:
        for name in args[:2] if event == 'os.rename' else args[:1]:
            if not isinstance(name, (str, bytes, os.PathLike)):
                continue
            if not final(name).is_relative_to(owned):
                raise RuntimeError('FACTORY_CHILD_WRITE_SCOPE')
sys.addaudithook(audit)
if probes:
    reasons = []
    for probe in probes:
        try:
            with open(probe, 'rb'):
                pass
        except RuntimeError as error:
            reasons.append(str(error))
    print(json.dumps({
        'status': 'PROBE_BLOCKED',
        'reasons': reasons,
        'protected_roots': [str(root) for root in protected_roots],
    }))
    raise SystemExit(0)
from memory_server import config_utils
assert final(config_utils.__file__) == (
    source / 'ToukaMemory/src/memory_server/config_utils.py'
)
try:
    manager = config_utils.get_resource_proxy()
except ModuleNotFoundError as error:
    missing = error.name if error.name in {
        'src', 'src.services', 'src.services.resource_manager'
    } else 'OTHER'
    result = {'status': 'IMPORT_ERROR', 'missing': missing}
else:
    from src import config
    module = sys.modules['src.services.resource_manager']
    assert final(module.__file__) == (
        source / 'ToukaProxy/src/services/resource_manager.py'
    )
    assert final(config.__file__) == source / 'ToukaProxy/src/config.py'
    assert manager is module.resource_manager
    assert final(config.get_app_data_dir()).is_relative_to(owned)
    result = {'status': 'FACTORY_OK', 'origins': 'SOURCE_EXACT', 'sandbox_ssot': True}
print(json.dumps(result))
"""


@pytest.mark.parametrize("inherited", ["empty", "relative", "duplicates"])
def test_q48_child_import_roots_keep_proxy_and_memory_first(monkeypatch, inherited):
    """A fresh child keeps both import roots when its cwd changes to Dispatch."""
    proxy = str(PROXY_ROOT)
    memory = str(WORKSPACE_ROOT / "ToukaMemory" / "src")
    paths = {
        "empty": [],
        "relative": [".", str(WORKSPACE_ROOT / "ToukaMemory")],
        "duplicates": [memory, proxy, ".", proxy, memory],
    }[inherited]
    original_env = {"PYTHONPATH": os.pathsep.join(paths), "Q48_SENTINEL": "preserved"}
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: dict(original_env))
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "q48-synthetic-token")
    environment = DispatchSupervisor().build_child_env()
    expected = list(dict.fromkeys([proxy, memory, *paths]))
    assert environment["PYTHONPATH"].split(os.pathsep) == expected
    assert original_env == {"PYTHONPATH": os.pathsep.join(paths), "Q48_SENTINEL": "preserved"}
    assert environment["Q48_SENTINEL"] == "preserved"
    assert environment["DISPATCH_TOKEN"] == "q48-synthetic-token"


def _q48_owned(tmp_path: Path) -> Path:
    """Create the owned sandbox root the fresh child may read and write."""
    owned = tmp_path / "fresh-child"
    owned.mkdir()
    for name in ("config", "data", "workspace"):
        (owned / name).mkdir()
    (owned / ".env").write_text("# owned empty factory regression environment\n", encoding="utf-8")
    return owned


def _q48_systemroot(environment: dict[str, str]) -> str:
    return next((value for key, value in environment.items() if key.upper() == "SYSTEMROOT"), "")


def _q48_plain(path: Path) -> Path:
    """Drop the verbatim prefix Windows may add on resolve, as the child's final() does."""
    text = str(path)
    return Path(text[4:]) if text.startswith("\\\\?\\") and text[5:7] == ":\\" else path


def _q48_outside_probes(token: str) -> list[Path]:
    """Missing paths beside the interpreter install and beside where its stdlib really lives."""
    stdlib = Path(os.__file__)
    depth = len(stdlib.relative_to(sys.base_prefix).parts)
    roots = (Path(sys.base_prefix).resolve(), stdlib.resolve().parents[depth - 1])
    return [root.parent / f"q48-outside-{index}-{token}" for index, root in enumerate(roots)]


def _run_q48_child(
    monkeypatch: pytest.MonkeyPatch,
    owned: Path,
    *,
    remove_proxy: bool = False,
    extra_protected: str = "",
    probes: list[Path] | None = None,
) -> dict[str, Any]:
    """Run the factory child with the supervisor's environment and return its last JSON line."""
    source = PROXY_ROOT.parent.resolve()
    env_file = owned / ".env"
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "PATH",
        "PATHEXT",
    }
    parent_env = {
        key: value for key, value in settings.get_full_env().items() if key.upper() in allowed
    }
    parent_env.update(
        MEMORY_APP_DATA_DIR=str(owned),
        SANCTUARY_DATA_PATH=str(owned / "data"),
        TOUKA_CONFIG_DIR=str(owned / "config"),
        TOUKA_ENV_FILE=str(env_file),
        WORKSPACE_ROOT=str(owned / "workspace"),
        HOST_WORKSPACE_ROOT=str(owned / "workspace"),
        TOUKA_BOOTSTRAP_ENABLED="false",
        DISPATCH_AUTOSPAWN="false",
        QDRANT_FORCE_DISABLE_MMAP="true",
        ENABLE_DYNAMIC_MODELS="false",
        TOUKA_EVAL_JUDGE_ENABLED="false",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        PYTHONPATH=os.pathsep.join(
            (".", str(source / "ToukaMemory/src"), str(source / "ToukaMemory"))
        ),
    )
    parent_env.update(dict.fromkeys(PROVIDER_CREDENTIAL_ENV_VARS, ""))
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: dict(parent_env))
    point_zone_roots(monkeypatch, owned, owned.parent / "zone_state")
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "q48-synthetic-token")
    monkeypatch.setattr(settings, "DISPATCH_SERVER_PATH", "")
    instance = DispatchSupervisor()
    child_env = instance.build_child_env()
    cwd = instance.resolve_server_path().parent
    assert cwd.resolve() == source / "ToukaDispatch"
    for key in (
        "MEMORY_APP_DATA_DIR",
        "SANCTUARY_DATA_PATH",
        "TOUKA_CONFIG_DIR",
        "TOUKA_ENV_FILE",
        "WORKSPACE_ROOT",
        "HOST_WORKSPACE_ROOT",
    ):
        assert Path(child_env[key]).resolve().is_relative_to(owned)
    if remove_proxy:
        child_env["PYTHONPATH"] = os.pathsep.join(
            path
            for path in child_env["PYTHONPATH"].split(os.pathsep)
            if path != str(PROXY_ROOT)
        )
    argv = [
        sys.executable,
        "-B",
        "-c",
        _Q48_FACTORY_CHILD,
        str(source),
        str(owned),
        _q48_systemroot(child_env),
        extra_protected,
        json.dumps([str(probe) for probe in probes or ()]),
    ]
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert len(result.stdout) <= 1024**2 and len(result.stderr) <= 1024**2
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("remove_proxy", [False, True])
def test_q48_fresh_child_resource_proxy_from_supervisor(monkeypatch, tmp_path, remove_proxy):
    """Exercise the original factory without parent module cache or path assistance."""
    observed = _run_q48_child(monkeypatch, _q48_owned(tmp_path), remove_proxy=remove_proxy)
    if remove_proxy:
        assert observed == {"status": "IMPORT_ERROR", "missing": "src.services"}
    else:
        assert observed == {"status": "FACTORY_OK", "origins": "SOURCE_EXACT", "sandbox_ssot": True}


@pytest.mark.parametrize("kind", ["outside", "protected"])
def test_q48_fresh_child_audit_blocks_probe_reads(monkeypatch, tmp_path, kind):
    """Reads beside the interpreter stay out of scope; data/ and .env stay blocked when readable."""
    owned = _q48_owned(tmp_path)
    token = uuid.uuid4().hex
    if kind == "outside":
        probes = _q48_outside_probes(token)
        extra_protected = ""
        expected = "FACTORY_CHILD_READ_SCOPE"
        readable = [PROXY_ROOT.parent.resolve(), owned.resolve(), Path(sys.prefix).resolve()]
        systemroot = _q48_systemroot(settings.get_full_env())
        if systemroot:
            readable.append(Path(systemroot).resolve())
        assert not any(
            _q48_plain(probe).is_relative_to(root) for probe in probes for root in readable
        )
    else:
        fake_root = owned / "fake-root"
        probes = [fake_root / ".env", fake_root / "data" / f"q48-{token}"]
        extra_protected = str(fake_root)
        expected = "FACTORY_CHILD_PROTECTED_PATH"
    assert not any(probe.exists() for probe in probes)
    observed = _run_q48_child(monkeypatch, owned, extra_protected=extra_protected, probes=probes)
    protected_roots = [Path(root) for root in observed.pop("protected_roots")]
    assert PROXY_ROOT.parent.resolve() in protected_roots
    assert observed == {"status": "PROBE_BLOCKED", "reasons": [expected] * len(probes)}


class FakeProc:
    """最小 asyncio subprocess 替身：可控退出、提供 pid/returncode/wait()。"""

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._exit = asyncio.Event()

    async def wait(self) -> int:
        await self._exit.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def exit(self, code: int = 1) -> None:
        self.returncode = code
        self._exit.set()

    def kill(self) -> None:
        self.exit(-9)


class _GracefulProc:
    """收到 graceful signal 後 wait() 立即以指定 exit code 結束的替身。"""

    pid = 4321

    def __init__(self, exit_code: int = 0) -> None:
        self.returncode: int | None = None
        self._exit_code = exit_code

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self.returncode


async def _group_is_quiescent(_target: object) -> bool:
    return True


async def _group_not_quiescent(_target: object) -> bool:
    return False


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """輪詢等待條件成立（事件迴圈讓步），避免測試對排程時序的脆弱假設。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition not met within timeout")
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 開關與路徑解析
# ---------------------------------------------------------------------------


async def test_disabled_by_default_no_spawn():
    """DISPATCH_AUTOSPAWN 預設 False（conftest 環境）→ start() no-op、不 spawn。"""
    assert settings.DISPATCH_AUTOSPAWN is False
    sup = DispatchSupervisor()
    with patch(
        "src.services.dispatch_supervisor.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        started = await sup.start()
    assert started is False
    assert sup.status()["status"] == "disabled"
    mock_exec.assert_not_awaited()


def test_resolve_server_path_default():
    """未覆寫時解析為 <repo根>/ToukaDispatch/server.py（native 與容器同一規則）。"""
    sup = DispatchSupervisor()
    resolved = sup.resolve_server_path()
    assert resolved == WORKSPACE_ROOT / "ToukaDispatch" / "server.py"
    assert resolved.exists(), "dispatch server.py 應存在於 repo（源碼不搬家 SSOT）"


def test_resolve_server_path_override(monkeypatch, tmp_path):
    """settings.DISPATCH_SERVER_PATH 非空時優先採用（部署彈性）。"""
    custom = tmp_path / "custom_server.py"
    monkeypatch.setattr(settings, "DISPATCH_SERVER_PATH", str(custom))
    sup = DispatchSupervisor()
    assert sup.resolve_server_path() == custom


# ---------------------------------------------------------------------------
# 子程序環境（dispatch 的安全前提：token / 路徑 / 回呼 URL 全由 supervisor 注入）
# ---------------------------------------------------------------------------


def test_build_child_env_injects_dispatch_contract(monkeypatch):
    """子程序 env 須含 bind host（預設 127.0.0.1）、port、token、回呼 URL、memory 路徑。"""
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "tok-unit-test")
    sup = DispatchSupervisor()
    env = sup.build_child_env()
    assert env["DISPATCH_BIND_HOST"] == "127.0.0.1"  # secure by default
    assert env["DISPATCH_PORT"] == str(settings.DISPATCH_PORT)
    assert env["DISPATCH_TOKEN"] == "tok-unit-test"
    assert env["TOUKA_PROXY_URL"] == f"http://127.0.0.1:{settings.PORT}"
    assert env["MEMORY_APP_DATA_DIR"]
    assert str(WORKSPACE_ROOT / "ToukaMemory" / "src") in env["PYTHONPATH"]


def test_build_child_env_keeps_inherited_token_when_setting_empty(monkeypatch):
    """settings.DISPATCH_TOKEN 空字串時不得覆蓋掉父環境既有的 DISPATCH_TOKEN。"""
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "")
    monkeypatch.setenv("DISPATCH_TOKEN", "tok-from-parent-env")
    sup = DispatchSupervisor()
    env = sup.build_child_env()
    assert env["DISPATCH_TOKEN"] == "tok-from-parent-env"


# ---------------------------------------------------------------------------
# spawn 與看門狗
# ---------------------------------------------------------------------------


async def test_start_spawns_with_expected_command(monkeypatch):
    """啟用時以 [python, server.py] spawn，cwd=dispatch 目錄、行程群組 kwargs 正確。"""
    monkeypatch.setattr(settings, "DISPATCH_AUTOSPAWN", True)
    proc = FakeProc()
    sup = DispatchSupervisor()
    with (
        patch(
            "src.services.dispatch_supervisor.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec,
        patch.object(DispatchSupervisor, "_wait_ready", new_callable=AsyncMock),
    ):
        started = await sup.start()
        assert started is True
        assert sup.status()["status"] == "running"
        assert sup.status()["pid"] == proc.pid

        mock_exec.assert_awaited_once()
        args, kwargs = mock_exec.await_args
        expected_path = WORKSPACE_ROOT / "ToukaDispatch" / "server.py"
        assert args[0] == sys.executable
        assert args[1] == str(expected_path)
        assert kwargs["cwd"] == str(expected_path.parent)
        assert kwargs["env"]["DISPATCH_BIND_HOST"] == "127.0.0.1"
        if sys.platform == "win32":
            assert kwargs.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            assert kwargs.get("start_new_session") is True

        with (
            patch(
                "src.services.dispatch_supervisor._signal_dispatch_process_group",
                side_effect=lambda p: p.exit(0),
            ) as graceful_signal,
            patch(
                "src.services.dispatch_supervisor._wait_dispatch_process_group_quiescent",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("src.services.dispatch_supervisor.terminate_process_tree") as hard_kill,
        ):
            await sup.stop()
        graceful_signal.assert_called_once_with(proc)
        hard_kill.assert_not_called()
    assert sup.status()["status"] == "stopped"


async def test_start_missing_server_file_fails_gracefully(monkeypatch, tmp_path):
    """server.py 不存在→ start() 回 False、狀態 failed、不 spawn、不拋例外。"""
    monkeypatch.setattr(settings, "DISPATCH_AUTOSPAWN", True)
    monkeypatch.setattr(settings, "DISPATCH_SERVER_PATH", str(tmp_path / "missing.py"))
    sup = DispatchSupervisor()
    with patch(
        "src.services.dispatch_supervisor.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        assert await sup.start() is False
    assert sup.status()["status"] == "failed"
    mock_exec.assert_not_awaited()


def test_backoff_delay_is_capped_exponential():
    """退避序列 1,2,4,8,16 後封頂 30 秒（防重生風暴）。"""
    delays = [DispatchSupervisor._backoff_delay(n) for n in range(1, 8)]
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_windows_group_quiescence_is_unprovable_without_job_containment(monkeypatch):
    """CREATE_NEW_PROCESS_GROUP 不是 containment，leader exit 後不可假裝群組已空。"""
    monkeypatch.setattr(supervisor_module.sys, "platform", "win32")
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda *_args: pytest.fail("Windows unprovable path must not probe POSIX PGID"),
        raising=False,
    )

    assert supervisor_module._dispatch_process_group_quiescence(FakeProc()) is None


def test_posix_group_quiescence_probes_the_spawned_pgid(monkeypatch):
    """start_new_session 將 leader pid 固定為 PGID；ESRCH 才能證明已空。"""
    proc = FakeProc(pid=4321)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")

    def missing_group(group_id: int, sig: int) -> None:
        calls.append((group_id, sig))
        raise ProcessLookupError

    monkeypatch.setattr(supervisor_module.os, "killpg", missing_group, raising=False)
    assert supervisor_module._dispatch_process_group_quiescence(proc) is True
    assert calls == [(proc.pid, 0)]

    monkeypatch.setattr(supervisor_module.os, "killpg", lambda *_args: None, raising=False)
    assert supervisor_module._dispatch_process_group_quiescence(proc) is False


def test_posix_group_cleanup_retains_pgid_after_leader_exit(monkeypatch):
    """leader 已被 wait/reap 後仍直接 kill 原 PGID，不重查消失的 leader pid。"""
    proc = FakeProc(pid=4321)
    calls: list[tuple[int, int]] = []
    sigkill = 9
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")
    monkeypatch.setattr(supervisor_module.signal, "SIGKILL", sigkill, raising=False)
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda group_id, sig: calls.append((group_id, sig)),
        raising=False,
    )

    supervisor_module._force_dispatch_process_group(proc)

    assert calls == [(proc.pid, sigkill)]


async def test_watchdog_respawns_after_crash(monkeypatch):
    """dispatch 崩潰→看門狗計數 +1 並退避後重生；stop() 後不再重生。"""
    monkeypatch.setattr(settings, "DISPATCH_AUTOSPAWN", True)
    # 退避歸零讓測試即時（退避數值已由 test_backoff_delay_is_capped_exponential 鎖定）
    monkeypatch.setattr(DispatchSupervisor, "_backoff_delay", staticmethod(lambda _n: 0))
    p1, p2 = FakeProc(pid=111), FakeProc(pid=222)
    sup = DispatchSupervisor()
    with (
        patch(
            "src.services.dispatch_supervisor.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=[p1, p2],
        ) as mock_exec,
        patch.object(DispatchSupervisor, "_wait_ready", new_callable=AsyncMock),
    ):
        await sup.start()
        assert sup.status()["pid"] == 111

        p1.exit(1)  # 模擬崩潰
        await _wait_until(lambda: sup.status()["restarts"] == 1)
        await _wait_until(lambda: mock_exec.await_count == 2)
        await _wait_until(lambda: sup.status()["status"] == "running")
        assert sup.status()["pid"] == 222

        with (
            patch(
                "src.services.dispatch_supervisor._signal_dispatch_process_group",
                side_effect=lambda p: p.exit(0),
            ) as graceful_signal,
            patch(
                "src.services.dispatch_supervisor._wait_dispatch_process_group_quiescent",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("src.services.dispatch_supervisor.terminate_process_tree") as hard_kill,
        ):
            await sup.stop()
        graceful_signal.assert_called_once_with(p2)
        hard_kill.assert_not_called()
    # stop 後即使再有退出事件也不得重生
    assert mock_exec.await_count == 2
    assert sup.status()["status"] == "stopped"


# ---------------------------------------------------------------------------
# stop() 終止語意（graceful group signal → 有界等待 → 不確定即強制且 fail-closed）
# ---------------------------------------------------------------------------


def _record_process_control(monkeypatch) -> dict[str, list[object]]:
    """攔截三個行程控制原語，只記錄呼叫對象（不碰真行程）。"""
    calls: dict[str, list[object]] = {"signals": [], "hard_kills": [], "forced_groups": []}
    monkeypatch.setattr(
        supervisor_module,
        "_signal_dispatch_process_group",
        calls["signals"].append,
    )
    monkeypatch.setattr(supervisor_module, "terminate_process_tree", calls["hard_kills"].append)
    monkeypatch.setattr(
        supervisor_module,
        "_force_dispatch_process_group",
        calls["forced_groups"].append,
    )
    return calls


def _started_supervisor(proc: object) -> DispatchSupervisor:
    supervisor = DispatchSupervisor()
    supervisor._proc = proc
    supervisor._process_started = True
    supervisor._status = "running"
    return supervisor


async def test_stop_prefers_bounded_graceful_group_stop_before_hard_kill(monkeypatch):
    """rc=0 且群組可證明已空 → 只送 graceful signal，狀態 stopped。"""
    calls = _record_process_control(monkeypatch)
    monkeypatch.setattr(
        supervisor_module, "_wait_dispatch_process_group_quiescent", _group_is_quiescent
    )
    proc = _GracefulProc()
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [], "forced_groups": []}
    assert supervisor.status()["status"] == "stopped"
    assert supervisor.status()["pid"] is None


async def test_stop_clean_leader_with_live_group_forces_group_and_fails_closed(monkeypatch):
    """leader rc=0 但群組仍有殘留 → 強制殺原 PGID，狀態 failed 而非 stopped。"""
    calls = _record_process_control(monkeypatch)
    monkeypatch.setattr(
        supervisor_module, "_wait_dispatch_process_group_quiescent", _group_not_quiescent
    )
    proc = _GracefulProc()
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [], "forced_groups": [proc]}
    assert supervisor.status()["status"] == "failed"


async def test_stop_group_quiescence_probe_error_forces_group_and_fails_closed(monkeypatch):
    """群組靜默探測本身拋例外＝無法證明乾淨 → 強制殺群組並 fail-closed。"""
    calls = _record_process_control(monkeypatch)

    async def probe_error(_target: object) -> bool:
        raise OSError

    monkeypatch.setattr(supervisor_module, "_wait_dispatch_process_group_quiescent", probe_error)
    proc = _GracefulProc()
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [], "forced_groups": [proc]}
    assert supervisor.status()["status"] == "failed"


async def test_stop_nonzero_after_graceful_signal_is_failed_not_stopped(monkeypatch):
    """graceful signal 後以非 0 退出 → 已退出不再硬殺，但狀態必須是 failed。"""
    calls = _record_process_control(monkeypatch)
    monkeypatch.setattr(
        supervisor_module,
        "_wait_dispatch_process_group_quiescent",
        lambda _target: pytest.fail("nonzero exit must not be probed as a clean stop"),
    )
    proc = _GracefulProc(exit_code=23)
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [], "forced_groups": []}
    assert supervisor.status()["status"] == "failed"


@pytest.mark.parametrize("failure", ["graceful_timeout", "signal_error"])
async def test_stop_hard_kill_fallback_is_failed_and_idempotent(monkeypatch, failure):
    """graceful 等待逾時或 signal 失敗 → 硬殺整棵行程樹；狀態 failed，重複 stop 不再動作。"""
    proc = FakeProc(pid=9876)
    calls = _record_process_control(monkeypatch)

    def hard_kill(target: FakeProc) -> None:
        calls["hard_kills"].append(target)
        target.exit(-9)

    monkeypatch.setattr(supervisor_module, "terminate_process_tree", hard_kill)
    if failure == "graceful_timeout":
        monkeypatch.setattr(supervisor_module, "_GRACEFUL_STOP_TIMEOUT_SECONDS", 0.001)
    else:

        def signal_error(target: object) -> None:
            calls["signals"].append(target)
            raise ProcessLookupError

        monkeypatch.setattr(supervisor_module, "_signal_dispatch_process_group", signal_error)
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [proc], "forced_groups": []}
    assert proc.returncode == -9
    assert supervisor.status()["status"] == "failed"

    await supervisor.stop()

    assert calls == {"signals": [proc], "hard_kills": [proc], "forced_groups": []}
    assert supervisor.status()["status"] == "failed"


async def test_stop_already_exited_race_is_not_relabelled_as_clean_stop(monkeypatch):
    """stop 前子程序已自行退出 → 不送 signal、不硬殺，且不得標成 stopped。"""
    calls = _record_process_control(monkeypatch)
    proc = _GracefulProc()
    proc.returncode = 1
    supervisor = _started_supervisor(proc)

    await supervisor.stop()

    assert calls == {"signals": [], "hard_kills": [], "forced_groups": []}
    assert supervisor.status()["status"] == "failed"


async def test_stop_after_watchdog_observed_exit_is_failed(monkeypatch):
    """看門狗已清掉 proc（曾啟動但已退出）→ stop 不做行程控制，狀態 failed。"""
    calls = _record_process_control(monkeypatch)
    supervisor = DispatchSupervisor()
    supervisor._process_started = True
    supervisor._status = "crashed"

    await supervisor.stop()

    assert calls == {"signals": [], "hard_kills": [], "forced_groups": []}
    assert supervisor.status()["status"] == "failed"


async def test_stop_never_started_keeps_disabled_status(monkeypatch):
    """從未啟動 → stop 為 no-op，不改寫 disabled 狀態。"""
    calls = _record_process_control(monkeypatch)
    supervisor = DispatchSupervisor()

    await supervisor.stop()

    assert calls == {"signals": [], "hard_kills": [], "forced_groups": []}
    assert supervisor.status()["status"] == "disabled"


# ---------------------------------------------------------------------------
# 停止不卡事件迴圈：Windows 的 terminate_process_tree 是同步、沒有逾時的 taskkill，
# 必須在執行緒裡跑。假的 terminate 卡在一個只有事件迴圈能打開的閘門上：
# 在迴圈執行緒上呼叫時迴圈被卡住、閘門等到逾時都打不開。
# ---------------------------------------------------------------------------

_HARD_KILL_GATE_SECONDS = 2.0


async def _stop_with_blocking_hard_kill(
    monkeypatch, supervisor: DispatchSupervisor
) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    seen: dict[str, object] = {}

    def blocking_hard_kill(target: object) -> None:
        started.set()
        seen["released_by_loop"] = release.wait(_HARD_KILL_GATE_SECONDS)
        seen["on_loop_thread"] = threading.get_ident() == loop_thread
        exit_proc = getattr(target, "exit", None)
        if exit_proc is not None:
            loop.call_soon_threadsafe(exit_proc, -9)

    monkeypatch.setattr(supervisor_module, "terminate_process_tree", blocking_hard_kill)
    stop_task = asyncio.create_task(supervisor.stop())
    await _wait_until(started.is_set)
    release.set()
    await stop_task
    return seen


async def test_stop_hard_kill_runs_off_the_event_loop(monkeypatch):
    """graceful signal 失敗 → 硬殺行程樹在執行緒裡跑，迴圈照常運轉；回收語意不變。"""
    proc = FakeProc(pid=9876)

    def signal_error(_target: object) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(supervisor_module, "_signal_dispatch_process_group", signal_error)
    supervisor = _started_supervisor(proc)

    seen = await _stop_with_blocking_hard_kill(monkeypatch, supervisor)

    assert seen == {"released_by_loop": True, "on_loop_thread": False}
    assert proc.returncode == -9
    assert supervisor.status()["status"] == "failed"


async def test_windows_forced_group_kill_runs_off_the_event_loop(monkeypatch):
    """Windows 證明不了群組已空，每次停止都強制殺樹；taskkill 不得卡住事件迴圈。"""
    monkeypatch.setattr(supervisor_module.sys, "platform", "win32")
    monkeypatch.setattr(supervisor_module, "_signal_dispatch_process_group", lambda _p: None)
    supervisor = _started_supervisor(_GracefulProc())

    seen = await _stop_with_blocking_hard_kill(monkeypatch, supervisor)

    assert seen == {"released_by_loop": True, "on_loop_thread": False}
    assert supervisor.status()["status"] == "failed"


async def test_wait_ready_timeout_is_nonfatal(monkeypatch):
    """Modern /mcp 就緒探測逾時→僅警告、不拋例外（看門狗續管）。"""
    sup = DispatchSupervisor()
    with patch(
        "src.services.dispatch_supervisor._probe_dispatch_mcp",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await sup._wait_ready(timeout=0.05)  # 不應 raise


async def test_modern_readiness_normalizes_connection_failure(monkeypatch):
    """連線／協定未就緒須回 False，不能讓 supervisor 啟動流程逸出例外。"""

    def fail_client():
        raise OSError

    monkeypatch.setattr(
        supervisor_module,
        "build_mcp_http_client",
        fail_client,
    )

    assert await supervisor_module._probe_dispatch_mcp(8083) is False


async def test_wait_ready_success_uses_modern_mcp_probe(monkeypatch):
    """探測成功必須是 modern /mcp tools/list，不再以裸 TCP socket 冒充協定健康。"""
    sup = DispatchSupervisor()
    with patch(
        "src.services.dispatch_supervisor._probe_dispatch_mcp",
        new_callable=AsyncMock,
        return_value=True,
    ) as probe:
        await sup._wait_ready(timeout=1.0)
    probe.assert_awaited_once_with(settings.DISPATCH_PORT)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
