"""隔離 profile：固定受保護集合 P、Touka 自選路徑、G、公開設定檔與 Touka 目錄偵測（iso-c2）.

前半（U1c）把 Touka 目錄偵測換成「放行」，只驗固定規則；後半（U1g）用真的偵測逐列驗
設計 §3.4 的例子表、第 4 版補的反例與大小寫變體。全部用 tmp_path 建的假版面。
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import stat
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest
from src.config import settings
from src.models.identity import AgentIdentity
from src.sandbox_kernel import spec as kernel_spec
from src.services import approval_flow
from src.services.isolation import profiles, project_scan
from src.services.isolation.consent import ConsentKey, consent_ledger
from src.services.isolation.profiles import (
    ProfileError,
    SandboxLayout,
    authorize_derived_path,
    blocked_ports,
    build_agent_command_profile,
)
from src.utils.async_db import init_db_async

from tests.support.isolation import (
    fake_kernel_layout,
    install_real_broker,
    match_state_root,
    pinned_snapshot,
)
from tests.support.zone_roots import point_zone_roots

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
_winapi = importlib.import_module("_winapi") if os.name == "nt" else None


@pytest.fixture(autouse=True)
def _rm_state_matches_layout(tmp_path, monkeypatch):
    """RM 的 ST 指向本檔版面的 ST（ADR-0293 P7：兩者不同時 ST 的存取函式拒絕）."""
    match_state_root(monkeypatch, tmp_path / "install" / "st")


def _layout(root: Path, *, in_container: bool = False) -> SandboxLayout:
    install = root / "install"
    data = install / "data"
    for sub in ("personal_drive/Alice", "custom/DebugAgent/drive", "temp/docgen_output"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("ToukaProxy/config/skills", "ToukaMemory", "workspace", "host_root", "st"):
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
        mirror_roots=(install / "host_root", install / "workspace"),
        in_container=in_container,
    )


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    return _layout(tmp_path)


@pytest.fixture
def allow_touka_check(monkeypatch):
    async def _allow(_path: Path, _layout: SandboxLayout) -> None:
        return None

    monkeypatch.setattr(profiles, "touka_directory_verdict", _allow)


def _rules(profile) -> dict[Path, str]:
    return {rule.path: rule.access for rule in profile.rules}


# --- 固定受保護集合 P ------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "data",
        "data/soul/Bob",
        "ToukaProxy",
        "ToukaProxy/config",
        "ToukaMemory/src",
        "ToukaDispatch",
        "ToukaHeartbeat",
        "backups",
        "kernel",
        "st",
        "st/proxy/agents/Bob/home",
        "host_root",
        "host_root/.env",
        ".",
        "workspace",
    ],
)
async def test_fixed_protected_set_and_mirror_root_denied(layout, allow_touka_check, relative):
    target = layout.install_root / relative
    assert await authorize_derived_path(target, layout) == "protected_location"


async def test_ancestor_of_protected_item_denied(layout, allow_touka_check):
    assert await authorize_derived_path(layout.install_root.parent, layout) == "protected_location"


async def test_env_dir_not_protected_when_it_contains_data_root(tmp_path, allow_touka_check):
    native = _layout(tmp_path)
    native_env = native.install_root / ".env"
    native_env.write_text("", encoding="utf-8")
    native = SandboxLayout(**{**native.__dict__, "env_file": native_env, "mirror_roots": ()})
    project = native.install_root / "myproj"
    project.mkdir()
    assert await authorize_derived_path(project, native) is None
    assert await authorize_derived_path(native_env, native) == "protected_location"


async def test_workspace_child_reaches_touka_directory_check(layout, allow_touka_check):
    project = layout.install_root / "workspace" / "myproj"
    project.mkdir()
    assert await authorize_derived_path(project, layout) is None


async def test_touka_directory_check_is_wired_into_derived_paths(layout):
    """沒有替身時走真的偵測：工作區是 Touka checkout 時，已追蹤的頂層資料夾不能授權。"""
    workspace = layout.install_root / "workspace"
    (workspace / "ToukaProxy").mkdir()
    (workspace / "scripts").mkdir()
    (workspace / "myproj").mkdir()
    assert await authorize_derived_path(workspace / "scripts", layout) == "protected_location"
    assert await authorize_derived_path(workspace / "myproj", layout) is None


async def test_container_install_root_children_denied_except_workspace(tmp_path, allow_touka_check):
    container = _layout(tmp_path, in_container=True)
    for name in ("hf_cache", "ipc", ".venv"):
        (container.install_root / name / "x").mkdir(parents=True)
        verdict = await authorize_derived_path(container.install_root / name / "x", container)
        assert verdict == "protected_location", name
    project = container.install_root / "workspace" / "myproj"
    project.mkdir()
    assert await authorize_derived_path(project, container) is None


async def test_grantable_root_is_only_the_callers_own_drive(layout):
    """G 依身分：只有代理人自己的 ``personal_drive/<id>``；共用的 docgen_output 不給。"""
    drives = layout.data_root / "personal_drive"
    for owner in ("Alice", "Bob"):
        (drives / owner / "proj").mkdir(parents=True)
    own = drives / "Alice" / "proj"
    assert await authorize_derived_path(own, layout, owner="Alice") is None
    for target, owner in (
        (drives / "Bob" / "proj", "Alice"),
        (drives, "Alice"),
        (layout.data_root / "temp" / "docgen_output", "Alice"),
        (own, "Bob"),
        (own, None),  # 沒有身分就沒有 G
        (own, ".."),
        (own, ""),  # 空字串會讓 G 變成整個 personal_drive
    ):
        assert await authorize_derived_path(target, layout, owner=owner) == PROTECTED, target
    profile = await build_agent_command_profile(
        ALICE, drives / "Alice", layout=layout, project_root=own
    )
    assert _rules(profile)[own.resolve()] == "rwx" and not profile.ungranted


async def test_bound_project_root_through_a_link_is_refused(layout):
    """專案根換成連結（Windows 用 junction）：resolve 前後不同就拒絕，不跟過去授權。"""
    drive = layout.data_root / "personal_drive" / "Alice"
    workspace_project = _user_project(layout.install_root / "workspace" / "myproj")
    bob = layout.data_root / "personal_drive" / "Bob"
    (bob / "notes").mkdir(parents=True)
    assert await authorize_derived_path(workspace_project, layout, owner="Alice") is None
    for name, target in (("to_project", workspace_project), ("to_bob", bob)):
        _dir_link(drive / name, target)
        verdict = await authorize_derived_path(drive / name, layout, owner="Alice")
        assert verdict == PROTECTED, name
    profile = await build_agent_command_profile(
        ALICE, drive, layout=layout, project_root=drive / "to_bob"
    )
    assert profile.ungranted == (PROTECTED,) and profile.project_root is None
    assert not [p for p in _rules(profile) if "Bob" in p.parts]


async def test_freshly_started_data_root_is_protected_through_a_workspace_alias(
    tmp_path, monkeypatch
):
    """改名、剛啟動的資料根（只有啟動一定會建的 ui_sessions.db）從工作區走到時仍受保護。"""
    workspace = tmp_path / "drive"  # WORKSPACE_ROOT 是整個磁碟
    fresh = workspace / "stuff" / "ToukaData"  # SANCTUARY_DATA_PATH 改名、放在工作區底下
    fresh.mkdir(parents=True)
    point_zone_roots(monkeypatch, fresh, tmp_path / "zone_state")
    await init_db_async()  # 啟動流程一定會跑的那一步（lifespan）
    (fresh / "notes").mkdir()
    project = _user_project(workspace / "proj")
    layout = replace(_layout(tmp_path), mirror_roots=(workspace,))
    for target in (fresh, fresh / "notes", workspace / "stuff"):
        assert await authorize_derived_path(target, layout) == PROTECTED, target
    assert await authorize_derived_path(project, layout) is None


async def test_card_isolation_uses_the_callers_own_drive(tmp_path, monkeypatch):
    """審批卡的降級去處也依身分：自己資料夾裡的專案能授權（landlock），別人的不行（direct）。"""
    layout = fake_kernel_layout(tmp_path)
    snap = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
    install_real_broker(monkeypatch, snap, layout)
    monkeypatch.setattr(approval_flow, "current_layout", lambda: layout)
    drives = layout.data_root / "personal_drive"
    for index, (owner, expected) in enumerate((("Alice", "landlock"), ("Bob", "direct"))):
        project = drives / owner / "proj"
        project.mkdir(parents=True)
        key = ConsentKey("Alice", "s1", f"c{index}")
        consent_ledger.record_policy(
            key, level="none", subject="x", reason="executor_unreachable", project_root=project
        )
        card = await approval_flow.card_isolation(key, "execute_command")
        assert card is not None and card.fallback == expected, owner


async def test_symlink_to_protected_resolves_before_judging(layout, allow_touka_check):
    project = layout.install_root / "workspace" / "myproj"
    project.mkdir()
    try:
        (project / "escape").symlink_to(layout.data_root / "soul", target_is_directory=True)
    except OSError:
        # Windows 沒有 symlink 權限時改用 resolve 等價的 ".." 寫法驗證同一條規則
        escape = project / ".." / ".." / "data"
    else:
        escape = project / "escape"
    assert await authorize_derived_path(escape, layout) == "protected_location"


# --- 代理人指令 profile -----------------------------------------------------------------


async def test_agent_profile_grants_cwd_state_and_system_paths(layout):
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    rules = _rules(profile)
    home = layout.state_root / "proxy" / "agents" / "Alice" / "home"
    tmp = layout.state_root / "proxy" / "agents" / "Alice" / "tmp"
    assert rules[agent_dir.resolve()] == "rwx"
    assert rules[home.resolve()] == "rwx" and home.is_dir()
    assert rules[tmp.resolve()] == "rwx" and tmp.is_dir()
    assert rules[(layout.config_dir / "skills").resolve()] == "rox"
    assert rules[Path("/etc").resolve()] == "ro"
    assert rules[Path("/dev/null").resolve()] == "dev_rw"
    assert rules[Path("/dev/urandom").resolve()] == "ro"
    assert profile.cwd == agent_dir.resolve()
    assert not profile.ungranted
    assert layout.data_root.resolve() not in rules
    assert layout.state_root.resolve() not in rules


async def test_agent_profile_accepts_custom_drive_shape(layout):
    debug = AgentIdentity.for_role("DebugAgent", "Debugger", capabilities=[])
    agent_dir = layout.data_root / "custom" / "DebugAgent" / "drive"
    profile = await build_agent_command_profile(debug, agent_dir, layout=layout)
    assert profile.cwd == agent_dir.resolve()


@pytest.mark.parametrize(
    "relative", ["personal_drive/Bob", "personal_drive", "custom/Alice", "soul/Alice", "."]
)
async def test_agent_profile_rejects_cwd_that_is_not_own_drive(layout, relative):
    target = layout.data_root / relative
    target.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ProfileError):
        await build_agent_command_profile(ALICE, target, layout=layout)


async def test_agent_profile_rejects_unsafe_agent_id(layout):
    evil = AgentIdentity.for_role("..", "Owner", capabilities=[])
    with pytest.raises(ProfileError):
        await build_agent_command_profile(evil, layout.data_root / "personal_drive", layout=layout)


async def test_agent_profile_without_state_root_is_profile_error(layout):
    unset = SandboxLayout(**{**layout.__dict__, "state_root": None})
    not_a_dir = layout.install_root / "st_file"
    not_a_dir.write_text("", encoding="utf-8")  # ST 不可寫：建不出子資料夾
    unwritable = SandboxLayout(**{**layout.__dict__, "state_root": not_a_dir})
    for broken in (unset, unwritable):
        with pytest.raises(ProfileError) as caught:
            await build_agent_command_profile(
                ALICE, layout.data_root / "personal_drive" / "Alice", layout=broken
            )
        assert caught.value.reason == "sandbox_state_unconfigured"


async def test_agent_profile_grants_authorized_project(layout, allow_touka_check):
    project = layout.install_root / "workspace" / "myproj"
    project.mkdir()
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    assert _rules(profile)[project.resolve()] == "rwx"
    assert profile.project_root == project.resolve()


async def test_agent_profile_marks_denied_project_as_ungranted(layout, allow_touka_check):
    profile = await build_agent_command_profile(
        ALICE,
        layout.data_root / "personal_drive" / "Alice",
        layout=layout,
        project_root=layout.install_root / "ToukaProxy",
    )
    assert profile.ungranted == ("protected_location",)
    assert layout.proxy_root.resolve() not in _rules(profile)
    assert profile.project_root is None


# --- 公開設定檔（R） -------------------------------------------------------------------


async def test_public_config_of_project_and_cwd_ancestors_read_only(layout, allow_touka_check):
    workspace = layout.install_root / "workspace"
    project = workspace / "myproj" / "pkg"
    project.mkdir(parents=True)
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (workspace / "myproj" / "pyproject.toml").write_text("", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("", encoding="utf-8")
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    by_path = {rule.path: rule for rule in profile.rules}
    for granted in (workspace / "pytest.ini", workspace / "myproj" / "pyproject.toml"):
        rule = by_path[granted.resolve()]
        assert rule.access == "ro" and rule.optional and rule.skip_symlink
    assert (workspace / ".env").resolve() not in by_path
    assert (workspace / "notes.txt").resolve() not in by_path


async def test_public_config_under_data_root_denied(layout):
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    for parent in (layout.data_root, layout.data_root / "personal_drive", layout.install_root):
        (parent / "pyproject.toml").write_text("", encoding="utf-8")
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    granted = {rule.path for rule in profile.rules}
    assert (layout.data_root / "pyproject.toml").resolve() not in granted
    assert (layout.data_root / "personal_drive" / "pyproject.toml").resolve() not in granted
    # Touka 目錄（INSTALL_ROOT）頂層的公開檔名仍給唯讀
    assert (layout.install_root / "pyproject.toml").resolve() in granted


def test_public_config_under_state_root_or_protected_env_dir_denied(layout):
    state_anchor = layout.state_root / "proxy" / "agents" / "Alice" / "home" / "proj"
    env_anchor = layout.env_file.parent / "nested"
    for anchor in (state_anchor, env_anchor):
        anchor.mkdir(parents=True)
        (anchor.parent / "setup.cfg").write_text("", encoding="utf-8")
    rules = profiles.public_config_rules((state_anchor, env_anchor), layout)
    granted = {rule.path for rule in rules}
    assert (state_anchor.parent / "setup.cfg").resolve() not in granted
    assert (env_anchor.parent / "setup.cfg").resolve() not in granted


async def test_hardlinked_public_config_is_skipped(layout, allow_touka_check):
    workspace = layout.install_root / "workspace"
    project = workspace / "myproj"
    project.mkdir()
    secret = layout.env_file
    os.link(secret, workspace / "pytest.ini")
    (project / "tox.ini").write_text("", encoding="utf-8")
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    granted = {rule.path for rule in profile.rules}
    assert (workspace / "pytest.ini").resolve() not in granted
    assert (project / "tox.ini").resolve() not in granted  # 專案根本身不是「上層」
    assert project.resolve() in granted


async def test_symlinked_public_config_is_skipped(layout, allow_touka_check, monkeypatch):
    workspace = layout.install_root / "workspace"
    project = workspace / "myproj"
    project.mkdir()
    link = workspace / "pytest.ini"
    link.write_text("", encoding="utf-8")
    # 先 resolve：POSIX 的 resolve 本身也呼叫 os.lstat，在替身裡再 resolve 會無限遞迴
    resolved = link.resolve()
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):
        if Path(path) == resolved:
            return os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(profiles.os, "lstat", _lstat)
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    assert resolved not in {rule.path for rule in profile.rules}


def test_profile_defaults_to_pin_mode(layout):
    fields = profiles.launcher_spec_fields(
        profiles.SandboxProfile(name="x", cwd=layout.data_root, rules=(), block_ports=())
    )
    assert fields["pin"] is True


# --- 和啟動器的 spec 對齊（sandbox_kernel.spec 是 SSOT）------------------------------------


def _launcher_paths(fields: dict) -> dict:
    """Linux（含 CI）原樣驗；Windows 的路徑帶磁碟代號、不是 POSIX 形狀，只去掉代號再驗。"""
    if os.name != "nt":
        return fields
    rules = [
        {**rule, "path": "/" + "/".join(PureWindowsPath(rule["path"]).parts[1:])}
        for rule in fields["rules"]
    ]
    return {**fields, "rules": rules}


async def test_every_profile_spec_passes_launcher_validation(layout, allow_touka_check):
    """proxy 產生的每一種 profile 都要通過啟動器的 spec 驗證（新增 profile 時加進這裡）。"""
    workspace = layout.install_root / "workspace"
    project = workspace / "myproj"
    project.mkdir()
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    agent = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    assert any(rule.access == "ro" and rule.skip_symlink for rule in agent.rules)
    limited = replace(agent, rlimits={"cpu": 30, "fsize": 1 << 20, "as": 1 << 30})
    for profile in (agent, limited):
        fields = _launcher_paths(profiles.launcher_spec_fields(profile))
        parsed = kernel_spec.parse_spec(json.dumps(fields).encode("utf-8"))
        assert parsed.version == kernel_spec.SPEC_VERSION
        assert (parsed.profile, parsed.pin) == (profile.name, profile.pin)
        assert [(r.access, r.optional, r.skip_symlink) for r in parsed.rules] == [
            (r.access, r.optional, r.skip_symlink) for r in profile.rules
        ]
        assert parsed.block_ports == profile.block_ports
        assert dict(parsed.rlimits) == dict(profile.rlimits)


# --- 環境變數改寫 -----------------------------------------------------------------------


async def test_agent_profile_env_redirects_and_git_ceiling(layout, allow_touka_check):
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    project = layout.install_root / "workspace" / "myproj"
    project.mkdir()
    profile = await build_agent_command_profile(
        ALICE, agent_dir, layout=layout, project_root=project
    )
    home = (layout.state_root / "proxy" / "agents" / "Alice" / "home").resolve()
    tmp = (layout.state_root / "proxy" / "agents" / "Alice" / "tmp").resolve()
    env = profile.env_set
    assert env["HOME"] == str(home)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(tmp)
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory" and env["GIT_CONFIG_VALUE_0"] == "*"
    ceilings = env["GIT_CEILING_DIRECTORIES"].split(os.pathsep)
    assert str(project.resolve().parent) in ceilings
    assert str(agent_dir.resolve().parent) in ceilings
    assert {"HF_HOME", "UV_CACHE_DIR", "NPM_CONFIG_CACHE", "npm_config_cache"} <= profile.env_unset
    assert "PIP_CACHE_DIR" in profile.env_unset


@pytest.mark.parametrize("with_project", [False, True])
async def test_agent_profile_roots_pytest_at_the_project_or_cwd(
    layout, allow_touka_check, with_project
):
    """上層有授權到的公開設定檔時，pytest 以專案根（沒有就 cwd）當 rootdir／confcutdir。"""
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    project = layout.install_root / "workspace" / "my proj"
    project.mkdir()
    (layout.install_root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    profile = await build_agent_command_profile(
        ALICE, agent_dir, layout=layout, project_root=project if with_project else None
    )
    root = str((project if with_project else agent_dir).resolve())
    options = shlex.split(profile.env_set["PYTEST_ADDOPTS"])
    assert options == [f"--rootdir={root}", f"--confcutdir={root}"]


@pytest.mark.parametrize("upper", ["none", "cwd_only"])
async def test_agent_profile_leaves_pytest_alone_without_an_upper_public_config(
    layout, allow_touka_check, monkeypatch, upper
):
    """上層沒有授權到公開設定檔（Docker 的代理人資料夾）：不注入，子專案自己的 testpaths 照常。"""
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    project = layout.install_root / "workspace" / "myproj"
    project.mkdir()
    above_cwd = profiles.PathRule(agent_dir.resolve().parent / "pytest.ini", "ro", optional=True)

    def _rules_for(_anchors, _layout):
        return (above_cwd,) if upper == "cwd_only" else ()

    monkeypatch.setattr(profiles, "public_config_rules", _rules_for)
    for project_root in ((None, project) if upper == "none" else (project,)):
        profile = await build_agent_command_profile(
            ALICE, agent_dir, layout=layout, project_root=project_root
        )
        assert "PYTEST_ADDOPTS" not in profile.env_set, project_root


# --- 封鎖埠集合 B ----------------------------------------------------------------------


def test_blocked_ports_include_touka_and_docker_ports(monkeypatch):
    monkeypatch.setattr(settings, "PORT", 8100)
    monkeypatch.setattr(settings, "DISPATCH_PORT", 8183)
    monkeypatch.setattr(settings, "HEARTBEAT_EXTERNAL_URL", "http://touka-heartbeat:9082")
    monkeypatch.setattr(settings, "UI_DEFAULT_URL", "http://localhost:4001")
    monkeypatch.setattr(settings, "PROXY_PORT", "18000")
    monkeypatch.setattr(settings, "HEARTBEAT_PORT", "")
    monkeypatch.setattr(settings, "CHAT_PORT", "not-a-port")
    monkeypatch.setattr(settings, "SEARXNG_BASE_URL", "")
    ports = set(blocked_ports("proxy"))
    assert {8000, 8082, 8083, 3001, 2375, 2376, 8100, 8183, 9082, 4001, 18000} <= ports
    assert all(1 <= port <= 65535 for port in ports)


def test_url_without_explicit_port_does_not_block_80(monkeypatch):
    monkeypatch.setattr(settings, "UI_DEFAULT_URL", "http://localhost")
    monkeypatch.setattr(settings, "HEARTBEAT_EXTERNAL_URL", "https://hb.example")
    monkeypatch.setattr(settings, "SEARXNG_BASE_URL", "http://touka-searxng")
    ports = set(blocked_ports("proxy"))
    assert 80 not in ports and 443 not in ports


def test_searxng_port_only_blocked_inside_proxy(monkeypatch):
    monkeypatch.setattr(settings, "SEARXNG_BASE_URL", "http://touka-searxng:8080")
    assert 8080 in blocked_ports("proxy")
    assert 8080 not in blocked_ports("executor")


async def test_agent_profile_applies_blocked_ports(layout):
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout
    )
    assert profile.block_ports == blocked_ports("proxy")
    assert {8000, 2375} <= set(profile.block_ports)


# --- Touka 目錄偵測：設計 §3.4 的例子表（U1g）---------------------------------------------
#
# 版面照 Docker 開發版：/app 是 INSTALL_ROOT，/app/workspace（工作區掛載根 M）與
# /app/host_root（E，也是 M）都是同一份 repo checkout。

PROTECTED = "protected_location"
SCAN_LIMIT = "scan_limit"


def _touka_checkout(root: Path) -> None:
    """repo checkout 的形狀：已追蹤的頂層、gitignore 的名稱、點目錄、頂層檔案與 data。"""
    for sub in (
        "ToukaProxy/src", "ToukaMemory", "ToukaChat", "scripts", "docs", "tests", ".git",
        ".claude", ".toukadev", "backups", "heartbeat-config", "venv", "env", "data/soul/Alice",
        "data/legacy",
    ):  # fmt: skip
        (root / sub).mkdir(parents=True, exist_ok=True)
    for name in ("pytest.ini", "pyproject.toml", "heartbeat-config.json", "README.md", "notes.txt"):
        (root / name).write_text("", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")


def _user_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "main.py").write_text("", encoding="utf-8")
    (root / ".env").write_text("OWN=1\n", encoding="utf-8")  # 專案自己的 .env 不是標記
    return root


def _container_layout(tmp: Path, workspace_shape=_touka_checkout) -> SandboxLayout:
    app = tmp / "app"
    data = app / "data"
    for sub in ("personal_drive/Alice", "personal_drive/Bob", "soul/Alice", "legacy"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (data / "temp" / "docgen_output").mkdir(parents=True)
    for sub in (
        "ToukaProxy/config/skills", "ToukaMemory", "ToukaDispatch", "ToukaHeartbeat", "hf_cache",
        "ipc", ".venv/bin", "sandbox_state",
    ):  # fmt: skip
        (app / sub).mkdir(parents=True, exist_ok=True)
    host_root = app / "host_root"
    host_root.mkdir()
    _touka_checkout(host_root)
    workspace = app / "workspace"
    workspace.mkdir()
    workspace_shape(workspace)
    return SandboxLayout(
        data_root=data,
        config_dir=app / "ToukaProxy" / "config",
        install_root=app,
        proxy_root=app / "ToukaProxy",
        env_file=host_root / ".env",
        state_root=app / "sandbox_state",
        kernel_dir=tmp / "opt" / "touka-sandbox",
        executor_dir=None,
        mirror_roots=(host_root, workspace),
        in_container=True,
    )


def _docker_dev_workspace(workspace: Path) -> None:
    _touka_checkout(workspace)
    _user_project(workspace / "myproj")


def _drive_workspace(workspace: Path) -> None:
    """WORKSPACE_ROOT=D:/：工作區是整個磁碟，Touka checkout 在 `Touka/`。"""
    _touka_checkout(workspace / "Touka")
    _user_project(workspace / "Touka" / "myproj")
    _user_project(workspace / "Other" / "a" / "b")
    _touka_checkout(workspace / "Other2" / "a" / "b" / "clone")  # 3 層內藏著 Touka checkout


def _ghcr_workspace(workspace: Path) -> None:
    (workspace / "docker-compose.ghcr.yml").write_text("", encoding="utf-8")
    (workspace / "heartbeat-config").mkdir()
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _user_project(workspace / "projects" / "x")


async def _verdict(layout: SandboxLayout, relative: str) -> str | None:
    return await authorize_derived_path(layout.install_root / relative, layout)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("data/personal_drive", PROTECTED),  # G 只給代理人自己的資料夾（要有身分）
        ("data/temp/docgen_output", PROTECTED),  # 共用輸出夾不給代理人指令
        ("data", PROTECTED),
        ("data/soul", PROTECTED),
        ("workspace", PROTECTED),  # 鏡像根本身不能當專案根
        ("workspace/myproj", None),  # 使用者自建的頂層資料夾（自己有 .env 也可以）
        ("workspace/myproj/src", None),
        ("workspace/ToukaChat", PROTECTED),
        ("workspace/scripts", PROTECTED),
        ("workspace/docs", PROTECTED),
        ("workspace/backups", PROTECTED),
        ("workspace/.git", PROTECTED),
        ("workspace/.claude", PROTECTED),
        ("workspace/.toukadev", PROTECTED),
        ("workspace/heartbeat-config", PROTECTED),
        ("workspace/venv", PROTECTED),
        ("workspace/env", PROTECTED),
        ("workspace/data/soul", PROTECTED),
        ("workspace/.env", PROTECTED),
        ("workspace/heartbeat-config.json", PROTECTED),  # 頂層檔案
        ("workspace/pytest.ini", PROTECTED),  # 公開檔名只經 R 給唯讀，不能當推導路徑
        ("workspace/notes.txt", PROTECTED),  # 使用者放的頂層檔案也不授權
        ("host_root", PROTECTED),
        ("host_root/myproj", PROTECTED),  # E 整個受保護
        ("host_root/.env", PROTECTED),
        ("sandbox_state", PROTECTED),  # ST 根
        ("sandbox_state/proxy/agents/Bob", PROTECTED),
        ("sandbox_state/proxy/agents/Alice/home", PROTECTED),  # 只有 Touka 自選時才給
        ("hf_cache", PROTECTED),  # 容器 /app 的子項目
        ("ipc", PROTECTED),
        (".venv", PROTECTED),
        (".", PROTECTED),
    ],
)
async def test_example_table_docker_dev(tmp_path, relative, expected):
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    assert await _verdict(layout, relative) == expected


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("workspace", PROTECTED),  # 鏡像根本身
        ("workspace/Touka", PROTECTED),  # 本身有 ToukaProxy/
        ("workspace/Touka/myproj", None),  # 上層是 Touka 目錄，myproj 是使用者自建
        ("workspace/Touka/data/soul", PROTECTED),  # 中間那層 Touka 也要看（不能只看 M）
        ("workspace/Touka/scripts", PROTECTED),
        ("workspace/Touka/.git", PROTECTED),
        ("workspace/Other", None),  # 往下沒有標記
        ("workspace/Other2", PROTECTED),  # 3 層內有 Touka checkout
        ("workspace/Other2/a", PROTECTED),
    ],
)
async def test_example_table_workspace_root_is_a_drive(tmp_path, relative, expected):
    layout = _container_layout(tmp_path, _drive_workspace)
    assert await _verdict(layout, relative) == expected


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("workspace/projects/x", None),
        ("workspace/projects", None),
        ("workspace/heartbeat-config", PROTECTED),
        ("workspace/.env", PROTECTED),
        ("workspace/docker-compose.ghcr.yml", PROTECTED),
    ],
)
async def test_example_table_ghcr_compose_dir(tmp_path, relative, expected):
    layout = _container_layout(tmp_path, _ghcr_workspace)
    assert await _verdict(layout, relative) == expected


async def test_plain_workspace_root_is_never_a_project_root(tmp_path):
    """工作區只放使用者的專案（不是 Touka 目錄、往下也沒有標記）時，M 本身仍不能當專案根。"""
    layout = _container_layout(tmp_path, lambda ws: _user_project(ws / "proj"))
    assert await _verdict(layout, "workspace") == PROTECTED
    assert await _verdict(layout, "workspace/proj") is None


def _case_variant_workspace(workspace: Path) -> None:
    """只有大小寫變體的 checkout：9p 與 NTFS 不分大小寫，比對一律 casefold。"""
    variants = ("TOUKAPROXY", "DATA/soul", "BACKUPS", ".GIT", "Scripts", "HEARTBEAT-CONFIG", "Venv")
    for sub in variants:
        (workspace / sub).mkdir(parents=True)
    (workspace / "HeartBeat-Config.JSON").write_text("", encoding="utf-8")
    _user_project(workspace / "MyProj")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("workspace/DATA", PROTECTED),
        ("workspace/DATA/soul", PROTECTED),
        ("workspace/BACKUPS", PROTECTED),
        ("workspace/.GIT", PROTECTED),
        ("workspace/Scripts", PROTECTED),
        ("workspace/HEARTBEAT-CONFIG", PROTECTED),
        ("workspace/Venv", PROTECTED),
        ("workspace/HeartBeat-Config.JSON", PROTECTED),
        ("workspace/MyProj", None),
    ],
)
async def test_case_variants_of_protected_names_are_denied(tmp_path, relative, expected):
    layout = _container_layout(tmp_path, _case_variant_workspace)
    assert await _verdict(layout, relative) == expected


def _counter_example_workspace(workspace: Path) -> None:
    _touka_checkout(workspace)
    _touka_checkout(workspace / "clone")  # 使用者自己 clone 的另一份 Touka
    _user_project(workspace / "clone" / "myproj")
    for name in ("soul", "legacy"):  # 資料根形狀的複本
        (workspace / "stuff" / "datacopy" / name).mkdir(parents=True)
    _user_project(workspace / "stuff" / "datacopy" / "x")
    for name in ("soul", "legacy"):
        (workspace / "proj2" / "backup" / name).mkdir(parents=True)
    (workspace / "proj3" / "sub").mkdir(parents=True)
    (workspace / "proj3" / "sub" / "docker-compose.ghcr.yml").write_text("", encoding="utf-8")
    (workspace / "proj4" / "a" / "b" / "toukaproxy").mkdir(parents=True)  # 小寫標記
    (workspace / "proj5" / "a" / "b" / "c" / "ToukaMemory").mkdir(parents=True)  # 第 3 層
    _user_project(workspace / "proj6")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("workspace/clone", PROTECTED),  # 本身是 Touka 目錄
        ("workspace/clone/myproj", PROTECTED),  # Touka 目錄底下的 Touka 子目錄整個受保護
        ("workspace/stuff/datacopy/x", PROTECTED),  # 上層是資料根形狀：沒有例外
        ("workspace/stuff", PROTECTED),  # 往下找到資料根形狀
        ("workspace/proj2", PROTECTED),
        ("workspace/proj3", PROTECTED),  # 標記也可以是檔案（GHCR compose 檔）
        ("workspace/proj4", PROTECTED),
        ("workspace/proj5", PROTECTED),
        ("workspace/proj6", None),
    ],
)
async def test_counter_examples_from_the_fourth_revision(tmp_path, relative, expected):
    layout = _container_layout(tmp_path, _counter_example_workspace)
    assert await _verdict(layout, relative) == expected


async def test_native_repo_root_env_dir_seals_every_child(tmp_path):
    """原生：E 就是 repo 根（INSTALL_ROOT），所有子項目都受保護，沒有例外。"""
    repo = tmp_path / "repo"
    _touka_checkout(repo)
    _user_project(repo / "myproj")
    native = SandboxLayout(
        data_root=repo / "data",
        config_dir=repo / "ToukaProxy" / "config",
        install_root=repo,
        proxy_root=repo / "ToukaProxy",
        env_file=repo / ".env",
        state_root=tmp_path / "st",
        kernel_dir=repo / "ToukaProxy" / "src" / "sandbox_kernel",
        executor_dir=None,
        mirror_roots=(repo,),
        in_container=False,
    )
    assert await authorize_derived_path(repo / "myproj", native) == PROTECTED
    outside = _user_project(tmp_path / "elsewhere" / "proj")
    assert await authorize_derived_path(outside, native) is None


async def test_missing_project_root_is_not_granted(tmp_path):
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    assert await _verdict(layout, "workspace/gone") == PROTECTED


async def test_container_profile_rows_cwd_state_and_public_configs(tmp_path, monkeypatch):
    """例子表的 profile 列：cwd、ST 自己的子資料夾、R 的公開檔名與 D 底下的設定檔。"""
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    match_state_root(monkeypatch, layout.state_root)
    app = layout.install_root
    (layout.data_root / "pyproject.toml").write_text("", encoding="utf-8")
    agent_dir = layout.data_root / "personal_drive" / "Alice"
    profile = await build_agent_command_profile(
        ALICE, agent_dir, layout=layout, project_root=app / "workspace" / "myproj"
    )
    rules = _rules(profile)
    assert rules[agent_dir.resolve()] == "rwx"
    assert rules[(app / "workspace" / "myproj").resolve()] == "rwx"
    own_state = app / "sandbox_state" / "proxy" / "agents" / "Alice"
    for own in ("home", "tmp"):
        assert rules[(own_state / own).resolve()] == "rwx"
    assert not [p for p in rules if "Bob" in p.parts]
    assert rules[(app / "workspace" / "pytest.ini").resolve()] == "ro"
    assert rules[(app / "workspace" / "pyproject.toml").resolve()] == "ro"
    assert (app / "workspace" / ".env").resolve() not in rules
    assert (app / "workspace" / "heartbeat-config.json").resolve() not in rules
    assert (layout.data_root / "pyproject.toml").resolve() not in rules
    assert (app / "pytest.ini").resolve() not in rules  # 映像已移除 /app 根目錄的設定檔
    assert not profile.ungranted


async def test_container_top_level_public_config_is_read_only_when_present(tmp_path, monkeypatch):
    """九個公開檔名是檔案級例外，優先於「容器 INSTALL_ROOT 的子項目全保護」（D、E、ST 除外）。"""
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    match_state_root(monkeypatch, layout.state_root)
    (layout.install_root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout
    )
    assert _rules(profile)[(layout.install_root / "pytest.ini").resolve()] == "ro"
    assert await _verdict(layout, "pytest.ini") == PROTECTED


async def test_denied_project_is_ungranted_with_the_reason(tmp_path, monkeypatch):
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    match_state_root(monkeypatch, layout.state_root)
    profile = await build_agent_command_profile(
        ALICE,
        layout.data_root / "personal_drive" / "Alice",
        layout=layout,
        project_root=layout.install_root / "workspace" / "scripts",
    )
    assert profile.ungranted == (PROTECTED,)
    assert (layout.install_root / "workspace" / "scripts").resolve() not in _rules(profile)


async def test_project_too_large_to_scan_is_scan_limit(tmp_path, monkeypatch):
    """例子表的 workspace/big：掃不完就不授權，原因是 scan_limit。"""
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    big = layout.install_root / "workspace" / "big"
    for index in range(12):
        (big / f"d{index}" / "sub").mkdir(parents=True)
    small = project_scan.ProjectScanner(budget=project_scan.ScanBudget(max_dirs=8))
    monkeypatch.setattr(profiles, "project_scanner", small)
    assert await authorize_derived_path(big, layout) == SCAN_LIMIT
    assert await _verdict(layout, "workspace/myproj") is None


def _dir_link(link: Path, target: Path) -> None:
    """建資料夾連結：符號連結；Windows 沒有權限時改用 junction；都不行就跳過。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if _winapi is None:
            pytest.skip("cannot create directory links here")
        _winapi.CreateJunction(str(target), str(link))


