"""Hook 指令的路徑界線（ADR-0292）。

hook 以 Touka 自己的權限、不經隔離執行；代理人改得到的程式或腳本若能被 hook 執行，
就等於不經審批跑程式。所以：

- 參數裡的路徑一律要在設定資料夾的 ``hooks/`` 裡（不看專案清單一類會變動的資料）；
- ``hooks/`` 裡的指令與參數：一路不能經過連結（``hooks/`` 本身也算），不能有其他硬連結；
- ``hooks/`` 以外的指令不能落在代理人可能寫得到的地方：資料根、隔離暫存區、工作區根、
  專案根、MCP 伺服器的可寫資料夾；設定資料夾裡只有 ``hooks/`` 可以放；連結的所在位置與
  最後指向都要檢查，中間經過的每一個連結也算；
- 指令必須是存在的絕對路徑一般檔；任何參數出現 ``..`` 一律拒收。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.core.exceptions import ToukaException
from src.models.hooks import CommandHook
from src.services.hooks import paths as paths_module
from src.services.hooks.paths import HookZones, command_path_problems, current_zones
from src.services.resource_manager import resource_manager

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
    ["data/evil.py", "projects/alpha/proj.py", "config/skills/skill.py", "data/not_yet_there.py",
     "bin/tool.exe"],
)
def test_script_argument_outside_hooks_is_rejected(layout, tmp_path, relative):
    """參數只看 ``hooks/``：不在禁區清單裡的地方（``bin/``）也不行，清單漏列也不會放行。"""
    target = str(tmp_path / relative)
    for args in ([target], ["--script", target], [f"--script={target}"]):
        hook = _hook(layout["outside"] / "tool.exe", *args)
        assert "outside_hooks_dir" in _reasons(command_path_problems(hook, _zones(layout))), args


def test_project_list_does_not_change_scripts_in_hooks(layout):
    """專案根蓋住 ``hooks/`` 時，``hooks/`` 裡的腳本照樣可以跑：登記專案不能讓 hook 停擺。"""
    zones = HookZones(
        config_dir=layout["config"],
        forbidden=(*_zones(layout).forbidden, layout["config"] / "hooks", layout["config"]),
    )
    hook = _hook(layout["outside"] / "tool.exe", str(layout["config"] / "hooks" / "guard.py"))
    assert command_path_problems(hook, zones) == []
    assert command_path_problems(_hook(layout["config"] / "hooks" / "guard.py"), zones) == []


def test_parent_segments_are_rejected(layout):
    hook = _hook(layout["outside"] / "tool.exe", "../data/evil.py")
    assert "parent_segment" in _reasons(command_path_problems(hook, _zones(layout)))
    sneaky = layout["config"] / "hooks" / ".." / "skills" / "skill.py"
    assert "parent_segment" in _reasons(command_path_problems(_hook(sneaky), _zones(layout)))


def test_relative_argument_is_read_from_the_hooks_folder(layout):
    """工作目錄是 ``hooks/``；相對參數經過那裡的連結也擋，檔案還沒建也一樣。"""
    _make_dir_link(layout["config"] / "hooks" / "drop", layout["data"])
    for relative in ("drop/evil.py", "drop/later.py", "-c import x; run('drop/evil.py')"):
        hook = _hook(layout["outside"] / "tool.exe", relative)
        assert "linked_path" in _reasons(command_path_problems(hook, _zones(layout))), relative
    plain = _hook(layout["outside"] / "tool.exe", "guard.py", "-v", "--name=demo")
    assert command_path_problems(plain, _zones(layout)) == []


def test_each_entry_of_a_path_list_argument_is_checked(layout, monkeypatch):
    """POSIX 的路徑清單用 ``:`` 串（``-cp a.jar:/data/x.jar``）：每一段都要在 ``hooks/``。"""
    monkeypatch.setattr(paths_module, "_PATH_LIST_SEPARATOR", ":")
    evil = str(layout["data"] / "evil.py")
    for arg in (f"guard.py:{evil}", f"--cp=guard.py:{evil}"):
        hook = _hook(layout["outside"] / "tool.exe", arg)
        assert "outside_hooks_dir" in _reasons(command_path_problems(hook, _zones(layout))), arg
    plain = _hook(layout["outside"] / "tool.exe", "guard.py:other.py")
    assert command_path_problems(plain, _zones(layout)) == []


def test_file_urls_in_arguments_are_rejected(layout):
    """``file:`` 網址看不出最後讀哪個檔，一律拒收（不分大小寫）。"""
    evil = (layout["data"] / "evil.py").as_posix()
    for arg in (f"file:///{evil}", f"--script=FILE://localhost/{evil}", "File:guard.py",
                f"jar:file:/{evil}"):
        hook = _hook(layout["outside"] / "tool.exe", arg)
        assert "file_url" in _reasons(command_path_problems(hook, _zones(layout))), arg
    plain = _hook(layout["outside"] / "tool.exe", "--profile:dev")
    assert command_path_problems(plain, _zones(layout)) == []


def test_paths_glued_to_a_short_option_or_an_at_sign_are_checked(layout):
    """``-I/data/lib`` 會從那裡載入模組；``@/data/args`` 會讀那個參數檔。"""
    data, evil = str(layout["data"]), str(layout["data"] / "evil.py")
    for arg in (f"-I{data}", f"@{evil}", f"--include=-I{data}"):
        hook = _hook(layout["outside"] / "tool.exe", arg)
        assert "outside_hooks_dir" in _reasons(command_path_problems(hook, _zones(layout))), arg
    plain = _hook(layout["outside"] / "tool.exe", "-Ilib/sub", "@scope/pkg", "-v", "@guard.py")
    assert command_path_problems(plain, _zones(layout)) == []


def test_paths_after_a_colon_or_at_sign_inside_a_word_are_checked(layout):
    """``-javaagent:<路徑>`` 會載入那個檔；Windows 的 ``:`` 是磁碟代號、不整個拆，也要查得到。"""
    evil = str(layout["data"] / "evil.py")
    rooted = evil[len(Path(evil).drive) :]  # Windows：同一個磁碟的根目錄起算
    for arg in (f"-javaagent:{evil}", f"-agentpath:{evil}", f"--rcfile:{evil}",
                f"-Xbootclasspath/a:{evil}", f"pkg@{evil}", f"--opt:{rooted}"):
        hook = _hook(layout["outside"] / "tool.exe", arg)
        assert "outside_hooks_dir" in _reasons(command_path_problems(hook, _zones(layout))), arg
    inside = str(layout["config"] / "hooks" / "guard.py")
    plain = _hook(layout["outside"] / "tool.exe", f"-javaagent:{inside}", "--profile:dev",
                  "user@example.com")
    assert command_path_problems(plain, _zones(layout)) == []


def test_double_slash_after_a_colon_or_at_sign_is_checked_unless_it_is_a_url(layout):
    """``-javaagent://host/share/x.jar``、``pkg@//host/x`` 在 Windows 是 UNC；只有 ``:`` 前面整段
    是網址 scheme 時才當網址略過（POSIX 的 ``:`` 另外會拆成路徑清單）。"""
    tool = layout["outside"] / "tool.exe"
    for arg in ("-javaagent://host/share/x.jar", "pkg@//host/share/x"):
        problems = command_path_problems(_hook(tool, arg), _zones(layout))
        if sys.platform == "win32":
            assert _reasons(problems) == {"unc_path"}, arg
        else:
            assert problems, arg
    for arg in ("--url=https://example.com/x", "jdbc:postgresql://db.example/app"):
        problems = command_path_problems(_hook(tool, arg), _zones(layout))
        assert (problems == []) if sys.platform == "win32" else bool(problems), arg


def test_drive_relative_paths_inside_a_word_are_rejected_on_windows(layout):
    """Windows 的 ``D:x`` 相對於那個磁碟的目前目錄，看不出指哪裡；夾在字詞中間或黏在選項後面
    也一樣。POSIX 沒有磁碟代號，這些是 ``hooks/`` 底下的相對名稱。"""
    for arg in ("-javaagent:C:evil.jar", r"x@C:proj\a.txt", "--rcfile:E:x", r"-oD:proj\x"):
        reasons = _reasons(command_path_problems(_hook(layout["outside"] / "tool.exe", arg),
                                                 _zones(layout)))
        assert reasons == ({"not_absolute"} if sys.platform == "win32" else set()), arg


def test_url_arguments_follow_the_platform_path_list_separator(layout, monkeypatch):
    """POSIX 的 ``:`` 也是路徑清單分隔（``-cp https://x/data/evil.jar`` 會讀 ``//x/data/...``），
    網址會被拆出 ``//host/x`` 而拒收；Windows 的 ``:`` 不拆，網址照常（ADR-0292 已知的界線）。"""
    url = _hook(layout["outside"] / "tool.exe", "--url=https://example.com/x")
    problems = command_path_problems(url, _zones(layout))
    if sys.platform == "win32":
        assert problems == []
    else:
        assert problems
    monkeypatch.setattr(paths_module, "_PATH_LIST_SEPARATOR", ":")
    assert command_path_problems(url, _zones(layout))


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


