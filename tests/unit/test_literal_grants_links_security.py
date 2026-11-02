"""給權限只看字面位置（ADR-0293 決定 16 補齊到隔離層的每一處授與）.

跟隨連結算出的結果只准用來拒絕、不准用來授與：MCP 來源分類的程式資料夾、代理人的專案根、
公開設定檔與第三方的 APPDATA 對映，都給「解析後的錨點＋字面段落」；解析後才落在別處的不給，
只用來多判自建、多凍結、多略過。字面比對同時看 data 根的兩種寫法，所以人經連結設定 data 根時
照常可用。全部用 tmp_path 建的假版面，不碰 ``data/``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.services.isolation import profiles
from src.services.isolation.mcp_profiles import (
    build_mcp_self_profile,
    build_mcp_third_party_profile,
    map_appdata,
    mcp_state_dirs,
)
from src.services.isolation.profiles import (
    SandboxLayout,
    build_agent_command_profile,
    current_layout,
    public_config_rules,
)
from src.services.resource_manager import resource_manager
from src.utils.mcp_origin import (
    NO_RECORD,
    OriginRecord,
    classify,
    deployed_skills,
    referenced_paths,
)

from tests.support.isolation import match_state_root
from tests.support.links import LINK_FOLLOWED, make_dir_link

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")
EVOLUTION = OriginRecord("present", "evolution")
HUB = OriginRecord("present", "hub")


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
        "mcp_servers/managed/foo",
        "mcp_servers/managed/bar",
        "temp/docgen_output",
    ):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("ToukaProxy/config/skills", "workspace/myproj", "host_root", "st"):
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


@pytest.fixture(autouse=True)
def _rm_state_matches_layout(tmp_path, monkeypatch):
    """RM 的 ST 指向本檔版面的 ST（ADR-0293 P7：兩者不同時 ST 的存取函式拒絕）."""
    match_state_root(monkeypatch, tmp_path.resolve() / "install" / "st")


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    return _layout(tmp_path)


@pytest.fixture
def allow_touka_check(monkeypatch):
    async def _allow(_path: Path, _layout: SandboxLayout) -> None:
        return None

    monkeypatch.setattr(profiles, "touka_directory_verdict", _allow)


def _skill(layout: SandboxLayout, name: str) -> Path:
    return layout.data_root / "mcp_servers" / "managed" / name


def _script(folder: Path) -> Path:
    script = folder / "server.py"
    script.write_text("", encoding="utf-8")
    return script


def _own_rules(profile) -> dict[Path, str]:
    """系統路徑以外的規則（路徑 → 權利）。"""
    system = {rule.path for rule in profiles.system_rules()}
    return {rule.path: rule.access for rule in profile.rules if rule.path not in system}


def _classify(layout: SandboxLayout, entry: Entry, record: OriginRecord = EVOLUTION, **kwargs):
    base_dir = kwargs.pop("base_dir", layout.proxy_root)
    return classify("srv", entry, record, layout, base_dir=base_dir, **kwargs)


def _doc(entry: dict) -> dict:
    return {"mcpServers": {"srv": entry}}


def _dangling_link(link: Path, tmp_path: Path) -> None:
    """建一個指到已不存在資料夾的連結（``Path.exists`` 看不到，lstat 看得到）。"""
    gone = tmp_path / "gone"
    gone.mkdir()
    link.parent.mkdir(parents=True, exist_ok=True)
    make_dir_link(link, gone)
    gone.rmdir()


# --- MCP 來源分類：程式資料夾只給字面位置 -----------------------------------------------


def test_link_from_outside_into_a_skill_grants_no_program_folder(layout, tmp_path):
    """外面的連結指進 ``managed/bar``：判成自建、凍結 bar，但不給 bar 的讀與執行。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    bar = _skill(layout, "bar")
    script = _script(bar)
    make_dir_link(outside / "via", bar)
    entry = Entry(command="python", args=(str(outside / "via" / script.name),))
    found = _classify(layout, entry)
    assert found.kind == "self"
    assert found.managed_skills == frozenset(), LINK_FOLLOWED
    assert bar not in _own_rules(build_mcp_self_profile("srv", found, layout=layout)), LINK_FOLLOWED
    assert deployed_skills(
        _doc({"command": "python", "args": list(entry.args)}), layout, base_dir=layout.proxy_root
    ).covers("bar")


