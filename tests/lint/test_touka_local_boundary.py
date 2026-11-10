"""本機小幫手的能力邊界（ADR-0312）。

小幫手跑在容器外、直接碰使用者的真實檔案，所以用允許清單限制它能用的模組：
沒有列在這裡的一律不能匯入，也就沒有執行程式、開網路入口的管道。

這是擋開發時不小心加進能力的檢查，不是沙箱；刻意繞過的寫法要靠審查擋。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "ToukaLocal"
WINDOWS_ONLY_FILE = "winapi.py"
WINDOWS_ONLY_MODULES = frozenset({"ctypes"})

ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "argparse",
        "collections",
        "contextlib",
        "ctypes",
        "dataclasses",
        "hashlib",
        "http",
        "json",
        "logging",
        "math",
        "ntpath",
        "os",
        "pathlib",
        "re",
        "secrets",
        "stat",
        "sys",
        "threading",
        "time",
        "tkinter",
        "touka_local",
        "typing",
        "unicodedata",
        "urllib",
    }
)
ALLOWED_SUBMODULES = frozenset(
    {
        "collections.abc",
        "ctypes.wintypes",
        "http.client",
        "logging.handlers",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        "urllib.error",
        "urllib.request",
    }
)
PACKAGES_WITH_SUBMODULES = frozenset(name.split(".", 1)[0] for name in ALLOWED_SUBMODULES)
OWN_PACKAGE = "touka_local"
NO_ALIAS_MODULES = frozenset({"os", "ctypes"})
BANNED_OS_ATTRIBUTES = frozenset({"system", "popen", "startfile", "replace", "open"})
BANNED_OS_PREFIXES = ("exec", "spawn")
BANNED_NAMES = frozenset(
    {"eval", "exec", "__import__", "importlib", "getattr", "vars", "globals", "locals", "compile"}
)
# 任何物件上都不行，換個變數名就看不出來了。.tk 能拿到 Tcl 直譯器
BANNED_ATTRIBUTES = frozenset(
    {"eval", "call", "tk", "system", "popen", "startfile", "os", "modules", "__dict__", "socket"}
)
# 啟動程式的那些函式（sys.executable 這種名字不算）
PROCESS_STARTERS = re.compile(r"(exec|spawn)[lv]p?e?")
# 只有這兩個檔案能寫檔
WRITE_FILES = frozenset({"fsops.py", "install.py"})
WRITE_ATTRIBUTES = frozenset({"write_text", "write_bytes"})
WRITE_MODE_LETTERS = frozenset("wax+")
# 這幾個方法的第二個引數傳字串時，tkinter 會把它當 Tcl 腳本
TK_CALLBACK_METHODS = frozenset({"bind", "after", "protocol"})


def _sources() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _is_banned_os_name(name: str) -> bool:
    return name in BANNED_OS_ATTRIBUTES or name.startswith(BANNED_OS_PREFIXES)


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    """這個匯入實際載入的模組。``from 套件 import 子模組`` 算到子模組。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if module in PACKAGES_WITH_SUBMODULES:
        return [f"{module}.{alias.name}" for alias in node.names]
    return [module]


def _check_module(path: Path, line: int, name: str) -> list[str]:
    top = name.split(".", 1)[0]
    where = f"{path.name}:{line}"
    if top not in ALLOWED_MODULES:
        return [f"{where} 不在允許清單：{name}"]
    problems: list[str] = []
    if name != top and top != OWN_PACKAGE and name not in ALLOWED_SUBMODULES:
        problems.append(f"{where} 不在允許清單：{name}")
    if top in WINDOWS_ONLY_MODULES and path.name != WINDOWS_ONLY_FILE:
        problems.append(f"{where} {top} 只能在 {WINDOWS_ONLY_FILE}")
    return problems


def _alias_violations(path: Path, node: ast.Import | ast.ImportFrom) -> list[str]:
    where = f"{path.name}:{node.lineno}"
    problems: list[str] = []
    for alias in node.names:
        if alias.name == "*":
            problems.append(f"{where} 不能用星號匯入")
        if isinstance(node, ast.Import):
            if alias.asname and alias.name.split(".", 1)[0] in NO_ALIAS_MODULES:
                problems.append(f"{where} 不能幫 {alias.name} 取別名")
            continue
        module = node.module or ""
        if module == "os" and _is_banned_os_name(alias.name):
            problems.append(f"{where} 不能匯入 os.{alias.name}")
        if module.split(".", 1)[0] == OWN_PACKAGE and alias.name in WINDOWS_ONLY_MODULES:
            problems.append(f"{where} 不能經由小幫手的模組拿到 {alias.name}")
    return problems


