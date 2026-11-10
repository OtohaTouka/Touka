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
WINDOWS_ONLY_MODULES = frozenset({"ctypes", "msvcrt"})

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
        "msvcrt",
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
ALLOWED_DOTTED_PREFIXES = {
    "http": frozenset({"http.client"}),
    "urllib": frozenset({"urllib.error", "urllib.request"}),
}
BANNED_OS_ATTRIBUTES = frozenset({"system", "popen", "startfile", "replace"})
BANNED_OS_PREFIXES = ("exec", "spawn")
BANNED_NAMES = frozenset({"eval", "exec", "__import__", "importlib", "getattr"})
BANNED_TK_ATTRIBUTES = frozenset({"eval", "call"})


def _sources() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _is_banned_os_name(name: str) -> bool:
    return name in BANNED_OS_ATTRIBUTES or name.startswith(BANNED_OS_PREFIXES)


def _import_violations(path: Path, node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            return [f"{path.name}:{node.lineno} 相對匯入"]
        module = node.module or ""
        names = [module]
        if module in ALLOWED_DOTTED_PREFIXES:
            names = [f"{module}.{alias.name}" for alias in node.names]
    problems: list[str] = []
    for name in names:
        top = name.split(".", 1)[0]
        if top not in ALLOWED_MODULES:
            problems.append(f"{path.name}:{node.lineno} 不在允許清單：{name}")
        allowed = ALLOWED_DOTTED_PREFIXES.get(top)
        if allowed is not None and name != top and name not in allowed:
            problems.append(f"{path.name}:{node.lineno} 不在允許清單：{name}")
        if top in WINDOWS_ONLY_MODULES and path.name != WINDOWS_ONLY_FILE:
            problems.append(f"{path.name}:{node.lineno} {top} 只能在 {WINDOWS_ONLY_FILE}")
    if isinstance(node, ast.ImportFrom) and node.module == "os":
        problems += [
            f"{path.name}:{node.lineno} 不能匯入 os.{alias.name}"
            for alias in node.names
            if _is_banned_os_name(alias.name)
        ]
    return problems


def _call_violations(path: Path, node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
        return [f"{path.name}:{node.lineno} 不能使用 {node.id}"]
    if isinstance(node, ast.Attribute):
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id == "os" and _is_banned_os_name(node.attr):
            return [f"{path.name}:{node.lineno} 不能使用 os.{node.attr}"]
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "tk"
            and node.attr in BANNED_TK_ATTRIBUTES
        ):
            return [f"{path.name}:{node.lineno} 不能使用 .tk.{node.attr}"]
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