async def test_scan_does_not_follow_links_and_the_link_itself_resolves_to_its_target(tmp_path):
    """專案裡指向 host_root 的連結：掃描不跟；連結本身先 resolve，落在 E 就拒絕。"""
    layout = _container_layout(tmp_path, _docker_dev_workspace)
    project = layout.install_root / "workspace" / "linked"
    _user_project(project)
    _dir_link(project / "link", layout.install_root / "host_root")
    assert await authorize_derived_path(project, layout) is None
    assert await authorize_derived_path(project / "link", layout) == PROTECTED


# --- 執行器位置（C2-2 U2e；START 的規則見 U2c 的 executor_session.check_start）-------------------


def _rwx(profile) -> set[Path]:
    return {rule.path for rule in profile.rules if rule.access == "rwx"}


async def test_executor_agent_profile_writes_only_its_drive_and_executor_state(layout):
    """HOME／TMP 在 READY 的 state_root（執行器那邊的路徑）；proxy 不建資料夾、不 resolve."""
    state = layout.install_root / "exec_state"
    drive = layout.data_root / "personal_drive" / "Alice"
    profile = await build_agent_command_profile(
        ALICE, drive, layout=layout, location="executor", executor_state=str(state),
        block_ports=(8000, 2375),
    )
    base = state / "agents" / "Alice"
    assert _rwx(profile) == {drive.resolve(), base / "home", base / "tmp"}
    assert (profile.cwd, profile.owner) == (drive.resolve(), "Alice")
    assert profile.block_ports == (8000, 2375)
    assert profile.env_set["HOME"] == str(base / "home")
    assert profile.env_set["TMPDIR"] == str(base / "tmp")
    assert not state.exists()  # 執行器自己建（伺服器 prepare），proxy 不碰
    assert not (layout.state_root / "executor").exists()


