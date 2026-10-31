"""MCP 來源分類（C2-3 U3c）：origin 與紀錄狀態、啟發式只會更嚴、``deployed_skills``.

分類是純函式：origin 與紀錄狀態由呼叫端（mcp_launch）傳入，不讀任何存放位置；只看條目
引用的路徑。全部用 tmp_path 建的假版面。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from src.services.isolation.profiles import SandboxLayout
from src.utils import mcp_origin
from src.utils.isolation_policy import MCP_SELF_ORIGINS, MCP_THIRD_PARTY_ORIGINS
from src.utils.mcp_origin import (
    ALL_SKILLS_DEPLOYED,
    NO_RECORD,
    UNREADABLE_RECORD,
    OriginRecord,
    classify,
    deployed_skills,
    proxy_base_dir,
)


@dataclass(frozen=True)
class Entry:
    """server_config 條目裡分類會看的四個欄位（``ProtocolServerConfig`` 的形狀）。"""

    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str | None = None


PACKAGE = Entry(command="npx", args=("-y", "@modelcontextprotocol/server-everything"))
_winapi = importlib.import_module("_winapi") if os.name == "nt" else None


def _layout(root: Path, *, mirrors: tuple[str, ...] = ("host_root", "workspace")) -> SandboxLayout:
    install = root.resolve() / "install"
    data = install / "data"
    for sub in (
        "personal_drive/Alice",
        "custom/Bob/drive",
        "mcp_servers/managed/foo",
        "temp/docgen_output",
        "soul/Alice",
    ):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("ToukaProxy/config/mcp_servers/demo", "workspace/myproj", "host_root", "st"):
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
        mirror_roots=tuple(install / m for m in mirrors),
        in_container=True,
    )


@pytest.fixture
def layout(tmp_path: Path) -> SandboxLayout:
    return _layout(tmp_path)


def _classify(layout: SandboxLayout, entry: Entry, record: OriginRecord, name: str = "srv"):
    return classify(name, entry, record, layout, base_dir=layout.proxy_root)


def _scaffold_script(layout: SandboxLayout, name: str = "demo") -> Path:
    script = layout.config_dir / "mcp_servers" / name / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")
    return script


# --- origin 與紀錄狀態 --------------------------------------------------------------------


@pytest.mark.parametrize("origin", sorted(MCP_THIRD_PARTY_ORIGINS))
def test_third_party_origins_are_third_party(layout, origin):
    result = _classify(layout, PACKAGE, OriginRecord("present", origin))
    assert (result.kind, result.origin, result.reason) == ("third_party", origin, "origin")


@pytest.mark.parametrize("origin", sorted(MCP_SELF_ORIGINS))
def test_self_origins_are_self(layout, origin):
    result = _classify(layout, PACKAGE, OriginRecord("present", origin))
    assert (result.kind, result.origin, result.reason) == ("self", origin, "origin")


@pytest.mark.parametrize("origin", ["Hub", "", "unknown", " manual", 1, None, ["hub"]])
def test_unknown_origin_in_a_record_is_self_not_unlabeled(layout, origin):
    """看不懂的 origin 不能當成較寬的類別（未標示會走第三方）。"""
    result = _classify(layout, PACKAGE, OriginRecord("present", origin))
    assert (result.kind, result.origin, result.reason) == ("self", None, "origin_unknown")


def test_unreadable_record_is_self_even_for_a_plain_package(layout):
    result = _classify(layout, PACKAGE, UNREADABLE_RECORD)
    assert (result.kind, result.origin, result.reason) == ("self", None, "record_unreadable")


def test_no_record_and_no_heuristic_hit_is_third_party(layout):
    result = _classify(layout, PACKAGE, NO_RECORD)
    assert (result.kind, result.origin, result.reason) == ("third_party", None, "unlabeled")


@pytest.mark.parametrize(
    "record", [NO_RECORD, UNREADABLE_RECORD, OriginRecord("present", "agent")]
)
def test_remote_entry_is_remote_whatever_the_record(layout, record):
    result = _classify(layout, Entry(url="https://mcp.example.com/mcp"), record)
    assert result.kind == "remote"


def test_proxy_base_dir_follows_the_working_directory_children_inherit(
    layout, tmp_path, monkeypatch
):
    """``base_dir``＝未隔離子行程繼承的 cwd：換了 cwd，``./x.py`` 就依新的 cwd 判斷."""
    entry = Entry(command="python", args=("./x.py",))
    record = OriginRecord("present", "manual")
    drive = layout.data_root / "personal_drive" / "Alice"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    kinds = []
    for cwd in (drive, elsewhere):
        monkeypatch.chdir(cwd)
        assert proxy_base_dir() == cwd.resolve()
        kinds.append(classify("srv", entry, record, layout, base_dir=proxy_base_dir()).kind)
    assert kinds == ["self", "third_party"]


# --- scaffold ---------------------------------------------------------------------------


def test_scaffold_origin_with_its_own_script_is_scaffold(layout):
    script = _scaffold_script(layout)
    entry = Entry(command="python", args=(str(script),))
    result = _classify(layout, entry, OriginRecord("present", "scaffold"), name="demo")
    assert (result.kind, result.origin) == ("scaffold", "scaffold")
    assert result.scaffold_dir == script.parent


def test_unlabeled_entry_with_the_scaffold_shape_is_scaffold(layout):
    script = _scaffold_script(layout)
    result = _classify(layout, Entry(command="python", args=(str(script),)), NO_RECORD, "demo")
    assert (result.kind, result.reason) == ("scaffold", "unlabeled_scaffold_shape")


def test_scaffold_origin_whose_script_is_in_another_folder_is_self(layout):
    """形狀對不上（別台的資料夾、不是直接在自己的資料夾裡）不給 scaffold 的唯讀資料夾。"""
    other = _scaffold_script(layout, "other")
    nested = layout.config_dir / "mcp_servers" / "demo" / "sub" / "server.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("", encoding="utf-8")
    for script in (other, nested):
        entry = Entry(command="python", args=(str(script),))
        result = _classify(layout, entry, OriginRecord("present", "scaffold"), name="demo")
        assert (result.kind, result.scaffold_dir) == ("self", None)


def test_manual_origin_with_the_scaffold_shape_stays_third_party(layout):
    script = _scaffold_script(layout)
    entry = Entry(command="python", args=(str(script),))
    result = _classify(layout, entry, OriginRecord("present", "manual"), name="demo")
    assert (result.kind, result.scaffold_dir) == ("third_party", None)


# --- 啟發式：對所有條目都套用、只會更嚴 -------------------------------------------------------


def _agent_files(layout: SandboxLayout) -> dict[str, Path]:
    """代理人影響得到的根底下各放一個檔（workspace 的使用者專案也算）。"""
    data = layout.data_root
    files = {
        "personal_drive": data / "personal_drive" / "Alice" / "server.py",
        "custom_drive": data / "custom" / "Bob" / "drive" / "tool.js",
        "managed": data / "mcp_servers" / "managed" / "foo" / "server.py",
        "state": layout.state_root / "proxy" / "agents" / "Alice" / "home" / "x.py",
        "docgen_output": data / "temp" / "docgen_output" / "run.py",
        "workspace_project": layout.install_root / "workspace" / "myproj" / "server.py",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return files


AGENT_ROOTS = (
    "personal_drive", "custom_drive", "managed", "state", "docgen_output", "workspace_project"
)


@pytest.mark.parametrize("root", AGENT_ROOTS)
@pytest.mark.parametrize("origin", ["hub", "skill", "manual", "scaffold", None])
def test_a_file_in_an_agent_root_makes_any_entry_self(layout, root, origin):
    """引用代理人寫得到的「檔案」就是自建，不論 origin（只會更嚴）。"""
    target = _agent_files(layout)[root]
    record = NO_RECORD if origin is None else OriginRecord("present", origin)
    for entry in (
        Entry(command="python", args=(str(target),)),
        Entry(command=str(target)),
        Entry(command="node", args=("main.js",), env={"CONFIG": str(target)}),
    ):
        result = _classify(layout, entry, record, name="demo")
        assert (result.kind, result.reason) == ("self", "agent_reference")


def test_a_folder_in_an_agent_root_is_self_even_as_a_data_argument(layout):
    """參數用途無法證明時從嚴，資料目錄也不能繞過來源分類。"""
    folder = layout.data_root / "personal_drive" / "Alice"
    project = layout.install_root / "workspace" / "myproj"
    entry = Entry(command="npx", args=("-y", "fs", str(folder), "--repository", str(project)))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("command", ["python", "python3.12", "node", "deno", "bun", "npx", "uvx"])
def test_an_agent_folder_used_as_the_program_is_self(layout, command):
    folder = layout.data_root / "personal_drive" / "Alice"
    assert _classify(layout, Entry(command=command, args=(str(folder),)), NO_RECORD).kind == "self"


@pytest.mark.parametrize(("command", "prefix"), [
    ("python", ("-X", "utf8")), ("python", ("-W", "ignore")),
    ("python", ("-O", "-X", "utf8", "-W", "ignore")),
    ("node", ("--inspect-port", "9230")),
])
def test_program_directory_after_separate_option_values_is_self(layout, command, prefix):
    folder = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command=command, args=(*prefix, str(folder)))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("prefix", [
    ("python",), ("-i", "python"), ("LANG=C", "python"),
    ("-i", "LANG=C", "python", "-X", "utf8"),
])
def test_env_wrapped_program_directory_is_self(layout, prefix):
    folder = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command="env", args=(*prefix, str(folder)))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize(("command", "prefix"), [
    ("py", ()), ("py.exe", ("-3",)), ("pythonw", ()), ("pythonw.exe", ()),
    ("bunx", ()), ("tsx", ()), ("uv", ("run",)), ("uv", ("run", "python")),
])
def test_alternate_runner_program_directory_is_self(layout, command, prefix):
    folder = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command=command, args=(*prefix, str(folder)))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("args", [
    ("-m", "package.module"), ("-c", "package.function()"),
    ("-W", "ignore::package.Warning", "-m", "package.module"),
])
def test_module_or_option_values_without_agent_paths_stay_third_party(layout, args):
    entry = Entry(command="python", args=args)
    assert _classify(layout, entry, NO_RECORD).kind == "third_party"


@pytest.mark.parametrize(("command", "prefix"), [
    ("node", ("--require", "helper")), ("deno", ("run",)), ("bun", ("run",)),
])
def test_program_folder_after_loader_options_is_self(layout, command, prefix):
    folder = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command=command, args=(*prefix, str(folder)))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


def test_macos_case_alias_is_compared_conservatively(layout, monkeypatch):
    script = _agent_files(layout)["personal_drive"]
    monkeypatch.setattr(mcp_origin.sys, "platform", "darwin")
    entry = Entry(command="python", args=(str(script).upper(),))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("name", ["UV_FIND_LINKS", "PIP_INDEX_URL", "UV_EXTRA_INDEX_URL",
                                      "npm_config_registry", "GIT_EXEC_PATH", "X_CACHE_DIR",
                                      "X_INSTALL_DIR", "GIT_SSH_COMMAND"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_package_env_directory_is_code(layout, name, ancestor):
    folder = layout.data_root if ancestor else layout.data_root / "personal_drive" / "Alice"
    value = folder.as_uri() if "INDEX" in name else str(folder)
    entry = Entry(command="uvx", args=("pkg",), env={name: value})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("flag", ["--index-url", "-i", "--extra-index-url", "--index",
                                      "--default-index", "--registry"])
def test_package_index_flags_load_code(layout, flag):
    folder = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command="uvx", args=(flag, folder.as_uri(), "pkg"))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


@pytest.mark.parametrize("where", ["arg", "env"])
def test_embedded_shell_file_reference_is_self(layout, where):
    script = _agent_files(layout)["personal_drive"]
    text = f"sh {script}"
    entry = Entry(command="sh", args=("-c", text)) if where == "arg" else Entry(
        command="uvx", args=("pkg",), env={"GIT_SSH_COMMAND": text}
    )
    assert _classify(layout, entry, NO_RECORD).kind == "self"


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespaces")
@pytest.mark.parametrize("namespace", ["extended", "unc"])
def test_windows_path_namespace_cannot_claim_third_party(layout, namespace):
    script = _agent_files(layout)["personal_drive"]
    path = "\\\\?\\" + str(script) if namespace == "extended" else (
        "\\\\localhost\\" + script.drive[0] + "$\\" + str(script)[3:]
    )
    assert _classify(layout, Entry(command="python", args=(path,)), NO_RECORD).kind == "self"


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespaces")
def test_unc_classification_does_not_probe_a_network_share(layout, monkeypatch):
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        assert path.drive[:2] != "\\\\", "classification must not probe a UNC share"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    entry = Entry(command="python", args=(r"\\localhost\share\server.py",))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


def test_missing_agent_paths_are_self_even_without_a_suffix(layout):
    drive = layout.data_root / "personal_drive" / "Alice"
    later = Entry(command="python", args=(str(drive / "later.py"),))
    folder = Entry(command="tool", args=(str(drive / "later_dir"),))
    assert _classify(layout, later, NO_RECORD).kind == "self"
    assert _classify(layout, folder, NO_RECORD).kind == "self"


def test_existing_file_without_a_suffix_is_a_file(layout):
    binary = layout.data_root / "personal_drive" / "Alice" / "run"
    binary.write_text("", encoding="utf-8")
    entry = Entry(command=str(binary))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


def test_relative_paths_with_a_separator_resolve_against_the_proxy_cwd(layout):
    """相對路徑以 proxy 的工作目錄（今天子行程的 cwd）補成絕對路徑；沒有分隔符的不算路徑。"""
    drive = layout.data_root / "personal_drive" / "Alice"
    (drive / "server.py").write_text("", encoding="utf-8")
    with_sep = Entry(command="python", args=("./server.py",))
    bare = Entry(command="python", args=("server.py",))
    up = Entry(command="python", args=("../Alice/server.py",))
    assert classify("s", with_sep, NO_RECORD, layout, base_dir=drive).kind == "self"
    assert classify("s", bare, NO_RECORD, layout, base_dir=drive).kind == "self"
    assert classify("s", up, NO_RECORD, layout, base_dir=drive / "sub").kind == "self"


def test_pathsep_lists_are_split(layout):
    target = _agent_files(layout)["personal_drive"]
    joined = os.pathsep.join([str(layout.install_root), str(target)])
    assert _classify(layout, Entry(command="tool", args=(joined,)), NO_RECORD).kind == "self"
    env_entry = Entry(command="tool", env={"EXTRA": joined})
    assert _classify(layout, env_entry, NO_RECORD).kind == "self"


@pytest.mark.parametrize(
    "name", ["PYTHONPATH", "pythonpath", "NODE_PATH", "LD_LIBRARY_PATH", "PATH", "Uv_Tool_Dir"]
)
def test_code_loading_env_pointing_at_an_agent_folder_is_self(layout, name):
    """會載入程式碼的環境變數指到代理人影響得到的地方，資料夾也算（名稱不分大小寫）。"""
    drive = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command="python", args=("-m", "srv"), env={name: str(drive)})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


def test_agent_folder_under_a_plain_env_name_is_self(layout):
    drive = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command="python", args=("-m", "srv"), env={"INPUT_DIR": str(drive)})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("ancestor", ["", "mcp_servers", "temp", "custom", "custom/Bob"])
def test_code_loading_env_at_an_ancestor_of_an_agent_root_is_self(layout, ancestor):
    """上層放進 PYTHONPATH：namespace package 可以 import 代理人資料夾裡的程式。"""
    env = {"PYTHONPATH": str(layout.data_root / ancestor)}
    entry = Entry(command="python", args=("-m", "srv"), env=env)
    assert _classify(layout, entry, NO_RECORD).kind == "self"


def test_code_loading_env_at_an_unrelated_data_folder_is_not_a_hit(layout):
    env = {"PYTHONPATH": str(layout.data_root / "soul" / "Alice")}
    entry = Entry(command="python", args=("-m", "srv"), env=env)
    assert _classify(layout, entry, NO_RECORD).kind == "third_party"


def test_node_options_require_is_split_on_whitespace(layout):
    script = _agent_files(layout)["personal_drive"]
    for value in (f"--require {script}", f"--require={script}"):
        entry = Entry(command="node", args=("main.js",), env={"NODE_OPTIONS": value})
        assert _classify(layout, entry, NO_RECORD).kind == "self"


@pytest.mark.parametrize("flag", ["--from", "--with", "-e", "--editable", "--directory"])
def test_package_source_flags_count_folders(layout, flag):
    """``uvx --from <資料夾>`` 一類：下一項落在代理人影響得到的根底下就是自建，資料夾也算。"""
    package = layout.data_root / "personal_drive" / "Alice"
    split = Entry(command="uvx", args=(flag, str(package), "tool"))
    joined = Entry(command="uvx", args=(f"{flag}={package}", "tool"))
    without = Entry(command="uvx", args=("tool", str(package)))
    assert _classify(layout, split, OriginRecord("present", "manual")).kind == "self"
    assert _classify(layout, joined, OriginRecord("present", "manual")).kind == "self"
    assert _classify(layout, without, OriginRecord("present", "manual")).kind == "self"


def test_file_urls_are_paths(layout):
    package = layout.data_root / "personal_drive" / "Alice"
    entry = Entry(command="uvx", args=("--from", f"git+file://{package.as_posix()}", "tool"))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


def test_hub_sentinels_referencing_writable_roots_are_self(layout):
    """哨兵仍須解析後分類，不能以已知套件名稱豁免。"""
    docgen = Entry(
        command="uvx",
        args=("mcp-docgen",),
        env={"MCP_DOCGEN_INPUT_DIR": "@TOUKA_APPDATA@", "MCP_DOCGEN_OUTPUT_DIR": "@TOUKA_OUTPUT@"},
    )
    filesystem = Entry(command="npx", args=("-y", "server-filesystem", "@TOUKA_APPDATA@"))
    for entry in (docgen, filesystem):
        assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("root", AGENT_ROOTS)
@pytest.mark.parametrize(("command", "prefix", "embedded"), [
    ("sh", ("-c",), True), ("bash", ("-c",), True),
    ("cmd", ("/c", "python"), False), ("powershell", ("-Command",), True),
    ("npm", ("exec",), False), ("pipx", ("run",), False),
    ("poetry", ("run", "python"), False), ("pypy3", (), False),
    ("ts-node", (), False), ("unknown-wrapper", ("run",), False),
])
def test_any_wrapper_agent_directory_is_self(layout, root, command, prefix, embedded):
    folder = _agent_files(layout)[root].parent / "program with spaces"
    folder.mkdir()
    value = f'python "{folder}"' if embedded else str(folder)
    entry = Entry(command=command, args=(*prefix, value))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("where", ["command", "argument", "option", "env"])
@pytest.mark.parametrize("existing", [False, True])
def test_bare_relative_agent_paths_are_self(layout, where, existing):
    base = layout.data_root / "personal_drive" / "Alice"
    if existing:
        (base / "program").mkdir()
    entry = {
        "command": Entry(command="./program"),
        "argument": Entry(command="unknown-wrapper", args=("./program",)),
        "option": Entry(command="unknown-wrapper", args=("--anything=./program",)),
        "env": Entry(command="unknown-wrapper", env={"ARBITRARY": "./program"}),
    }[where]
    assert classify("s", entry, NO_RECORD, layout, base_dir=base).kind == "self"


def test_existing_directory_as_unknown_program_is_self(layout, tmp_path):
    folder = tmp_path / "program"
    folder.mkdir()
    entry = Entry(command="unknown-wrapper", args=(str(folder),))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


@pytest.mark.parametrize("where", ["argument", "env"])
@pytest.mark.parametrize("template", ["--load='{path}'", 'PROGRAM="{path}"',
                                      'python;python "{path}"', 'python "{path}"&&exit'])
def test_embedded_quoted_agent_paths_are_self(layout, where, template):
    folder = layout.data_root / "personal_drive" / "Alice" / "program with spaces"
    folder.mkdir()
    value = template.format(path=folder)
    entry = (Entry(command="wrapper", args=(value,)) if where == "argument"
             else Entry(command="wrapper", env={"ANY": value}))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


@pytest.mark.parametrize("entry", [
    Entry(command="npx", args=("-y", "pkg")), Entry(command="uvx", args=("pkg",)),
    Entry(command="docker", args=("run", "image")),
])
def test_plain_third_party_commands_remain_third_party(layout, entry):
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "third_party"


@pytest.mark.parametrize("command", ["npx", "uvx", "bunx"])
def test_scoped_name_that_also_resolves_to_a_directory_is_self(layout, command):
    base = layout.data_root / "personal_drive" / "Alice"
    (base / "@scope" / "pkg").mkdir(parents=True)
    entry = Entry(command=command, args=("@scope/pkg",))
    assert classify("s", entry, NO_RECORD, layout, base_dir=base).kind == "self"


def test_sentinel_in_a_code_loading_env_is_resolved_first(layout):
    entry = Entry(command="python", args=("-m", "srv"), env={"PYTHONPATH": "@TOUKA_OUTPUT@"})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


def test_files_outside_agent_roots_are_not_hits(layout):
    soul = layout.data_root / "soul" / "Alice" / "x.py"
    soul.write_text("", encoding="utf-8")
    script = _scaffold_script(layout)
    for path in (soul, script):
        entry = Entry(command="python", args=(str(path),))
        assert _classify(layout, entry, OriginRecord("present", "manual")).kind == "third_party"


def test_workspace_folders_that_are_touka_directories_are_not_agent_roots(layout):
    """鏡像根底下只有代理人可能拿到的專案算；Touka 目錄（checkout 裡的程式、E）不算。"""
    workspace = layout.install_root / "workspace"
    for name in ("ToukaProxy", "scripts"):
        (workspace / name).mkdir()
    tracked = workspace / "ToukaProxy" / "server.py"
    tracked.write_text("", encoding="utf-8")
    sealed = layout.install_root / "host_root" / "tool.py"
    sealed.write_text("", encoding="utf-8")
    for path in (tracked, sealed):
        entry = Entry(command="python", args=(str(path),))
        assert _classify(layout, entry, OriginRecord("present", "manual")).kind == "third_party"
    project = workspace / "myproj" / "server.py"
    project.write_text("", encoding="utf-8")
    entry = Entry(command="python", args=(str(project),))
    assert _classify(layout, entry, NO_RECORD).kind == "self"


def test_native_install_root_as_mirror_keeps_touka_venv_out(tmp_path):
    """原生安裝的鏡像根就是 repo：Touka 自己的 .venv 不是代理人寫得到的，scaffold 照舊。"""
    layout = _layout(tmp_path, mirrors=("",))
    python = layout.install_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    script = _scaffold_script(layout)
    entry = Entry(command=str(python), args=(str(script),))
    assert _classify(layout, entry, NO_RECORD, name="demo").kind == "scaffold"


def test_missing_folder_under_a_mirror_root_counts(layout):
    """鏡像根底下還不存在的資料夾查不了 Touka 目錄：當成代理人可能寫得到（只會更嚴）。"""
    later = layout.install_root / "workspace" / "newproj" / "server.py"
    assert _classify(layout, Entry(command="python", args=(str(later),)), NO_RECORD).kind == "self"


def _dir_link(link: Path, target: Path) -> None:
    """建資料夾連結：符號連結；Windows 沒有權限時改用 junction；都不行就跳過。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if _winapi is None:
            pytest.skip("cannot create directory links here")
        _winapi.CreateJunction(str(target), str(link))