def _import_violations(path: Path, node: ast.AST) -> list[str]:
    if not isinstance(node, ast.Import | ast.ImportFrom):
        return []
    if isinstance(node, ast.ImportFrom) and node.level:
        return [f"{path.name}:{node.lineno} 相對匯入"]
    problems = _alias_violations(path, node)
    for name in _imported_modules(node):
        problems += _check_module(path, node.lineno, name)
    return problems


@dataclass(frozen=True)
class _Usage:
    """哪些節點是被直接呼叫的、哪些是屬性的擁有者。"""

    called: frozenset[int]
    owners: frozenset[int]


def _usage_of(tree: ast.AST) -> _Usage:
    nodes = list(ast.walk(tree))
    return _Usage(
        called=frozenset(id(node.func) for node in nodes if isinstance(node, ast.Call)),
        owners=frozenset(id(node.value) for node in nodes if isinstance(node, ast.Attribute)),
    )


def _is_text(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _name_violations(path: Path, node: ast.Name, usage: _Usage) -> list[str]:
    where = f"{path.name}:{node.lineno}"
    if node.id in BANNED_NAMES:
        return [f"{where} 不能使用 {node.id}"]
    if node.id == "open" and path.name not in WRITE_FILES:
        return [f"{where} 不能用 open 寫檔"]
    if node.id in NO_ALIAS_MODULES and id(node) not in usage.owners:
        return [f"{where} 不能把 {node.id} 模組傳來傳去"]
    return []


def _attribute_violations(path: Path, node: ast.Attribute, usage: _Usage) -> list[str]:
    where = f"{path.name}:{node.lineno}"
    attribute = node.attr
    owner = node.value
    if isinstance(owner, ast.Name) and owner.id == "os" and _is_banned_os_name(attribute):
        return [f"{where} 不能使用 os.{attribute}"]
    if attribute in BANNED_ATTRIBUTES or PROCESS_STARTERS.fullmatch(attribute):
        return [f"{where} 不能使用 .{attribute}"]
    if attribute == "ctypes" and path.name != WINDOWS_ONLY_FILE:
        return [f"{where} ctypes 只能在 {WINDOWS_ONLY_FILE}"]
    if attribute in WRITE_ATTRIBUTES and path.name not in WRITE_FILES:
        return [f"{where} 只有 {sorted(WRITE_FILES)} 能寫檔"]
    if attribute == "replace" and id(node) not in usage.called:
        return [f"{where} 不能把 .replace 存起來用"]
    return []


def _is_overwriting_replace(node: ast.Call) -> bool:
    """``Path.replace`` 會覆蓋目的地；``str.replace`` 至少要兩個引數，也不會掛在 Path 上。"""
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr != "replace":
        return False
    owner = function.value
    on_path_class = (isinstance(owner, ast.Name) and owner.id == "Path") or (
        isinstance(owner, ast.Attribute) and owner.attr == "Path"
    )
    return on_path_class or len(node.args) + len(node.keywords) == 1


def _opens_for_writing(path: Path, node: ast.Call) -> bool:
    function = node.func
    if path.name in WRITE_FILES or not isinstance(function, ast.Attribute):
        return False
    if function.attr != "open":
        return False
    mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
    if node.args:
        mode = node.args[0]
    return mode is not None and _is_text(mode) and bool(WRITE_MODE_LETTERS & set(mode.value))


def _passes_tcl_script(node: ast.Call) -> bool:
    if any(keyword.arg == "command" and _is_text(keyword.value) for keyword in node.keywords):
        return True
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr in TK_CALLBACK_METHODS
        and any(_is_text(argument) for argument in node.args[1:2])
    )


def _call_violations(path: Path, node: ast.Call) -> list[str]:
    where = f"{path.name}:{node.lineno}"
    if _is_overwriting_replace(node):
        return [f"{where} 不能用會覆蓋目的地的 .replace()"]
    if _opens_for_writing(path, node):
        return [f"{where} 只有 {sorted(WRITE_FILES)} 能寫檔"]
    if _passes_tcl_script(node):
        return [f"{where} 不能把字串當 Tcl 腳本傳給 tkinter"]
    return []


def violations_in(path: Path, source: str) -> list[str]:
    """掃一個檔案，回傳違規清單。"""
    tree = ast.parse(source)
    usage = _usage_of(tree)
    problems: list[str] = []
    for node in ast.walk(tree):
        problems += _import_violations(path, node)
        if isinstance(node, ast.Name):
            problems += _name_violations(path, node, usage)
        elif isinstance(node, ast.Attribute):
            problems += _attribute_violations(path, node, usage)
        elif isinstance(node, ast.Call):
            problems += _call_violations(path, node)
    return problems


