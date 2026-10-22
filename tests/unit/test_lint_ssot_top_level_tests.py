"""lint_ssot 的業務層判斷：repo 頂層 tests/ 底下的檔案一律不算業務層（ADR-0278）。

以前用子字串比對整條路徑，檔名含 tools、service 的測試會被當成業務程式而不准讀檔。
其他路徑照舊比對，所以產品程式的判定不變；比對不上時只會更嚴。
"""

import ast
from pathlib import Path

import pytest
from lint_ssot import SSOTGuard  # scripts/ 由 tests/conftest.py 加進 sys.path

PROJECT_ROOT = Path(__file__).absolute().parent.parent.parent
IO_CALL = "open('x.txt', 'w')"


def _io_prison_hits(filename: str) -> bool:
    visitor = SSOTGuard(filename)
    visitor.visit(ast.parse(IO_CALL))
    return any(v["rule"] == "IO_PRISON_VIOLATION" for v in visitor.violations)


@pytest.mark.parametrize(
    "filename",
    [
        "tests/unit/test_mcp_tools_probe.py",
        "./tests/integration/test_auth_service.py",
        "tests/lint/routers/test_middleware_probe.py",
        str(PROJECT_ROOT / "tests" / "unit" / "test_creation_tools.py"),
    ],
)
def test_top_level_tests_are_not_business_layer(filename: str) -> None:
    assert not _io_prison_hits(filename)


@pytest.mark.parametrize(
    "filename",
    [
        "ToukaProxy/src/services/probe.py",
        "ToukaProxy/src/tools/probe.py",
        "ToukaProxy/tests/tools/probe.py",
        "tests_old/tools/probe.py",
        "src/tests/services/probe.py",
        "ToukaHeartbeat/storage_service.py",
        str(PROJECT_ROOT / "ToukaProxy" / "src" / "tools" / "probe.py"),
        "tests/../ToukaProxy/src/services/probe.py",
        str(PROJECT_ROOT / "tests" / ".." / "ToukaProxy" / "src" / "services" / "probe.py"),
    ],
)
def test_other_paths_keep_the_business_layer_check(filename: str) -> None:
    assert _io_prison_hits(filename)
