"""MCP 伺服器的隔離 profile（C2-3 U3c）：段名、自建、第三方與 scaffold.

- 段名一律由 Touka 從伺服器名算（類別標籤＋sha256），代理人給的字串不可能等於別台或別類的段名；
  自建與第三方分開父目錄。
- 自建只有自己的 state 與（演化部署的）程式資料夾唯讀＋執行；不收 ``sandbox_paths``。
- 第三方引用的路徑逐一過永不授權（``authorize_derived_path``），APPDATA 對映到 personal_drive 根。
全部用 tmp_path 建的假版面。
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from src.services.isolation import mcp_profiles, profiles
from src.services.isolation.mcp_profiles import (
    HOME_OR_ROOT,
    MCP_SCAFFOLD_PROFILE,
    MCP_SELF_PROFILE,
    MCP_THIRD_PARTY_PROFILE,
    SANDBOX_PATH_INVALID,
    SANDBOX_PATHS_OVERFLOW,
    SCAFFOLD_DIR_MISMATCH,
    build_mcp_self_profile,
    build_mcp_third_party_profile,
    state_segment,
)
from src.services.isolation.profiles import ProfileError, SandboxLayout
from src.utils.isolation_policy import MAX_MCP_SANDBOX_PATHS
from src.utils.mcp_origin import NO_RECORD, McpClassification, OriginRecord, classify
from src.utils.touka_dirs import PROTECTED_LOCATION

_winapi = importlib.import_module("_winapi") if os.name == "nt" else None


@dataclass(frozen=True)
class Entry:
    """server_config 條目的四個欄位（``ProtocolServerConfig`` 的形狀）。"""

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str | None = None


def _layout(root: Path) -> SandboxLayout:
    install = root.resolve() / "install"
    data = install / "data"
    for sub in (
        "personal_drive/Alice",
        "custom/Bob/drive",
        "mcp_servers/managed/foo",
        "mcp_servers/managed/bar",
        "temp/docgen_output",
        "soul/Alice",
    ):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("ToukaProxy/config/skills", "workspace/myproj", "host_root", "st", "tools/bin"):
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
        in_container=True,
    )


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    return _layout(tmp_path)


async def test_relative_launch_paths_are_only_resolved_for_profile_rules(layout, monkeypatch):
    seen = []

    async def deny(path, _layout, owner=None):
        seen.append(path)
        return PROTECTED_LOCATION

    monkeypatch.setattr(mcp_profiles, "authorize_derived_path", deny)
    entry = Entry(command="./tools/python", args=("../external/server.py", "--index=../wheels"))
    result = await build_mcp_third_party_profile(
        "relative", entry, McpClassification("third_party", "manual", "origin"),
        layout=layout, block_ports=(),
    )
    assert result.command == entry.command and result.args == entry.args
    assert result.profile.cwd.parent / "external" in seen
    assert result.profile.cwd.parent / "wheels" in seen
    assert result.profile.cwd != layout.proxy_root


def _classify(layout: SandboxLayout, entry: Entry, record: OriginRecord, name: str = "srv"):
    return classify(name, entry, record, layout, base_dir=layout.proxy_root)


def _own_rules(profile) -> dict[Path, str]:
    """系統路徑以外的規則（路徑 → 權利）。"""
    system = {rule.path for rule in profiles.system_rules()}
    return {rule.path: rule.access for rule in profile.rules if rule.path not in system}


def _managed(layout: SandboxLayout, skill: str, *parts: str) -> Path:
    return layout.data_root / "mcp_servers" / "managed" / skill / Path(*parts)


SELF = McpClassification("self", "creation", "origin")


# --- 段名 --------------------------------------------------------------------------------


def test_segment_is_a_fixed_shape_hash_of_category_and_name():
    seg = state_segment("self", "github")
    assert re.fullmatch(r"m[0-9a-f]{24}", seg)
    assert seg == state_segment("self", "github")
    assert seg != state_segment("third", "github")
    assert seg != state_segment("self", "github2")
    assert "github" not in seg


@pytest.mark.parametrize("name", ["../x", "a/b", "", " ", "名稱", "\ud800", "mcp-0123456789abcdef"])
def test_any_name_gives_a_safe_segment(name):
    assert re.fullmatch(r"m[0-9a-f]{24}", state_segment("self", name))


def test_a_name_shaped_like_another_segment_gets_its_own_state(layout):
    """HIGH-1：skill_id 取別台（別類）的段名字串，state 路徑仍不同。"""
    third_seg = state_segment("third", "notion")
    victim = layout.state_root / "proxy" / "mcp_third" / third_seg / "home"
    for name in (third_seg, "mcp-0123456789abcdef", state_segment("self", "other")):
        profile = build_mcp_self_profile(name, SELF, layout=layout)
        assert profile.cwd != victim
        assert profile.cwd.parent.name == state_segment("self", name)
        assert profile.cwd.parent.name != name


def test_self_state_lives_under_its_own_parent(layout):
    profile = build_mcp_self_profile("github", SELF, layout=layout)
    seg = state_segment("self", "github")
    assert profile.cwd == layout.state_root / "proxy" / "mcp_self" / seg / "home"
    assert profile.owner == seg


# --- 自建 profile -------------------------------------------------------------------------


def test_self_profile_has_only_its_own_state(layout):
    profile = build_mcp_self_profile("srv", SELF, layout=layout)
    seg = state_segment("self", "srv")
    base = layout.state_root / "proxy" / "mcp_self" / seg
    assert profile.name == MCP_SELF_PROFILE
    assert _own_rules(profile) == {base / "home": "rwx", base / "tmp": "rwx"}
    assert (base / "home").is_dir() and (base / "tmp").is_dir()
    assert profile.cwd == base / "home"
    assert profile.env_set["HOME"] == str(base / "home")
    assert profile.env_set["TMPDIR"] == str(base / "tmp")
    assert profile.ungranted == ()


def test_self_profile_does_not_take_sandbox_paths():
    """自建條目磁碟上有 ``sandbox_paths`` 也不採用：建 profile 的函式根本不收。"""
    assert "sandbox_paths" not in inspect.signature(build_mcp_self_profile).parameters


def test_evolution_deployment_gets_its_code_folder_read_only(layout):
    """部署後的程式資料夾唯讀＋執行：伺服器改不了自己的程式。"""
    entry = Entry(
        command=str(_managed(layout, "foo", ".venv", "bin", "python")),
        args=(str(_managed(layout, "foo", "server.py")),),
    )
    result = _classify(layout, entry, OriginRecord("present", "evolution"), name="foo")
    profile = build_mcp_self_profile("foo", result, layout=layout)
    own = _own_rules(profile)
    assert own[_managed(layout, "foo")] == "rox"
    writable = [path for path, access in own.items() if access == "rwx"]
    assert not any(path.is_relative_to(layout.data_root) for path in writable)


def test_code_folder_is_the_one_the_entry_references_not_the_name(layout):
    """r2 LOW-N8：rox 路徑正好是條目指令引用的那個 managed/<skill>，改名的條目也一樣。"""
    entry = Entry(command="python", args=(str(_managed(layout, "bar", "server.py")),))
    result = _classify(layout, entry, NO_RECORD, name="foo")
    profile = build_mcp_self_profile("foo", result, layout=layout)
    rox = {path for path, access in _own_rules(profile).items() if access == "rox"}
    assert rox == {_managed(layout, "bar")}


def test_self_entry_without_managed_code_gets_no_code_folder(layout):
    entry = Entry(command="npx", args=("-y", "pkg"))
    result = _classify(layout, entry, OriginRecord("present", "creation"))
    own = _own_rules(build_mcp_self_profile("srv", result, layout=layout))
    assert "rox" not in own.values()


def test_two_referenced_code_folders_get_neither(layout):
    entry = Entry(
        command=str(_managed(layout, "foo", ".venv", "bin", "python")),
        args=(str(_managed(layout, "bar", "server.py")),),
    )
    result = _classify(layout, entry, OriginRecord("present", "evolution"))
    profile = build_mcp_self_profile("srv", result, layout=layout)
    assert not any(a == "rox" for a in _own_rules(profile).values())
    assert profile.ungranted == (mcp_profiles.CODE_DIR_REFUSED,)


def test_self_profile_gives_nothing_of_the_agents_or_touka(layout):
    entry = Entry(
        command="python",
        args=(str(layout.data_root / "personal_drive" / "Alice" / "server.py"),),
        env={"PYTHONPATH": str(layout.install_root / "workspace" / "myproj")},
    )
    result = _classify(layout, entry, NO_RECORD)
    assert result.kind == "self"
    own = _own_rules(build_mcp_self_profile("srv", result, layout=layout))
    for path in own:
        assert path.is_relative_to(layout.state_root / "proxy" / "mcp_self")
    assert layout.config_dir / "skills" not in own


@pytest.mark.parametrize("kind", ["third_party", "scaffold", "remote"])
def test_self_profile_refuses_other_classes(layout, kind):
    with pytest.raises(ProfileError):
        build_mcp_self_profile("srv", McpClassification(kind, None, "x"), layout=layout)


def test_self_profile_at_the_executor_uses_the_executor_state(layout, tmp_path):
    executor_state = tmp_path / "executor_state"
    profile = build_mcp_self_profile(
        "srv", SELF, layout=layout, location="executor", executor_state=str(executor_state)
    )
    seg = state_segment("self", "srv")
    base = executor_state / "mcp" / seg
    assert _own_rules(profile) == {base / "home": "rwx", base / "tmp": "rwx"}
    assert profile.cwd == base / "home" and profile.owner == seg
    assert not executor_state.exists()  # 執行器端由伺服器建，proxy 不建
    assert not (layout.state_root / "proxy" / "mcp_self").exists()


def test_self_profile_at_the_executor_needs_its_state(layout):
    with pytest.raises(ProfileError):
        build_mcp_self_profile("srv", SELF, layout=layout, location="executor")


# --- 第三方與 scaffold --------------------------------------------------------------------

THIRD = McpClassification("third_party", "hub", "origin")


@pytest.mark.parametrize("command,args", [
    ("uvx", ("pkg==1.2.0",)), ("python", ("-m", "package.module")),
    ("npx", ("package.name",)),
])
async def test_plain_package_and_module_names_do_not_request_path_grants(
    layout, monkeypatch, command, args,
):
    authorize = []

    async def record(path, _layout, owner=None):
        authorize.append(path)
        return PROTECTED_LOCATION

    monkeypatch.setattr(mcp_profiles, "authorize_derived_path", record)
    launch = await _third(layout, Entry(command=command, args=args))
    assert not authorize and not launch.profile.ungranted
    assert (launch.command, launch.args) == (command, args)


@dataclass(frozen=True)
class Grant:
    """人設的「額外允許的資料夾」一筆（U3f 的模型形狀）。"""

    path: str
    access: str


async def _third(layout: SandboxLayout, entry: Entry, classification=THIRD, name="srv", **kw):
    return await build_mcp_third_party_profile(
        name, entry, classification, layout=layout, **kw
    )


def _outside_tool(layout: SandboxLayout) -> Path:
    """Touka 版面以外、使用者自己裝的工具（第三方引用的檔案）。"""
    script = layout.install_root.parent / "tools" / "bin" / "server.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("", encoding="utf-8")
    return script


def _dir_link(link: Path, target: Path) -> None:
    """建資料夾連結：符號連結；Windows 沒有權限時改用 junction；都不行就跳過。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if _winapi is None:
            pytest.skip("cannot create directory links here")
        _winapi.CreateJunction(str(target), str(link))


