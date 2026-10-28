"""Hook 指令的路徑界線（ADR-0292）。

hook 以 Touka 自己的權限、不經隔離執行；代理人改得到的程式或腳本若能被 hook 執行，
就等於不經審批跑程式。
所以指令與參數裡的路徑只要落在代理人寫得到的地方就拒收：

- 資料根（各代理人的個人空間、產出物、自建 MCP）、隔離暫存區、所有已登記的專案根目錄；
- 設定資料夾裡只有 ``hooks/`` 可以放（``skills``、``mcp_servers``、``evals`` 由創建與演化寫入）；
- 連結的所在位置與最後指向都要檢查，中間經過的每一個連結也算；
- 指令必須是存在的絕對路徑一般檔；任何參數出現 ``..`` 一律拒收。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from src.models.hooks import CommandHook
from src.services.hooks.paths import HookZones, command_path_problems

if sys.platform == "win32":
    import _winapi


def _make_dir_link(link: Path, target: Path) -> None:
    """目錄連結：能建 symlink 就用 symlink，Windows 沒權限時改用 junction（不需特權、不跳過）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "data": tmp_path / "data",
        "state": tmp_path / "state",
        "project": tmp_path / "projects" / "alpha",
        "config": tmp_path / "config",
        "outside": tmp_path / "bin",
    }
    for directory in paths.values():
        directory.mkdir(parents=True)
    for sub in ("hooks", "skills", "mcp_servers", "evals"):
        (paths["config"] / sub).mkdir()
    for name, base in (
        ("tool.exe", paths["outside"]),
        ("guard.py", paths["config"] / "hooks"),
        ("evil.py", paths["data"]),
        ("skill.py", paths["config"] / "skills"),
        ("server.py", paths["config"] / "mcp_servers"),
        ("proj.py", paths["project"]),
        ("tmp.py", paths["state"]),
    ):
        (base / name).write_text("", encoding="utf-8")
    return paths


def _zones(layout: dict[str, Path]) -> HookZones:
    return HookZones(
        config_dir=layout["config"],
        forbidden=(layout["data"], layout["state"], layout["project"]),
    )


def _hook(command: Path | str, *args: str) -> CommandHook:
    return CommandHook(type="command", command=str(command), args=list(args))


def _reasons(problems) -> set[str]:
    return {p.reason for p in problems}


def test_program_outside_and_script_in_config_hooks_are_accepted(layout):
    hook = _hook(layout["outside"] / "tool.exe", str(layout["config"] / "hooks" / "guard.py"), "-v")
    assert command_path_problems(hook, _zones(layout)) == []


@pytest.mark.parametrize("command", ["tool.exe", "bin/tool.exe"])
def test_command_must_be_absolute(layout, command):
    """不從 PATH 找程式：PATH 上的資料夾不一定是人管的。"""
    assert "not_absolute" in _reasons(command_path_problems(_hook(command), _zones(layout)))


def test_command_must_exist_and_be_a_file(layout):
    assert "missing" in _reasons(
        command_path_problems(_hook(layout["outside"] / "nope.exe"), _zones(layout))
    )
    assert "not_file" in _reasons(command_path_problems(_hook(layout["outside"]), _zones(layout)))


@pytest.mark.parametrize(
    "relative",
    ["data/evil.py", "state/tmp.py", "projects/alpha/proj.py", "config/skills/skill.py",
     "config/mcp_servers/server.py"],
)
def test_command_in_agent_writable_place_is_rejected(layout, tmp_path, relative):
    problems = command_path_problems(_hook(tmp_path / relative), _zones(layout))
    assert "agent_writable" in _reasons(problems)


@pytest.mark.parametrize(
    "relative",
    ["data/evil.py", "projects/alpha/proj.py", "config/skills/skill.py", "data/not_yet_there.py"],
)
def test_script_argument_in_agent_writable_place_is_rejected(layout, tmp_path, relative):
    target = str(tmp_path / relative)
    for args in ([target], ["--script", target], [f"--script={target}"]):
        hook = _hook(layout["outside"] / "tool.exe", *args)
        assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout))), args


def test_parent_segments_are_rejected(layout):
    hook = _hook(layout["outside"] / "tool.exe", "../data/evil.py")
    assert "parent_segment" in _reasons(command_path_problems(hook, _zones(layout)))
    sneaky = layout["config"] / "hooks" / ".." / "skills" / "skill.py"
    assert "parent_segment" in _reasons(command_path_problems(_hook(sneaky), _zones(layout)))


def test_relative_argument_is_read_from_the_hooks_folder(layout):
    """工作目錄是 ``hooks/``；相對參數經過那裡的連結指到代理人資料夾也擋，檔案還沒建也一樣。"""
    _make_dir_link(layout["config"] / "hooks" / "drop", layout["data"])
    for relative in ("drop/evil.py", "drop/later.py", "-c import x; run('drop/evil.py')"):
        hook = _hook(layout["outside"] / "tool.exe", relative)
        assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout))), relative
    plain = _hook(layout["outside"] / "tool.exe", "guard.py", "-v", "--name=demo")
    assert command_path_problems(plain, _zones(layout)) == []


def test_unc_and_drive_relative_paths_are_rejected(layout):
    if sys.platform != "win32":
        assert command_path_problems(_hook("//server/share/tool"), _zones(layout))
        return
    unc = _hook(r"\\server\share\tool.exe")
    assert "unc_path" in _reasons(command_path_problems(unc, _zones(layout)))
    drive_relative = _hook(layout["outside"] / "tool.exe", "C:evil.py")
    assert "not_absolute" in _reasons(command_path_problems(drive_relative, _zones(layout)))


def test_hard_linked_script_in_hooks_is_rejected(layout):
    """``hooks/`` 裡的腳本另有一個名字在代理人資料夾時，代理人改那個名字就改到腳本。"""
    os.link(layout["data"] / "evil.py", layout["config"] / "hooks" / "twin.py")
    hook = _hook(layout["outside"] / "tool.exe", str(layout["config"] / "hooks" / "twin.py"))
    assert "hard_link" in _reasons(command_path_problems(hook, _zones(layout)))


def test_link_pointing_into_agent_writable_place_is_rejected(layout):
    _make_dir_link(layout["config"] / "hooks" / "linked", layout["data"])
    hook = _hook(layout["config"] / "hooks" / "linked" / "evil.py")
    assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout)))


def test_link_located_in_agent_writable_place_is_rejected(layout):
    _make_dir_link(layout["data"] / "bin_link", layout["outside"])
    hook = _hook(layout["data"] / "bin_link" / "tool.exe")
    assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout)))


def test_link_hop_through_agent_writable_place_is_rejected(layout, tmp_path):
    """外面的連結 → 代理人資料夾裡的連結 → 外面的檔：中間那一個可以被代理人改指向。"""
    real = tmp_path / "real"
    real.mkdir()
    (real / "tool.exe").write_text("", encoding="utf-8")
    _make_dir_link(layout["data"] / "hop", real)
    _make_dir_link(layout["outside"] / "entry", layout["data"] / "hop")
    hook = _hook(layout["outside"] / "entry" / "tool.exe")
    assert Path(os.path.realpath(layout["outside"] / "entry" / "tool.exe")).parent == real.resolve()
    assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout)))


def test_path_case_follows_the_platform(layout):
    """Windows 路徑不分大小寫，大寫寫法也落在資料根；POSIX 是另一個路徑。"""
    upper = Path(str(layout["data"] / "evil.py").upper())
    reasons = _reasons(command_path_problems(_hook(upper), _zones(layout)))
    assert ("agent_writable" in reasons) is (sys.platform == "win32")