async def test_executor_profile_drops_public_config_rules_and_projects(layout):
    """執行器只收系統路徑、唯讀 root 與目標／HOME／TMP：上層的公開設定檔規則拿掉；不掛專案."""
    (layout.install_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    drive = layout.data_root / "personal_drive" / "Alice"
    proxy = await build_agent_command_profile(ALICE, drive, layout=layout)
    assert (layout.install_root / "pyproject.toml").resolve() in _rules(proxy)  # proxy 照舊
    state = str(layout.install_root / "exec_state")
    executor = await build_agent_command_profile(
        ALICE, drive, layout=layout, location="executor", executor_state=state
    )
    assert not any(rule.path.name in kernel_spec.PUBLIC_CONFIG_NAMES for rule in executor.rules)
    assert "PYTEST_ADDOPTS" not in executor.env_set
    with pytest.raises(ProfileError):
        await build_agent_command_profile(
            ALICE, drive, layout=layout, location="executor", executor_state=state,
            project_root=layout.install_root / "workspace",
        )


async def test_executor_profile_needs_the_ready_state_root(layout):
    with pytest.raises(ProfileError):
        await build_agent_command_profile(
            ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout,
            location="executor",
        )


def test_executor_evolution_profile_writes_only_its_skill_and_executor_state(layout):
    skill = "probe_skill"
    root = layout.data_root / "mcp_servers" / "managed" / skill
    root.mkdir(parents=True)
    (layout.install_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    state = layout.install_root / "exec_state"
    profile = profiles.build_evolution_profile(
        skill, root, layout=layout, location="executor", executor_state=str(state)
    )
    base = state / "evolution" / skill
    assert _rwx(profile) == {root.resolve(), base / "home", base / "tmp"}
    assert (profile.cwd, profile.owner) == (root.resolve(), skill)
    assert not any(rule.path.name in kernel_spec.PUBLIC_CONFIG_NAMES for rule in profile.rules)
    assert not state.exists()