async def test_third_party_state_cwd_and_name(layout):
    launch = await _third(layout, Entry(command="npx", args=("-y", "pkg")))
    seg = state_segment("third", "srv")
    base = layout.state_root / "proxy" / "mcp_third" / seg
    profile = launch.profile
    assert profile.name == MCP_THIRD_PARTY_PROFILE
    assert profile.cwd == base / "home" and profile.owner == seg
    assert _own_rules(profile) == {base / "home": "rwx", base / "tmp": "rwx"}
    assert profile.env_set["HOME"] == str(base / "home")
    assert launch.args == ("-y", "pkg") and dict(launch.env) == {}


async def test_self_and_third_state_never_share_a_folder(layout):
    """同名的自建與第三方：不同父目錄、不同段名。"""
    third = (await _third(layout, Entry(command="npx"), name="github")).profile
    own = build_mcp_self_profile("github", SELF, layout=layout)
    assert third.cwd.parent.parent.name == "mcp_third"
    assert own.cwd.parent.parent.name == "mcp_self"
    assert third.cwd.parent.name != own.cwd.parent.name


async def test_referenced_folder_is_read_write_and_file_folder_is_read_execute(layout):
    project = layout.install_root / "workspace" / "myproj"
    script = _outside_tool(layout)
    entry = Entry(command="node", args=(str(script), "--repository", str(project)))
    launch = await _third(layout, entry)
    own = _own_rules(launch.profile)
    assert own[project] == "rwx"
    assert own[script.parent] == "rox"
    assert launch.profile.ungranted == ()


