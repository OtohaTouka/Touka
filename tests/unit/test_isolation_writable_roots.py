"""Z10：隔離 profile 的可寫規則都落在錨點清單裡（ADR-0293）.

以 ``ISOLATION_WIRED_ACTIONS`` 與 MCP 的種類驅動，每一種都實際建出 profile；輸入組合含有沒有綁
專案、有沒有人授權的資料夾、執行器或本機、state 根的兩種來源與四種身分。「可寫」看 Landlock 的寫入
權利。每條可寫規則都要在某個區裡（``zone_of``，全部 5 個錨點）或就是某個錨點本身；``custom/<id>``
與錨點的上層都不行。豁免只有由測試輸入算出的確切路徑（專案根、人授權或條目引用的資料夾、執行器的
state、系統裝置）。另核對執行器的掛載根、權限自癒只修一層的根、心跳與 kernel 用的資料夾名。
只用 ``tmp_path`` 的假版面。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from memory_server.utils.zone_paths import (
    ALL_ANCHORS,
    Anchor,
    anchor_at,
    anchor_dirname,
    contains_zone,
    zone_of,
)
from src.models.identity import AgentIdentity
from src.sandbox_kernel import landlock, spec
from src.sandbox_kernel.spec import AccessName
from src.services.isolation import executor_session, profiles
from src.services.isolation.mcp_profiles import (
    build_mcp_self_profile,
    build_mcp_third_party_profile,
    state_segment,
)
from src.services.isolation.profiles import (
    DEVICE_RW_PATHS,
    PathRule,
    SandboxLayout,
    SandboxProfile,
    build_agent_command_profile,
    build_evolution_profile,
    layout_zone_roots,
)
from src.services.isolation.repair import reserved_roots
from src.services.isolation.status import expected_executor_roots
from src.utils.isolation_policy import ISOLATION_WIRED_ACTIONS, TERMINAL_TOOLS, VERIFY_ACTION
from src.utils.mcp_origin import McpClassification

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ToukaProxy" / "src"
SKILL = "tool1"
# 每個接上 runner 的動作建哪一種 profile；新增動作沒列進來就紅
ACTION_PROFILES = {
    **dict.fromkeys(TERMINAL_TOOLS, "agent_command"),
    VERIFY_ACTION: "agent_command",
    "evolution_implement_and_verify": "evolution",
    "write_mcp_implementation": "evolution",
    "verify_mcp_locally": "evolution",
    "create_mcp": "mcp_self",
    "scaffold_mcp": "mcp_scaffold",
}
# Landlock 的讀與執行；其他權利都算可寫
_READ_ONLY = landlock.READ_FILE | landlock.READ_DIR | landlock.EXECUTE
# ``SandboxProfile(`` 與 ``PathRule(`` 的建構點（「檔案:函式」→ 次數）；新增一處要先在這裡登記
CONSTRUCTIONS = {
    "SandboxProfile": {
        "services/isolation/agent_command.py:prepare_command": 1,
        "services/isolation/agent_command.py:unbound_or_refuse": 1,
        "services/isolation/evolution_command.py:acquire_evolution_launch": 1,
        "services/isolation/mcp_profiles.py:build_mcp_self_profile": 1,
        "services/isolation/mcp_profiles.py:build_mcp_third_party_profile": 1,
        "services/isolation/profiles.py:build_agent_command_profile": 1,
        "services/isolation/profiles.py:build_evolution_profile": 1,
    },
    "PathRule": {
        "services/isolation/mcp_profiles.py:_reference_rule": 2,
        "services/isolation/mcp_profiles.py:_sandbox_rules": 1,
        "services/isolation/mcp_profiles.py:build_mcp_self_profile": 2,
        "services/isolation/mcp_profiles.py:build_mcp_third_party_profile": 2,
        "services/isolation/profiles.py:build_agent_command_profile": 3,
        "services/isolation/profiles.py:build_evolution_profile": 2,
        "services/isolation/profiles.py:public_config_rules": 1,
        "services/isolation/profiles.py:system_rules": 4,
    },
}
# 「未隔離」的 profile（沒經 builder）：規則一定是空的
UNBOUND_PROFILES = {
    "services/isolation/agent_command.py:prepare_command",
    "services/isolation/agent_command.py:unbound_or_refuse",
    "services/isolation/evolution_command.py:acquire_evolution_launch",
}


@dataclass(frozen=True)
class Who:
    identity: AgentIdentity
    drive: str  # data 根底下自己的個人空間


WHO = {
    "soul": Who(AgentIdentity.for_role("Alice", "Owner"), "personal_drive/Alice"),
    "custom_model": Who(AgentIdentity.for_role("M1", "Base"), "custom/M1/drive"),
    "human": Who(AgentIdentity.for_role("bob", "user", is_human=True), "custom/bob/drive"),
    "debugger": Who(AgentIdentity.for_role("DebugAgent", "Debugger"), "custom/DebugAgent/drive"),
}


@dataclass(frozen=True)
class Entry:
    """server_config 條目的四個欄位（``ProtocolServerConfig`` 的形狀）。"""

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str | None = None


@dataclass(frozen=True)
class Grant:
    """人設的「額外允許的資料夾」一筆。"""

    path: str
    access: str


def _layout(tmp_path: Path, state: str) -> SandboxLayout:
    install = tmp_path.resolve() / "install"
    data = install / "data"
    for who in WHO.values():
        (data / who.drive).mkdir(parents=True)
    for sub in (f"mcp_servers/managed/{SKILL}", "temp/docgen_output", "soul/Alice", "legacy"):
        (data / sub).mkdir(parents=True)
    for sub in ("ToukaProxy/config/skills", "host_root", "workspace"):
        (install / sub).mkdir(parents=True)
    env_file = install / "host_root" / ".env"
    env_file.write_text("", encoding="utf-8")
    # state 根的兩種來源：設定指定的資料夾，或沒設定時本機的 data 根 temp/sandbox_state
    state_root = install / "st" if state == "configured" else data / "temp" / "sandbox_state"
    state_root.mkdir(parents=True)
    return SandboxLayout(
        data_root=data,
        config_dir=install / "ToukaProxy" / "config",
        install_root=install,
        proxy_root=install / "ToukaProxy",
        env_file=env_file,
        state_root=state_root,
        kernel_dir=install / "kernel",
        executor_dir=None,
        mirror_roots=(install / "host_root", install / "workspace"),
        in_container=False,
    )


@pytest.fixture
def allow_touka_check(monkeypatch):
    """Touka 目錄偵測放行（往上走會走出 ``tmp_path``）；永不授權的其他規則照舊."""

    async def _allow(_path: Path, _layout: SandboxLayout) -> None:
        return None

    monkeypatch.setattr(profiles, "touka_directory_verdict", _allow)


def _folder(base: Path, name: str) -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    return folder


def _writable(access: str) -> bool:
    return bool(landlock.ACCESS_RIGHTS[access] & ~_READ_ONLY)


def _anchor_of(path: Path, layout: SandboxLayout) -> Anchor | None:
    roots = layout_zone_roots(layout)
    found = zone_of(path, roots, anchors=ALL_ANCHORS)
    return found[0] if found else anchor_at(path, roots, anchors=ALL_ANCHORS)


def outside_zones(
    profile: SandboxProfile, layout: SandboxLayout, exempt: Iterable[Path] = ()
) -> list[tuple[Path, str]]:
    """可寫、卻不在任何區裡也不是錨點本身的規則（確切豁免與系統裝置除外）."""
    roots = layout_zone_roots(layout)
    allowed = set(exempt)
    devices = {Path(p).resolve() for p in DEVICE_RW_PATHS}
    found = []
    for rule in profile.rules:
        if not _writable(rule.access) or rule.path in allowed:
            continue
        if rule.access == "dev_rw" and rule.path in devices:
            continue
        is_anchor = anchor_at(rule.path, roots, anchors=ALL_ANCHORS) is not None
        inside = zone_of(rule.path, roots, anchors=ALL_ANCHORS) is not None
        above = contains_zone(rule.path, roots, anchors=ALL_ANCHORS) and not is_anchor
        if above or not (inside or is_anchor):
            found.append((rule.path, rule.access))
    return found


def _state_owner(executor: Path, kind: str, owner: str) -> list[Path]:
    base = executor / kind / owner
    return [base / "home", base / "tmp"]


# --- 清單與建構點 -------------------------------------------------------------


def test_access_names_and_their_write_rights_are_pinned():
    assert set(get_args(AccessName)) == {"ro", "rox", "rwx", "dev_rw"}
    assert set(landlock.ACCESS_RIGHTS) == set(get_args(AccessName))
    assert {name for name in get_args(AccessName) if _writable(name)} == {"rwx", "dev_rw"}


def test_every_wired_action_builds_a_listed_profile():
    assert set(ACTION_PROFILES) == set(ISOLATION_WIRED_ACTIONS)


def _builds(node: ast.AST, name: str) -> bool:
    """``name(...)`` 或 ``模組.name(...)``（例如 ``spec.PathRule(...)``）."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) == name