@pytest.mark.parametrize("target", ["data", "outside"])
def test_link_inside_hooks_is_rejected(layout, target):
    """``hooks/`` 裡的連結不論指到哪裡都擋（指向的地方之後可能變成代理人寫得到的）。"""
    _make_dir_link(layout["config"] / "hooks" / "linked", layout[target])
    name = "evil.py" if target == "data" else "tool.exe"
    script = layout["config"] / "hooks" / "linked" / name
    as_command = command_path_problems(_hook(script), _zones(layout))
    as_argument = command_path_problems(_hook(layout["outside"] / "tool.exe", str(script)),
                                        _zones(layout))
    assert "linked_path" in _reasons(as_command)
    assert "linked_path" in _reasons(as_argument)


def test_hooks_folder_that_is_itself_a_link_is_rejected(layout, tmp_path):
    real = tmp_path / "real_hooks"
    real.mkdir()
    (real / "guard.py").write_text("", encoding="utf-8")
    config = tmp_path / "config2"
    config.mkdir()
    _make_dir_link(config / "hooks", real)
    zones = HookZones(config_dir=config, forbidden=_zones(layout).forbidden)
    script = config / "hooks" / "guard.py"
    assert "linked_path" in _reasons(command_path_problems(_hook(script), zones))
    hook = _hook(layout["outside"] / "tool.exe", str(script))
    assert "linked_path" in _reasons(command_path_problems(hook, zones))