def test_link_inside_a_skill_grants_only_the_literal_skill(layout):
    """字面在 foo、經連結解析到 bar：授與只有 foo；bar 只是多被凍結。"""
    foo, bar = _skill(layout, "foo"), _skill(layout, "bar")
    script = _script(bar)
    make_dir_link(foo / "via", bar)
    literal = foo / "via" / script.name
    found = _classify(layout, Entry(command="python", args=(str(literal),)))
    assert found.managed_skills == frozenset({"foo"}), LINK_FOLLOWED
    own = _own_rules(build_mcp_self_profile("srv", found, layout=layout))
    assert own[foo] == "rox" and bar not in own
    deployed = deployed_skills(
        _doc({"command": "python", "args": [str(literal)]}), layout, base_dir=layout.proxy_root
    )
    assert deployed.covers("foo") and deployed.covers("bar") and not deployed.every


def test_link_in_the_state_home_grants_no_program_folder(layout):
    """隔離中的 MCP 自己在 ST home 放的連結：相對參數解析進 ``managed/bar`` 也不給。"""
    bar = _skill(layout, "bar")
    script = _script(bar)
    home, _tmp = mcp_state_dirs("self", "srv", layout=layout, location="proxy")
    make_dir_link(home / "tools", bar)
    entry = Entry(command="python", args=(f"tools/{script.name}",))
    found = _classify(layout, entry, base_dir=home)
    assert found.kind == "self"
    assert found.managed_skills == frozenset(), LINK_FOLLOWED
    assert bar not in _own_rules(build_mcp_self_profile("srv", found, layout=layout)), LINK_FOLLOWED


def test_literal_reference_still_gets_its_program_folder(layout):
    """沒有連結：照常給條目引用的那個技能（演化部署的條目）。"""
    foo = _skill(layout, "foo")
    script = _script(foo)
    found = _classify(layout, Entry(command="python", args=(str(script),)))
    assert found.managed_skills == frozenset({"foo"})
    own = _own_rules(build_mcp_self_profile("srv", found, layout=layout))
    assert own[foo] == "rox"


def test_dotdot_is_removed_literally_before_judging(layout):
    """``..`` 以字面消掉：``foo/../bar`` 的字面位置是 bar，不是 foo。"""
    bar = _skill(layout, "bar")
    script = _script(bar)
    literal = f"{_skill(layout, 'foo')}{os.sep}..{os.sep}bar{os.sep}{script.name}"
    found = _classify(layout, Entry(command="python", args=(literal,)))
    assert found.managed_skills == frozenset({"bar"})


# --- 人經連結設定 data 根：字面比對看兩種寫法 --------------------------------------------


def _linked_data_root(layout: SandboxLayout, tmp_path: Path) -> tuple[SandboxLayout, Path]:
    """版面的 data 根經人做的連結設定：``current_layout`` 的形狀（解析後＋設定裡的寫法）。"""
    (tmp_path / "configured").mkdir()
    link = tmp_path.resolve() / "configured" / "touka_data"
    make_dir_link(link, layout.data_root)
    return replace(layout, data_root_literal=link), link


def test_current_layout_keeps_the_configured_spelling_of_the_data_root():
    layout = current_layout()
    configured = Path(os.path.normpath(Path(resource_manager.get_base_path()).absolute()))
    assert layout.data_root_literal == configured
    assert layout.data_root == configured.resolve()


def test_program_folder_works_with_either_spelling_of_the_data_root(layout, tmp_path):
    """條目寫設定裡的寫法或解析後的寫法都給技能；規則一律是解析後的錨點＋字面段落。"""
    linked, link = _linked_data_root(layout, tmp_path)
    foo = _skill(layout, "foo")
    script = _script(foo)
    spelled = link / "mcp_servers" / "managed" / "foo" / script.name
    for path in (script, spelled):
        found = _classify(linked, Entry(command="python", args=(str(path),)))
        assert found.managed_skills == frozenset({"foo"}), path
        own = _own_rules(build_mcp_self_profile("srv", found, layout=linked))
        assert own[foo] == "rox" and spelled.parent not in own