def _calls(name: str) -> dict[str, list[ast.Call]]:
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_calls = sum(_builds(node, name) for node in ast.walk(tree))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [node for node in ast.walk(fn) if _builds(node, name)]
            if calls:
                key = f"{path.relative_to(SOURCE).as_posix()}:{fn.name}"
                found.setdefault(key, []).extend(calls)
                module_calls -= len(calls)
        assert module_calls == 0, path  # 模組層或巢狀函式裡的建構點也要算到
    return found


@pytest.mark.parametrize("name", sorted(CONSTRUCTIONS))
def test_profile_and_rule_constructions_are_listed(name):
    assert {key: len(calls) for key, calls in _calls(name).items()} == CONSTRUCTIONS[name]


def test_no_profile_gets_its_rules_replaced():
    """``dataclasses.replace(profile, rules=...)`` 會繞過建構點名單：產品程式不准這樣換規則."""
    replaced = [
        f"{path.relative_to(SOURCE).as_posix()}:{node.lineno}"
        for path in sorted(SOURCE.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if _builds(node, "replace") and any(k.arg == "rules" for k in node.keywords)
    ]
    assert replaced == []


@pytest.mark.parametrize(
    ("text", "count"),
    [("PathRule(p, a)\n", 1), ("spec.PathRule(p, a)\n", 1), ("x.other(p)\n", 0)],
)
def test_constructions_are_found_in_both_spellings(text, count):
    tree = ast.parse(text)
    assert sum(_builds(node, "PathRule") for node in ast.walk(tree)) == count


def test_profiles_built_outside_the_builders_have_no_rules():
    for key, calls in _calls("SandboxProfile").items():
        if key not in UNBOUND_PROFILES:
            continue
        for call in calls:
            rules = next(k.value for k in call.keywords if k.arg == "rules")
            assert isinstance(rules, ast.Tuple) and rules.elts == [], key


# --- 代理人指令（終端、互審的驗證指令）-------------------------------------------


@pytest.mark.parametrize("state", ["configured", "default"])
@pytest.mark.parametrize("who", sorted(WHO))
@pytest.mark.parametrize("bound", [False, True])
async def test_agent_command_writes_only_into_zones(tmp_path, allow_touka_check, state, who, bound):
    layout = _layout(tmp_path, state)
    project = _folder(layout.install_root / "workspace", "proj") if bound else None
    agent = WHO[who]
    profile = await build_agent_command_profile(
        agent.identity, layout.data_root / agent.drive, layout=layout, project_root=project
    )
    assert profile.project_root == project
    assert outside_zones(profile, layout, [project] if project else []) == []


@pytest.mark.parametrize("who", sorted(WHO))
async def test_agent_command_in_the_executor_writes_only_into_zones(tmp_path, who):
    layout = _layout(tmp_path, "configured")
    executor = _folder(tmp_path.resolve(), "executor_state")
    agent = WHO[who]
    profile = await build_agent_command_profile(
        agent.identity,
        layout.data_root / agent.drive,
        layout=layout,
        location="executor",
        executor_state=str(executor),
    )
    exempt = _state_owner(executor, "agents", agent.identity.agent_id)
    assert outside_zones(profile, layout, exempt) == []


# --- 演化的建置與驗證 ---------------------------------------------------------------


@pytest.mark.parametrize("state", ["configured", "default"])
@pytest.mark.parametrize("location", ["proxy", "executor"])
def test_evolution_writes_only_into_zones(tmp_path, state, location):
    layout = _layout(tmp_path, state)
    executor = _folder(tmp_path.resolve(), "executor_state") if location == "executor" else None
    profile = build_evolution_profile(
        SKILL,
        layout.data_root / "mcp_servers" / "managed" / SKILL,
        layout=layout,
        location=location,
        executor_state=str(executor) if executor else None,
    )
    exempt = _state_owner(executor, "evolution", SKILL) if executor else []
    assert outside_zones(profile, layout, exempt) == []


# --- MCP：自建、第三方、scaffold ------------------------------------------------------


@pytest.mark.parametrize("state", ["configured", "default"])
@pytest.mark.parametrize("location", ["proxy", "executor"])
def test_self_built_mcp_writes_only_into_zones(tmp_path, state, location):
    layout = _layout(tmp_path, state)
    executor = _folder(tmp_path.resolve(), "executor_state") if location == "executor" else None
    classification = McpClassification(
        "self", "creation", "origin", managed_skills=frozenset({SKILL})
    )
    profile = build_mcp_self_profile(
        "srv",
        classification,
        layout=layout,
        location=location,
        executor_state=str(executor) if executor else None,
    )
    segment = state_segment("self", "srv")
    exempt = _state_owner(executor, "mcp", segment) if executor else []
    assert outside_zones(profile, layout, exempt) == []


@pytest.mark.parametrize("state", ["configured", "default"])
@pytest.mark.parametrize("granted", [False, True])
async def test_third_party_mcp_writes_only_into_zones(tmp_path, allow_touka_check, state, granted):
    """APPDATA、docgen 輸出、個人空間裡的資料夾與 Touka 以外的資料夾都引用；人授權的另一個."""
    layout = _layout(tmp_path, state)
    outside = _folder(tmp_path.resolve(), "user_tools")
    folder = _folder(tmp_path.resolve(), "human_granted")
    drive = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(
        command="npx",
        args=("-y", "fs", "@TOUKA_APPDATA@", str(drive), str(outside)),
        env={"OUT": "@TOUKA_OUTPUT@"},
    )
    launch = await build_mcp_third_party_profile(
        "srv",
        entry,
        McpClassification("third_party", "hub", "origin"),
        layout=layout,
        sandbox_paths=[Grant(str(folder), "rw")] if granted else [],
        block_ports=(),
    )
    writable = {rule.path for rule in launch.profile.rules if _writable(rule.access)}
    assert {outside, drive} <= writable and (folder in writable) is granted
    assert outside_zones(launch.profile, layout, [outside, folder]) == []


async def test_scaffold_mcp_writes_only_into_zones(tmp_path):
    layout = _layout(tmp_path, "configured")
    script = layout.config_dir / "mcp_servers" / "demo" / "server.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")
    classification = McpClassification(
        "scaffold", "scaffold", "origin", scaffold_dir=script.parent.resolve()
    )
    launch = await build_mcp_third_party_profile(
        "demo",
        Entry(command="python", args=(str(script),)),
        classification,
        layout=layout,
        block_ports=(),
    )
    assert launch.profile.ungranted == ()
    assert outside_zones(launch.profile, layout) == []