def test_links_are_resolved_before_judging(layout, tmp_path):
    """連結先 resolve：外面的連結指進代理人資料夾，引用經過它的檔案就是自建。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    drive = layout.data_root / "personal_drive" / "Alice"
    (drive / "server.py").write_text("", encoding="utf-8")
    _dir_link(outside / "via", drive)
    entry = Entry(command="python", args=(str(outside / "via" / "server.py"),))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


def test_a_link_itself_counts_as_a_file(layout, tmp_path):
    """路徑本身是連結時當「檔案」：代理人資料夾裡指向外面的連結也算（代理人可以改指向）。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = layout.data_root / "personal_drive" / "Alice" / "tools"
    _dir_link(link, elsewhere)
    entry = Entry(command="npx", args=("-y", "fs", str(link)))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


# --- deployed_skills：讀原始設定（含停用的條目）、看路徑不看名稱、讀不到一律當已部署 ---


def _managed(layout: SandboxLayout, skill: str, *parts: str) -> str:
    return str(layout.data_root / "mcp_servers" / "managed" / skill / Path(*parts))


def _deployed(layout: SandboxLayout, document: object):
    return deployed_skills(document, layout, base_dir=layout.proxy_root)


def test_renamed_entry_still_marks_its_skill_deployed(layout):
    """人把條目改名：只看名稱擋不住，要看指令與參數引用的 managed/<skill>。"""
    entry = {"command": "python", "args": [_managed(layout, "foo", "server.py")]}
    doc = {"mcpServers": {"renamed": entry}}
    result = _deployed(layout, doc)
    assert result.covers("foo") and not result.covers("renamed") and not result.every