def test_data_root_given_through_the_layout_field_itself_also_counts(layout, tmp_path):
    """版面的 ``data_root`` 本身就是連結（測試與呼叫端自己組的版面）：一樣認兩種寫法。"""
    link = tmp_path.resolve() / "touka_data"
    make_dir_link(link, layout.data_root)
    foo = _skill(layout, "foo")
    script = _script(foo)
    linked = replace(layout, data_root=link)
    spelled = link / "mcp_servers" / "managed" / "foo" / script.name
    found = _classify(linked, Entry(command="python", args=(str(spelled),)))
    assert found.managed_skills == frozenset({"foo"})


@pytest.mark.parametrize("spelling", ["configured", "resolved"])
def test_plain_data_root_argument_with_either_spelling_freezes_nothing(layout, tmp_path, spelling):
    linked, link = _linked_data_root(layout, tmp_path)
    raw = str(link if spelling == "configured" else layout.data_root)
    result = deployed_skills(
        _doc({"command": "npx", "args": ["fs", raw]}), linked, base_dir=linked.proxy_root
    )
    assert not result.every and not result.skills


@pytest.mark.parametrize("spelling", ["configured", "resolved"])
def test_appdata_maps_with_either_spelling_of_the_data_root(layout, tmp_path, spelling):
    linked, link = _linked_data_root(layout, tmp_path)
    raw = str(link if spelling == "configured" else layout.data_root)
    assert map_appdata(raw, linked) == str(layout.data_root / "personal_drive")


# --- 三處放寬判斷：跟隨連結的結果只會更嚴 -------------------------------------------------


