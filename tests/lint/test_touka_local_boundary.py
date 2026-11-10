"""本機小幫手的能力邊界（ADR-0311）。

小幫手跑在容器外、直接碰使用者的真實檔案，所以用允許清單限制它能用的模組：
沒有列在這裡的一律不能匯入，也就沒有執行程式、開網路入口的管道。
"""

from __future__ import annotations

import ast
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
BANNED_OS_ATTRIBUTES = frozenset({"system", "popen", "startfile", "replace"})
BANNED_OS_PREFIXES = ("exec", "spawn")
BANNED_NAMES = frozenset({"eval", "exec", "__import__", "importlib", "getattr"})
# 任何物件上都不行：tk 的這兩個方法能執行 Tcl，換個變數名就看不出來了
BANNED_ATTRIBUTES = frozenset({"eval", "call"})


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


def _call_violations(path: Path, node: ast.AST) -> list[str]:
    where = f"{path.name}:{node.lineno}" if hasattr(node, "lineno") else path.name
    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return [f"{where} 不能使用 {node.id}"]
    if isinstance(node, ast.Attribute):
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id == "os" and _is_banned_os_name(node.attr):
            return [f"{where} 不能使用 os.{node.attr}"]
        if node.attr in BANNED_ATTRIBUTES:
            return [f"{where} 不能使用 .{node.attr}"]
    # Path.replace 只收一個引數，而且會覆蓋目的地；str.replace 至少兩個
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and len(node.args) + len(node.keywords) == 1
    ):
        return [f"{where} 不能用會覆蓋目的地的 .replace()"]
    return []


def violations_in(path: Path, source: str) -> list[str]:
    """掃一個檔案，回傳違規清單。"""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        problems += _import_violations(path, node)
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
    ],
)
def test_guard_allows_what_the_helper_needs(source: str, tmp_path: Path) -> None:
    assert violations_in(tmp_path / "other.py", source) == []


def test_guard_allows_wintypes_in_the_windows_file(tmp_path: Path) -> None:
    assert violations_in(tmp_path / WINDOWS_ONLY_FILE, "from ctypes import wintypes") == []