def test_command_reaching_hooks_through_a_link_is_rejected(layout):
    """程式字面在外面、經連結指進 ``hooks/``：照 ``hooks/`` 的規則查（要照字面寫），所以拒收；
    不然 ``hooks/`` 裡那個檔另有硬連結在代理人資料夾也查不到。"""
    _make_dir_link(layout["outside"] / "hooks_alias", layout["config"] / "hooks")
    hook = _hook(layout["outside"] / "hooks_alias" / "guard.py")
    assert _reasons(command_path_problems(hook, _zones(layout))) == {"outside_hooks_dir"}


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


def _stat_alias(monkeypatch: pytest.MonkeyPatch, aliases: dict[Path, Path]) -> None:
    """讓 ``aliases`` 的鍵回報值的檔案識別碼。

    模擬同一個資料夾的另一個寫法（8.3 短名、bind mount）：字面與解析後都不在禁區。
    """
    real_stat = paths_module._stat

    def fake(path: Path):
        target = aliases.get(Path(path))
        return real_stat(target if target is not None else path)

    monkeypatch.setattr(paths_module, "_stat", fake)


def test_alias_of_an_agent_folder_is_rejected_by_file_identity(layout, monkeypatch):
    alias = layout["outside"] / "alias"
    alias.mkdir()
    (alias / "evil.py").write_text("", encoding="utf-8")
    _stat_alias(monkeypatch, {alias: layout["data"]})
    hook = _hook(alias / "evil.py")
    assert "agent_writable" in _reasons(command_path_problems(hook, _zones(layout)))


