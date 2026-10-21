"""隔離啟動器核心只用標準函式庫，而且能從任何目錄依路徑執行（``python -I -S``、不經 site）.

映像會把 ``sandbox_kernel/`` 另存一份到唯讀的信任根；proxy 與執行器依路徑載入它。所以：
- 每個模組只能 import 標準函式庫，不能有相對匯入或專案模組；
- 複製到別的目錄後，``launcher.py`` 要載入「自己目錄」的模組，而不是 ``src`` 的那份。
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from src.sandbox_kernel import launcher
from src.services.execution_broker import run_sync

KERNEL = Path(launcher.__file__).resolve().parent
FILES = sorted(KERNEL.glob("*.py"))


def _imported_roots(path: Path) -> list[tuple[int, str]]:
    roots: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            roots.append((node.level, (node.module or "").split(".")[0]))
        elif isinstance(node, ast.Import):
            roots.extend((0, alias.name.split(".")[0]) for alias in node.names)
    return roots


def test_kernel_has_the_expected_modules():
    assert {p.name for p in FILES} >= {
        "__init__.py", "landlock.py", "launcher.py", "spec.py", "framing.py", "relay.py",
        "owner_repair.py",
    }


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_kernel_imports_only_the_standard_library(path):
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    offenders = [
        (level, root) for level, root in _imported_roots(path) if level or root not in allowed
    ]
    assert offenders == [], f"{path.name} 只能匯入標準函式庫：{offenders}"


def test_launcher_loads_its_own_directory_under_isolated_mode(tmp_path):
    """複本的 spec 版本改成 77：``-I -S`` 執行複本，回報的必須是複本的值與雜湊。"""
    copy = tmp_path / "kernel"
    shutil.copytree(KERNEL, copy, ignore=shutil.ignore_patterns("__pycache__"))
    spec_file = copy / "spec.py"
    text, count = re.subn(
        r"^SPEC_VERSION = \d+\r?$", "SPEC_VERSION = 77", spec_file.read_text("utf-8"), flags=re.M
    )
    assert count == 1
    spec_file.write_text(text, encoding="utf-8")
    result = run_sync(
        [sys.executable, "-I", "-S", str(copy / "launcher.py"), "--probe"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["spec_version"] == 77
    assert report["kernel_digest"] == launcher.spec.kernel_digest(copy)
    assert report["kernel_digest"] != launcher.spec.kernel_digest(KERNEL)


_PARSE_COPY = """
import importlib.util, json, sys
module_spec = importlib.util.spec_from_file_location("touka_kernel_copy_spec", sys.argv[1])
module = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = module
module_spec.loader.exec_module(module)
print(json.dumps(dict(module.parse_spec(sys.argv[2].encode()).exec_env)))
"""


def test_spec_copy_handles_exec_env_on_its_own_under_isolated_mode(tmp_path):
    """複本的 spec 以 ``-I -S`` 依路徑載入：保留名稱與禁令 19 的名單都在 kernel 自己身上."""
    copy = tmp_path / "kernel"
    shutil.copytree(KERNEL, copy, ignore=shutil.ignore_patterns("__pycache__"))
    document = {"version": 1, "exec_env": {"TOKEN": "t", "LD_PRELOAD": "/srv/x.so"}}
    argv = [sys.executable, "-I", "-S", "-c", _PARSE_COPY, str(copy / "spec.py")]
    result = run_sync(
        [*argv, json.dumps(document)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"TOKEN": "t"}


def test_launch_mode_off_linux_never_runs_the_command():
    """非 Linux（沒有 Landlock、也沒有可讀的 spec fd）時 --spec-fd 模式回 125，指令不執行。"""
    if sys.platform.startswith("linux"):
        pytest.skip("Linux 上的行為由 test_isolation_linux 驗證")
    result = run_sync(
        [sys.executable, "-I", "-S", str(KERNEL / "launcher.py"), "--spec-fd", "3", "--", "x"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == launcher.spec.FAILURE_EXIT
    marker = re.search(re.escape(launcher.spec.FAILURE_MARKER) + r"(\w+)", result.stderr)
    assert marker and marker.group(1) in {"spec_read", "landlock_unavailable"}
