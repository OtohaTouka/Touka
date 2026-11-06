"""隔離層的形狀判斷改呼叫 ``zone_of`` 之後，分類結果和以前相同（ADR-0293 R3、Z10 行為測試）.

逐列寫明以前的結果：大小寫不同、``custom/<id>`` 與 ``custom/<id>/drive`` 的差別、錨點本身與
上層。代理人指令的個人空間、MCP 的「代理人影響得到」與權限自癒的白名單都在這裡。改之前的
程式也全綠（只有標明的 Windows 大小寫一列是刻意改的）。只用 tmp_path 建的假版面。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from src.models.identity import AgentIdentity
from src.services.isolation.profiles import (
    ProfileError,
    SandboxLayout,
    build_agent_command_profile,
)
from src.services.isolation.repair import WhitelistMatch, match_whitelist
from src.utils.mcp_origin import agent_influenced

from tests.support.isolation import match_state_root
from tests.support.links import make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

WINDOWS = sys.platform == "win32"
ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
_AGENT, _STATE, _MOUNT = "agent_area", "state_area", "mount_root"
_PD_ALICE, _MANAGED = "data/personal_drive/Alice", "data/mcp_servers/managed"
_TOOL = f"{_MANAGED}/tool"


@pytest.fixture(autouse=True)
def _rm_state_matches_layout(tmp_path, monkeypatch):
    """RM 的 ST 指向本檔版面的 ST（ADR-0293 P7：兩者不同時 ST 的存取函式拒絕）."""
    match_state_root(monkeypatch, tmp_path / "install" / "st")


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    install = tmp_path.resolve() / "install"
    data = install / "data"
    for sub in ("personal_drive/Alice", "custom/Alice/drive", "custom/M1/drive", "soul/Alice"):
        (data / sub).mkdir(parents=True)
    for sub in ("ToukaProxy/config/skills", "host_root", "workspace", "st"):
        (install / sub).mkdir(parents=True)
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
        in_container=False,
    )


# --- 代理人指令的個人空間（profiles._own_drive）-------------------------------------


@pytest.mark.parametrize("rel", ["personal_drive/Alice", "custom/Alice/drive"])
async def test_the_agent_profile_takes_either_own_drive_shape(layout, rel):
    agent_dir = layout.data_root / rel
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    assert profile.cwd == agent_dir


@pytest.mark.parametrize(
    "rel",
    [
        "personal_drive/Alice/sub",
        "custom/Alice",
        "custom/Alice/drive/sub",
        "custom/M1/drive",
    ],
)
async def test_the_agent_profile_refuses_other_shapes(layout, rel):
    target = layout.data_root / rel
    target.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ProfileError):
        await build_agent_command_profile(ALICE, target, layout=layout)


async def test_the_agent_profile_takes_a_dot_dot_that_lands_on_its_drive(layout):
    agent_dir = layout.data_root / "personal_drive" / "Alice" / ".." / "Alice"
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    assert profile.cwd == layout.data_root / "personal_drive" / "Alice"


async def test_the_agent_profile_refuses_a_linked_drive(layout, tmp_path):
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    link = layout.data_root / "personal_drive" / "Alice"
    link.rmdir()
    make_dir_link(link, elsewhere)
    with pytest.raises(ProfileError):
        await build_agent_command_profile(ALICE, link, layout=layout)


@pytest.mark.skipif(not WINDOWS, reason="大小寫不分只在 Windows")
async def test_the_agent_profile_ignores_case_on_windows(layout):
    agent_dir = layout.data_root / "PERSONAL_DRIVE" / "alice"
    profile = await build_agent_command_profile(ALICE, agent_dir, layout=layout)
    assert profile.cwd == layout.data_root / "personal_drive" / "Alice"


# --- MCP 的「代理人影響得到」（mcp_origin.agent_influenced）---------------------------


@pytest.mark.parametrize(
    ("rel", "code_dir", "expected"),
    [
        ("personal_drive", False, True),
        ("personal_drive/Alice/x.py", False, True),
        ("custom", False, False),
        ("custom", True, True),
        ("custom/M1", False, False),
        ("custom/M1", True, True),
        ("custom/M1/drive", False, True),
        ("custom/M1/drive/x.py", False, True),
        ("custom/M1/config.json", False, False),
        ("custom/M1/config.json", True, False),
        ("custom/M1/driver/x.py", False, False),
        ("", False, False),
        ("", True, True),
        ("mcp_servers", False, False),
        ("mcp_servers", True, True),
        ("mcp_servers/managed", False, True),
        ("mcp_servers/managed/tool/main.py", False, True),
        ("mcp_servers/other/x.py", False, False),
        ("temp", False, False),
        ("temp", True, True),
        ("temp/docgen_output", False, True),
        ("temp/docgen_output/a.pdf", False, True),
        ("soul/Alice", False, False),
        ("soul/Alice", True, False),
    ],
)
def test_agent_influenced_by_shape(layout, rel, code_dir, expected):
    path = layout.data_root / rel if rel else layout.data_root
    assert agent_influenced(path, layout, code_dir=code_dir) is expected


@pytest.mark.parametrize(("rel", "expected"), [("", True), ("home/Alice", True), ("x", True)])
def test_agent_influenced_in_the_state_root(layout, rel, expected):
    path = layout.state_root / rel if rel else layout.state_root
    assert agent_influenced(path, layout, code_dir=False) is expected


def test_agent_influenced_through_a_link(layout, tmp_path):
    link = tmp_path.resolve() / "to_alice"
    make_dir_link(link, layout.data_root / "personal_drive" / "Alice")
    assert agent_influenced(link / "x.py", layout, code_dir=False) is True


@pytest.mark.skipif(not WINDOWS, reason="大小寫不分只在 Windows")
def test_agent_influenced_ignores_case_on_windows(layout):
    data = layout.data_root
    assert agent_influenced(data / "PERSONAL_DRIVE" / "alice" / "x", layout, code_dir=False)
    # 刻意改的一列（ADR-0293）：以前 ``drive`` 這一段分大小寫，Windows 上同一個資料夾被漏判
    assert agent_influenced(data / "custom" / "M1" / "DRIVE" / "x", layout, code_dir=False)


# --- 權限自癒的白名單（repair.match_whitelist）---------------------------------------


def _match(layout: SandboxLayout, root: str, scope: str, kind: str, reserved: bool):
    base = {"data": layout.data_root, "st": layout.state_root}
    head, _, tail = root.partition("/")
    root_path = base[head] / tail if tail else base[head]
    head, _, tail = scope.partition("/")
    scope_path = base[head] / tail if tail else base[head]
    return WhitelistMatch(root_path, scope_path, kind, reserved)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("data/personal_drive", ("data/personal_drive", "data/personal_drive", _MOUNT, True)),
        ("data/personal_drive/Alice", ("data/personal_drive", _PD_ALICE, _AGENT, False)),
        ("data/personal_drive/Alice/a/b", ("data/personal_drive", _PD_ALICE, _AGENT, False)),
        ("data/custom", ("data/custom", "data/custom", _MOUNT, True)),
        ("data/custom/M1", None),
        ("data/custom/M1/config.json", None),
        ("data/custom/M1/drive", ("data/custom", "data/custom/M1/drive", _AGENT, False)),
        ("data/custom/M1/drive/x", ("data/custom", "data/custom/M1/drive", _AGENT, False)),
        ("data/mcp_servers", ("data/mcp_servers", "data/mcp_servers", _MOUNT, True)),
        ("data/mcp_servers/managed", ("data/mcp_servers", _MANAGED, _MOUNT, True)),
        ("data/mcp_servers/managed/tool/x", ("data/mcp_servers", _TOOL, _AGENT, False)),
        ("data/mcp_servers/other/x", None),
        ("data/temp/docgen_output/x", None),
        ("data/soul/Alice", None),
        ("data", None),
        ("st", ("st", "st", _STATE, True)),
        ("st/proxy", ("st", "st/proxy", _STATE, True)),
        ("st/proxy/home/x", ("st", "st/proxy", _STATE, False)),  # noqa: PATH_LITERAL_SSOT
        ("st/other/x", None),
    ],
)
def test_whitelist_by_shape(layout, target, expected):
    head, _, tail = target.partition("/")
    base = {"data": layout.data_root, "st": layout.state_root}[head]
    path = base / tail if tail else base
    want = None if expected is None else _match(layout, *expected)
    assert match_whitelist(path, layout) == want


def test_whitelist_does_not_follow_a_link_into_a_drive(layout, tmp_path):
    """白名單只看字面：經連結到個人空間的路徑不在白名單（解析後的那次另外比）."""
    link = tmp_path.resolve() / "to_alice"
    make_dir_link(link, layout.data_root / "personal_drive" / "Alice")
    assert match_whitelist(link / "x", layout) is None
