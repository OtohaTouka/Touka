"""隔離邊界不碰完整環境：``child_python_env()``／``get_full_env()`` 帶著全部金鑰。

``utils/child_env.child_python_env()``（ADR-0262）回傳 ``settings.get_full_env()`` 加上
PYTHONPATH，只給 Touka 自己的子程序。``sandbox_kernel/`` 與 ``services/isolation/``
（runner、啟動器、之後的執行器、relay）組出的環境會交給代理人指令，所以靜態 AST 掃描禁止：
匯入 ``src.utils.child_env``（含相對匯入）、引用 ``child_python_env``、``get_full_env`` 或
``get_clean_env``（黑名單過濾；PM 10/15 決定隔離邊界內一律只用允許清單），以及用字串動態取用
它們（``getattr``、``importlib``）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ToukaProxy" / "src"
BOUNDARY_ROOTS = (SRC / "sandbox_kernel", SRC / "services" / "isolation")
FORBIDDEN_MODULE = "src.utils.child_env"
FORBIDDEN_NAMES = frozenset({"child_python_env", "get_full_env", "get_clean_env"})
FORBIDDEN_STRINGS = FORBIDDEN_NAMES | {FORBIDDEN_MODULE}


def _boundary_files() -> list[Path]:
    return sorted(
        [p for root in BOUNDARY_ROOTS for p in root.rglob("*.py") if "__pycache__" not in p.parts]
        + [SRC / "protocols" / "mcp" / "isolated_stdio.py"]
    )


def _package_of(path: Path) -> str:
    return ".".join(("src", *path.parent.relative_to(SRC).parts))


def _absolute(module: str | None, level: int, package: str) -> str:
    if not level:
        return module or ""
    base = package.split(".")[: len(package.split(".")) - level + 1]
    return ".".join([*base, module] if module else base)


def _is_forbidden_module(name: str) -> bool:
    return name == FORBIDDEN_MODULE or name.startswith(FORBIDDEN_MODULE + ".")


def _full_env_uses(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (node.lineno, f"import {alias.name}")
                for alias in node.names
                if _is_forbidden_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node.module, node.level, package)
            for alias in node.names:
                if (
                    _is_forbidden_module(module)
                    or _is_forbidden_module(f"{module}.{alias.name}")
                    or alias.name in FORBIDDEN_NAMES
                ):
                    found.append((node.lineno, f"from {module} import {alias.name}"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append((node.lineno, f".{node.attr}"))
        elif isinstance(node, ast.Constant) and node.value in FORBIDDEN_STRINGS:
            found.append((node.lineno, repr(node.value)))
    return found


def test_scan_covers_the_isolation_boundary() -> None:
    names = {p.relative_to(SRC).as_posix() for p in _boundary_files()}
    assert {
        "sandbox_kernel/launcher.py",
        "sandbox_kernel/spec.py",
        "services/isolation/runners.py",
        "services/isolation/profiles.py",
        "services/isolation/status.py",
        "protocols/mcp/isolated_stdio.py",
    } <= names


@pytest.mark.parametrize("path", _boundary_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_isolation_boundary_never_touches_the_full_environment(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _full_env_uses(tree, _package_of(path)) == [], (
        "隔離邊界的模組不能用 child_python_env()／get_full_env()（完整環境含金鑰）；"
        "交給代理人指令的環境只能來自允許清單"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from src.utils.child_env import child_python_env\n",
        "import src.utils.child_env\n",
        "import src.utils.child_env as ce\n",
        "from src.utils import child_env\n",
        "from ...utils.child_env import child_python_env as env\n",
        "from ...utils import child_env\n",
        "env = child_python_env()\n",
        "env = settings.get_full_env()\n",
        "build = settings.get_full_env\n",
        "from src.config import get_full_env\n",
        "env = getattr(settings, 'get_full_env')()\n",
        "env = EvolutionPathGuard.get_clean_env()\n",
        "from src.services.evolution_security import get_clean_env\n",
        "mod = importlib.import_module('src.utils.child_env')\n",
    ],
)
def test_scan_catches_every_form(source: str) -> None:
    assert _full_env_uses(ast.parse(source), "src.services.isolation")


def test_scan_ignores_unrelated_code() -> None:
    source = (
        "from src.services.execution_broker import spawn_exec\n"
        "from src.utils.child_envelope import wrap\n"
        "env = {'LANG': 'C.UTF-8', 'note': 'get_full_env is banned here'}\n"
    )
    assert _full_env_uses(ast.parse(source), "src.services.isolation") == []
