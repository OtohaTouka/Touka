"""執行器伺服器的匯入邊界（iso-c2 C2-2 設計 §2.1）.

``services/isolation/executor_server.py`` 與 ``executor_session.py`` 在執行器容器裡以
``python -m`` 執行，那裡沒有 Touka 的設定、金鑰與原始碼以外的東西可用：

- 只能匯入標準函式庫、``src.services.execution_broker`` 與這兩個檔彼此；kernel 模組由伺服器從
  ``--kernel-dir`` 依路徑載入，不是匯入；
- 不讀環境變數、不載入 ``src.config``；
- 每個匯入到的專案模組都要在 ``EXECUTOR_CODE_FILES`` 裡，``server_digest`` 才涵蓋得到
  （proxy 用同一份清單算 live 原始碼的雜湊，版本不一致時判 ``executor_version_mismatch``）。
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest
from src.services.isolation import executor_server

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / "ToukaProxy"
ISOLATION = PROXY / "src" / "services" / "isolation"
FILES = (ISOLATION / "executor_server.py", ISOLATION / "executor_session.py")
PROJECT_ALLOWED = frozenset(
    {
        "src.services.execution_broker",
        "src.services.isolation.executor_server",
        "src.services.isolation.executor_session",
    }
)
_STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}
_ENV_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """（行號、模組全名）；相對匯入記成 ``.<module>``，一律不允許."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                found.append((node.lineno, "." * node.level + module))
            elif module == "src" or module.startswith("src."):
                for alias in node.names:  # ``from pkg import module`` 記成模組本身
                    full = f"{module}.{alias.name}"
                    found.append((node.lineno, full if full in PROJECT_ALLOWED else module))
            else:
                found.append((node.lineno, module))
    return found


def _violations(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    for lineno, module in _imported_modules(tree):
        root = module.split(".")[0]
        if module.startswith("."):
            bad.append(f"{lineno}: relative {module}")
        elif module in PROJECT_ALLOWED:
            continue
        elif root not in _STDLIB:
            bad.append(f"{lineno}: {module}")
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.ImportFrom):
            bad.extend(f"{node.lineno}: {a.name}" for a in node.names if a.name in _ENV_NAMES)
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value if node.value in {"src.config", *_ENV_NAMES} else None
        if name in _ENV_NAMES or name == "src.config":
            bad.append(f"{getattr(node, 'lineno', 0)}: {name}")
    return bad


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_executor_imports_only_stdlib_the_broker_and_each_other(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _violations(tree) == [], (
        "執行器伺服器只能匯入標準函式庫、execution_broker 與彼此，也不讀環境變數"
    )


def test_every_imported_project_module_is_in_the_code_file_list() -> None:
    listed = set(executor_server.EXECUTOR_CODE_FILES)
    for path in FILES:
        assert path.relative_to(PROXY).as_posix() in listed
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for _lineno, module in _imported_modules(tree):
            if module.startswith("src."):
                assert module.replace(".", "/") + ".py" in listed, module


def test_the_server_does_import_the_broker() -> None:
    """正向對照：掃描真的看得到伺服器的專案匯入（掃描壞掉時這裡會紅）。"""
    tree = ast.parse(FILES[0].read_text(encoding="utf-8"))
    modules = {module for _lineno, module in _imported_modules(tree)}
    assert {"src.services.execution_broker", "src.services.isolation.executor_session"} <= modules


@pytest.mark.parametrize(
    "source",
    [
        "import src.config\n",
        "from src.config import settings\n",
        "from src.utils.isolation_policy import is_safe_segment\n",
        "from src.services import resource_manager\n",
        "from . import profiles\n",
        "import requests\n",
        "import os\nx = os.environ['A']\n",
        "import os\nx = os.getenv('A')\n",
        "from os import environ\n",
        "import importlib\nm = importlib.import_module('src.config')\n",
    ],
)
def test_scan_catches_every_forbidden_form(source: str) -> None:
    assert _violations(ast.parse(source))


def test_code_digest_covers_every_listed_file(tmp_path: Path) -> None:
    copy = tmp_path / "ToukaProxy"
    for name in executor_server.EXECUTOR_CODE_FILES:
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROXY / name, target)
    original = executor_server.executor_code_digest(copy)
    assert original == executor_server.executor_code_digest(PROXY)
    for name in executor_server.EXECUTOR_CODE_FILES:
        target = copy / name
        text = target.read_bytes()
        target.write_bytes(text + b"\n# changed\n")
        assert executor_server.executor_code_digest(copy) != original, name
        target.write_bytes(text)
    assert executor_server.executor_code_digest(copy) == original


# ── 型別要具名：不准用 Any、SimpleNamespace 壓掉型別（CTO 10/16）──────────────────────
_LOOSE_TYPES = frozenset({"Any", "SimpleNamespace"})


def _loose_types(tree: ast.AST) -> list[str]:
    found = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in _LOOSE_TYPES}
    found |= {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in _LOOSE_TYPES
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found |= {alias.name for alias in node.names if alias.name in _LOOSE_TYPES}
    return sorted(found)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_executor_modules_use_named_types(path: Path) -> None:
    """kernel 模組、命令列結果、注入點用 Protocol 或 dataclass，不用 Any 或 SimpleNamespace."""
    assert _loose_types(ast.parse(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Any\nx: Any = 1\n",
        "import typing\nx: typing.Any = 1\n",
        "from types import SimpleNamespace\nconfig = SimpleNamespace(a=1)\n",
        "import types\nconfig = types.SimpleNamespace(a=1)\n",
    ],
)
def test_loose_type_scan_catches_every_form(source: str) -> None:
    assert _loose_types(ast.parse(source))