async def test_every_anchor_is_reached_by_a_writable_rule(tmp_path, allow_touka_check):
    """PR 1 就檢查到全部 5 個錨點（含 PR 2 才執法的 MCP_SERVERS、DOCGEN_OUTPUT、SANDBOX_STATE）."""
    layout = _layout(tmp_path, "configured")
    built = [
        await build_agent_command_profile(
            WHO[who].identity, layout.data_root / WHO[who].drive, layout=layout
        )
        for who in ("soul", "custom_model")
    ]
    tool = layout.data_root / "mcp_servers" / "managed" / SKILL
    built.append(build_evolution_profile(SKILL, tool, layout=layout))
    docgen = Entry(command="uvx", args=("mcp-docgen",), env={"OUT": "@TOUKA_OUTPUT@"})
    third = McpClassification("third_party", "hub", "origin")
    launch = await build_mcp_third_party_profile("srv", docgen, third, layout=layout)
    built.append(launch.profile)
    seen = {
        _anchor_of(rule.path, layout)
        for profile in built
        for rule in profile.rules
        if _writable(rule.access)
    }
    assert set(ALL_ANCHORS) <= seen


# --- 核對本身 ---------------------------------------------------------------------


def test_the_check_reports_a_custom_model_folder_an_anchor_parent_and_soul(tmp_path):
    layout = _layout(tmp_path, "configured")
    data = layout.data_root
    bad = [data / "custom" / "M1", data, data / "soul" / "Alice", data / "mcp_servers"]
    good = [data / "personal_drive", data / "custom" / "M1" / "drive", layout.state_root / "x"]
    rules = (
        *(PathRule(p, "rwx") for p in bad),
        *(PathRule(p, "rwx") for p in good),
        PathRule(data, "ro"),
    )
    profile = SandboxProfile(name="x", cwd=data, rules=rules, block_ports=())
    assert [path for path, _access in outside_zones(profile, layout)] == bad


