"""執行器伺服器（``executor_server.py``＋``executor_session.py``，C2-2 設計 §2.2～§2.7）.

假 stream、假 spawn、假行程表，任何平台都能跑；真核心的案例在 ``test_isolation_linux.py``。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import dataclasses
import hashlib
import inspect
import json
import os
import re
import stat
import struct
import sys
import threading
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from src.sandbox_kernel import framing as kfr
from src.sandbox_kernel import landlock as kll
from src.sandbox_kernel import owner_repair as kor
from src.sandbox_kernel import spec as kspec
from src.services.isolation import executor_client as ec
from src.services.isolation import executor_server as srv
from src.services.isolation import executor_session as es
from src.services.isolation import profiles
from src.utils import isolation_policy

PD = "/srv/data/personal_drive"
MCP = "/srv/data/mcp_servers"
SKILLS = "/srv/config/skills"
STATE = "/srv/executor_state"
VENV = "/opt/venv"
EXEC_PATHS = (*es.SYSTEM_EXEC_PATHS, VENV)
LAYOUT = es.Layout(rw=(PD, MCP), ro=(SKILLS,), state=STATE, exec_paths=EXEC_PATHS)
HELLO_PORTS = frozenset({*kspec.DEFAULT_BLOCK_PORTS, 4000})
SERVER_PID = 4321


def _system_rules() -> list[dict]:
    rules = [{"path": p, "access": "rox", "optional": True} for p in (*es.SYSTEM_EXEC_PATHS, VENV)]
    rules += [{"path": p, "access": "ro", "optional": True} for p in es.SYSTEM_READ_PATHS]
    rules += [{"path": p, "access": "dev_rw", "optional": True} for p in es.DEVICE_RW_PATHS]
    rules += [{"path": p, "access": "ro", "optional": True} for p in es.DEVICE_READ_PATHS]
    return rules


def _start(kind: str = "agents", owner: str = "Alice", **overrides) -> dict:
    target = f"{PD}/{owner}" if kind == "agents" else f"{MCP}/managed/{owner}"
    home, tmp = LAYOUT.state_dirs(kind, owner)
    rules = [*_system_rules(), {"path": SKILLS, "access": "rox", "optional": True}]
    rules += [{"path": p, "access": "rwx"} for p in (target, home, tmp)]
    document = {
        "argv": ["sh", "-c", "echo hi"],
        "cwd": target,
        "env": {"PATH": "/usr/bin:/bin", "HOME": home, **dict(kspec.EXECUTOR_GIT_CONFIG)},
        "spec": {
            "version": kspec.SPEC_VERSION,
            "profile": "agent_command",
            "rules": rules,
            "block_ports": sorted(HELLO_PORTS | {11434}),
            "rlimits": {},
            "pin": True,
            "parent_pid": 99,
            "net_strict": True,
        },
        "kind": kind,
        "owner": owner,
        "io": "merged",
        "max_seconds": 60,
    }
    document.update(overrides)
    return document


def _with_spec(**fields) -> dict:
    document = _start()
    document["spec"] = {**document["spec"], **fields}
    return document


def _with_rule(rule: dict) -> dict:
    document = _start()
    return _with_spec(rules=[*document["spec"]["rules"], rule])


def _check(document: dict) -> es.StartPlan:
    return es.check_start(
        document, spec=kspec, layout=LAYOUT, hello_ports=HELLO_PORTS, server_pid=SERVER_PID
    )


# ── START 驗證（§2.4）──────────────────────────────────────────────────────────────


def test_valid_agent_start_becomes_a_plan_with_the_servers_parent_pid():
    plan = _check(_start())
    assert plan.cwd == f"{PD}/Alice" and plan.root == PD
    assert (plan.home, plan.tmp) == tuple(f"{STATE}/agents/Alice/{d}" for d in ("home", "tmp"))
    assert plan.argv == ("sh", "-c", "echo hi") and plan.max_seconds == 60
    assert plan.ports == tuple(sorted(HELLO_PORTS | {11434}))
    sent = json.loads(plan.spec)
    assert sent["parent_pid"] == SERVER_PID  # 啟動器的父行程是伺服器，不是 proxy
    assert sent["net_strict"] is True and sent["pin"] is True and sent["profile"] == "agent_command"
    assert kspec.parse_spec(plan.spec).rules == kspec.parse_spec(
        json.dumps(_start()["spec"]).encode()
    ).rules
    assert es.EXEC_ID_ENV not in plan.env


def test_net_strict_false_is_passed_through():
    assert json.loads(_check(_with_spec(net_strict=False)).spec)["net_strict"] is False


def test_valid_evolution_start_uses_managed_skill_folder():
    plan = _check(_start("evolution", "my_tool"))
    assert plan.cwd == f"{MCP}/managed/my_tool" and plan.root == MCP
    assert plan.home == f"{STATE}/evolution/my_tool/home"


REJECTED = {
    "extra_key": dict(_start(), extra=1),
    "missing_key": {k: v for k, v in _start().items() if k != "io"},
    "kind_unknown": _start(kind="tools"),
    "owner_dotdot": _start(owner="..", cwd=f"{PD}/.."),
    "owner_slash": _start(owner="a/b", cwd=f"{PD}/a/b"),
    "owner_empty": _start(owner="", cwd=PD),
    "owner_space": _start(owner=" Alice", cwd=f"{PD}/ Alice"),
    "io_pty": _start(io="pty"),  # C2-3：stdio 改成通過（``test_stdio_is_accepted_like_merged``）
    "io_null": _start(io=None),
    "max_seconds_zero": _start(max_seconds=0),
    "max_seconds_over": _start(max_seconds=901),
    "max_seconds_bool": _start(max_seconds=True),
    "argv_empty": _start(argv=[]),
    "argv_nul": _start(argv=["sh", "a\x00b"]),
    "argv_not_str": _start(argv=["sh", 1]),
    "argv_too_many": _start(argv=["x"] * (es.MAX_ARGV_ITEMS + 1)),
    "argv_too_long": _start(argv=["x" * (es.MAX_ARGV_BYTES + 1)]),
    "cwd_other_owner": _start(cwd=f"{PD}/Bob"),
    "cwd_root": _start(cwd=PD),
    "cwd_state_home": _start(cwd=f"{STATE}/agents/Alice/home"),
    "env_ld_preload": _start(env={"LD_PRELOAD": "x.so"}),
    "env_exec_id": _start(env={es.EXEC_ID_ENV: "f" * 32}),
    "env_git_other": _start(env={"GIT_CONFIG_COUNT": "2"}),
    "env_proxy": _start(env={"HTTPS_PROXY": "http://u:p@h"}),
    "spec_not_dict": _start(spec=[]),
    "spec_version": _with_spec(version=2),
    "spec_extra_key": _with_spec(umask=18),
    "spec_ports_missing_hello": _with_spec(block_ports=sorted(kspec.DEFAULT_BLOCK_PORTS)),
    "rw_other_owner": _with_rule({"path": f"{PD}/Bob", "access": "rwx"}),
    "rw_whole_root": _with_rule({"path": PD, "access": "rwx"}),
    "rw_other_kind_root": _with_rule({"path": f"{MCP}/managed/Alice", "access": "rwx"}),
    "rw_other_state_kind": _with_rule({"path": f"{STATE}/evolution/Alice/home", "access": "rwx"}),
    "rw_other_state_owner": _with_rule(
        {"path": LAYOUT.state_dirs("agents", "Bob")[1], "access": "rwx"}
    ),
    "rw_state_root": _with_rule({"path": STATE, "access": "rwx"}),
    "rw_system": _with_rule({"path": "/usr", "access": "rwx"}),
    "ro_other_owner": _with_rule({"path": f"{PD}/Bob", "access": "ro"}),
    "ro_data_soul": _with_rule({"path": "/srv/data/soul", "access": "ro"}),
    "ro_public_config_above_cwd": _with_rule({"path": "/srv/pyproject.toml", "access": "ro"}),
    "rox_etc": _with_rule({"path": "/etc", "access": "rox"}),
    "dev_rw_etc": _with_rule({"path": "/etc/passwd", "access": "dev_rw"}),
    "dev_rw_random": _with_rule({"path": "/dev/random", "access": "dev_rw"}),
    "ro_state_root": _with_rule({"path": STATE, "access": "ro"}),
    "rwx_rule_missing": _with_spec(rules=_system_rules()),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_start_is_rejected(name):
    with pytest.raises(es.Rejected):
        _check(REJECTED[name])


MCP_SEG = "m" + "0123456789abcdef01234567"


def _mcp_start(**overrides) -> dict:
    """kind mcp（C2-3）：讀寫只有自己的 home／tmp、cwd 是 home、stdio、沒有時限（0）."""
    home, tmp = LAYOUT.state_dirs("mcp", MCP_SEG)
    rules = [*_system_rules(), *({"path": p, "access": "rwx"} for p in (home, tmp))]
    document = _start(cwd=home, kind="mcp", owner=MCP_SEG, io="stdio", max_seconds=0)
    document["spec"] = {**document["spec"], "profile": "mcp_self", "rules": rules}
    document.update(overrides)
    return document


def test_stdio_is_accepted_like_merged():
    """``io`` 收 ``stdio``（MCP 伺服器、演化驗證）；其他值仍拒絕（``REJECTED`` 的 io_*）."""
    plan = _check(_start(io="stdio"))
    assert (plan.io, plan.max_seconds, plan.cwd) == ("stdio", 60, f"{PD}/Alice")
    assert _check(_start()).io == "merged"
    assert _check(_mcp_start()).max_seconds == 0


def test_ro_rules_inside_system_paths_and_read_only_roots_are_allowed():
    for rule in (
        {"path": "/usr/lib/python3", "access": "ro"},
        {"path": f"{VENV}/bin", "access": "rox"},
        {"path": "/proc/self", "access": "ro"},
        {"path": f"{SKILLS}/demo", "access": "rox"},
        {"path": "/dev/null", "access": "ro"},
    ):
        _check(_with_rule(rule))


def test_rejection_reasons_are_codes():
    with pytest.raises(es.Rejected) as caught:
        _check(_start(env={"LD_PRELOAD": "x"}))
    assert caught.value.reason == "env_name"
    with pytest.raises(es.Rejected) as caught:
        _check(_with_spec(version=2))
    assert caught.value.reason == "spec_version"


# ── 和正本一致（伺服器不能匯入 profiles／isolation_policy，各存一份）─────────────────────


def test_system_path_tables_match_the_profile_ssot():
    assert es.SYSTEM_EXEC_PATHS == profiles.SYSTEM_EXEC_PATHS
    assert es.SYSTEM_READ_PATHS == profiles.SYSTEM_READ_PATHS
    assert es.DEVICE_RW_PATHS == profiles.DEVICE_RW_PATHS
    assert es.DEVICE_READ_PATHS == profiles.DEVICE_READ_PATHS


@pytest.mark.parametrize(
    "name",
    ["Alice", "小愛", "a.b", "", " ", " a", "a ", ".", "..", "a/b", "a\\b", "a\x00b", "...", "a b"],
)
def test_safe_segment_matches_isolation_policy(name):
    assert es.safe_segment(name) == isolation_policy.is_safe_segment(name)


@pytest.mark.parametrize("abi", [0, 2, 3, 4, 5, 6, 7, 9])
@pytest.mark.parametrize("net", [None, kspec.NET_TCP_FASTOPEN])
def test_features_match_isolation_policy(abi, net):
    expected = isolation_policy.landlock_features(abi)
    if net:
        expected -= {isolation_policy.TCP_FEATURE}
    assert frozenset(es.landlock_features(abi, net)) == expected


def test_layout_targets_follow_the_kind_table():
    assert LAYOUT.target("agents", "Alice") == (PD, f"{PD}/Alice")
    assert LAYOUT.target("evolution", "t") == (MCP, f"{MCP}/managed/t")
    only_pd = es.Layout(rw=(PD,), ro=(), state=STATE, exec_paths=())
    assert only_pd.target("evolution", "t") is None
    assert str(PurePosixPath(STATE, "agents", "Alice")) == f"{STATE}/agents/Alice"


def test_server_exec_paths_include_its_own_prefixes():
    """同 profiles 的 ``system_rules``：系統路徑加上（resolve 過的）sys.prefix 與 base_prefix."""
    paths = es.system_exec_paths()
    assert paths[: len(es.SYSTEM_EXEC_PATHS)] == es.SYSTEM_EXEC_PATHS
    assert str(Path(sys.prefix).resolve()) in paths
    assert str(Path(sys.base_prefix).resolve()) in paths


# ── 伺服器：假 stream、假 spawn（§2.2、§2.3）──────────────────────────────────────────

KERNEL = srv.Kernel(
    directory=Path(kspec.__file__).resolve().parent, framing=kfr, spec=kspec, landlock=kll,
    owner_repair=kor,
)
TOKEN = "ab" * 32
PROC_PID = 7777


class FakeSock:
    def __init__(self, pid):
        self.pid = pid


class FakeWriter:
    def __init__(self, peer=0):
        self.data = bytearray()
        self.closed = False
        self.peer = peer

    def write(self, data):
        if self.closed:
            raise ConnectionResetError
        self.data += data

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    def get_extra_info(self, name):
        return FakeSock(self.peer) if name == "socket" else None

    def frames(self):
        out = []
        for frame in kfr.FrameDecoder().feed(bytes(self.data)):
            body = kfr.decode_json(frame) if frame.kind in kfr.JSON_TYPES else frame.payload
            out.append((kfr.TYPE_NAMES[frame.kind], body))
        return out


class FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, chunks=(), code=None, err=()):
        self.pid = PROC_PID
        self.returncode = None
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()  # 只有 stdio 會讀（merged 時 stderr 併進 stdout）
        self._done = asyncio.Event()
        for chunk in chunks:
            self.stdout.feed_data(chunk)
        for chunk in err:
            self.stderr.feed_data(chunk)
        if code is not None:
            self.finish(code)

    def finish(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    async def communicate(self):
        output = await self.stdout.read()
        return output, b""

    def kill(self):
        self.finish(-9)


class Recorder:
    """假 spawn：記下 argv／kwargs、讀出啟動器收到的 spec，照指定寫狀態碼，回 ``FakeProc``。"""

    def __init__(self, proc_factory=lambda: FakeProc(code=0), status=b""):
        self.calls = []
        self.specs = []
        self.proc_factory = proc_factory
        self.status = status
        self.procs = []
        self.server = None
        self.running_at_spawn = None

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.server is not None:
            self.running_at_spawn = dict(self.server._running)
        spec_fd = int(argv[argv.index("--spec-fd") + 1])
        chunks = []
        while chunk := os.read(spec_fd, 65536):
            chunks.append(chunk)
        self.specs.append(json.loads(b"".join(chunks)))
        if self.status:
            os.write(int(argv[argv.index("--status-fd") + 1]), self.status)
        proc = self.proc_factory()
        self.procs.append(proc)
        return proc


def _config(**overrides) -> srv.ServerConfig:
    fields = {
        "ipc": "/srv/ipc", "kernel": str(KERNEL.directory), "state": STATE, "rw": (PD, MCP),
        "ro": (SKILLS,), "require_foreign_peer": False,
    }
    fields.update(overrides)
    return srv.ServerConfig(**fields)


def _server(spawn=None, *, abi=6, net=None, **config):
    killed = []
    swept = []

    class Table(FakeTable):
        def children(self, parent):
            swept.append(parent)
            return []

    hooks = srv.Hooks(
        spawn=spawn or Recorder(), peer_pid=lambda writer: writer.peer,
        kill_group=lambda pid: (killed.append(pid), [p.finish(-9) for p in spawn.procs]),
        build_ruleset=lambda ports: os.dup(0), processes=Table({}),
    )
    server = srv.ExecutorServer(_config(**config), KERNEL, hooks, require_root_interpreter=False)
    server.layout = LAYOUT
    server._token = TOKEN
    server.launcher_prefix = ("/py", "-I", "-S", "/kernel/launcher.py")
    result = kspec.SelfTestResult(ok=True, abi=abi, net=net)
    server._tests = {"data": result, "state": result}
    server.uid = 1001

    async def prepare(plan):
        server.prepared = plan

    server.prepare = prepare
    server.killed, server.swept = killed, swept
    if isinstance(spawn, Recorder):
        spawn.server = server
    return server


def _hello(**overrides):
    document = {"v": 1, "token": TOKEN, "block_ports": sorted(HELLO_PORTS), "mode": "exec"}
    document.update(overrides)
    return kfr.encode_json(kfr.HELLO, document)


def _start_frame(document=None):
    return kfr.encode_json(kfr.START, document or _start())


async def _session(server, *frames, eof=False, peer=0, timeout=5):
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(frame)
    if eof:
        reader.feed_eof()
    writer = FakeWriter(peer)
    await asyncio.wait_for(server.handle(reader, writer), timeout)
    assert writer.closed
    return writer.frames()


def _kinds(frames):
    return [kind for kind, _ in frames]


async def test_peer_is_checked_before_the_connection_limits():
    """同 pid namespace 的 peer 一律當 peer 拒絕，就算上限已滿也不會被算成上限拒絕（先查 peer）。"""
    server = _server(Recorder(), require_foreign_peer=True)
    server._pending = server.max_pending
    assert await _session(server, _hello(), peer=4242) == []
    assert server.refusals == {"peer": 1}
    assert await _session(server, _hello(), peer=0) == []
    assert server.refusals == {"peer": 1, "pending": 1}
    server._pending = 0
    frames = await _session(server, _hello(mode="health"), peer=0)
    assert _kinds(frames) == ["READY"]
    assert await _session(server, _hello(mode="health"), peer=None) == []  # 拿不到 peer 也拒絕
    assert server.refusals["peer"] == 2


async def test_without_the_flag_same_namespace_peers_are_served():
    server = _server(Recorder())
    assert _kinds(await _session(server, _hello(mode="health"), peer=4242)) == ["READY"]


async def test_hello_must_arrive_in_time():
    server = _server(Recorder())
    server.hello_seconds = 0.05
    assert await _session(server) == []
    assert server.refusals == {"hello": 1}


async def test_unauthenticated_connections_are_capped_and_freed():
    server = _server(Recorder())
    readers, tasks = [], []
    for _ in range(server.max_pending):
        reader = asyncio.StreamReader()
        readers.append(reader)
        tasks.append(asyncio.ensure_future(server.handle(reader, FakeWriter())))
    await asyncio.sleep(0.01)
    assert server._pending == server.max_pending
    assert await _session(server, _hello(mode="health")) == []
    assert server.refusals == {"pending": 1}
    readers[0].feed_data(_hello(mode="health"))
    await asyncio.sleep(0.01)
    assert _kinds(await _session(server, _hello(mode="health"))) == ["READY"]
    for reader in readers:
        reader.feed_eof()
    await asyncio.gather(*tasks)
    assert (server._pending, server._open) == (0, 0)


async def test_connection_cap():
    server = _server(Recorder())
    server.max_connections = 0
    assert await _session(server, _hello(mode="health")) == []
    assert server.refusals == {"connections": 1}


@pytest.mark.parametrize(
    ("frames", "code"),
    [
        ([_hello(token="cd" * 32)], "auth"),
        ([_hello(token="x")], "auth"),
        ([_hello(token="")], "auth"),
        ([_hello(token=None)], "auth"),
        ([_hello(v=2)], "version"),
        ([_hello(v="1")], "version"),
        ([_hello(mode="shell")], "protocol"),
        ([_hello(block_ports=sorted(HELLO_PORTS - {8000}))], "protocol"),
        ([_hello(block_ports=[*sorted(HELLO_PORTS), 8000])], "protocol"),
        ([_hello(block_ports=list(range(1, 1026)))], "protocol"),
        ([_hello(block_ports=[*sorted(kspec.DEFAULT_BLOCK_PORTS), "4000"])], "protocol"),
        ([kfr.encode_json(kfr.HELLO, {"v": 1, "token": TOKEN, "mode": "exec"})], "protocol"),
        ([_start_frame()], "protocol"),
        ([b"\x63\x00\x00\x00\x00"], "protocol"),
    ],
)
async def test_hello_is_refused_with_an_error_code(frames, code):
    server = _server(Recorder())
    assert await _session(server, *frames) == [("ERROR", {"code": code})]
    assert server.hooks.spawn.calls == []


async def test_token_is_checked_before_the_version():
    server = _server(Recorder())
    assert await _session(server, _hello(token="cd" * 32, v=2)) == [("ERROR", {"code": "auth"})]


async def test_server_without_a_token_refuses_everyone():
    server = _server(Recorder())
    server._token = None
    assert await _session(server, _hello()) == [("ERROR", {"code": "auth"})]


async def test_ready_reports_what_the_proxy_checks():
    server = _server(Recorder(), abi=6, net=None)
    server._digests = ("k" * 64, "s" * 64)
    server.repairs["repaired"] = 2
    (kind, ready), = await _session(server, _hello(mode="health"), _start_frame())
    assert kind == "READY"
    assert set(ready) == {
        "v", "abi", "enforced", "net", "features", "roots", "state_root", "kernel_digest",
        "server_digest", "boot_id", "block_ports_digest", "uid", "repairs",
    }
    assert ready["v"] == kfr.PROTOCOL_VERSION and ready["abi"] == 6 and ready["enforced"] is True
    assert ready["features"] == es.landlock_features(6, None) and "tcp" in ready["features"]
    assert ready["roots"] == [
        {"path": PD, "access": "rw"}, {"path": MCP, "access": "rw"},
        {"path": SKILLS, "access": "ro"},
    ]
    assert ready["state_root"] == STATE and ready["uid"] == 1001
    assert (ready["kernel_digest"], ready["server_digest"]) == ("k" * 64, "s" * 64)
    assert ready["block_ports_digest"] == kfr.block_ports_digest(HELLO_PORTS)
    assert kfr.BOOT_ID_PATTERN.fullmatch(ready["boot_id"])
    assert ready["repairs"] == {"repaired": 2, "stopped": 0, "throttled": 0}


async def test_ready_features_follow_selftests_and_net():
    server = _server(Recorder(), abi=6, net=kspec.NET_TCP_FASTOPEN)
    ready = server.ready(HELLO_PORTS)
    assert ready["net"] == kspec.NET_TCP_FASTOPEN and "tcp" not in ready["features"]
    server._tests["data"] = kspec.SelfTestResult(ok=False, abi=6, error="check_failed")
    ready = server.ready(HELLO_PORTS)
    assert ready["enforced"] is False and ready["features"] == []


def test_block_ports_digest_is_the_kernel_ssot_and_ignores_order_and_duplicates():
    """SSOT 在 kernel 的 framing：排序、去重、緊湊 JSON、sha256 小寫 hex（proxy 用同一個函式）."""
    digest = kfr.block_ports_digest
    assert digest([8000, 3001, 8000]) == digest([3001, 8000]) == digest(p for p in (3001, 8000))
    assert digest([3001, 8000]) == hashlib.sha256(b"[3001,8000]").hexdigest()
    assert digest([3001]) != digest([3001, 8000])
    assert not hasattr(srv, "block_ports_digest")  # 伺服器不另存一份


def _proxy_expectation(server) -> ec.ExecutorExpectation:
    """proxy 端對這台伺服器的預期（kernel、伺服器程式、封鎖埠 digest、roots）."""
    roots = [ec.ExecutorRoot(path, "rw") for path in server.config.rw]
    roots += [ec.ExecutorRoot(path, "ro") for path in server.config.ro]
    return ec.ExecutorExpectation(
        kernel_digest=kspec.kernel_digest(KERNEL.directory),
        server_digest=srv.executor_code_digest(srv.PROXY_ROOT),
        block_ports_digest=kfr.block_ports_digest(sorted(HELLO_PORTS)),
        roots=frozenset(roots),
    )


@pytest.mark.parametrize("net", [None, kspec.NET_TCP_FASTOPEN, kspec.NET_MPTCP])
@pytest.mark.parametrize("ok", [True, False])
async def test_ready_the_server_sends_passes_the_proxy_contract(net, ok):
    """伺服器實際送出的 READY 要通過 U2d 的 ``parse_ready``／``ready_issue``."""
    server = _server(Recorder(), abi=6, net=net)
    server._tests["state"] = kspec.SelfTestResult(ok=ok, abi=6, net=net)
    kernel_digest = kspec.kernel_digest(KERNEL.directory)
    server._digests = (kernel_digest, srv.executor_code_digest(srv.PROXY_ROOT))
    (kind, ready), = await _session(server, _hello(mode="health"))
    assert kind == "READY"
    parsed = ec.parse_ready(ready)
    assert parsed is not None
    expected = None if ok else ec.EXECUTOR_SELFTEST_FAILED
    assert ec.ready_issue(parsed, _proxy_expectation(server)) == expected
    assert ec.parse_ready({**ready, "extra": 1}) is None  # 反向對照：多一個鍵就是版本不符
    assert ec.parse_ready({**ready, "uid": True}) is None


async def test_proxy_and_server_compute_the_same_block_ports_digest():
    """proxy 用送出的 B（src 的 framing）、伺服器用收到的 B（依路徑載入的 kernel）算，結果相同."""
    sent = [11434, *sorted(kspec.DEFAULT_BLOCK_PORTS, reverse=True), 4000]
    server = _server(Recorder())
    server.framing = srv.load_kernel(KERNEL.directory).framing  # 正式環境的載入方式
    assert server.framing is not ec.framing
    (_kind, ready), = await _session(server, _hello(mode="health", block_ports=sent))
    assert ready["block_ports_digest"] == ec.framing.block_ports_digest(sent)
    assert ready["block_ports_digest"] == ec.framing.block_ports_digest(sorted(sent))


async def test_health_mode_closes_after_ready():
    spawn = Recorder()
    server = _server(spawn)
    assert _kinds(await _session(server, _hello(mode="health"), _start_frame())) == ["READY"]
    assert spawn.calls == []


@pytest.mark.parametrize("name", ["rw_other_owner", "env_exec_id", "spec_ports_missing_hello"])
async def test_rejected_start_never_spawns(name):
    spawn = Recorder()
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(REJECTED[name]))
    assert frames == [("READY", frames[0][1]), ("ERROR", {"code": "rejected"})]
    assert spawn.calls == []


async def test_start_b_must_cover_the_hello_b_even_when_hello_is_bigger():
    spawn = Recorder()
    server = _server(spawn)
    bigger = _hello(block_ports=sorted(HELLO_PORTS | {9999}))
    assert _kinds(await _session(server, bigger, _start_frame())) == ["READY", "ERROR"]
    assert spawn.calls == []


async def test_accepted_start_spawns_the_launcher_with_only_its_own_fds():
    spawn = Recorder(lambda: FakeProc([b"hello\n"], code=0))
    server = _server(spawn, abi=6)
    frames = await _session(server, _hello(), _start_frame(), kfr.encode(kfr.STDIN_EOF))
    assert _kinds(frames) == ["READY", "STARTED", "OUT", "EXIT"]
    assert frames[2][1] == b"hello\n"
    assert frames[3][1] == {"code": 0, "signal": None, "timed_out": False, "launcher_error": None}
    (argv, kwargs), = spawn.calls
    fd_of = {name: argv[argv.index(name) + 1] for name in ("--spec-fd", "--status-fd")}
    net = argv[argv.index("--net-ruleset-fd") + 1]
    assert list(argv[:4]) == list(server.launcher_prefix)
    assert argv[argv.index("--") + 1 :] == tuple(_start()["argv"])
    assert kwargs["pass_fds"] == (int(fd_of["--spec-fd"]), int(fd_of["--status-fd"]), int(net))
    assert kwargs["close_fds"] is True and kwargs["new_process_group"] is True
    assert kwargs["cwd"] == f"{PD}/Alice"
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    env = kwargs["env"]
    exec_id = env.pop(es.EXEC_ID_ENV)
    assert re.fullmatch(r"[0-9a-f]{32}", exec_id) and frames[1][1] == {"exec_id": exec_id}
    assert env == _start()["env"]
    sent = spawn.specs[0]
    assert sent["parent_pid"] == os.getpid()
    assert sent["block_ports"] == _start()["spec"]["block_ports"]


async def test_abi_3_has_no_shared_ruleset_so_only_two_fds_pass():
    spawn = Recorder()
    server = _server(spawn, abi=3)
    await _session(server, _hello(), _start_frame())
    (argv, kwargs), = spawn.calls
    assert "--net-ruleset-fd" not in argv and len(kwargs["pass_fds"]) == 2


async def test_each_exec_gets_a_fresh_exec_id():
    spawn = Recorder()
    server = _server(spawn)
    await _session(server, _hello(), _start_frame())
    await _session(server, _hello(), _start_frame())
    ids = [kwargs["env"][es.EXEC_ID_ENV] for _argv, kwargs in spawn.calls]
    assert len(set(ids)) == 2


async def test_exec_id_is_registered_before_the_spawn_and_released_after():
    spawn = Recorder()
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame())
    exec_id = frames[1][1]["exec_id"]
    assert spawn.running_at_spawn == {exec_id: None}
    assert server._running == {} and server.swept  # 結束時取消登記並掃描孤兒


async def test_launcher_failure_code_comes_back_in_exit():
    spawn = Recorder(lambda: FakeProc(code=kspec.FAILURE_EXIT), status=b"path_open\n")
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame())
    assert frames[-1] == (
        "EXIT", {"code": 125, "signal": None, "timed_out": False, "launcher_error": "path_open"},
    )


async def test_command_written_marker_is_not_a_launcher_error():
    spawn = Recorder(lambda: FakeProc([b"TOUKA_SANDBOX_ERROR:path_open\n"], code=125))
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame())
    assert frames[-1][1]["launcher_error"] is None and frames[-1][1]["code"] == 125


async def test_disconnect_before_exit_is_a_stop():
    """relay 收到 SIGTERM 不送 STOP：連線一斷（EOF）就殺整個行程群組、有界回收，不回 EXIT."""
    spawn = Recorder(lambda: FakeProc([b"partial\n"]))
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(), kfr.encode(kfr.STDIN_EOF), eof=True)
    assert "EXIT" not in _kinds(frames)
    assert server.killed == [PROC_PID]
    assert spawn.procs[0].returncode == -9 and server._running == {}


async def test_stop_kills_the_group_and_still_reports_exit():
    spawn = Recorder(lambda: FakeProc())
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(), kfr.encode(kfr.STOP))
    assert server.killed == [PROC_PID]
    assert frames[-1] == ("EXIT", {"code": None, "signal": 9, "timed_out": False,
                                   "launcher_error": None})


async def test_max_seconds_kills_the_group():
    spawn = Recorder(lambda: FakeProc())
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(_start(max_seconds=1)))
    assert server.killed == [PROC_PID]
    assert frames[-1][1]["timed_out"] is True and frames[-1][1]["signal"] == 9


async def test_unexpected_frame_after_start_is_a_protocol_error_and_kills():
    spawn = Recorder(lambda: FakeProc())
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(), _hello())
    assert server.killed == [PROC_PID]
    assert frames[-1] == ("ERROR", {"code": "protocol"})


async def test_stdin_is_forwarded_until_stdin_eof():
    gate = asyncio.Event()

    def factory():
        proc = FakeProc()

        async def finish_later():
            await gate.wait()
            proc.finish(0)

        asyncio.ensure_future(finish_later())
        return proc

    spawn = Recorder(factory)
    server = _server(spawn)
    reader = asyncio.StreamReader()
    for frame in (_hello(), _start_frame(), kfr.encode(kfr.STDIN, b"abc")):
        reader.feed_data(frame)
    reader.feed_data(kfr.encode(kfr.STDIN_EOF))
    task = asyncio.ensure_future(server.handle(reader, FakeWriter()))
    await asyncio.sleep(0.05)
    proc = spawn.procs[0]
    assert proc.stdin.data == b"abc" and proc.stdin.closed
    gate.set()
    await asyncio.wait_for(task, 5)


async def test_stdio_forwards_stdin_while_running_and_sends_stderr_as_err():
    """stdio：stderr 另開 pipe、以 ERR 送回（不併進 OUT）；STDIN 不必等 STDIN_EOF 就轉送、回應."""
    gate = asyncio.Event()

    def factory():
        proc = FakeProc([b"result\n"], err=[b"log line\n"])

        async def finish_later():
            await gate.wait()
            proc.finish(0)

        asyncio.ensure_future(finish_later())
        return proc

    spawn = Recorder(factory)
    server = _server(spawn)
    reader, writer = asyncio.StreamReader(), FakeWriter()
    reader.feed_data(_hello() + _start_frame(_mcp_start()) + kfr.encode(kfr.STDIN, b"ping\n"))
    task = asyncio.ensure_future(server.handle(reader, writer))

    async def answered():
        while len(writer.frames()) < 4 or not spawn.procs[0].stdin.data:  # 到 OUT 與 ERR
            await asyncio.sleep(0.01)

    await asyncio.wait_for(answered(), 5)
    proc = spawn.procs[0]
    assert proc.stdin.data == b"ping\n" and not proc.stdin.closed  # stdin 還開著就有回應
    gate.set()
    await asyncio.wait_for(task, 5)
    (_argv, kwargs), = spawn.calls
    assert (kwargs["stdout"], kwargs["stderr"]) == (asyncio.subprocess.PIPE,) * 2
    frames = writer.frames()
    sent = {k: b"".join(body for name, body in frames if name == k) for k in ("OUT", "ERR")}
    assert sent == {"OUT": b"result\n", "ERR": b"log line\n"}
    assert frames[-1] == (
        "EXIT", {"code": 0, "signal": None, "timed_out": False, "launcher_error": None}
    )


async def test_merged_never_sends_stderr_separately():
    """merged：stderr 已併進 stdout（STDOUT），伺服器不另外讀、不送 ERR."""
    spawn = Recorder(lambda: FakeProc([b"all\n"], code=0, err=[b"unused\n"]))
    server = _server(spawn)
    frames = await _session(server, _hello(), _start_frame(), kfr.encode(kfr.STDIN_EOF))
    assert _kinds(frames) == ["READY", "STARTED", "OUT", "EXIT"]
    assert spawn.calls[0][1]["stderr"] == asyncio.subprocess.STDOUT


async def test_stdio_without_a_time_limit_is_never_timed_out(monkeypatch):
    """kind mcp＋stdio 的 ``max_seconds`` 0＝沒有時限（常駐的 MCP 伺服器）：上限改成 1 秒也不殺."""
    monkeypatch.setattr(es, "MAX_SECONDS", 1)
    spawn = Recorder(lambda: FakeProc())
    server = _server(spawn)
    reader, writer = asyncio.StreamReader(), FakeWriter()
    reader.feed_data(_hello() + _start_frame(_mcp_start()))
    task = asyncio.ensure_future(server.handle(reader, writer))
    await asyncio.sleep(1.5)
    assert server.killed == [] and not task.done()
    spawn.procs[0].finish(0)
    await asyncio.wait_for(task, 5)
    assert writer.frames()[-1] == (
        "EXIT", {"code": 0, "signal": None, "timed_out": False, "launcher_error": None}
    )


@pytest.mark.parametrize("ending", ["stop", "disconnect", "overflow"])
async def test_blocked_stdin_never_blocks_session_shutdown(ending):
    blocked = asyncio.Event()

    class BlockedStdin(FakeStdin):
        async def drain(self):
            blocked.set()
            await asyncio.Event().wait()

    proc = FakeProc()
    proc.stdin = BlockedStdin()
    spawn = Recorder(lambda: proc)
    server = _server(spawn)
    reader, writer = asyncio.StreamReader(), FakeWriter()
    reader.feed_data(_hello() + _start_frame(_mcp_start()) + kfr.encode(kfr.STDIN, b"request"))
    task = asyncio.create_task(server.handle(reader, writer))
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        if ending == "stop":
            reader.feed_data(kfr.encode(kfr.STOP))
        elif ending == "disconnect":
            reader.feed_eof()
        else:
            reader.feed_data(kfr.encode(kfr.STDIN, b"x" * 65536) * 64)
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "stdin back-pressure blocked control channel shutdown"
        await task
        assert server.killed == [proc.pid]
        assert proc.returncode == -9
        assert server._running == {} and server._direct == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_spawn_failure_is_a_rejection():
    async def broken(*_argv, **_kwargs):
        raise FileNotFoundError("launcher")

    server = _server(Recorder())
    server.hooks.spawn = broken
    frames = await _session(server, _hello(), _start_frame())
    assert frames[-1] == ("ERROR", {"code": "rejected"}) and server._running == {}


def test_exit_document_shapes():
    assert es.exit_document(3, None, timed_out=False, failure_exit=125)["code"] == 3
    assert es.exit_document(-15, None, timed_out=False, failure_exit=125)["signal"] == 15
    assert es.exit_document(None, None, timed_out=True, failure_exit=125)["signal"] == 9
    killed = es.exit_document(125, "net_mptcp", timed_out=False, failure_exit=125)
    assert killed == {
        "code": 125, "signal": None, "timed_out": False, "launcher_error": "net_mptcp"
    }


# ── 共用網路規則集快取（r4 LOW-1、LOW-2）──────────────────────────────────────────────


def _ruleset_files(tmp_path):
    built = []

    def build(ports):
        path = tmp_path / f"rules-{len(built)}"
        path.write_text(",".join(map(str, ports)), encoding="ascii")
        built.append(ports)
        return os.open(path, os.O_RDONLY)

    return build, built


def _content(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 1 << 16).decode("ascii")


async def test_ruleset_cache_lends_dups_and_keeps_at_most_two(tmp_path):
    build, built = _ruleset_files(tmp_path)
    cache = srv.RulesetCache(build)
    lent_a = await cache.lend([8000, 3001])
    assert _content(lent_a) == "3001,8000"
    lent_b = await cache.lend([8000, 3001, 4000])
    again = await cache.lend([3001, 8000])
    assert len(built) == 2  # 同一組鍵不重建
    await cache.lend([1, 2])  # 第三組：淘汰最舊的（4000 那組）
    assert len(cache._fds) == srv.RULESET_CACHE_SIZE
    await cache.lend([5, 6])  # 第四組：淘汰 3001,8000
    # 淘汰之後再開新檔，fd 號碼可能被重用；借出去的複本仍然指向自己的那一份
    reused = [os.open(tmp_path / "rules-0", os.O_RDONLY) for _ in range(4)]
    assert _content(lent_a) == "3001,8000" and _content(again) == "3001,8000"
    assert _content(lent_b) == "3001,4000,8000"
    for fd in (lent_a, lent_b, again, *reused, *cache._fds.values()):
        os.close(fd)


async def test_ruleset_key_is_the_specs_ports_not_hellos():
    built = []
    spawn = Recorder()
    server = _server(spawn)
    server.hooks.build_ruleset = None
    server._rulesets = srv.RulesetCache(lambda ports: (built.append(ports), os.dup(0))[1])
    await _session(server, _hello(), _start_frame())
    assert built == [tuple(sorted(_start()["spec"]["block_ports"]))]
    assert built[0] != tuple(sorted(HELLO_PORTS))


# ── subreaper（§2.4）─────────────────────────────────────────────────────────────


class FakeTable:
    """假的 ``ProcessTable``：pid → (exec id, sid, 殭屍)；記下殺掉與回收的."""

    def __init__(self, processes):
        self.processes = dict(processes)  # pid → (exec_id, sid, zombie)
        self.killed, self.reaped = [], []

    def children(self, parent):
        assert parent == SERVER_PID
        return [(pid, zombie) for pid, (_e, _s, zombie) in self.processes.items()]

    def reap(self, pid):
        self.reaped.append(pid)
        self.processes.pop(pid)

    def kill_unless(self, pid, parent, keep):
        exec_id, sid, _zombie = self.processes[pid]
        if keep(exec_id, sid):
            return False
        self.killed.append(pid)
        self.processes[pid] = (exec_id, sid, True)
        return True


@pytest.mark.parametrize("table", [srv.LinuxProcesses(), FakeTable({})], ids=["linux", "fake"])
def test_process_tables_have_every_process_table_member(table):
    members = _protocol_members(srv.ProcessTable)
    assert set(members) == {"children", "reap", "kill_unless"}
    for member in members:
        assert callable(getattr(table, member, None)), member


def test_orphan_sweep_keeps_only_running_execs():
    table = FakeTable(
        {
            10: ("a" * 32, 99, False),  # 執行中的 exec id
            11: ("", 500, False),  # 讀得到、沒有 exec id，sid 屬於執行中的指令（setsid 前的子行程）
            12: ("b" * 32, 501, False),  # 已結束的指令
            13: ("", 777, False),  # 不明
            14: (None, None, False),  # 兩種都讀不到
            15: (None, 500, False),  # environ 讀不到、sid 執行中
            16: ("", 600, False),  # 直接子行程（asyncio 回收）
            17: ("", 1, True),  # 殭屍
        }
    )
    killed = srv.sweep_orphans(
        table, SERVER_PID, direct=frozenset({16}), running_ids=frozenset({"a" * 32}),
        running_sids=frozenset({500}),
    )
    assert sorted(table.killed) == [12, 13, 14] and killed == 3
    assert sorted(table.reaped) == [12, 13, 14, 17]  # 殺掉的在下一輪回收
    assert 16 in table.processes


async def test_sweep_protects_running_execs_sid():
    table = FakeTable({20: ("", PROC_PID, False), 21: ("", 4, False)})
    server = _server(Recorder())
    server.server_pid = SERVER_PID
    server.hooks.processes = table
    server._running = {"e" * 32: PROC_PID}
    assert await server.sweep() == 1
    assert table.killed == [21]


# ── 其他（頻率、命令列、kernel、啟動失敗時保持存活）──────────────────────────────────


def test_repair_budget_per_target_and_window():
    budget = srv.RepairBudget()
    assert budget.allow("a", 0.0) and not budget.allow("a", 5.0) and budget.allow("a", 10.0)
    budget = srv.RepairBudget()
    assert all(budget.allow(f"t{i}", 1.0) for i in range(srv.REPAIR_PER_WINDOW))
    assert not budget.allow("late", 2.0)
    assert budget.allow("late", 1.0 + srv.REPAIR_WINDOW_SECONDS)  # 下一個時間窗自動恢復


def test_parse_cli():
    config = srv.parse_cli(
        ["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s", "--root", "rw:/a",
         "--root", "ro:/b", "--root", "rw:/c", "--require-foreign-peer"]
    )
    assert (config.ipc, config.kernel, config.state) == ("/i", "/k", "/s")
    assert (config.rw, config.ro, config.require_foreign_peer) == (("/a", "/c"), ("/b",), True)
    assert srv.parse_cli(["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s"]).rw == ()


def test_parse_cli_returns_a_frozen_server_config():
    config = srv.parse_cli(
        ["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s", "--root", "rw:/a"]
    )
    assert config == srv.ServerConfig(
        ipc="/i", kernel="/k", state="/s", rw=("/a",), ro=(), require_foreign_peer=False
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.rw = ("/elsewhere",)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--ipc-dir", "/i", "--kernel-dir", "/k"],
        ["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s", "--root", "rx:/a"],
        ["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s", "--root", "rw:"],
        ["--ipc-dir", "/i", "--kernel-dir", "/k", "--state-dir", "/s", "--token", "x"],
    ],
)
def test_parse_cli_rejects_bad_usage(argv):
    with pytest.raises(srv.ConfigError):
        srv.parse_cli(argv)


def test_load_kernel_by_path_matches_the_launchers_module_names(tmp_path):
    kernel = srv.load_kernel(KERNEL.directory)
    assert kernel.spec.SPEC_VERSION == kspec.SPEC_VERSION
    assert kernel.framing.PROTOCOL_VERSION == kfr.PROTOCOL_VERSION
    assert kernel.spec is not kspec  # 依路徑載入，不是 src 的那份
    with pytest.raises(srv.ConfigError):
        srv.load_kernel(tmp_path)


class FakeView:
    """假的檔案系統：``dirs`` 是資料夾 → (權限位元, 擁有者, 內容名稱)，``links`` 是連結 → 指向."""

    def __init__(self, uid=1001):
        self.uid = uid
        self.dirs = {p: (0o755, 0, ["notes.md"]) for p in (PD, MCP, SKILLS)}
        self.dirs |= {STATE: (0o700, uid, []), "/srv/ipc": (0o700, uid, [])}
        self.links = {}
        self.resolved = {}  # 本身是資料夾、上層有連結：實際路徑不同
        self.unreadable = set()

    def lstat(self, path):
        if path in self.links:
            return os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        if path not in self.dirs:
            raise FileNotFoundError(path)
        mode, owner, _names = self.dirs[path]
        kind = 0 if stat.S_IFMT(mode) else stat.S_IFDIR  # 權限位元沒帶類型就是資料夾
        return os.stat_result((kind | mode, 0, 0, 1, owner, 0, 0, 0, 0, 0))

    def resolve(self, path):
        if path in self.links:
            return self.links[path]
        self.lstat(path)
        return self.resolved.get(path, path)

    def names(self, path):
        if path in self.unreadable:
            raise PermissionError(path)
        return list(self.dirs[path][2])


def _unreadable(view):
    view.unreadable.add(PD)


def _nested_state(view):
    view.state = f"{PD}/state"
    view.dirs[view.state] = (0o700, view.uid, [])  # 本身是私有資料夾，只剩巢狀這個問題


def _long_ipc(view):
    view.ipc = "/srv/" + "i" * srv.MAX_SOCKET_PATH_BYTES
    view.dirs[view.ipc] = (0o700, view.uid, [])


CONFIG_PROBLEMS = {
    "ok": (lambda view: None, None),
    "root_missing": (lambda view: view.dirs.pop(SKILLS), "root_path"),
    "root_is_a_link": (
        lambda view: view.links.update({MCP: "/elsewhere/mcp_servers"}), "root_path"
    ),
    "root_under_a_linked_parent": (
        lambda view: view.resolved.update({PD: "/mnt/real/personal_drive"}), "root_path"
    ),
    "root_is_a_file": (
        lambda view: view.dirs.update({SKILLS: (stat.S_IFREG | 0o644, 0, [])}), "root_path"
    ),
    "root_is_a_data_root": (
        lambda view: view.dirs.update({PD: (0o755, 0, ["Soul"])}), "touka_root"
    ),
    "root_has_a_marker": (
        lambda view: view.dirs.update({MCP: (0o755, 0, sorted(kspec.TOUKA_MARKERS)[:1])}),
        "touka_root",
    ),
    "root_unreadable": (_unreadable, "touka_root"),
    "state_inside_a_root": (_nested_state, "nested"),
    "socket_path_too_long": (_long_ipc, "socket_path"),
    "state_not_private": (
        lambda view: view.dirs.update({STATE: (0o750, view.uid, [])}), "private_dir"
    ),
    "ipc_owned_by_someone_else": (
        lambda view: view.dirs.update({"/srv/ipc": (0o700, 0, [])}), "private_dir"
    ),
    "state_missing": (lambda view: view.dirs.pop(STATE), "private_dir"),
}


@pytest.mark.parametrize("case", sorted(CONFIG_PROBLEMS))
def test_config_problems_are_found_through_the_fs_view(case):
    """每個檢查各一案，只看 ``FsView``（任何平台都能跑；真核心的在 ``test_isolation_linux.py``）."""
    change, expected = CONFIG_PROBLEMS[case]
    view = FakeView()
    view.state, view.ipc = STATE, "/srv/ipc"
    change(view)
    config = _config(state=view.state, ipc=view.ipc)
    assert srv.config_problem(config, KERNEL, view) == expected


def test_posix_view_has_every_fs_view_member():
    view = srv.PosixView()
    for member in _protocol_members(srv.FsView):
        assert hasattr(view, member), member
    assert view.uid == (os.geteuid() if hasattr(os, "geteuid") else 0)


def test_duplicate_rw_root_names_are_a_config_problem():
    config = _config(rw=(PD, "/other/personal_drive"))
    assert srv.config_problem(config, KERNEL, FakeView()) == "roots"
    assert srv.config_problem(_config(rw=()), KERNEL, FakeView()) == "roots"


async def test_start_with_a_bad_ipc_folder_stays_alive_without_serving(tmp_path):
    """設定有錯：start() 回 False、記下原因、不建 socket、不起背景工作（程序不退出）。"""
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    ipc.chmod(0o755)
    server = srv.ExecutorServer(_config(ipc=str(ipc)), KERNEL, srv.Hooks())
    assert await server.start() is False
    assert server.problem in {"private_dir", "platform"}
    assert not (ipc / kfr.SOCKET_NAME).exists() and server._tasks == []
    assert not server.token_ok(TOKEN)


def test_maintenance_repairs_ipc_and_state_and_roots_every_twelfth_round():
    """每 5 秒：ipc 的資料夾、socket、權杖、alive 與 state_root；約每 60 秒加上掛載根與 managed."""
    chains = []
    server = _server(Recorder())
    server.hooks.repair = lambda root, target: (
        chains.append((root, target)), SimpleNamespace(changed=0, stopped=None)
    )[1]
    server._maintain(roots=False, alive=False)
    ipc = "/srv/ipc"
    expected = [(ipc, ipc)] + [(ipc, f"{ipc}/{n}") for n in ("ctl.sock", "token", "alive")]
    assert chains == [*expected, (STATE, STATE)]
    chains.clear()
    server._maintain(roots=True, alive=False)
    assert chains[5:] == [(PD, PD), (MCP, MCP), (MCP, f"{MCP}/managed")]


def test_repair_outcomes_are_counted():
    server = _server(Recorder())
    outcomes = iter(
        [SimpleNamespace(changed=2, stopped=None), SimpleNamespace(changed=0, stopped="symlink")]
    )
    server.hooks.repair = lambda _root, _target: next(outcomes)
    server._repair("/a", "/a/b")
    server._repair("/a", "/a/c")
    assert server.ready(HELLO_PORTS)["repairs"] == {"repaired": 1, "stopped": 1, "throttled": 0}


# ── 第 3 輪 F1：自己的私有資料夾被改成群組或其他人可存取時收回（啟動檢查要 0700）──────────


def _recording(calls: list, changed: int = 0):
    return lambda root, target: (calls.append((root, target)), kor.RepairOutcome(changed))[1]


def test_maintenance_also_closes_its_private_inodes_to_group_and_other():
    """W6（ipc、socket、權杖、alive 與 state_root）另外拿掉群組與其他人的位元；掛載根不會."""
    owner, private = [], []
    server = _server(Recorder())
    server.hooks.repair, server.hooks.repair_private = _recording(owner), _recording(private, 1)
    server._maintain(roots=True, alive=False)
    ipc = "/srv/ipc"
    files = [(ipc, f"{ipc}/{name}") for name in ("ctl.sock", "token", "alive")]
    w6 = [(ipc, ipc), *files, (STATE, STATE)]
    assert private == w6 and owner[:5] == w6 and len(owner) == 8
    assert server.repairs["repaired"] == 5


def _opened_view() -> FakeView:
    view = FakeView()
    view.dirs |= {STATE: (0o777, view.uid, []), "/srv/ipc": (0o777, view.uid, [])}
    return view


async def test_start_closes_its_private_folders_before_checking_them():
    """重啟時 state 與 ipc 是 777：設定其他項都對，就先收回成 0700 再檢查，不判 ``private_dir``."""
    server, view, closed = _server(Recorder()), _opened_view(), []
    server.view = view

    def close(root, target):
        closed.append(target)
        if target in view.dirs:
            mode, owner, names = view.dirs[target]
            view.dirs[target] = (mode & ~0o077, owner, names)
        return kor.RepairOutcome(changed=1)

    server.hooks.repair, server.hooks.repair_private = _recording([]), close
    assert await server.start() is False  # 假的 ipc 拿不到 flock（Windows 沒有 fcntl）
    assert server.problem in {"platform", "ipc_lock"}
    assert {"/srv/ipc", STATE} <= set(closed)


async def test_start_leaves_its_private_folders_alone_when_the_rest_of_the_config_is_wrong():
    """設定有別的錯（這裡是唯讀 root 不見了）：什麼都不修，照現在判 ``private_dir``."""
    server, view, calls = _server(Recorder()), _opened_view(), []
    view.dirs.pop(SKILLS)
    server.view = view
    server.hooks.repair = server.hooks.repair_private = _recording(calls)
    assert await server.start() is False
    assert (server.problem, calls) == ("private_dir", [])


async def test_failed_data_selftest_keeps_the_executor_unenforced_until_it_passes():
    """9p（data）自測失敗時，state 自測通過也不算；之後只重做失敗的那次，通過才恢復."""
    results = {"data": [False, True], "state": [True]}
    seen = []

    async def selftest(directory):
        name = "data" if directory.startswith(MCP) else "state"
        seen.append(name)
        assert PurePosixPath(directory).name.startswith(srv.SELFTEST_PREFIX)
        return kspec.SelfTestResult(ok=results[name].pop(0), abi=6)

    server = _server(Recorder())
    server.hooks.selftest = selftest
    server._tests = {}
    await server.run_selftests(("data", "state"))
    assert server.enforced is False and server.ready(HELLO_PORTS)["features"] == []
    await server._retry_selftests()
    assert seen == ["data", "state", "data"] and server.enforced is True


# ── 型別：kernel 模組照 Protocol 標註（CTO 10/16：恢復具名型別）──────────────────────


def _kernel_protocols() -> dict:
    return {
        "framing": es.FramingModule, "spec": es.SpecModule,
        "landlock": srv.LandlockModule, "owner_repair": srv.OwnerRepairModule,
    }


def _protocol_members(protocol: type) -> dict[str, str]:
    """Protocol 宣告的成員：資料成員對應標註字串，唯讀屬性標成 ``property``，方法標成 ``method``."""
    members = {name: str(note) for name, note in vars(protocol).get("__annotations__", {}).items()}
    for name, value in vars(protocol).items():
        if name.startswith("_"):
            continue
        if isinstance(value, property):
            members[name] = "property"
        elif callable(value):
            members[name] = "method"
    return members


@pytest.mark.parametrize("loaded", ["src", "by_path"])
@pytest.mark.parametrize("name", ["framing", "spec", "landlock", "owner_repair"])
def test_kernel_modules_have_every_protocol_member(name, loaded):
    """kernel 改名或拿掉伺服器用到的成員時這裡會紅（伺服器依路徑載入，靜態型別抓不到）."""
    source = srv.load_kernel(KERNEL.directory) if loaded == "by_path" else KERNEL
    module = getattr(source, name)
    members = _protocol_members(_kernel_protocols()[name])
    assert members, name
    for member, note in members.items():
        assert hasattr(module, member), f"{name}.{member}"
        value = getattr(module, member)
        if note == "type[CodedError]":  # 伺服器 except 之後讀 ``exc.code``
            assert issubclass(value, Exception), f"{name}.{member}"
            assert isinstance(getattr(value("x"), "code", None), str), f"{name}.{member}"
        elif note == "method" or note.startswith(("Callable", "type[")):
            assert callable(value), f"{name}.{member}"
        elif note in {"int", "str"}:
            assert type(value).__name__ == note, f"{name}.{member}"


def _callable_shape(note: str) -> tuple[int, str] | None:
    """``Callable[[A, B], R]`` → (參數個數, R)；``Callable[..., R]`` 與不是 Callable 的回 None."""
    node = ast.parse(note, mode="eval").body
    if not (isinstance(node, ast.Subscript) and getattr(node.value, "id", "") == "Callable"):
        return None
    params, returned = node.slice.elts
    return (len(params.elts), ast.unparse(returned)) if isinstance(params, ast.List) else None


def _parameters(function) -> list[tuple[str, inspect._ParameterKind]]:
    return [(p.name, p.kind) for p in inspect.signature(function).parameters.values()
            if p.name != "self"]


def _methods(protocol: type) -> list[str]:
    return [name for name, note in _protocol_members(protocol).items() if note == "method"]


@pytest.mark.parametrize("loaded", ["src", "by_path"])
@pytest.mark.parametrize("name", ["framing", "spec", "landlock", "owner_repair"])
def test_kernel_signatures_match_the_protocols(name, loaded):
    """簽章也要對（第 3 輪 F8）：Protocol 的方法比對參數名稱與種類.

    ``Callable[[A, B], R]`` 要剛好能用這幾個位置參數呼叫、沒有別的必填；回傳物件有 Protocol 的
    （``FrameDecoder``、``LinuxLandlock``）再比對它們的方法。
    """
    source = srv.load_kernel(KERNEL.directory) if loaded == "by_path" else KERNEL
    module, protocol = getattr(source, name), _kernel_protocols()[name]
    nested = {"FrameDecoderLike": es.FrameDecoderLike, "LandlockApi": srv.LandlockApi}
    for member, note in _protocol_members(protocol).items():
        value, label = getattr(module, member), f"{name}.{member}"
        if note == "method":
            assert _parameters(value) == _parameters(getattr(protocol, member)), label
        elif (shape := _callable_shape(note)) is not None:
            inspect.signature(value).bind(*range(shape[0]))  # 個數不對或有別的必填：TypeError
            returns = nested.get(shape[1]) if inspect.isclass(value) else None
            for method in _methods(returns) if returns else []:
                expected = _parameters(getattr(returns, method))
                assert _parameters(getattr(value, method)) == expected, f"{label}.{method}"


def test_executor_server_has_every_session_host_member():
    """``executor_session`` 只經 ``SessionHost`` 用伺服器；同步／非同步也要一致."""
    server = _server(Recorder())
    members = _protocol_members(es.SessionHost)
    assert {"framing", "spec", "layout", "server_pid", "launch", "finished"} <= set(members)
    for member, note in members.items():
        assert hasattr(server, member), member
        if note == "method":
            expected = inspect.iscoroutinefunction(getattr(es.SessionHost, member))
            actual = inspect.iscoroutinefunction(getattr(srv.ExecutorServer, member))
            assert actual == expected, member


async def test_spawned_processes_have_every_child_process_member():
    """``Hooks.spawn`` 預設（broker）回傳的行程與單元測試的 ``FakeProc`` 都照 ``ChildProcess``."""
    members = _protocol_members(es.ChildProcess)
    expected = {"pid", "returncode", "stdin", "stdout", "wait", "communicate", "kill"}
    assert set(members) == expected | {"stderr"}  # C2-3：stdio 的 stderr 另外讀
    proc = await srv.Hooks().spawn(
        sys.executable, "-I", "-S", "-c", "", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    for candidate in (proc, FakeProc(code=0)):
        for member, note in members.items():
            assert hasattr(candidate, member), (type(candidate).__name__, member)
            if note == "method":
                assert callable(getattr(candidate, member)), (type(candidate).__name__, member)


def test_load_kernel_returns_a_frozen_typed_kernel():
    kernel = srv.load_kernel(KERNEL.directory)
    assert isinstance(kernel, srv.Kernel) and dataclasses.is_dataclass(kernel)
    assert kernel.directory == KERNEL.directory.resolve()
    with pytest.raises(dataclasses.FrozenInstanceError):
        kernel.spec = kspec


# ── 8474b22f 的補測（CTO 10/16：每個行為拿掉程式碼都要有測試變紅）────────────────────


async def test_group_is_killed_only_while_the_leader_is_unreaped_and_then_reaped():
    """群組長回收後 pgid 可能被重用，所以只在還沒回收時 killpg；殺了之後有界等待回收."""
    server = _server(Recorder())
    await es._kill(server, FakeProc(code=0))
    assert server.killed == []
    running = FakeProc()
    loop = asyncio.get_running_loop()
    server.hooks.kill_group = lambda pid: (
        server.killed.append(pid), loop.call_later(0.01, running.finish, -9)
    )
    await asyncio.wait_for(es._kill(server, running), 2)
    assert server.killed == [PROC_PID] and running.returncode == -9


async def test_cancelled_session_kills_the_running_group():
    """伺服器停止（連線工作被取消）時，還在跑的指令整個群組要被殺."""
    server = _server(Recorder(lambda: FakeProc()))
    task = asyncio.ensure_future(_session(server, _hello(), _start_frame()))
    for _ in range(200):
        if server._running:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # 進到轉送輸出與聽 relay 的階段
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert server.killed == [PROC_PID]


async def test_direct_child_is_skipped_by_the_sweep_until_it_is_reaped():
    proc = FakeProc()

    async def spawn(*argv, **kwargs):
        return proc

    server = _server(spawn)
    assert await server.launch("e" * 32, ["/launcher"]) is proc
    assert server._running == {"e" * 32: PROC_PID} and server._direct == {PROC_PID}
    proc.finish(0)
    for _ in range(3):
        await asyncio.sleep(0)
    assert server._direct == set() and not server._waiters


def _selftest_server(proc):
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return proc

    return _server(spawn), calls


async def test_launcher_selftest_is_pinned_without_env_and_parses_the_report():
    report = {"ok": True, "abi": 6, "error": None, "pin": True, "failed": [], "net": None}
    proc = FakeProc([json.dumps(report).encode("ascii") + b"\n"], code=0)
    server, calls = _selftest_server(proc)
    target = f"{STATE}/{srv.SELFTEST_PREFIX}x"
    result = await server._launcher_selftest(target)
    assert (result.ok, result.abi) == (True, 6)
    [(argv, kwargs)] = calls
    assert argv == (*server.launcher_prefix, "--self-test", "--pin", "--dir", target)
    assert kwargs["env"] == {} and kwargs["close_fds"] is True


async def test_a_hanging_launcher_selftest_is_killed_and_fails(monkeypatch):
    monkeypatch.setattr(srv, "SELFTEST_SECONDS", 0.05)
    proc = FakeProc()
    server, _calls = _selftest_server(proc)
    result = await asyncio.wait_for(server._launcher_selftest(f"{STATE}/x"), 2)
    assert (result.ok, result.error, proc.returncode) == (False, "timeout", -9)


async def test_a_selftest_that_cannot_start_is_a_failure_not_a_crash():
    server = _server(Recorder())

    async def broken(directory):
        raise OSError

    server.hooks.selftest = broken
    server._tests = {}
    await server.run_selftests(("data",))
    assert (server._tests["data"].ok, server._tests["data"].error) == (False, "selftest_error")


async def test_background_rounds_continue_after_an_os_error():
    calls = []

    async def action():
        calls.append(len(calls))
        if len(calls) == 1:
            raise OSError

    task = asyncio.ensure_future(srv.ExecutorServer._every(0, action))
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2


async def test_background_rounds_continue_after_any_other_error_but_stop_on_cancel():
    """pids 吃滿時 ``to_thread`` 開不了執行緒會丟 RuntimeError：記 log、下一輪照常；取消照樣停."""
    calls = []

    async def action():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError

    task = asyncio.ensure_future(srv.ExecutorServer._every(0, action))
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    [result] = await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2 and isinstance(result, asyncio.CancelledError)


def _disk_server(tmp_path):
    """真的資料夾（tmp_path）上的伺服器：清自測、清 tmp、寫 ipc 檔案的案例用."""
    dirs = {n: tmp_path / n for n in ("personal_drive", "mcp_servers", "skills", "state", "ipc")}
    for path in dirs.values():
        path.mkdir()
    rw = (dirs["personal_drive"].as_posix(), dirs["mcp_servers"].as_posix())
    server = _server(
        Recorder(), rw=rw, ro=(dirs["skills"].as_posix(),), state=dirs["state"].as_posix(),
        ipc=dirs["ipc"].as_posix(),
    )
    return server, dirs


def test_startup_removes_only_its_own_selftest_folders(tmp_path):
    server, dirs = _disk_server(tmp_path)
    for base in (dirs["mcp_servers"], dirs["state"]):
        (base / f"{srv.SELFTEST_PREFIX}old" / "x").mkdir(parents=True)
        (base / ".touka-selftest-proxy").mkdir()  # proxy 自己的，不碰
    (dirs["personal_drive"] / f"{srv.SELFTEST_PREFIX}old").mkdir()  # 不是資料所在的 root
    server._clean_stale_selftests()
    for base in (dirs["mcp_servers"], dirs["state"]):
        assert [p.name for p in base.iterdir()] == [".touka-selftest-proxy"]
    assert (dirs["personal_drive"] / f"{srv.SELFTEST_PREFIX}old").is_dir()


OWNERS = (("agents", "Alice"), ("evolution", "tool"))


def _owners_tmp(state):
    for kind, owner in OWNERS:
        (state / kind / owner / "tmp" / "sub").mkdir(parents=True)
        (state / kind / owner / "tmp" / "sub" / "f").write_text("x")
        (state / kind / owner / "home").mkdir()
        (state / kind / owner / "home" / "keep").write_text("x")


def test_startup_empties_every_owners_tmp_but_not_home(tmp_path):
    server, dirs = _disk_server(tmp_path)
    _owners_tmp(dirs["state"])
    server._clean_state_tmp()
    for kind, owner in OWNERS:
        base = dirs["state"] / kind / owner
        assert list((base / "tmp").iterdir()) == []  # 清空後重建
        assert (base / "home" / "keep").exists()


def test_tmp_cleanup_stops_at_the_owner_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "MAX_TMP_OWNERS", 1)
    server, dirs = _disk_server(tmp_path)
    _owners_tmp(dirs["state"])
    server._clean_state_tmp()
    left = [o for kind, o in OWNERS if (dirs["state"] / kind / o / "tmp" / "sub").exists()]
    assert len(left) == 1


def test_ipc_files_are_replaced_whole_and_alive_is_kept_fresh(tmp_path):
    server, dirs = _disk_server(tmp_path)
    server.hooks.repair = lambda root, target: kor.RepairOutcome()
    server._replace_file("token", b"old")
    server._replace_file("token", b"new")
    assert (dirs["ipc"] / "token").read_bytes() == b"new"
    assert [p.name for p in dirs["ipc"].iterdir()] == ["token"]  # 沒有留下暫存檔
    alive = dirs["ipc"] / srv.ALIVE_NAME
    server._maintain(roots=False)  # alive 不在：建一個
    assert alive.read_bytes() == b""
    os.utime(alive, (0, 0))
    server._maintain(roots=False)  # 已經在：更新時間
    assert alive.stat().st_mtime > 0


class CredSock:
    def getsockopt(self, level, option, size):
        return struct.pack("3i", 4242, 1000, 1000)[:size]


def test_peer_pid_reads_so_peercred_and_is_none_without_it():
    writer = FakeWriter()
    writer.get_extra_info = lambda name: CredSock()
    assert srv.peer_pid(writer) == 4242
    assert srv.peer_pid(FakeWriter()) is None  # FakeSock 沒有 getsockopt


def test_a_kernel_that_fails_to_load_leaves_no_module_behind(tmp_path):
    tag = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:12]
    with pytest.raises(srv.ConfigError):
        srv.load_kernel(tmp_path)
    assert not [name for name in sys.modules if name.startswith(f"touka_sandbox_kernel_{tag}_")]


async def test_prepare_counts_throttled_repairs_and_rejects_problems():
    server = _server(Recorder())
    plan = _check(_start())
    seen = []
    server._prepare_dirs = lambda plan, allowed: seen.append(allowed)
    await srv.ExecutorServer.prepare(server, plan)
    await srv.ExecutorServer.prepare(server, plan)  # 10 秒內同一目標：不修
    assert seen == [{plan.home, plan.tmp, plan.cwd}, set()]
    assert server.repairs["throttled"] == 3
    server._prepare_dirs = lambda plan, allowed: "cwd"
    with pytest.raises(es.Rejected):
        await srv.ExecutorServer.prepare(server, plan)


async def test_ruleset_build_failure_lets_the_launcher_build_its_own():
    server = _server(Recorder())

    def broken(ports):
        raise OSError

    server._rulesets = srv.RulesetCache(broken)
    assert await server.lend_ruleset((22,)) is None


def test_token_check_rejects_non_ascii_tokens_without_raising():
    server = _server(Recorder())
    assert server.token_ok("é" * 64) is False  # compare_digest 遇到非 ASCII 字串會拋 TypeError
    assert server.token_ok(TOKEN.upper()) is False and server.token_ok(TOKEN) is True


async def test_spawn_failure_releases_the_exec_id():
    async def spawn(*argv, **kwargs):
        raise OSError

    server = _server(spawn)
    with pytest.raises(OSError):
        await server.launch("e" * 32, ["/launcher"])
    assert server._running == {}


def test_an_argparse_that_exits_is_still_a_usage_error(monkeypatch):
    """有些 Python 版本的 argparse 在 ``exit_on_error=False`` 時仍會 SystemExit."""

    def exits(self, args=None, namespace=None):
        raise SystemExit(2)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", exits)
    with pytest.raises(srv.ConfigError):
        srv.parse_cli(["--ipc-dir", "/i"])


async def test_a_frame_other_than_start_after_ready_is_a_protocol_error():
    spawn = Recorder()
    server = _server(spawn)
    frames = await _session(server, _hello(), kfr.encode(kfr.STOP))
    assert frames[1:] == [("ERROR", {"code": "protocol"})] and spawn.calls == []


async def test_the_launchers_fds_are_closed_in_the_server_after_the_spawn():
    spawn = Recorder()
    server = _server(spawn)
    await _session(server, _hello(), _start_frame())
    [(_argv, kwargs)] = spawn.calls
    for fd in kwargs["pass_fds"]:
        with pytest.raises(OSError):
            os.fstat(fd)


async def test_exit_waits_for_the_command_even_after_its_output_closes():
    """指令可以先關 stdout 再結束；EXIT 要等回收，帶真的 exit code."""

    def late_exit():
        proc = FakeProc()
        proc.stdout.feed_eof()
        asyncio.get_running_loop().call_later(0.05, proc.finish, 3)
        return proc

    server = _server(Recorder(late_exit))
    frames = await _session(server, _hello(), _start_frame())
    assert frames[-1] == (
        "EXIT", {"code": 3, "signal": None, "timed_out": False, "launcher_error": None}
    )


async def test_a_malformed_frame_after_start_is_a_protocol_error_and_kills():
    """START 之後才到的壞 frame（同一批到的話連 HELLO 都不算）."""
    spawn = Recorder(lambda: FakeProc())
    server = _server(spawn)
    reader, writer = asyncio.StreamReader(), FakeWriter()
    reader.feed_data(_hello() + _start_frame())
    task = asyncio.ensure_future(server.handle(reader, writer))
    while not spawn.procs:
        await asyncio.sleep(0.01)
    reader.feed_data(b"\x63\x00\x00\x00\x00")
    await asyncio.wait_for(task, 5)
    assert server.killed == [PROC_PID]
    assert writer.frames()[-1] == ("ERROR", {"code": "protocol"})


def test_status_is_read_without_waiting_for_leftover_writers():
    """啟動器留下的子行程可能還開著狀態 pipe 的寫端；讀狀態不能卡住（伺服器在 event loop 裡讀）."""
    read_fd, write_fd = os.pipe()
    result = []
    reader = threading.Thread(target=lambda: result.append(es.read_status(read_fd, kspec)))
    reader.start()
    reader.join(2)
    finished = not reader.is_alive()
    os.close(write_fd)  # 讓卡住的讀取（突變時）結束
    reader.join(5)
    os.close(read_fd)
    assert finished and result == [None]


async def test_a_truncated_hello_is_a_protocol_error():
    server = _server(Recorder())
    assert await _session(server, _hello()[:3], eof=True) == [("ERROR", {"code": "protocol"})]


async def test_sending_to_a_closed_connection_reports_false():
    writer = FakeWriter()
    writer.closed = True
    io = es.FrameIO(kfr, asyncio.StreamReader(), writer)
    assert await io.send(b"x") is False
