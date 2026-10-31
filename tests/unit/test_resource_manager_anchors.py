"""ResourceManager 對 PR 2 三個錨點的產生函式與區內方法（ADR-0293 PR 2：P1–P7）.

三個錨點：``mcp_servers/managed``（演化工具）、``temp/docgen_output``（文件輸出）、sandbox state
（ST）。用真的 ResourceManager；data 根與 ST 是 ``zone_data_root`` 給的 ``tmp_path``，不碰
``data/``。連結另一端是測試自己建的 ``outside`` 資料夾，每個案例都核對它沒被動到。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from memory_server.utils.no_follow import WalkReport, ZoneLinkRefusedError
from memory_server.utils.zone_paths import (
    Anchor,
    HandoffKind,
    ZonePath,
    ZonePathRequiredError,
    ZoneSegmentError,
    is_handed_off,
)
from src import config as app_config
from src.services.resource_manager import resource_manager as rm

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal, raises_zone

MCP, DOC, ST = Anchor.MCP_SERVERS, Anchor.DOCGEN_OUTPUT, Anchor.SANDBOX_STATE
PD, CD = Anchor.PERSONAL_DRIVE, Anchor.CUSTOM_DRIVE
WINDOWS = sys.platform == "win32"


@pytest.fixture
def state(zone_data_root: Path) -> Path:
    """``zone_data_root`` 的 ST（conftest 換到 ``tmp_path/zone_state``）."""
    return zone_data_root.parent / "zone_state"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    other = tmp_path.resolve() / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("secret", encoding="utf-8")
    return other


def _untouched(outside: Path) -> bool:
    names = sorted(p.name for p in outside.iterdir())
    return names == ["secret.txt"] and (outside / "secret.txt").read_text("utf-8") == "secret"


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.fspath(a)) == os.path.normcase(os.fspath(b))


# --- P1：三個錨點都執法 -------------------------------------------------------------


def _new_anchor_files(data: Path, state: Path) -> dict[str, Path]:
    return {
        "mcp_servers": data / "mcp_servers" / "managed" / "sample" / "server.py",
        "docgen_output": data / "temp" / "docgen_output" / "report.docx",
        "sandbox_state": state / "proxy" / "agents" / "Alice" / "home" / "x.txt",
    }


@pytest.mark.parametrize("anchor", ["mcp_servers", "docgen_output", "sandbox_state"])
def test_string_methods_refuse_the_three_new_anchors(zone_data_root, state, anchor):
    target = _new_anchor_files(zone_data_root, state)[anchor]
    with raises_zone(ZonePathRequiredError):
        rm.write_file_text(target, "x")
    with raises_zone(ZonePathRequiredError):
        rm.read_file_text(target)
    assert not target.exists()


# --- P3：產生函式 -----------------------------------------------------------------


def test_tool_root():
    assert rm.tool_root("sample") == ZonePath(MCP, ("sample",))
    assert rm.tool_root("Sample_2-x") == ZonePath(MCP, ("Sample_2-x",))


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "-x", "soul", "Personal_Drive"])
def test_tool_root_gives_none_only_for_a_bad_name(bad):
    assert rm.tool_root(bad) is None


def test_sandbox_state_generators(zone_data_root):
    assert rm.sandbox_state_proxy() == ZonePath(ST, ("proxy",))
    assert rm.sandbox_state_dir("agents", "Alice") == ZonePath(ST, ("proxy", "agents", "Alice"))


@pytest.mark.parametrize(("kind", "owner"), [("agents", "a/b"), ("agents", ".."), ("a/b", "x")])
def test_sandbox_state_dir_raises_for_a_bad_name(zone_data_root, kind, owner):
    with raises_zone(ZoneSegmentError):
        rm.sandbox_state_dir(kind, owner)


def test_sandbox_state_generators_give_none_only_when_unset(zone_data_root, monkeypatch):
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", "")
    monkeypatch.setattr(app_config, "is_container_runtime", lambda: True)
    assert rm.sandbox_state_root() is None
    assert rm.sandbox_state_proxy() is None
    assert rm.sandbox_state_dir("agents", "Alice") is None
    assert rm.sandbox_state_dir("agents", "a/b") is None  # 沒設時不看名稱：None 只有一個意思


def test_sandbox_state_root(zone_data_root, state, monkeypatch):
    assert _same(rm.sandbox_state_root(), state)
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", "")
    monkeypatch.setattr(app_config, "is_container_runtime", lambda: False)
    assert _same(rm.sandbox_state_root(), Path(rm.get_temp_path("sandbox_state")))
    monkeypatch.setattr(app_config, "is_container_runtime", lambda: True)
    assert rm.sandbox_state_root() is None
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", str(state))
    assert _same(rm.sandbox_state_root(), state)


def test_anchor_root_gives_only_the_personal_drive_and_the_docgen_output():
    assert rm.anchor_root(PD) == ZonePath(PD, ())
    assert rm.anchor_root(DOC) == ZonePath(DOC, ())


@pytest.mark.parametrize("anchor", [MCP, ST, CD])
def test_anchor_root_refuses_the_others(anchor):
    with raises_zone(ZoneSegmentError):
        rm.anchor_root(anchor)


# --- P2：ST 的代理人是三段 ------------------------------------------------------------


def test_sandbox_state_agent_is_three_segments():
    home = ZonePath(ST, ("proxy", "agents", "Alice", "home"))
    owner = ZonePath(ST, ("proxy", "agents", "Alice"))
    assert home.parent == owner
    assert owner.parent == owner
    assert home.same_agent(ZonePath(ST, ("proxy", "agents", "Alice", "tmp")))
    assert not home.same_agent(ZonePath(ST, ("proxy", "agents", "Bob", "home")))
    assert not home.same_agent(ZonePath(ST, ("proxy", "evolution", "Alice", "home")))


# --- P4：zone_text ------------------------------------------------------------------


def test_zone_text_is_the_resolved_anchor_and_literal_segments(zone_data_root, state):
    managed = zone_data_root / "mcp_servers" / "managed"
    server = rm.zone_text(ZonePath(MCP, ("sample", "server.py")))
    assert _same(server, managed / "sample" / "server.py")
    owner = ZonePath(ST, ("proxy", "agents", "Alice"))
    assert _same(rm.zone_text(owner), state / "proxy" / "agents" / "Alice")
    assert not (state / "proxy").exists()  # 不建資料夾


def test_zone_text_does_not_resolve_the_segments(zone_data_root, outside):
    managed = zone_data_root / "mcp_servers" / "managed"
    managed.mkdir(parents=True)
    make_dir_link(managed / "sample", outside)
    text = rm.zone_text(ZonePath(MCP, ("sample",)))
    assert _same(text, managed / "sample"), LINK_FOLLOWED


def test_zone_text_follows_a_human_made_anchor_link(zone_data_root, tmp_path):
    real = tmp_path.resolve() / "real_managed"
    real.mkdir()
    (zone_data_root / "mcp_servers").mkdir()
    make_dir_link(zone_data_root / "mcp_servers" / "managed", real)
    assert _same(rm.zone_text(ZonePath(MCP, ("sample",))), real / "sample")


def test_zone_text_refuses_anything_but_a_zone_path(zone_data_root):
    with raises_zone(ZonePathRequiredError):
        rm.zone_text(str(zone_data_root / "mcp_servers" / "managed" / "x"))


# --- P5：zone_rename、zone_access、刪除類的深度 ----------------------------------------


def _owner_tmp(state: Path) -> Path:
    tmp = state / "proxy" / "agents" / "Alice" / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "a.txt").write_text("a", encoding="utf-8")
    return tmp


def test_zone_rename_renames_the_last_segment(zone_data_root, state):
    tmp = _owner_tmp(state)
    rm.zone_rename(ZonePath(ST, ("proxy", "agents", "Alice", "tmp")), "tmp.trash-1")
    assert not tmp.exists()
    assert (tmp.parent / "tmp.trash-1" / "a.txt").read_text("utf-8") == "a"


def test_zone_rename_refuses_a_link_in_the_middle(zone_data_root, state, outside):
    (state / "proxy").mkdir(parents=True)
    (outside / "Alice" / "tmp").mkdir(parents=True)
    make_dir_link(state / "proxy" / "agents", outside)
    with raises_zone(ZoneLinkRefusedError):
        rm.zone_rename(ZonePath(ST, ("proxy", "agents", "Alice", "tmp")), "tmp.trash-1")
    assert (outside / "Alice" / "tmp").is_dir(), LINK_FOLLOWED
    assert not (outside / "Alice" / "tmp.trash-1").exists(), LINK_FOLLOWED


def test_zone_rename_refuses_a_link_as_the_last_segment(zone_data_root, state, outside):
    owner = state / "proxy" / "agents" / "Alice"
    owner.mkdir(parents=True)
    make_dir_link(owner / "tmp", outside)
    with raises_zone(ZoneLinkRefusedError):
        rm.zone_rename(ZonePath(ST, ("proxy", "agents", "Alice", "tmp")), "tmp.trash-1")
    assert (owner / "tmp").exists() and _untouched(outside)


@pytest.mark.parametrize(
    "zp",
    [
        ZonePath(ST, ("proxy", "agents", "Alice")),
        ZonePath(ST, ("proxy", "agents")),
        ZonePath(PD, ("Alice",)),
        ZonePath(MCP, ("sample",)),
    ],
    ids=["st_owner", "st_kind", "pd_agent", "mcp_skill"],
)
def test_zone_rename_needs_a_path_below_the_agent(zone_data_root, state, zp):
    _owner_tmp(state)
    with raises_zone(ZoneSegmentError):
        rm.zone_rename(zp, "renamed")
    assert (state / "proxy" / "agents" / "Alice" / "tmp").is_dir()


def test_zone_rename_refuses_a_bad_new_name(zone_data_root, state):
    _owner_tmp(state)
    with raises_zone(ZoneSegmentError):
        rm.zone_rename(ZonePath(ST, ("proxy", "agents", "Alice", "tmp")), "../x")


def test_zone_access(zone_data_root, state):
    home = state / "proxy" / "agents" / "Alice" / "home"
    home.mkdir(parents=True)
    zp = ZonePath(ST, ("proxy", "agents", "Alice", "home"))
    assert rm.zone_access(zp, os.W_OK | os.X_OK) is True
    assert rm.zone_access(zp.child("missing"), os.R_OK) is False


@pytest.mark.skipif(WINDOWS, reason="檔案連結只在 POSIX 建得出來")
def test_zone_access_does_not_follow_a_link_as_the_last_segment(zone_data_root, state, outside):
    owner = state / "proxy" / "agents" / "Alice"
    owner.mkdir(parents=True)
    (owner / "home").symlink_to(outside, target_is_directory=True)
    with expect_zone_refusal(ZoneLinkRefusedError, at_least=0):
        assert rm.zone_access(ZonePath(ST, ("proxy", "agents", "Alice", "home")), os.W_OK) is False


def test_zone_access_refuses_a_link_in_the_middle(zone_data_root, state, outside):
    (state / "proxy").mkdir(parents=True)
    (outside / "Alice" / "home").mkdir(parents=True)
    make_dir_link(state / "proxy" / "agents", outside)
    with expect_zone_refusal(ZoneLinkRefusedError, at_least=0):
        assert rm.zone_access(ZonePath(ST, ("proxy", "agents", "Alice", "home")), os.W_OK) is False


@pytest.mark.parametrize(
    "zp",
    [
        ZonePath(ST, ()),
        ZonePath(ST, ("proxy",)),
        ZonePath(ST, ("proxy", "agents")),
        ZonePath(MCP, ()),
        ZonePath(DOC, ()),
        ZonePath(PD, ()),
    ],
    ids=["st", "st_proxy", "st_kind", "mcp", "docgen", "pd"],
)
@pytest.mark.parametrize("method", ["zone_remove_dir", "zone_remove_file"])
def test_removes_refuse_paths_above_the_agent(zone_data_root, state, zp, method):
    _owner_tmp(state)
    (zone_data_root / "mcp_servers" / "managed" / "sample").mkdir(parents=True)
    (zone_data_root / "temp" / "docgen_output").mkdir(parents=True)
    (zone_data_root / "personal_drive" / "Alice").mkdir(parents=True)
    with raises_zone(ZoneSegmentError):
        getattr(rm, method)(zp)
    assert (state / "proxy" / "agents" / "Alice" / "tmp" / "a.txt").is_file()
    assert (zone_data_root / "mcp_servers" / "managed" / "sample").is_dir()
    assert (zone_data_root / "personal_drive" / "Alice").is_dir()


def test_removes_at_the_agent_depth_are_allowed(zone_data_root, state):
    _owner_tmp(state)
    rm.zone_remove_dir(ZonePath(ST, ("proxy", "agents", "Alice")))
    assert not (state / "proxy" / "agents" / "Alice").exists()
    assert (state / "proxy" / "agents").is_dir()
    (zone_data_root / "mcp_servers" / "managed" / "sample").mkdir(parents=True)
    rm.zone_remove_dir(ZonePath(MCP, ("sample",)))
    assert not (zone_data_root / "mcp_servers" / "managed" / "sample").exists()


def test_zone_walk_list_depth(zone_data_root, state):
    base = state / "proxy" / "selftest"
    (base / "a" / "inner").mkdir(parents=True)
    (base / "b").mkdir()
    (base / "f.txt").write_text("f", encoding="utf-8")
    zp = ZonePath(ST, ("proxy", "selftest"))
    shallow = sorted(e.parts for e in rm.zone_walk(zp, WalkReport(), list_depth=1))
    assert shallow == [("a",), ("b",), ("f.txt",)]
    deep = sorted(e.parts for e in rm.zone_walk(zp, WalkReport()))
    assert ("a", "inner") in deep


# --- P6：handoff_cwd ----------------------------------------------------------------


def test_handoff_cwd_outside_any_zone_gives_the_judged_text(tmp_path, zone_data_root):
    kernel = tmp_path.resolve() / "kernel"
    kernel.mkdir()
    with rm.handoff_cwd(str(kernel)) as cwd:
        assert cwd == str(kernel)
        assert not is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)


def test_handoff_cwd_gives_the_text_it_judged(tmp_path, state):
    """區外交出的是判斷用的字串（絕對、以字面消掉 ``..``）：``連結/..`` 實際走進區裡也不算（P6）."""
    base = tmp_path.resolve() / "base"
    (base / "b").mkdir(parents=True)
    deep = state / "proxy" / "deep"
    deep.mkdir(parents=True)
    make_dir_link(base / "b" / "link", deep)
    with rm.handoff_cwd(os.path.join(base, "b", "link", "..", "..")) as cwd:
        assert cwd == str(base)
        assert not is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)


def test_handoff_cwd_inside_a_zone_is_handed_off(zone_data_root, state):
    home = state / "proxy" / "mcp_self" / "tool" / "home"
    home.mkdir(parents=True)
    text = rm.zone_text(ZonePath(ST, ("proxy", "mcp_self", "tool", "home")))
    with rm.handoff_cwd(text) as cwd:
        assert _same(cwd, text)
        assert is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)
    assert not is_handed_off(text, HandoffKind.SUBPROCESS_CWD)


def test_handoff_cwd_through_a_linked_state_root(zone_data_root, tmp_path, monkeypatch):
    """主機上 ST 經過人做的連結：交出的是解析後的寫法，比對要認錨點解析後的位置（P6）."""
    real = tmp_path.resolve() / "real_state"
    (real / "proxy" / "mcp_self" / "tool" / "home").mkdir(parents=True)
    linked = tmp_path.resolve() / "linked_state"
    make_dir_link(linked, real)
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", str(linked))
    text = rm.zone_text(ZonePath(ST, ("proxy", "mcp_self", "tool", "home")))
    assert _same(text, real / "proxy" / "mcp_self" / "tool" / "home")
    with rm.handoff_cwd(text) as cwd:
        assert _same(cwd, text)
        assert is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)


def test_handoff_cwd_refuses_the_literal_form_of_a_linked_anchor(
    zone_data_root, tmp_path, monkeypatch
):
    """字面在區裡、交出的字串卻和要的 cwd 不同（錨點經過連結時的字面寫法）→ 拒絕（P6 第 3 種）."""
    real = tmp_path.resolve() / "real_state"
    (real / "proxy" / "x").mkdir(parents=True)
    linked = tmp_path.resolve() / "linked_state"
    make_dir_link(linked, real)
    monkeypatch.setattr(app_config.settings, "TOUKA_SANDBOX_STATE_DIR", str(linked))
    with raises_zone(ZonePathRequiredError), rm.handoff_cwd(str(linked / "proxy" / "x")):
        pytest.fail("handed off a cwd that is not what was asked for")


def test_handoff_cwd_refuses_a_link_as_the_last_segment(zone_data_root, state, outside):
    owner = state / "proxy" / "mcp_self" / "tool"
    owner.mkdir(parents=True)
    make_dir_link(owner / "home", outside)
    with raises_zone(ZoneLinkRefusedError), rm.handoff_cwd(str(owner / "home")):
        pytest.fail("handed off a link")


def test_handoff_cwd_refuses_a_path_that_is_in_a_zone_only_through_a_link(
    zone_data_root, state, tmp_path
):
    home = state / "proxy" / "mcp_self" / "tool" / "home"
    home.mkdir(parents=True)
    alias = tmp_path.resolve() / "alias"
    make_dir_link(alias, home)
    with raises_zone(ZonePathRequiredError), rm.handoff_cwd(str(alias)):
        pytest.fail("handed off a path that reaches a zone through a link")


@pytest.mark.parametrize("where", ["state_root", "data_root", "managed"])
def test_handoff_cwd_refuses_an_anchor_or_a_folder_holding_zones(zone_data_root, state, where):
    state.mkdir(exist_ok=True)
    (zone_data_root / "mcp_servers" / "managed").mkdir(parents=True)
    text = {
        "state_root": state,
        "data_root": zone_data_root,
        "managed": zone_data_root / "mcp_servers" / "managed",
    }[where]
    with raises_zone(ZonePathRequiredError), rm.handoff_cwd(str(text)):
        pytest.fail("handed off an anchor or a folder holding zones")


def test_handoff_cwd_refuses_a_bad_segment(zone_data_root, state):
    if not WINDOWS:
        pytest.skip("POSIX 上只有 NUL 與分隔符不能當一段，組不出字面在區裡的這種路徑")
    text = str(state / "proxy" / "mcp_self" / "con")
    with raises_zone(ZonePathRequiredError), rm.handoff_cwd(text):
        pytest.fail("handed off a bad segment")
