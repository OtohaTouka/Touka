"""執行器 kind ``mcp`` 與 ``io: "stdio"``（C2-3 設計 §2.3；第 2 版差異 §2.2、§2.3）.

- START：kind ``mcp`` 讀寫只有自己的 ``<state>/mcp/<owner>/{home,tmp}``，cwd 必須是 home；程式資料夾
  最多一條 ``rox``，路徑正好是 ``<mcp_servers>/managed/<skill>``（部署後唯讀：``rwx`` 一律拒絕）；
- ``io`` 收 ``merged``、``stdio``；``max_seconds`` 1～900，0（沒有時限）只准 kind ``mcp``
  加 ``stdio``；
- ``prepare`` 只建、修自己的 ``{home,tmp}``；程式資料夾只修擁有者位元，不存在也不建；
- ``ExecutorRunner.plan`` 收 ``stdio``；kind ``mcp`` 沒給 ``max_seconds`` 就是 0；
- ``profiles``：``mcp_self`` 的執行器 kind 是 ``mcp``，執行器位置的 state 在 ``mcp/<seg>``；
  ``SANDBOX_ENV_UNSET`` 只有 kernel 那一份。
任何平台都能跑；真核心的案例在 ``test_isolation_linux.py``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import get_args

import pytest
from src.sandbox_kernel import framing as kfr
from src.sandbox_kernel import landlock as kll
from src.sandbox_kernel import owner_repair as kor
from src.sandbox_kernel import spec as kspec
from src.services.isolation import executor_server as srv
from src.services.isolation import executor_session as es
from src.services.isolation import profiles
from src.services.isolation.profiles import PathRule, SandboxProfile
from src.services.isolation.runners import ExecRequest, ExecutorRunner, IsolationUnavailableError

PD = "/srv/data/personal_drive"
MCP = "/srv/data/mcp_servers"
MANAGED = f"{MCP}/managed"
STATE = "/srv/executor_state"
LAYOUT = es.Layout(rw=(PD, MCP), ro=("/srv/config/skills",), state=STATE, exec_paths=("/usr",))
HELLO_PORTS = frozenset(kspec.DEFAULT_BLOCK_PORTS)
SEG = "m" + "0123456789abcdef01234567"  # Touka 算的段名（sha256 前 24 字）
OTHER_SEG = "m" + "f" * 24
SKILL = "probe_tool"
CODE = f"{MANAGED}/{SKILL}"
HOME, TMP = LAYOUT.state_dirs("mcp", SEG)
CODE_RULE = {"path": CODE, "access": "rox"}
KERNEL = srv.Kernel(
    directory=Path(kspec.__file__).resolve().parent, framing=kfr, spec=kspec, landlock=kll,
    owner_repair=kor,
)


def _system_rules() -> list[dict]:
    rules = [{"path": "/usr", "access": "rox", "optional": True}]
    rules += [{"path": p, "access": "ro", "optional": True} for p in es.SYSTEM_READ_PATHS]
    return [*rules, {"path": "/dev/null", "access": "dev_rw", "optional": True}]


def _document(rules: list[dict], **overrides: object) -> dict:
    document = {
        "argv": ["/usr/bin/python3", f"{CODE}/server.py"],
        "cwd": HOME,
        "env": {"PATH": "/usr/bin:/bin", "HOME": HOME, "TMPDIR": TMP},
        "spec": {
            "version": kspec.SPEC_VERSION, "profile": "mcp_self", "rules": rules,
            "block_ports": sorted(HELLO_PORTS), "rlimits": {}, "pin": True, "parent_pid": 99,
            "net_strict": True,
        },
        "kind": "mcp", "owner": SEG, "io": "stdio", "max_seconds": 0,
    }
    document.update(overrides)
    return document


def _start(*extra: dict, code: bool = True, state: tuple[str, ...] = (HOME, TMP), **overrides):
    """kind ``mcp`` 的 START：系統路徑、自己的 home／tmp 讀寫；``code`` 時加程式資料夾 ``rox``."""
    rules = [*_system_rules(), *({"path": p, "access": "rwx"} for p in state)]
    return _document([*rules, *([CODE_RULE] if code else []), *extra], **overrides)


def _data_start(kind: str, owner: str, **overrides: object) -> dict:
    """kind agents／evolution 的合格 START（目標、home、tmp 讀寫，cwd 是目標）."""
    target = f"{PD}/{owner}" if kind == "agents" else f"{MANAGED}/{owner}"
    home, tmp = LAYOUT.state_dirs(kind, owner)
    rules = [*_system_rules(), *({"path": p, "access": "rwx"} for p in (target, home, tmp))]
    document = _document(rules, cwd=target, kind=kind, owner=owner, io="merged", max_seconds=60)
    document["env"] = {"PATH": "/usr/bin:/bin", "HOME": home}
    document.update(overrides)
    return document


def _check(document: dict, layout: es.Layout = LAYOUT) -> es.StartPlan:
    return es.check_start(
        document, spec=kspec, layout=layout, hello_ports=HELLO_PORTS, server_pid=4321
    )


def _reason(document: dict, layout: es.Layout = LAYOUT) -> str:
    with pytest.raises(es.Rejected) as caught:
        _check(document, layout)
    return caught.value.reason


# ── START：kind mcp（§2.3）────────────────────────────────────────────────────────────


def test_kind_mcp_runs_in_its_own_state_home_with_the_code_folder_read_only():
    plan = _check(_start())
    assert (plan.root, plan.cwd, plan.home, plan.tmp) == (STATE, HOME, HOME, TMP)
    assert (plan.code, plan.code_root) == (CODE, MCP)
    assert (plan.io, plan.max_seconds) == ("stdio", 0)
    assert plan.home == f"{STATE}/mcp/{SEG}/home"  # 執行器的 kind mcp 只放自建，段名是 owner
    sent = {rule.path: rule.access for rule in kspec.parse_spec(plan.spec).rules}
    assert (sent[CODE], sent[HOME], sent[TMP]) == ("rox", "rwx", "rwx")


def test_kind_mcp_without_a_code_folder_needs_no_data_root():
    """非演化的自建沒有程式資料夾：沒掛 ``mcp_servers`` 也能跑；有程式資料夾的規則就拒絕."""
    only_pd = es.Layout(rw=(PD,), ro=(), state=STATE, exec_paths=("/usr",))
    plan = _check(_start(code=False), only_pd)
    assert (plan.root, plan.cwd, plan.code, plan.code_root) == (STATE, HOME, None, None)
    assert _reason(_start(), only_pd) == "spec_rule"


MCP_REJECTED = {
    # 部署後唯讀（凍結）：程式資料夾、managed 與它們底下都不能讀寫
    "rwx_code": (_start({"path": CODE, "access": "rwx"}, code=False), "spec_rule"),
    "rwx_code_and_rox": (_start({"path": CODE, "access": "rwx"}), "spec_rule"),
    "rwx_inside_code": (_start({"path": f"{CODE}/data", "access": "rwx"}), "spec_rule"),
    "rwx_managed": (_start({"path": MANAGED, "access": "rwx"}), "spec_rule"),
    "rwx_mcp_root": (_start({"path": MCP, "access": "rwx"}), "spec_rule"),
    # 讀寫只有自己的 home／tmp
    "rwx_own_state_folder": (_start({"path": f"{STATE}/mcp/{SEG}", "access": "rwx"}), "spec_rule"),
    "rwx_other_seg_home": (
        _start({"path": f"{STATE}/mcp/{OTHER_SEG}/home", "access": "rwx"}), "spec_rule"
    ),
    "rwx_mcp_state_kind": (_start({"path": f"{STATE}/mcp", "access": "rwx"}), "spec_rule"),
    "rwx_agent_drive": (_start({"path": f"{PD}/Alice", "access": "rwx"}), "spec_rule"),
    "rwx_agent_state": (
        _start({"path": f"{STATE}/agents/{SEG}/home", "access": "rwx"}), "spec_rule"
    ),
    # 程式資料夾：最多一條 rox，正好是 managed 的直接子資料夾
    "rox_inside_code": (_start({"path": f"{CODE}/bin", "access": "rox"}), "spec_rule"),
    "rox_managed_itself": (_start({"path": MANAGED, "access": "rox"}, code=False), "spec_rule"),
    "rox_mcp_root": (_start({"path": MCP, "access": "rox"}, code=False), "spec_rule"),
    "rox_two_skills": (_start({"path": f"{MANAGED}/other", "access": "rox"}), "spec_rule"),
    "rox_same_code_twice": (_start(CODE_RULE), "spec_rule"),
    "rox_agent_drive": (_start({"path": f"{PD}/Alice", "access": "rox"}), "spec_rule"),
    "rox_other_seg_home": (
        _start({"path": f"{STATE}/mcp/{OTHER_SEG}/home", "access": "rox"}), "spec_rule"
    ),
    "ro_code": (_start({"path": CODE, "access": "ro"}, code=False), "spec_rule"),
    "dev_rw_code": (_start({"path": CODE, "access": "dev_rw"}, code=False), "spec_rule"),
    # cwd 必須是 home（而且 home 有讀寫）
    "cwd_tmp": (_start(cwd=TMP), "cwd"),
    "cwd_code": (_start(cwd=CODE), "cwd"),
    "cwd_state_folder": (_start(cwd=f"{STATE}/mcp/{SEG}"), "cwd"),
    "home_not_granted": (_start(state=(TMP,)), "cwd"),
    # owner 是單一安全段（Touka 算的雜湊）
    "owner_dotdot": (_start(owner=".."), "owner"),
    "owner_slash": (_start(owner=f"{SEG}/x"), "owner"),
    # 時限：0 只准 stdio；其他照 1～900
    "merged_zero": (_start(io="merged"), "max_seconds"),
    "stdio_over": (_start(max_seconds=es.MAX_SECONDS + 1), "max_seconds"),
    "stdio_negative": (_start(max_seconds=-1), "max_seconds"),
    "stdio_false": (_start(max_seconds=False), "max_seconds"),
    "stdio_float": (_start(max_seconds=0.0), "max_seconds"),
    "stdio_null": (_start(max_seconds=None), "max_seconds"),
    # io 只收兩個值
    "io_pty": (_start(io="pty"), "io"),
    "io_upper": (_start(io="STDIO"), "io"),
    "io_null": (_start(io=None), "io"),
    "io_list": (_start(io=["stdio"]), "io"),
}


@pytest.mark.parametrize("name", sorted(MCP_REJECTED))
def test_kind_mcp_start_is_rejected(name):
    document, reason = MCP_REJECTED[name]
    assert _reason(document) == reason


@pytest.mark.parametrize("kind", ["agents", "evolution"])
def test_zero_seconds_is_only_for_kind_mcp_over_stdio(kind):
    """演化驗證走 stdio 也要時限：kind agents／evolution 帶 0 一律拒絕（帶 60 就通過）."""
    owner = "Alice" if kind == "agents" else SKILL
    assert _check(_data_start(kind, owner, io="stdio", max_seconds=60)).io == "stdio"
    for io in ("stdio", "merged"):
        assert _reason(_data_start(kind, owner, io=io, max_seconds=0)) == "max_seconds"


@pytest.mark.parametrize("io", ["merged", "stdio"])
@pytest.mark.parametrize("seconds", [1, es.MAX_SECONDS])
def test_every_kind_takes_one_to_nine_hundred_seconds_on_both_io_modes(io, seconds):
    plans = [
        _check(_data_start("agents", "Alice", io=io, max_seconds=seconds)),
        _check(_data_start("evolution", SKILL, io=io, max_seconds=seconds)),
        _check(_start(io=io, max_seconds=seconds)),
    ]
    assert {(plan.io, plan.max_seconds) for plan in plans} == {(io, seconds)}
    assert [plan.code for plan in plans] == [None, None, CODE]


def test_data_kinds_have_no_code_folder_even_with_a_managed_rox_rule():
    """只有 kind mcp 有程式資料夾：kind evolution 的 ``rox`` 不能拿 managed 底下別的資料夾."""
    document = _data_start("evolution", SKILL)
    document["spec"]["rules"].append({"path": f"{MANAGED}/other", "access": "rox"})
    assert _reason(document) == "spec_rule"


def test_kind_list_and_layout_targets_include_mcp():
    assert es.KINDS == ("agents", "evolution", "mcp")
    assert LAYOUT.state_dirs("mcp", SEG) == (HOME, TMP)
    assert LAYOUT.target("mcp", SEG) == (STATE, HOME)  # 沒有資料的目標：目標（cwd）就是 home
    assert LAYOUT.target("agents", "Alice") == (PD, f"{PD}/Alice")


def test_cache_variables_have_a_single_copy_in_the_kernel():
    """profile 拿掉的快取變數與 ``exec_env`` 的保留名稱同一份（U3a 建議）."""
    assert profiles.SANDBOX_ENV_UNSET is kspec.SANDBOX_ENV_UNSET
    assert {name.upper() for name in kspec.SANDBOX_ENV_UNSET} <= kspec.EXEC_ENV_RESERVED


# ── 伺服器的 prepare（MEDIUM-1）與開機清 tmp ─────────────────────────────────────────────


def _disk_server(tmp_path: Path, calls: list) -> tuple[srv.ExecutorServer, Path, Path]:
    """真的資料夾上的伺服器；``calls`` 記下每次修路徑鏈的（root、目標）。"""
    state, mcp = tmp_path / "state", tmp_path / "mcp_servers"
    for path in (state, mcp / "managed"):
        path.mkdir(parents=True)
    config = srv.ServerConfig(
        ipc=(tmp_path / "ipc").as_posix(), kernel=str(KERNEL.directory), state=state.as_posix(),
        rw=(mcp.as_posix(),), ro=(), require_foreign_peer=False,
    )
    hooks = srv.Hooks(
        repair=lambda root, target: (calls.append((root, target)), kor.RepairOutcome())[1]
    )
    server = srv.ExecutorServer(config, KERNEL, hooks, require_root_interpreter=False)
    server.uid = os.lstat(state).st_uid
    return server, state, mcp


def _disk_plan(server: srv.ExecutorServer, mcp: Path, *, code: bool) -> es.StartPlan:
    home, tmp = server.layout.state_dirs("mcp", SEG)
    folder = (mcp / "managed" / SKILL).as_posix() if code else None
    return es.StartPlan(
        argv=("true",), cwd=home, env={}, spec=b"{}", ports=(), root=server.config.state,
        home=home, tmp=tmp, max_seconds=0, io="stdio", code=folder,
        code_root=mcp.as_posix() if code else None,
    )


def test_prepare_builds_only_its_own_home_and_tmp_and_never_the_code_folder(tmp_path):
    calls: list = []
    server, state, mcp = _disk_server(tmp_path, calls)
    plan = _disk_plan(server, mcp, code=True)
    assert server._prepare_dirs(plan, {plan.home, plan.tmp, plan.code}) is None
    built = sorted(path.relative_to(state).as_posix() for path in state.rglob("*"))
    assert built == ["mcp", f"mcp/{SEG}", *(f"mcp/{SEG}/{name}" for name in ("home", "tmp"))]
    assert list((mcp / "managed").iterdir()) == []  # 不見了也不建：啟動器 exec 時照實失敗
    root = state.as_posix()
    assert calls == [(root, plan.home), (root, plan.tmp), (mcp.as_posix(), plan.code)]


def test_prepare_repairs_an_existing_code_folder_only_within_the_budget(tmp_path):
    calls: list = []
    server, _state, mcp = _disk_server(tmp_path, calls)
    (mcp / "managed" / SKILL).mkdir()
    plan = _disk_plan(server, mcp, code=True)
    assert server._prepare_dirs(plan, {plan.code}) is None
    assert calls == [(mcp.as_posix(), plan.code)]  # 只修擁有者位元
    assert list((mcp / "managed" / SKILL).iterdir()) == []
    calls.clear()
    assert server._prepare_dirs(plan, set()) is None
    assert calls == []


def test_prepare_without_a_code_folder_touches_only_the_state(tmp_path):
    calls: list = []
    server, state, mcp = _disk_server(tmp_path, calls)
    plan = _disk_plan(server, mcp, code=False)
    assert server._prepare_dirs(plan, {plan.home, plan.tmp}) is None
    assert calls == [(state.as_posix(), plan.home), (state.as_posix(), plan.tmp)]


async def test_prepare_budget_counts_home_tmp_and_code_once_each(tmp_path):
    server, _state, mcp = _disk_server(tmp_path, [])
    plan = _disk_plan(server, mcp, code=True)
    seen = []
    server._prepare_dirs = lambda plan, allowed: seen.append(allowed)
    await srv.ExecutorServer.prepare(server, plan)
    await srv.ExecutorServer.prepare(server, plan)  # 10 秒內同一目標：不修
    assert seen == [{plan.home, plan.tmp, plan.code}, set()]
    assert server.repairs["throttled"] == 3  # cwd 就是 home，不重複計


def test_startup_empties_kind_mcp_tmp_but_not_home(tmp_path):
    server, state, _mcp = _disk_server(tmp_path, [])
    base = state / "mcp" / SEG
    (base / "tmp" / "sub").mkdir(parents=True)
    (base / "tmp" / "sub" / "f").write_text("x", encoding="utf-8")
    (base / "home").mkdir()
    (base / "home" / "keep").write_text("x", encoding="utf-8")
    server._clean_state_tmp()
    assert list((base / "tmp").iterdir()) == []
    assert (base / "home" / "keep").exists()


# ── proxy 端：ExecutorRunner.plan 與 profiles 的 kind ──────────────────────────────────


def _mcp_profile(name: str = "mcp_self") -> SandboxProfile:
    home, tmp = Path(HOME), Path(TMP)
    rules = (PathRule(Path("/usr"), "rox", optional=True), PathRule(Path(CODE), "rox"))
    return SandboxProfile(
        name=name, cwd=home, rules=(*rules, PathRule(home, "rwx"), PathRule(tmp, "rwx")),
        block_ports=tuple(sorted(HELLO_PORTS)), env_set={"HOME": HOME, "TMPDIR": TMP}, owner=SEG,
    )


def _runner(tmp_path: Path) -> ExecutorRunner:
    return ExecutorRunner(
        tmp_path, tmp_path / "ipc", "b" * 32, frozenset({"fs", "tcp"}), python=sys.executable
    )


def _sent_start(tmp_path: Path, request: ExecRequest) -> dict:
    plan = _runner(tmp_path).plan(request)
    try:
        fd = int(plan.argv[plan.argv.index("--request-fd") + 1])
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        plan.release()
        plan.read_status()
    return json.loads(b"".join(chunks))["start"]


def test_runner_starts_kind_mcp_over_stdio_without_a_time_limit(tmp_path):
    request = ExecRequest(argv=("python3", "server.py"), env={}, profile=_mcp_profile(), io="stdio")
    start = _sent_start(tmp_path, request)
    assert (start["kind"], start["owner"], start["io"], start["max_seconds"]) == (
        "mcp", SEG, "stdio", 0,
    )
    if os.name != "nt":  # Windows 的 Path 把 POSIX 路徑寫成反斜線，伺服器的形狀檢查只在 POSIX
        plan = _check(start)
        assert (plan.cwd, plan.code, plan.max_seconds) == (HOME, CODE, 0)


@pytest.mark.parametrize(
    ("name", "given", "expected"),
    [
        ("mcp_self", None, 0), ("mcp_self", 0, 0), ("mcp_self", 30, 30),
        ("mcp_self", 5000, es.MAX_SECONDS), ("agent_command", None, es.MAX_SECONDS),
        ("agent_command", 0, es.MAX_SECONDS), ("evolution", None, es.MAX_SECONDS),
        ("evolution", 45, 45),
    ],
)
def test_runner_time_limit_on_stdio(tmp_path, name, given, expected):
    """有給就夾在 1～900；沒給時只有 kind mcp 是 0（不限時），其他 kind 照舊用上限."""
    request = ExecRequest(
        argv=("true",), env={}, profile=_mcp_profile(name), io="stdio", max_seconds=given
    )
    start = _sent_start(tmp_path, request)
    assert (start["io"], start["max_seconds"]) == ("stdio", expected)


def _relay_cwd_and_start(tmp_path: Path, profile: SandboxProfile) -> tuple[Path, dict]:
    request = ExecRequest(argv=("python3", "server.py"), env={}, profile=profile, io="stdio")
    plan = _runner(tmp_path).plan(request)
    try:
        fd = int(plan.argv[plan.argv.index("--request-fd") + 1])
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        plan.release()
        plan.read_status()
    return plan.cwd, json.loads(b"".join(chunks))["start"]


def test_relay_for_kind_mcp_runs_from_the_kernel_folder_and_the_server_from_its_home(tmp_path):
    """執行器位置的自建 MCP：home 只在執行器容器的 state（proxy 沒掛），relay 是 proxy
    本機的子行程，所以 relay 的本機 cwd 是唯讀的 kernel 資料夾（relay 所在處）；
    伺服器自己的 cwd 照 START＝home."""
    cwd, start = _relay_cwd_and_start(tmp_path, _mcp_profile())
    assert cwd == tmp_path  # _runner 的 kernel 資料夾
    assert start["cwd"] == str(Path(HOME))


@pytest.mark.parametrize("name", ["agent_command", "evolution"])
def test_relay_for_data_kinds_keeps_the_profile_cwd(tmp_path, name):
    """agents／evolution 的目標在共用的 data 底下（proxy 也有），照舊用 profile 的 cwd."""
    cwd, start = _relay_cwd_and_start(tmp_path, _mcp_profile(name))
    assert cwd == Path(HOME)
    assert start["cwd"] == str(Path(HOME))


def test_runner_keeps_merged_as_the_default(tmp_path):
    request = ExecRequest(argv=("true",), env={}, profile=_mcp_profile("agent_command"))
    assert _sent_start(tmp_path, request)["io"] == "merged"


@pytest.mark.parametrize("io", ["pty", "STDIO", "", "merged "])
def test_runner_refuses_an_unknown_io_mode(tmp_path, io):
    with pytest.raises(IsolationUnavailableError):
        _runner(tmp_path).plan(ExecRequest(argv=("true",), env={}, profile=_mcp_profile(), io=io))


def test_profile_kinds_map_onto_the_executor_kinds():
    """proxy 的 state 分自建與第三方兩個父目錄；執行器的 kind ``mcp`` 只放自建."""
    assert get_args(profiles.StateKind) == ("agents", "evolution", "mcp_self", "mcp_third")
    assert dict(profiles.EXECUTOR_KINDS) == {
        "agent_command": "agents", "evolution": "evolution", "mcp_self": "mcp",
    }
    assert set(get_args(profiles.ExecutorKind)) == set(es.KINDS)