@pytest.mark.parametrize(
    "where",
    [
        "data/soul/Alice",
        "data/custom/Bob/drive",
        "ToukaProxy/config",
        "ToukaProxy",
        "host_root",
        "st",
    ],
)
async def test_referenced_protected_paths_are_never_granted(layout, where):
    target = layout.install_root / where
    target.mkdir(parents=True, exist_ok=True)
    launch = await _third(layout, Entry(command="npx", args=("-y", "fs", str(target))))
    assert target not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (PROTECTED_LOCATION,)


async def test_every_reference_goes_through_the_never_grant_rules(layout, monkeypatch):
    """第三方引用的每條路徑都過 ``authorize_derived_path(owner=None)``；被拒就不給。"""
    seen: list[tuple[Path, str | None]] = []

    async def _deny(path: Path, _layout: SandboxLayout, owner: str | None = None) -> str:
        seen.append((path, owner))
        return PROTECTED_LOCATION

    monkeypatch.setattr(mcp_profiles, "authorize_derived_path", _deny)
    project = layout.install_root / "workspace" / "myproj"
    script = _outside_tool(layout)
    entry = Entry(command="node", args=(str(script),), env={"REPO": str(project)})
    launch = await _third(layout, entry, sandbox_paths=[Grant(str(project), "ro")])
    assert {path for path, _ in seen} == {script.parent, project}
    assert {owner for _, owner in seen} == {None}
    assert _own_rules(launch.profile) == {
        p: a for p, a in _own_rules(launch.profile).items() if p.parent.parent.name == "mcp_third"
    }


