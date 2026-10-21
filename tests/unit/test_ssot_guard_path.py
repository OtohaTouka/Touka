"""Unit tests for SSOTGuard physical path detection."""

import ast
import sys
from pathlib import Path

# 獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# 將 scripts 加入路徑以便匯入 (如果尚未加入)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

try:
    from lint_ssot import SSOTGuard
except ImportError:
    # 備用方案：如果 lint_ssot.py 不在 scripts 下，嘗試直接從根目錄找
    sys.path.append(str(PROJECT_ROOT))
    from lint_ssot import SSOTGuard

def check_code(code, filename="test.py"):
    """Helper to run SSOTGuard on a code string."""
    tree = ast.parse(code)
    visitor = SSOTGuard(filename)
    visitor.visit(tree)
    return visitor.violations

def test_detect_windows_absolute_path():
    """Should detect Windows absolute paths like D:/... or C:\\..."""
    code = 'path = "D:/Touka/data"'
    violations = check_code(code)
    assert any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations)
    
    code2 = 'path = "C:\\\\Users\\\\someone"'
    violations2 = check_code(code2)
    assert any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations2)

def test_detect_unix_absolute_path():
    """Should detect Unix/Docker absolute paths like /app/data."""
    code = 'path = "/app/data/log.txt"'
    violations = check_code(code)
    assert any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations)

def test_ignore_valid_strings():
    """Should NOT flag URLs, relative paths, or encoding strings."""
    valid_codes = [
        'url = "http://localhost:8000"',
        'api = "/v1/messages"',
        'encoding = "utf-8"',
        'rel = "./local/file.txt"',
        'fmt = "%Y-%m-%d"',
        'css = "white-space: pre-wrap"'
    ]
    for code in valid_codes:
        violations = check_code(code)
        assert not any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations), f"Failed on: {code}"

def test_whitelist_files():
    """Should ignore path literals in whitelisted files like config.py."""
    code = 'DEFAULT_ROOT = "D:/Touka"'
    violations = check_code(code, filename="config.py")
    assert not any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations)