def test_data_root_argument_through_a_link_freezes_every_skill(layout, tmp_path):
    """指到 data 根的位置參數經連結：不算「普通 data 根參數」，整個 managed 都算已部署。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_link(outside / "datalink", layout.data_root)
    result = deployed_skills(
        _doc({"command": "npx", "args": ["fs", str(outside / "datalink")]}),
        layout,
        base_dir=layout.proxy_root,
    )
    assert result.every, LINK_FOLLOWED


def test_dangling_link_named_without_a_separator_counts_as_a_reference(layout, tmp_path):
    """代理人影響得到的基準裡指到不存在位置的連結也算引用（以前 ``exists`` 看不到）。"""
    home, _tmp = mcp_state_dirs("third", "srv", layout=layout, location="proxy")
    _dangling_link(home / "tool", tmp_path)
    entry = Entry(command="npx", args=("pkg", "tool"))
    paths = [ref.path for ref in referenced_paths(entry, layout, base_dir=home)]
    assert home / "tool" in paths, LINK_FOLLOWED
    assert _classify(layout, entry, HUB, base_dir=home).kind == "self", LINK_FOLLOWED


def test_dangling_link_shaped_like_a_scoped_package_counts_as_a_reference(layout, tmp_path):
    """長得像 ``@scope/pkg`` 的參數，基準裡有同名連結就當路徑，懸空的也算。"""
    home, _tmp = mcp_state_dirs("third", "srv", layout=layout, location="proxy")
    _dangling_link(home / "@scope" / "pkg", tmp_path)
    entry = Entry(command="npx", args=("@scope/pkg",))
    assert _classify(layout, entry, HUB, base_dir=home).kind == "self", LINK_FOLLOWED


def test_scoped_package_without_a_link_stays_third_party(layout):
    home, _tmp = mcp_state_dirs("third", "srv", layout=layout, location="proxy")
    entry = Entry(command="npx", args=("@scope/pkg",))
    assert _classify(layout, entry, HUB, base_dir=home).kind == "third_party"


def test_mirror_folder_linked_into_the_touka_directory_counts(layout):
    """鏡像根裡的專案指進 Touka 目錄的連結：解析後判成受保護不能拿來放行。"""
    target = layout.config_dir / "elsewhere"
    target.mkdir()
    script = _script(target)
    via = layout.install_root / "workspace" / "myproj" / "via"
    make_dir_link(via, target)
    entry = Entry(command="python", args=(str(via / script.name),))
    assert _classify(layout, entry, HUB).kind == "self", LINK_FOLLOWED


def test_mirror_folder_without_a_link_inside_the_touka_directory_is_unchanged(layout):
    """沒有連結：Touka 目錄裡（不是使用者專案）的鏡像根資料夾照舊不算代理人影響得到。"""
    folder = layout.install_root / "host_root" / "ToukaProxy"
    folder.mkdir()
    script = _script(folder)
    entry = Entry(command="python", args=(str(script),))
    assert _classify(layout, entry, NO_RECORD).kind == "third_party"


# --- 代理人的專案根 ------------------------------------------------------------------------


async def test_project_root_swapped_after_the_check_grants_the_literal_location(
    layout, tmp_path, monkeypatch
):
    """核對之後才換成連結：規則是字面位置（啟動器開不了就不執行），不是連結另一端。"""
    project = layout.install_root / "workspace" / "myproj"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def _swap(_path: Path, _layout: SandboxLayout) -> None:
        project.rmdir()
        make_dir_link(project, elsewhere)

    monkeypatch.setattr(profiles, "touka_directory_verdict", _swap)
    profile = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, project_root=project
    )
    own = _own_rules(profile)
    assert elsewhere.resolve() not in own, LINK_FOLLOWED
    assert own[project] == "rwx" and profile.project_root == project


def test_literal_position_removes_dotdot_without_following_links(tmp_path):
    """連結本身就是字面位置；``via/../x`` 在 via 的上層，不是連結另一端的上層。"""
    base = tmp_path.resolve()
    target = base / "far" / "target"
    target.mkdir(parents=True)
    (base / "near").mkdir()
    via = base / "near" / "via"
    make_dir_link(via, target)
    assert profiles._literal(via) == via, LINK_FOLLOWED
    assert profiles._literal(via / ".." / "x") == base / "near" / "x"


# --- 公開設定檔 ----------------------------------------------------------------------------


def test_public_config_is_searched_from_the_literal_anchor(layout):
    """錨點路上有連結：連結另一端的上層不給，連結本身以下的候選檔也不給；連結以上照給。"""
    workspace = layout.install_root / "workspace"
    other = workspace / "other"
    (other / "sub").mkdir(parents=True)
    (other / "pytest.ini").write_text("", encoding="utf-8")
    (workspace / "tox.ini").write_text("", encoding="utf-8")
    via = workspace / "myproj" / "via"
    make_dir_link(via, other)
    granted = {rule.path for rule in public_config_rules((via / "sub",), layout)}
    assert other / "pytest.ini" not in granted, LINK_FOLLOWED
    assert via / "pytest.ini" not in granted, LINK_FOLLOWED
    assert workspace / "tox.ini" in granted


def test_public_config_through_a_link_into_the_data_root_is_skipped(layout):
    """字面不在 data 根、解析後在：一樣不給（只會更嚴）。"""
    workspace = layout.install_root / "workspace"
    drive_parent = layout.data_root / "personal_drive"
    (drive_parent / "pyproject.toml").write_text("", encoding="utf-8")
    via = workspace / "via"
    make_dir_link(via, layout.data_root)
    anchor = via / "personal_drive" / "Alice"
    granted = {rule.path for rule in public_config_rules((anchor,), layout)}
    assert via / "personal_drive" / "pyproject.toml" not in granted, LINK_FOLLOWED
    assert drive_parent / "pyproject.toml" not in granted, LINK_FOLLOWED


# --- 第三方的 APPDATA 對映 -----------------------------------------------------------------


async def test_link_to_the_data_root_is_not_mapped_to_the_personal_drive(
    layout, tmp_path, allow_touka_check
):
    """指到 data 根的連結當一般引用：不對映、不給 personal_drive，也不給 data 根裡任何東西。"""
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    link = outside / "datalink"
    make_dir_link(link, layout.data_root)
    entry = Entry(command="npx", args=("-y", "pkg"), env={"INPUT": str(link)})
    found = _classify(layout, entry, HUB)
    assert found.kind == "third_party"
    launch = await build_mcp_third_party_profile(
        "srv", entry, found, layout=layout, block_ports=()
    )
    assert dict(launch.env) == {"INPUT": str(link)}, LINK_FOLLOWED
    data = layout.data_root
    covering = [
        path
        for path in _own_rules(launch.profile)
        if path == data or path.is_relative_to(data) or data.is_relative_to(path)
    ]
    assert covering == [], LINK_FOLLOWED
    assert _own_rules(launch.profile)[outside] == "rox"