def test_a_link_into_a_managed_skill_marks_it_deployed(layout, tmp_path):
    """外面的連結指進 ``managed/<skill>``：字面看不出來，解析後的位置也要算（``_candidates``）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (layout.data_root / "mcp_servers" / "managed" / "foo" / "server.py").write_text("", "utf-8")
    _dir_link(outside / "via", layout.data_root / "mcp_servers" / "managed" / "foo")
    entry = {"command": "python", "args": [str(outside / "via" / "server.py")]}
    result = _deployed(layout, {"mcpServers": {"elsewhere": entry}})
    assert result.covers("foo") and not result.every


def test_entry_named_like_a_skill_but_pointing_elsewhere_freezes_nothing(layout):
    doc = {"mcpServers": {"foo": {"command": "npx", "args": ["-y", "pkg"]}}}
    assert not _deployed(layout, doc).covers("foo")


def test_disabled_entries_and_venv_command_and_env_count(layout):
    doc = {
        "mcpServers": {
            "a": {"command": _managed(layout, "a1", ".venv", "bin", "python"), "enabled": False},
            "b": {"command": "python", "args": ["-m", "srv"], "env": {"X": _managed(layout, "b1")}},
        }
    }
    result = _deployed(layout, doc)
    assert result.skills == frozenset({"a1", "b1"})


def test_remote_and_filesystem_root_entries_freeze_no_skills(layout):
    doc = {
        "mcpServers": {
            "remote": {"url": "https://mcp.example.com/mcp"},
            "fs": {"command": "npx", "args": ["-y", "server-filesystem", "@TOUKA_APPDATA@"]},
            "plain": {"command": "npx", "args": ["fs", str(layout.data_root)]},
        }
    }
    result = _deployed(layout, {"mcpServers": {"remote": doc["mcpServers"]["remote"]}})
    assert result.skills == frozenset() and not result.every
    result = _deployed(layout, doc)
    assert not result.every and not result.skills


def test_missing_mcp_servers_key_freezes_nothing(layout):
    assert not _deployed(layout, {}).covers("foo")


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "text",
        {"mcpServers": []},
        {"mcpServers": {"a": "x"}},
        {"mcpServers": {"a": {"command": 1}}},
        {"mcpServers": {"a": {"command": "p", "args": "x"}}},
        {"mcpServers": {"a": {"command": "p", "args": None}}},
        {"mcpServers": {"a": {"command": "p", "args": [1]}}},
        {"mcpServers": {"a": {"command": "p", "env": {"A": 1}}}},
        {"mcpServers": {"a": {"command": "p", "env": ["A"]}}},
        {"mcpServers": {"a": {"url": 3}}},
    ],
)
def test_unreadable_or_malformed_config_freezes_every_skill(layout, document):
    result = _deployed(layout, document)
    assert result.every and result.covers("anything")


def test_unreadable_marker_freezes_every_skill():
    assert ALL_SKILLS_DEPLOYED.covers("anything")


@pytest.mark.parametrize("where", ["managed", "parent", "data", "install", "appdata"])
@pytest.mark.parametrize("position", ["command", "PYTHONPATH", "NODE_PATH", "option", "inline"])
def test_code_loading_root_without_a_specific_skill_freezes_all(layout, where, position):
    folder = {
        "managed": _managed(layout, ""), "parent": str(layout.data_root / "mcp_servers"),
        "data": str(layout.data_root), "install": str(layout.install_root),
        "appdata": "@TOUKA_APPDATA@",
    }[where]
    entry = {"command": "python"}
    if position == "command":
        entry["command"] = folder
    elif position == "option":
        entry = {"command": "uv", "args": ["run", "--directory", folder, "main.py"]}
    elif position == "inline":
        entry = {"command": "uv", "args": ["run", f"--directory={folder}", "main.py"]}
    else:
        entry["env"] = {position: folder}
    doc = {"mcpServers": {"x": entry}}
    result = _deployed(layout, doc)
    assert result.every and result.covers("not_created_yet")


@pytest.mark.parametrize("where", ["command", "arg", "env"])
@pytest.mark.parametrize("broad", ["data", "managed", "appdata"])
def test_broad_roots_do_not_hide_specific_deployed_skills(layout, where, broad):
    folder = {"data": str(layout.data_root), "managed": _managed(layout, ""),
              "appdata": "@TOUKA_APPDATA@"}[broad]
    entry = {"command": "wrapper", "args": [_managed(layout, "foo", "nested", "run.py")]}
    if where == "command":
        entry["command"] = folder
    elif where == "arg":
        entry["args"].append(folder)
    else:
        entry["env"] = {"PYTHONPATH": folder}
    result = _deployed(layout, {"mcpServers": {"fs": entry}})
    assert result.covers("foo")
    assert result.every == (where != "arg" or broad == "managed")


@pytest.mark.parametrize("where", ["arg", "shell", "env", "code_env"])
@pytest.mark.parametrize("value", [
    "~/personal_drive/run", "~someone/run", "$ROOT/personal_drive/run",
    "${ROOT}/personal_drive/run", "%ROOT%/personal_drive/run", "$PROGRAM",
    "${PROGRAM}", "%PROGRAM%", "--source=${ROOT}", '"$ROOT"/run', "~", "${PROGRAM}=value",
    "~+/run", "~-/run", "prefix:~/run",
])
def test_expandable_references_are_self(layout, where, value):
    if where == "shell":
        entry = Entry(command="sh", args=("-c", f"python {value}"))
    elif where == "arg":
        entry = Entry(command="wrapper", args=(value,))
    else:
        entry = Entry(command="wrapper", env={"PYTHONPATH" if where == "code_env" else "P": value})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("where", ["command", "arg", "shell", "env", "code_env"])
@pytest.mark.parametrize("value", [
    "$(pwd)/run", "`pwd`/run", "!PROGRAM!", "run*", "run?", "run[ab]", "run{a,b}",
    'pro"gram"', "pro'gram'", "price=$5", "secret$", "token%2F%3A",
    "https://host/${TOKEN}", "https://host/?token=%2F%3A",
])
def test_dynamic_reference_syntax_is_self(layout, where, value):
    entry = {
        "command": Entry(command=value),
        "arg": Entry(command="wrapper", args=(value,)),
        "shell": Entry(command="sh", args=("-c", f"python {value}")),
        "env": Entry(command="wrapper", env={"TOKEN": value}),
        "code_env": Entry(command="wrapper", env={"PYTHONPATH": value}),
    }[where]
    result = _classify(layout, entry, OriginRecord("present", "hub"))
    assert (result.kind, result.reason) == ("self", "expandable_reference")


def test_url_in_shell_text_does_not_hide_an_expandable_path(layout):
    entry = Entry(command="sh", args=("-c", '"python https://example.invalid $ROOT/run"'))
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "self"


@pytest.mark.parametrize("where", ["arg", "loader", "other_env"])
def test_scaffold_appdata_mapping_does_not_exempt_other_references(layout, where):
    script = _scaffold_script(layout)
    args = [str(script), "@TOUKA_APPDATA@"]
    env = {"DATA": "@TOUKA_APPDATA@"}
    if where == "arg":
        args.append(str(layout.data_root / "personal_drive" / "run"))
    elif where == "loader":
        env["PYTHONPATH"] = "@TOUKA_APPDATA@"
    else:
        env["PROGRAM"] = "$ROOT/run"
    entry = Entry(command="python", args=tuple(args), env=env)
    assert _classify(layout, entry, OriginRecord("present", "scaffold"), name="demo").kind == "self"


@pytest.mark.parametrize("value", [
    "100%", "v~1.0", "https://host/path", "https://host/?page=1",
])
def test_non_path_expansion_lookalikes_do_not_force_self(layout, value):
    entry = Entry(command="npx", args=("pkg",), env={"LABEL": value})
    assert _classify(layout, entry, OriginRecord("present", "hub")).kind == "third_party"


def test_classification_reports_the_referenced_skill(layout):
    python = _managed(layout, "foo", ".venv", "bin", "python")
    entry = Entry(command=python, args=(_managed(layout, "foo", "server.py"),))
    result = _classify(layout, entry, OriginRecord("present", "evolution"), name="renamed")
    assert (result.kind, result.managed_skills) == ("self", frozenset({"foo"}))