def test_an_exemption_covers_only_the_exact_path(tmp_path):
    layout = _layout(tmp_path, "configured")
    project = _folder(tmp_path.resolve(), "proj")
    rules = (PathRule(project, "rwx"), PathRule(project.parent, "rwx"))
    profile = SandboxProfile(name="x", cwd=project, rules=rules, block_ports=())
    assert outside_zones(profile, layout, [project]) == [(project.parent, "rwx")]


# --- 其他清單 -------------------------------------------------------------------


def test_zones_are_not_under_soul_or_legacy(tmp_path):
    layout = _layout(tmp_path, "configured")
    roots = layout_zone_roots(layout)
    for name in ("soul", "legacy"):
        base = layout.data_root / name
        for anchor in ALL_ANCHORS:
            literal = roots.literal(anchor)
            assert literal is None or not Path(literal).is_relative_to(base), (anchor, name)


def test_the_executor_mounts_anchors_writable(tmp_path):
    """執行器容器可寫的掛載根是錨點；``mcp_servers`` 是 MCP_SERVERS 的上一層（PR 2 收窄）.

    執行器裡每次 START 的 Landlock 只給 ``KIND_LAYOUT`` 的目標（``managed/<skill>``），代理人寫不到
    ``mcp_servers`` 本身；這裡釘住這一個例外，多一個就紅。
    """
    layout = _layout(tmp_path, "configured")
    roots = layout_zone_roots(layout)
    writable = {Path(root.path) for root in expected_executor_roots(layout) if root.access == "rw"}
    anchors = {path for path in writable if anchor_at(path, roots, anchors=ALL_ANCHORS)}
    assert writable - anchors == {layout.data_root / "mcp_servers"}
    names = {anchor_dirname(anchor) for anchor in ALL_ANCHORS}
    for top, middle in executor_session.KIND_LAYOUT.values():
        assert "/".join((top, *middle)) in names