def test_alias_of_the_config_folder_is_rejected(layout, monkeypatch):
    """設定資料夾的別名：``hooks/`` 以外是代理人寫得到的地方；指進 ``hooks/`` 的也要照字面寫
    （不然查不到硬連結），所以都拒收。"""
    alias = layout["outside"] / "cfg"
    for sub, name in (("skills", "skill.py"), ("hooks", "guard.py")):
        (alias / sub).mkdir(parents=True)
        (alias / sub / name).write_text("", encoding="utf-8")
    _stat_alias(monkeypatch, {
        alias: layout["config"],
        alias / "hooks": layout["config"] / "hooks",
        alias / "skills": layout["config"] / "skills",
    })
    zones = _zones(layout)
    assert "agent_writable" in _reasons(
        command_path_problems(_hook(alias / "skills" / "skill.py"), zones)
    )
    assert _reasons(command_path_problems(_hook(alias / "hooks" / "guard.py"), zones)) == {
        "outside_hooks_dir"
    }


def test_a_path_that_cannot_be_read_is_unverifiable(layout, monkeypatch):
    def denied(_path: Path):
        raise PermissionError

    monkeypatch.setattr(paths_module, "_lstat", denied)
    hook = _hook(layout["outside"] / "tool.exe")
    assert _reasons(command_path_problems(hook, _zones(layout))) == {"unverifiable"}


def _fake_mcp_store(monkeypatch: pytest.MonkeyPatch, state: str, grants: tuple = ()) -> None:
    record = SimpleNamespace(sandbox_paths=tuple(
        SimpleNamespace(path=str(path), access=access) for path, access in grants
    ))

    class _Store:
        def __init__(self, config_dir: Path) -> None:
            self.config_dir = config_dir

        def load(self) -> SimpleNamespace:
            return SimpleNamespace(state=state, records={"srv": record} if grants else {})

    monkeypatch.setattr(paths_module, "McpIsolationStore", _Store)


def _fake_zone_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, in_container: bool):
    def under(name: str) -> Path:
        return tmp_path / name

    monkeypatch.setattr(paths_module.profiles, "current_layout", lambda: SimpleNamespace(
        data_root=under("data"), config_dir=under("config"), state_root=under("state"),
        executor_dir=None, in_container=in_container, mirror_roots=(under("mirror"),),
    ))

    async def projects() -> list[str]:
        return [str(under("proj")), "relative/proj"]

    async def translate(path: str) -> str:
        return str(under("host") / Path(path).name) if Path(path).is_absolute() else path

    async def workspace() -> Path:
        return under("ws")

    monkeypatch.setattr(paths_module, "list_project_root_paths", projects)
    monkeypatch.setattr(resource_manager, "translate_path", translate)
    monkeypatch.setattr(resource_manager, "get_workspace_jail_root", workspace)
    return under


@pytest.mark.parametrize("in_container", [True, False])
async def test_zones_cover_agent_folders_workspace_projects_mcp_and_container_mirrors(
    tmp_path, monkeypatch, in_container
):
    under = _fake_zone_sources(monkeypatch, tmp_path, in_container)
    _fake_mcp_store(monkeypatch, "ok", ((under("mcp_rw"), "rw"), (under("mcp_ro"), "ro")))
    zones = await current_zones()
    expected = {
        under("data"), under("state"), under("ws"), under("host") / "ws", under("proj"),
        under("host") / "proj", under("mcp_rw"),
    }
    if in_container:
        expected.add(under("mirror"))
    assert set(zones.forbidden) == expected
    assert zones.config_dir == under("config")


async def test_zones_fail_when_the_mcp_isolation_file_cannot_be_read(tmp_path, monkeypatch):
    """讀不到 MCP 隔離設定就不知道哪些資料夾可寫：丟例外，呼叫端當成不能執行。"""
    _fake_zone_sources(monkeypatch, tmp_path, in_container=False)
    _fake_mcp_store(monkeypatch, "unreadable")
    with pytest.raises(ToukaException):
        await current_zones()
