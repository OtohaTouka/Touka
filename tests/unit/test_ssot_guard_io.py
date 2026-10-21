"""Unit tests for SSOTGuard Layer-Aware IO Prison."""

import ast
import sys
from pathlib import Path

# 獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from lint_ssot import SSOTGuard


def check_code(code, filename="src/services/test_service.py"):
    """Helper to run SSOTGuard on a code string."""
    tree = ast.parse(code)
    visitor = SSOTGuard(filename)
    visitor.visit(tree)
    return visitor.violations

def test_detect_builtin_open():
    """Should detect builtin open() in services."""
    code = 'with open("test.txt") as f: pass'
    violations = check_code(code)
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_detect_path_open():
    """Should detect Path.open() in services."""
    code = 'from pathlib import Path\nPath("test.txt").open()'
    violations = check_code(code)
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_detect_path_read_write():
    """Should detect Path.read_text() and write_text() in services."""
    code1 = 'from pathlib import Path\nPath("test.txt").read_text()'
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in check_code(code1))
    
    code2 = 'from pathlib import Path\nPath("test.txt").write_text("hello")'
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in check_code(code2))

def test_detect_os_remove():
    """Should detect os.remove() in services."""
    code = 'import os\nos.remove("test.txt")'
    violations = check_code(code)
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_allow_io_in_whitelist():
    """Should allow IO calls in whitelisted files."""
    code = 'with open("test.txt") as f: pass'
    # resource_manager.py is in WHITELIST_FILES
    violations = check_code(code, filename="src/utils/resource_manager.py")
    assert not any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_allow_io_in_non_restricted_layers():
    """Should allow IO calls in non-restricted layers like tests or scripts."""
    code = 'with open("test.txt") as f: pass'
    # A random file not in services/routers/tools/middleware
    violations = check_code(code, filename="scripts/scratch.py")
    assert not any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_detect_io_in_routers_and_middleware():
    """Should detect IO calls in routers and middleware."""
    code = 'open("test.txt")'
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in check_code(code, filename="src/routers/api.py"))
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in check_code(code, filename="src/middleware/auth.py"))