def test_one_level_repair_roots_are_anchors_or_above_them(tmp_path):
    """權限自癒只修一層的根（W1、W7、W8 與 ``ST/proxy``）是錨點、錨點的上層或在區裡."""
    layout = _layout(tmp_path, "configured")
    roots = layout_zone_roots(layout)
    for root in reserved_roots(layout):
        above = contains_zone(root, roots, anchors=ALL_ANCHORS)
        assert above or zone_of(root, roots, anchors=ALL_ANCHORS) is not None, root


def _heartbeat_workspace_roots() -> tuple[tuple[str, str], ...]:
    """``ToukaHeartbeat/permission_init.py`` 的 ``WORKSPACE_ROOTS``（不匯入：它不能依賴 Memory）."""
    tree = ast.parse((ROOT / "ToukaHeartbeat" / "permission_init.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "WORKSPACE_ROOTS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("WORKSPACE_ROOTS")


def test_folder_names_outside_zone_paths_match_it():
    tops = {anchor_dirname(a).split("/")[0] for a in (Anchor.PERSONAL_DRIVE, Anchor.MCP_SERVERS)}
    assert {name for _data, name in _heartbeat_workspace_roots()} == tops
    assert anchor_dirname(Anchor.PERSONAL_DRIVE) in spec.DATA_ROOT_NAMES


def test_layout_roots_carry_every_anchor(tmp_path):
    roots = layout_zone_roots(_layout(tmp_path, "configured"))
    assert {a for a in Anchor if roots.literal(a) is not None} == set(ALL_ANCHORS)
