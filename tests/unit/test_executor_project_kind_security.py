"""執行器的 kind ``project``：專用空間的專案（ADR-0332，融合段 2 PR A）.

- ``--project-root`` 和其他掛載根分開驗：不存在、是連結、上層有連結、不是資料夾、名稱不是
  ``touka-projects``、和其他根或 state、ipc 巢狀時只是不回報（READY 的 ``project_root`` 是 null），
  伺服器照常服務；驗過的才併進 ``es.Layout`` 的讀寫根，``config.rw`` 不動。
- START：kind ``project`` 的目標是 ``<project_root>/<資料夾名>``，cwd 必須是它；讀寫只有它與
  ``<state>/project/<資料夾名>/{home,tmp}``；沒有驗過的專案根就拒絕（``kind``）。
- ``_prepare_dirs`` 對 kind ``project`` 不修 cwd 的路徑鏈（專案內容不動），home、tmp 照修。
- ``_maintain`` 的掛載根加上專案根本身。
- proxy 的 ``parse_ready`` 收 ``project_root``（null 或絕對路徑），沒帶當 null。

任何平台都能跑；真核心的案例在 ``test_isolation_linux.py``。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import get_args

import pytest
from src.sandbox_kernel import framing as kfr
from src.sandbox_kernel import landlock as kll
from src.sandbox_kernel import owner_repair as kor
from src.sandbox_kernel import spec as kspec
from src.services.isolation import executor_client as ec
from src.services.isolation import executor_server as srv
from src.services.isolation import executor_session as es
from src.services.isolation import profiles

PD = "/srv/data/personal_drive"
MANAGED = "/srv/data/mcp_servers/managed"
SKILLS = "/srv/config/skills"
PRJ = "/srv/workspace/touka-projects"
STATE = "/srv/executor_state"
IPC = "/srv/ipc"
HELLO_PORTS = frozenset(kspec.DEFAULT_BLOCK_PORTS)
LAYOUT = es.Layout(rw=(PD, MANAGED, PRJ), ro=(SKILLS,), state=STATE, exec_paths=("/usr",))
WITHOUT_PROJECTS = es.Layout(rw=(PD, MANAGED), ro=(SKILLS,), state=STATE, exec_paths=("/usr",))
KERNEL = srv.Kernel(
    directory=Path(kspec.__file__).resolve().parent, framing=kfr, spec=kspec, landlock=kll,
    owner_repair=kor,
)


def _system_rules() -> list[dict]:
    rules = [{"path": "/usr", "access": "rox", "optional": True}]
    rules += [{"path": p, "access": "ro", "optional": True} for p in es.SYSTEM_READ_PATHS]
    return [*rules, {"path": "/dev/null", "access": "dev_rw", "optional": True}]


def _start(owner: str = "repo", *, rw: tuple[str, ...] | None = None, **overrides: object) -> dict:
    target = f"{PRJ}/{owner}"
    home, tmp = LAYOUT.state_dirs("project", owner)
    granted = rw if rw is not None else (target, home, tmp)
    rules = [*_system_rules(), *({"path": p, "access": "rwx"} for p in granted)]
    document = {
        "argv": ["sh", "-c", "git status"],
        "cwd": target,
        "env": {"PATH": "/usr/bin:/bin", "HOME": home, **dict(kspec.EXECUTOR_GIT_CONFIG)},
        "spec": {
            "version": kspec.SPEC_VERSION,
            "profile": "project_command",
            "rules": rules,
            "block_ports": sorted(HELLO_PORTS),
            "rlimits": {},
            "pin": True,
            "parent_pid": 99,
            "net_strict": True,
        },
        "kind": "project",
        "owner": owner,
        "io": "merged",
        "max_seconds": 60,
    }
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


# ── 種類表與 START ──────────────────────────────────────────────────────────────────


def test_project_kind_lives_under_the_touka_projects_root():
    assert es.KIND_LAYOUT["project"] == ("touka-projects", ())
    assert "project" in es.KINDS and es.KINDS[-1] == es.MCP_KIND
    assert LAYOUT.target("project", "repo") == (PRJ, f"{PRJ}/repo")
    assert LAYOUT.state_dirs("project", "repo") == (
        f"{STATE}/project/repo/home", f"{STATE}/project/repo/tmp",
    )
    assert WITHOUT_PROJECTS.target("project", "repo") is None


def test_a_project_start_runs_in_the_project_with_its_own_home():
    plan = _check(_start())
    assert (plan.cwd, plan.root, plan.kind) == (f"{PRJ}/repo", PRJ, "project")
    assert (plan.home, plan.tmp) == (f"{STATE}/project/repo/home", f"{STATE}/project/repo/tmp")
    assert json.loads(plan.spec)["parent_pid"] == 4321


def test_other_kinds_say_their_kind_in_the_plan():
    home, tmp = LAYOUT.state_dirs("agents", "Alice")
    target = f"{PD}/Alice"
    document = _start(rw=(target, home, tmp), kind="agents", owner="Alice", cwd=target)
    assert _check(document).kind == "agents"


def test_without_a_checked_project_root_a_project_start_is_refused():
    assert _reason(_start(), WITHOUT_PROJECTS) == "kind"


OWN_STATE = LAYOUT.state_dirs("project", "repo")


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (_start(cwd=f"{PRJ}/other"), "cwd"),
        (_start(cwd=PRJ), "cwd"),
        (_start(rw=(f"{PRJ}/repo", f"{PRJ}/other", *OWN_STATE)), "spec_rule"),
        (_start(rw=(PRJ, *OWN_STATE)), "spec_rule"),
        (_start(rw=(f"{PRJ}/repo", f"{PD}/Alice", *OWN_STATE)), "spec_rule"),
        (_start(rw=(f"{PRJ}/repo", *LAYOUT.state_dirs("project", "other"))), "spec_rule"),
        (_start(owner="../repo"), "owner"),
        (_start(owner="a/b"), "owner"),
    ],
)
def test_a_project_start_gets_nothing_outside_its_own_folder(document, reason):
    assert _reason(document) == reason


def test_profile_kinds_include_project():
    assert "project" in get_args(profiles.StateKind)
    assert "project" in get_args(profiles.ExecutorKind)
    assert profiles.EXECUTOR_KINDS["project_command"] == "project"
    assert set(get_args(profiles.ExecutorKind)) == set(es.KINDS)


# ── 命令列與專案根的檢查 ──────────────────────────────────────────────────────────────


BASE_ARGV = ["--ipc-dir", IPC, "--kernel-dir", "/k", "--state-dir", STATE, "--root", f"rw:{PD}"]


def test_parse_cli_takes_an_optional_project_root():
    assert srv.parse_cli([*BASE_ARGV, "--project-root", PRJ]).project_root == PRJ
    config = srv.parse_cli(BASE_ARGV)
    assert config.project_root is None
    assert config.rw == (PD,)  # 專案根不混進讀寫根


@pytest.mark.parametrize("argv", [["--project-root", ""], ["--project-root"]])
def test_parse_cli_rejects_an_empty_project_root(argv):
    with pytest.raises(srv.ConfigError):
        srv.parse_cli([*BASE_ARGV, *argv])


class FakeView:
    """假的檔案系統：``dirs`` 是資料夾 → 權限位元；``links`` 是連結；``resolved`` 是上層有連結."""

    uid = 1001

    def __init__(self) -> None:
        self.dirs = dict.fromkeys((PD, MANAGED, SKILLS, PRJ), 493)
        self.dirs |= {STATE: 0o700, IPC: 0o700}
        self.links: dict[str, str] = {}
        self.resolved: dict[str, str] = {}

    def lstat(self, path: str) -> os.stat_result:
        if path in self.links:
            return os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        if path not in self.dirs:
            raise FileNotFoundError(path)
        mode = self.dirs[path]
        kind = 0 if stat.S_IFMT(mode) else stat.S_IFDIR
        return os.stat_result((kind | mode, 0, 0, 1, self.uid, 0, 0, 0, 0, 0))

    def resolve(self, path: str) -> str:
        if path in self.links:
            return self.links[path]
        self.lstat(path)
        return self.resolved.get(path, path)

    def names(self, path: str) -> list[str]:
        return []


def _config(project_root: str | None = PRJ, **overrides: object) -> srv.ServerConfig:
    fields: dict = {
        "ipc": IPC, "kernel": str(KERNEL.directory), "state": STATE, "rw": (PD, MANAGED),
        "ro": (SKILLS,), "require_foreign_peer": False, "project_root": project_root,
    }
    fields.update(overrides)
    return srv.ServerConfig(**fields)


def _elsewhere(view: FakeView) -> str:
    path = "/srv/elsewhere/touka-projects"
    view.dirs[path] = 0o755
    return path


PROJECT_ROOT_CASES = {
    "ok": (lambda view: PRJ, PRJ),
    "not_given": (lambda view: None, None),
    "missing": (lambda view: (view.dirs.pop(PRJ), PRJ)[1], None),
    "a_link": (lambda view: (view.dirs.pop(PRJ), view.links.update({PRJ: "/x"}), PRJ)[2], None),
    "linked_parent": (lambda view: (view.resolved.update({PRJ: "/mnt/real"}), PRJ)[1], None),
    "a_file": (lambda view: (view.dirs.update({PRJ: stat.S_IFREG | 0o644}), PRJ)[1], None),
    "relative": (lambda view: "touka-projects", None),
    "wrong_name": (lambda view: (view.dirs.update({"/srv/workspace/projects": 0o755}),
                                 "/srv/workspace/projects")[1], None),
    "inside_a_root": (lambda view: (view.dirs.update({f"{PD}/touka-projects": 0o755}),
                                    f"{PD}/touka-projects")[1], None),
    "elsewhere_ok": (_elsewhere, "/srv/elsewhere/touka-projects"),
}


@pytest.mark.parametrize("case", sorted(PROJECT_ROOT_CASES))
def test_the_project_root_is_checked_on_its_own(case):
    """專案根有問題只是不回報；``config_problem`` 不看它，伺服器照常服務其他 kind."""
    change, expected = PROJECT_ROOT_CASES[case]
    view = FakeView()
    config = _config(project_root=change(view))
    assert srv.checked_project_root(config, view) == expected
    assert srv.config_problem(config, KERNEL, view) is None


def test_a_project_root_containing_the_state_is_not_reported():
    view = FakeView()
    state = f"{PRJ}/state"
    view.dirs[state] = 0o700
    assert srv.checked_project_root(_config(state=state), view) is None


def _server(project_root: str | None = PRJ) -> srv.ExecutorServer:
    server = srv.ExecutorServer(
        _config(project_root), KERNEL, srv.Hooks(), require_root_interpreter=False
    )
    server.view = FakeView()
    return server


def test_a_checked_project_root_joins_the_layout_and_ready():
    server = _server()
    assert server.ready(HELLO_PORTS)["project_root"] is None  # 啟動驗過之前不回報
    assert server.layout.target("project", "repo") is None
    server.adopt_project_root()
    assert server.ready(HELLO_PORTS)["project_root"] == PRJ
    assert server.layout.target("project", "repo") == (PRJ, f"{PRJ}/repo")
    assert server.config.rw == (PD, MANAGED)
    roots = {item["path"] for item in server.ready(HELLO_PORTS)["roots"]}
    assert PRJ not in roots  # roots 照舊只有三個，proxy 的 roots 比對不變


def test_a_bad_project_root_stays_out_of_the_layout():
    server = _server()
    server.view.dirs.pop(PRJ)
    server.adopt_project_root()
    assert server.ready(HELLO_PORTS)["project_root"] is None
    assert server.layout.target("project", "repo") is None


def test_maintenance_repairs_only_the_project_root_itself():
    chains: list = []
    server = _server()
    server.adopt_project_root()
    server.hooks.repair = lambda root, target: (
        chains.append((root, target)), SimpleNamespace(changed=0, stopped=None)
    )[1]
    server.hooks.repair_private = lambda root, target: SimpleNamespace(changed=0, stopped=None)
    server._maintain(roots=True, alive=False)
    assert (PRJ, PRJ) in chains
    assert not any(target.startswith(f"{PRJ}/") for _root, target in chains)


def _disk_server(tmp_path: Path, calls: list) -> tuple[srv.ExecutorServer, Path, Path]:
    state, projects = tmp_path / "state", tmp_path / "workspace" / "touka-projects"
    for path in (state, projects):
        path.mkdir(parents=True)
    config = srv.ServerConfig(
        ipc=(tmp_path / "ipc").as_posix(), kernel=str(KERNEL.directory), state=state.as_posix(),
        rw=(), ro=(), require_foreign_peer=False, project_root=projects.as_posix(),
    )
    hooks = srv.Hooks(
        repair=lambda root, target: (calls.append((root, target)), kor.RepairOutcome())[1]
    )
    server = srv.ExecutorServer(config, KERNEL, hooks, require_root_interpreter=False)
    server.uid = os.lstat(state).st_uid
    return server, state, projects


def _disk_plan(server: srv.ExecutorServer, projects: Path, name: str = "repo") -> es.StartPlan:
    base = PurePosixPath(server.config.state, "project", name)
    return es.StartPlan(
        argv=("true",), cwd=(projects / name).as_posix(), env={}, spec=b"{}", ports=(),
        root=projects.as_posix(), home=str(base / "home"), tmp=str(base / "tmp"),
        max_seconds=60, kind="project",
    )


def test_prepare_never_repairs_inside_the_project(tmp_path):
    calls: list = []
    server, state, projects = _disk_server(tmp_path, calls)
    (projects / "repo").mkdir()
    plan = _disk_plan(server, projects)
    assert server._prepare_dirs(plan, {plan.home, plan.tmp, plan.cwd}) is None
    root = state.as_posix()
    assert calls == [(root, plan.home), (root, plan.tmp)]  # cwd（專案）不修
    assert (state / "project" / "repo" / "home").is_dir()


def test_prepare_refuses_a_missing_project_folder(tmp_path):
    server, _state, projects = _disk_server(tmp_path, [])
    plan = _disk_plan(server, projects)
    assert server._prepare_dirs(plan, {plan.home, plan.tmp, plan.cwd}) == "cwd"


# ── proxy 讀 READY ────────────────────────────────────────────────────────────────────


def _ready_document(**changes: object) -> dict:
    document: dict = {
        "v": kfr.PROTOCOL_VERSION, "abi": 6, "enforced": True, "net": None, "features": ["fs"],
        "roots": [{"path": PD, "access": "rw"}], "state_root": STATE, "kernel_digest": "a" * 64,
        "server_digest": "b" * 64, "boot_id": "c" * 32, "block_ports_digest": "d" * 64, "uid": 1001,
    }
    document.update(changes)
    return document


@pytest.mark.parametrize(
    ("changes", "expected"),
    [({}, None), ({"project_root": None}, None), ({"project_root": PRJ}, PRJ)],
)
def test_proxy_reads_the_project_root_from_ready(changes, expected):
    ready = ec.parse_ready(_ready_document(**changes))
    assert ready is not None and ready.project_root == expected


@pytest.mark.parametrize("value", ["touka-projects", "", 7, ["/a"], f"{PRJ}\x00"])
def test_a_malformed_project_root_makes_ready_invalid(value):
    assert ec.parse_ready(_ready_document(project_root=value)) is None


def test_the_servers_ready_passes_the_proxy_parser_with_a_project_root():
    server = _server()
    server.adopt_project_root()
    server._digests = ("a" * 64, "b" * 64)
    ready = ec.parse_ready(server.ready(HELLO_PORTS))
    assert ready is not None and ready.project_root == PRJ