async def test_link_reference_is_not_granted(layout, tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = layout.install_root / "workspace" / "linked"
    _dir_link(link, target)
    launch = await _third(layout, Entry(command="npx", args=("-y", "fs", str(link))))
    assert link not in _own_rules(launch.profile) and target not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (PROTECTED_LOCATION,)


@pytest.mark.parametrize("value", ["@TOUKA_APPDATA@", "data_root"])
async def test_appdata_maps_to_the_personal_drive_root(layout, value):
    """哨兵與「等於 data 根的絕對路徑」兩種寫法都改成 personal_drive 根（不給整個 data 根）。"""
    raw = str(layout.data_root) if value == "data_root" else value
    entry = Entry(command="npx", args=("-y", "server-filesystem", raw), env={"INPUT": raw})
    launch = await _third(layout, entry)
    drive = layout.data_root / "personal_drive"
    assert launch.args == ("-y", "server-filesystem", str(drive))
    assert dict(launch.env) == {"INPUT": str(drive)}
    own = _own_rules(launch.profile)
    assert own[drive] == "rwx" and layout.data_root not in own


async def test_docgen_output_is_read_write(layout):
    """docgen 的 OUTPUT、Figma 的 IMAGE_DIR：哨兵與絕對路徑都給 docgen_output 讀寫。"""
    output = layout.data_root / "temp" / "docgen_output"
    for value in ("@TOUKA_OUTPUT@", str(output)):
        entry = Entry(command="uvx", args=("mcp-docgen",), env={"OUT": value})
        launch = await _third(layout, entry)
        assert dict(launch.env) == {"OUT": str(output)}
        assert _own_rules(launch.profile)[output] == "rwx"


async def test_a_folder_inside_the_personal_drive_root_is_read_write(layout):
    drive = layout.data_root / "personal_drive" / "Alice"
    launch = await _third(layout, Entry(command="npx", args=("-y", "fs", str(drive))))
    assert _own_rules(launch.profile)[drive] == "rwx"


async def test_personal_drive_folder_reached_through_a_link_is_not_granted(layout, tmp_path):
    """personal_drive 底下經過連結的資料夾：resolve 前後不同就不給（連結可以被改指向）。"""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sub").mkdir(parents=True)
    via = layout.data_root / "personal_drive" / "Alice" / "via"
    _dir_link(via, elsewhere)
    launch = await _third(layout, Entry(command="npx", args=("-y", "fs", str(via / "sub"))))
    own = _own_rules(launch.profile)
    assert via / "sub" not in own and elsewhere / "sub" not in own
    assert launch.profile.ungranted == (PROTECTED_LOCATION,)


async def test_other_data_paths_are_not_mapped(layout):
    soul = layout.data_root / "soul"
    launch = await _third(layout, Entry(command="npx", args=("-y", "fs", str(soul))))
    assert launch.args == ("-y", "fs", str(soul))
    assert soul not in _own_rules(launch.profile)


async def test_file_in_the_home_folder_does_not_grant_the_home(layout, monkeypatch):
    """LOW 16：引用檔案的所在資料夾是家目錄時不給，要人另加更窄的資料夾。"""
    home = layout.install_root.parent / "home" / "bob"
    home.mkdir(parents=True)
    (home / "server.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(mcp_profiles, "user_home", lambda: home)
    launch = await _third(layout, Entry(command="python", args=(str(home / "server.py"),)))
    assert home not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (HOME_OR_ROOT,)


async def test_file_in_any_users_home_or_root_home_is_not_granted(layout, monkeypatch):
    homes = layout.install_root.parent / "home"
    other = homes / "carol"
    other.mkdir(parents=True)
    (other / "server.py").write_text("", encoding="utf-8")
    root_home = layout.install_root.parent / "roothome"
    root_home.mkdir()
    (root_home / "server.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(mcp_profiles, "HOME_DIR_PARENTS", (homes,))
    monkeypatch.setattr(mcp_profiles, "ROOT_HOME_DIRS", (root_home,))
    for script in (other / "server.py", root_home / "server.py"):
        launch = await _third(layout, Entry(command="python", args=(str(script),)))
        assert launch.profile.ungranted == (HOME_OR_ROOT,)


async def test_unknown_home_directory_does_not_break_the_profile(layout, monkeypatch):
    """容器使用者沒有 HOME 也沒有 passwd 項目時 ``Path.home()`` 會拋錯：照樣組得出 profile。"""

    def _no_home(_cls):
        raise RuntimeError

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    script = _outside_tool(layout)
    launch = await _third(layout, Entry(command="node", args=(str(script),)))
    assert _own_rules(launch.profile)[script.parent] == "rox"


async def test_file_at_the_filesystem_root_is_not_granted(layout):
    script = Path(layout.install_root.anchor) / "server.py"
    launch = await _third(layout, Entry(command="python", args=(str(script),)))
    assert Path(layout.install_root.anchor) not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (HOME_OR_ROOT,)


# --- sandbox_paths：啟動時逐筆重驗 ----------------------------------------------------------


async def test_sandbox_paths_grant_read_only_or_read_write(layout, tmp_path):
    repo = layout.install_root / "workspace" / "myproj"
    docs = tmp_path / "docs"
    docs.mkdir()
    grants = [Grant(str(repo), "rw"), Grant(str(docs), "ro")]
    entry = Entry(command="uvx", args=("mcp-server-git",))
    launch = await _third(layout, entry, sandbox_paths=grants)
    own = _own_rules(launch.profile)
    assert own[repo] == "rwx" and own[docs] == "ro"


@pytest.mark.parametrize(
    "grant",
    [Grant("relative/dir", "ro"), Grant("ABS", "rx"), Grant("ABS", ""), Grant("ABS\x00x", "ro")],
)
async def test_malformed_sandbox_paths_are_not_granted(layout, grant):
    repo = str(layout.install_root / "workspace" / "myproj")
    fixed = Grant(grant.path.replace("ABS", repo), grant.access)
    launch = await _third(layout, Entry(command="uvx"), sandbox_paths=[fixed])
    assert launch.profile.ungranted == (SANDBOX_PATH_INVALID,)
    assert _own_rules(launch.profile) == {
        p: a for p, a in _own_rules(launch.profile).items() if p.parent.parent.name == "mcp_third"
    }


async def test_protected_sandbox_path_is_not_granted(layout):
    soul = layout.data_root / "soul"
    launch = await _third(layout, Entry(command="uvx"), sandbox_paths=[Grant(str(soul), "rw")])
    assert soul not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (PROTECTED_LOCATION,)


async def test_sandbox_path_that_became_a_link_is_not_granted(layout, tmp_path):
    """存檔時通過、啟動前被換成連結：啟動時重驗就不給。"""
    folder = layout.install_root / "workspace" / "shared"
    folder.mkdir()
    grants = [Grant(str(folder), "rw")]
    first = await _third(layout, Entry(command="uvx"), sandbox_paths=grants)
    assert _own_rules(first.profile)[folder] == "rwx"
    folder.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _dir_link(folder, elsewhere)
    again = await _third(layout, Entry(command="uvx"), sandbox_paths=grants)
    assert folder not in _own_rules(again.profile) and elsewhere not in _own_rules(again.profile)
    assert again.profile.ungranted == (PROTECTED_LOCATION,)


async def test_only_the_first_sixteen_sandbox_paths_are_looked_at(layout, monkeypatch):
    calls: list[Path] = []

    async def _allow(path: Path, _layout: SandboxLayout, owner: str | None = None) -> None:
        calls.append(path)

    monkeypatch.setattr(mcp_profiles, "authorize_derived_path", _allow)
    base = layout.install_root / "workspace"
    grants = [Grant(str(base / f"d{i}"), "ro") for i in range(MAX_MCP_SANDBOX_PATHS + 1)]
    launch = await _third(layout, Entry(command="uvx"), sandbox_paths=grants)
    assert len(calls) == MAX_MCP_SANDBOX_PATHS
    assert launch.profile.ungranted == (SANDBOX_PATHS_OVERFLOW,)


# --- scaffold ---------------------------------------------------------------------------


def _scaffold(layout: SandboxLayout, name: str = "demo") -> tuple[Entry, Path]:
    script = layout.config_dir / "mcp_servers" / name / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")
    return Entry(command="python", args=(str(script),)), script.parent


async def test_scaffold_gets_only_its_own_folder_read_execute(layout):
    entry, folder = _scaffold(layout)
    result = _classify(layout, entry, OriginRecord("present", "scaffold"), name="demo")
    launch = await _third(layout, entry, result, name="demo")
    own = _own_rules(launch.profile)
    assert launch.profile.name == MCP_SCAFFOLD_PROFILE
    assert own[folder] == "rox"
    assert not any(p.is_relative_to(layout.config_dir) for p in own if p != folder)
    assert launch.profile.ungranted == ()


async def test_third_party_with_the_same_script_gets_no_config_folder(layout):
    entry, folder = _scaffold(layout)
    result = _classify(layout, entry, OriginRecord("present", "manual"), name="demo")
    launch = await _third(layout, entry, result, name="demo")
    assert folder not in _own_rules(launch.profile)
    assert launch.profile.ungranted == (PROTECTED_LOCATION,)


async def test_scaffold_folder_is_rechecked_against_the_entry(layout):
    """分類之後條目改指別台的資料夾：不給 scaffold 的資料夾。"""
    entry, folder = _scaffold(layout)
    result = classify("demo", entry, NO_RECORD, layout, base_dir=layout.proxy_root)
    moved, _ = _scaffold(layout, "other")
    launch = await _third(layout, moved, result, name="demo")
    assert folder not in _own_rules(launch.profile)
    assert SCAFFOLD_DIR_MISMATCH in launch.profile.ungranted


@pytest.mark.parametrize("kind", ["self", "remote"])
async def test_third_party_profile_refuses_other_classes(layout, kind):
    with pytest.raises(ProfileError):
        await _third(layout, Entry(command="npx"), McpClassification(kind, None, "x"))