def test_helper_sources_exist() -> None:
    assert (ROOT / "touka_local" / "winapi.py").is_file()
    assert len(_sources()) >= 10


def test_helper_stays_inside_its_boundary() -> None:
    problems: list[str] = []
    for path in _sources():
        problems += violations_in(path, path.read_text(encoding="utf-8"))
    assert problems == []


def test_helper_does_not_import_touka_itself() -> None:
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".", 1)[0] != "src", path.name


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import socket",
        "import http.server",
        "from http import server",
        "import multiprocessing",
        "import webbrowser",
        "import shutil",
        "import importlib",
        "import ctypes",
        "from ctypes import windll",
        "import msvcrt",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.startfile('x')",
        "import os\nos.execv('x', [])",
        "import os\nos.spawnl(0, 'x')",
        "import os\nos.replace('a', 'b')",
        "import logging.config",
        "import tkinter.tix",
        "import ctypes.util",
        "from tkinter import Tcl",
        "from logging import config",
        "from os import *",
        "from json import *",
        "import os as o",
        "import ctypes as c",
        "foo.eval('x')",
        "foo.bar.call('x')",
        "from pathlib import Path\nPath('a').replace('b')",
        "x.replace(target='b')",
        "from touka_local.winapi import ctypes",
        "from touka_local import winapi\nwinapi.ctypes.WinDLL('kernel32')",
        "import os\no = os\no.system('x')",
        "import os\nhandler(os)",
        "foo.execv('x', [])",
        "foo.spawnl(0, 'x')",
        "import pathlib\npathlib.os.system('x')",
        "import sys\nsys.modules['os'].system('x')",
        "import os\nvars(os)['system']('x')",
        "x.__dict__",
        "import logging.handlers\nlogging.handlers.socket.socket()",
        "root.tk.evalfile('x')",
        "w.bind('<Map>', 'exec x')",
        "ttk.Button(command='exec x')",
        "root.after(1, 'exec x')",
        "from pathlib import Path\nPath.replace(a, 'b')",
        "f = p.replace\nf('b')",
        "p.write_text('x')",
        "p.write_bytes(b'x')",
        "open('x', 'w')",
        "p.open('w')",
        "p.open(mode='w')",
        "import pathlib\npathlib.Path.replace(a, 'b')",
        "import os\nos.open('x', 1)",
        "from os import system",
        "from os import replace",
        "from os import execv",
        "eval('1')",
        "exec('1')",
        "__import__('os')",
        "getattr(x, 'y')",
        "root.tk.eval('exec x')",
        "root.tk.call('exec', 'x')",
        "from . import x",
    ],
)
def test_guard_catches(source: str, tmp_path: Path) -> None:
    assert violations_in(tmp_path / "other.py", source) != []


def test_guard_allows_ctypes_in_the_windows_file(tmp_path: Path) -> None:
    assert violations_in(tmp_path / WINDOWS_ONLY_FILE, "import ctypes") == []


def test_guard_allows_ordinary_code(tmp_path: Path) -> None:
    source = "import os\nimport json\nx = os.environ\ny = 'a'.replace('a', 'b')\n"
    assert violations_in(tmp_path / "other.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import tkinter as tk",
        "from tkinter import filedialog, messagebox, ttk",
        "import logging.handlers",
        "from collections.abc import Callable",
        "from dataclasses import replace\nx = replace(a, b=1)",
        "import urllib.request",
        "w.bind('<Map>', handler)",
        "w.bind('<Map>', handler, '+')",
        "p.open(mode='r')",
        "ttk.Button(command=handler)",
        "root.after(1, handler)",
        "p.open()",
        "p.open('rb')",
        "opener.open(request, timeout=1)",
        "import sys\nx = sys.executable",
    ],
)
def test_guard_allows_what_the_helper_needs(source: str, tmp_path: Path) -> None:
    assert violations_in(tmp_path / "other.py", source) == []


def test_guard_allows_wintypes_in_the_windows_file(tmp_path: Path) -> None:
    assert violations_in(tmp_path / WINDOWS_ONLY_FILE, "from ctypes import wintypes") == []


@pytest.mark.parametrize(
    "source",
    ["p.write_text('x')", "p.write_bytes(b'x')", "open('x', 'w')", "p.open('xb')"],
)
@pytest.mark.parametrize("file_name", ["fsops.py", "install.py"])
def test_guard_allows_writes_in_the_files_that_write(
    source: str, file_name: str, tmp_path: Path
) -> None:
    assert violations_in(tmp_path / file_name, source) == []


def test_guard_allows_ctypes_attribute_in_the_windows_file(tmp_path: Path) -> None:
    assert violations_in(tmp_path / WINDOWS_ONLY_FILE, "import ctypes\nx = ctypes.WinDLL") == []
