"""Tests for the Architectural SSOT Guard (lint_ssot.py)."""

import ast
import sys
from pathlib import Path

# 為了方便測試，我們動態導入 scripts/lint_ssot.py
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from lint_ssot import SSOTGuard


def parse_and_check(code_str: str, filename: str = "dummy.py") -> list[dict]:
    """Helper function to parse code and return violations."""
    tree = ast.parse(code_str)
    visitor = SSOTGuard(filename)
    visitor.visit(tree)
    return visitor.violations

def test_time_ssot_violation():
    code = "import time\nx = time.time()"
    violations = parse_and_check(code)
    assert any(v["rule"] == "TIME_SSOT_VIOLATION" for v in violations)

    code2 = "from datetime import datetime\nx = datetime.now()"
    violations2 = parse_and_check(code2)
    assert any(v["rule"] == "TIME_SSOT_VIOLATION" for v in violations2)

def test_env_ssot_violation():
    code = "import os\nx = os.getenv('API_KEY')"
    violations = parse_and_check(code)
    assert any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)

    code2 = "import os\nx = os.environ['API_KEY']"
    violations2 = parse_and_check(code2)
    assert any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations2)

def test_path_literal_ssot_violation():
    code = "x = 'D:/Touka/data/soul'"
    violations = parse_and_check(code)
    assert any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations)

    code2 = "x = '/app/data/logs'"
    violations2 = parse_and_check(code2)
    assert any(v["rule"] == "PATH_LITERAL_SSOT" for v in violations2)

def test_io_prison_violation():
    # 必須是在特定的目錄下才會觸發 (例如 services/)
    # 這裡假設 dummy.py 符合 is_service 判定 (在 services/ 下)
    code = "open('test.txt', 'w')"
    violations = parse_and_check(code, "ToukaProxy/src/services/test_service.py")
    assert any(v["rule"] == "IO_PRISON_VIOLATION" for v in violations)

def test_error_ssot_violation():
    code = "from fastapi import HTTPException\nraise HTTPException(status_code=400, detail='User not found')"
    violations = parse_and_check(code)
    assert any(v["rule"] == "ERROR_SSOT_VIOLATION" for v in violations)

def test_pac_manual_level_check():
    code = "if context.get('auth_level') == 'FullYOLO': pass"
    violations = parse_and_check(code)
    assert any(v["rule"] == "PAC_MANUAL_LEVEL_CHECK" for v in violations)

def test_identity_ssot_violation():
    code = "def my_func(agent_id: str): pass"
    # 必須是 service 才會觸發
    violations = parse_and_check(code, "ToukaProxy/src/services/test.py")
    assert any(v["rule"] == "IDENTITY_SSOT" for v in violations)

def test_whitelist_exclusion():
    # 在 config.py 中使用 os.getenv 應該被允許 (在 ENV_WHITELIST 中)
    code = "import os\nx = os.getenv('API_KEY')"
    violations = parse_and_check(code, "config.py")
    assert len(violations) == 0


# ── PRINT_BAN 規則測試 ──────────────────────────────────────────────────────

def test_print_ban_violation():
    """print() 在 src/ 業務代碼中應被偵測。"""
    code = 'print("debug info")'
    violations = parse_and_check(code, "ToukaProxy/src/services/some_service.py")
    assert any(v["rule"] == "PRINT_BAN" for v in violations)


def test_print_ban_allows_whitelisted_file():
    """白名單檔案中的 print() 不報告。"""
    code = 'print("exporting...")'
    violations = parse_and_check(code, "export_schema.py")
    assert not any(v["rule"] == "PRINT_BAN" for v in violations)


def test_print_ban_allows_non_src_path():
    """非 src/ 路徑（測試、腳本、工具）的 print() 不報告。"""
    code = 'print("Guard passed!")'
    violations = parse_and_check(code, "tests/integration/test_something.py")
    assert not any(v["rule"] == "PRINT_BAN" for v in violations)


def test_print_ban_noqa():
    """# noqa: PRINT_BAN 豁免機制：直接傳入 ignored_lines 測試 add_violation 的抑制邏輯。"""
    code = 'print("sandbox output")'
    # 模擬第 1 行有 PRINT_BAN noqa 豁免（等同 print(...)  # noqa: PRINT_BAN）
    ignored: dict[int, set[str]] = {1: {"PRINT_BAN"}}
    tree = ast.parse(code)
    visitor = SSOTGuard("src/tools/some_tool.py", ignored_lines=ignored)
    visitor.visit(tree)
    assert not any(v["rule"] == "PRINT_BAN" for v in visitor.violations)


# ── BASICCONFIG_BAN 規則測試 ────────────────────────────────────────────────

def test_basicconfig_ban_violation():
    """非白名單檔案的 logging.basicConfig() 應被偵測。"""
    code = "import logging\nlogging.basicConfig(level=logging.INFO)"
    violations = parse_and_check(code, "src/services/some_service.py")
    assert any(v["rule"] == "BASICCONFIG_BAN" for v in violations)


def test_basicconfig_ban_allows_whitelist():
    """logging_config.py（日誌 SSOT 入口）中的 basicConfig() 不報告。"""
    code = "import logging\nlogging.basicConfig(level=logging.INFO)"
    violations = parse_and_check(code, "logging_config.py")
    assert not any(v["rule"] == "BASICCONFIG_BAN" for v in violations)


# ── RAISE_STRING_BAN 規則測試 ────────────────────────────────────────────────

def test_raise_string_ban_string_literal():
    """raise ValueError("literal") 在 src/ 業務代碼中應被偵測。"""
    code = 'raise ValueError("error occurred")'
    violations = parse_and_check(code, "ToukaProxy/src/services/some_service.py")
    assert any(v["rule"] == "RAISE_STRING_BAN" for v in violations)


def test_raise_string_ban_fstring():
    """raise RuntimeError(f"f-string {var}") 應被偵測。"""
    code = 'raise RuntimeError(f"Server {name} not found")'
    violations = parse_and_check(code, "ToukaProxy/src/services/some_service.py")
    assert any(v["rule"] == "RAISE_STRING_BAN" for v in violations)


def test_raise_string_ban_no_violation_variable():
    """raise ValueError(some_variable) 不應被偵測（傳入變數，非字串字面量）。"""
    code = 'raise ValueError(error_message)'
    violations = parse_and_check(code, "ToukaProxy/src/services/some_service.py")
    assert not any(v["rule"] == "RAISE_STRING_BAN" for v in violations)


def test_raise_string_ban_not_banned_exception():
    """raise NotImplementedError("msg") 不在禁止清單，不應被偵測。"""
    code = 'raise NotImplementedError("not implemented")'
    violations = parse_and_check(code, "ToukaProxy/src/services/some_service.py")
    assert not any(v["rule"] == "RAISE_STRING_BAN" for v in violations)


def test_raise_string_ban_outside_src():
    """tests/ 目錄下的 raise 不應被偵測（/src/ 路徑過濾）。"""
    code = 'raise ValueError("test error")'
    violations = parse_and_check(code, "tests/integration/test_something.py")
    assert not any(v["rule"] == "RAISE_STRING_BAN" for v in violations)


def test_raise_string_ban_noqa():
    """# noqa: RAISE_STRING_BAN 豁免機制：直接傳 ignored_lines 測試抑制邏輯。"""
    code = 'raise RuntimeError("Direct access banned")'
    ignored: dict[int, set[str]] = {1: {"RAISE_STRING_BAN"}}
    tree = ast.parse(code)
    visitor = SSOTGuard("ToukaProxy/src/services/soul_service.py", ignored_lines=ignored)
    visitor.visit(tree)
    assert not any(v["rule"] == "RAISE_STRING_BAN" for v in visitor.violations)


# ── ENV_SSOT_VIOLATION 擴充：系統 API 禁令 ─────────────────────────────────


def test_os_system_violation():
    """os.system() 在 src/ 業務代碼中應觸發 ENV_SSOT_VIOLATION。"""
    code = "import os\nos.system('ls')"
    violations = parse_and_check(code, "ToukaProxy/src/services/foo.py")
    assert any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)


def test_os_chdir_violation():
    """os.chdir() 在 src/ 業務代碼中應觸發 ENV_SSOT_VIOLATION。"""
    code = "import os\nos.chdir('/tmp')"
    violations = parse_and_check(code, "ToukaProxy/src/services/foo.py")
    assert any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)


def test_sys_path_append_violation():
    """sys.path.append() 在 src/ 業務代碼中應觸發 ENV_SSOT_VIOLATION。"""
    code = "import sys\nsys.path.append('/some/path')"
    violations = parse_and_check(code, "ToukaProxy/src/services/foo.py")
    assert any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)


def test_os_system_outside_src_no_violation():
    """tests/ 目錄（/src/ 路徑過濾）不應被偵測。"""
    code = "import os\nos.system('ls')"
    violations = parse_and_check(code, "tests/unit/test_something.py")
    assert not any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)


def test_env_whitelist_allows_system_api():
    """ENV_WHITELIST 中的檔案（如 bootstrap.py）應被豁免。"""
    code = "import os\nos.system('ls')"
    violations = parse_and_check(code, "bootstrap.py")
    assert not any(v["rule"] == "ENV_SSOT_VIOLATION" for v in violations)


# ── API_PREFIX_SSOT 規則測試（ApiPrefixSovereigntyRule，先前零覆蓋）─────────────


def test_api_prefix_router_missing_prefix():
    """APIRouter 缺少 prefix 參數 → 應觸發 API_PREFIX_SSOT。"""
    violations = parse_and_check("r = APIRouter(tags=['x'])")
    assert any(v["rule"] == "API_PREFIX_SSOT" for v in violations)


def test_api_prefix_router_missing_tags():
    """APIRouter 缺少 tags 參數 → 應觸發 API_PREFIX_SSOT。"""
    violations = parse_and_check("r = APIRouter(prefix='/users')")
    assert any(v["rule"] == "API_PREFIX_SSOT" for v in violations)


def test_api_prefix_router_hardcoded_v1_banned():
    """APIRouter 硬編碼 '/v1' 前綴 → 應觸發 API_PREFIX_SSOT（須在 main.py 統一掛載）。"""
    violations = parse_and_check("r = APIRouter(prefix='/v1/users', tags=['x'])")
    assert any(
        v["rule"] == "API_PREFIX_SSOT" and "/v1" in v["msg"] for v in violations
    )


def test_api_prefix_router_valid_passes():
    """APIRouter 同時帶合法 prefix + tags（非 /v1）→ 不應報告。"""
    violations = parse_and_check("r = APIRouter(prefix='/users', tags=['x'])")
    assert not any(v["rule"] == "API_PREFIX_SSOT" for v in violations)


def test_api_prefix_trailing_slash_banned():
    """端點路徑以斜線結尾 → 應觸發 API_PREFIX_SSOT（無斜線韌性）。"""
    violations = parse_and_check("@r.get('/users/')\ndef f(): pass")
    assert any(v["rule"] == "API_PREFIX_SSOT" for v in violations)


def test_api_prefix_root_slash_allowed():
    """根路徑 '/' 例外 → 不應報告。"""
    violations = parse_and_check("@r.get('/')\ndef f(): pass")
    assert not any(v["rule"] == "API_PREFIX_SSOT" for v in violations)


# ── FILELOCK_ASYNC_SSOT 規則測試（FileLockAsyncRule，先前零覆蓋，ADR-0049）──────


def test_filelock_async_banned_in_service():
    """服務層 `from filelock import FileLock` → 應觸發 FILELOCK_ASYNC_SSOT。"""
    violations = parse_and_check(
        "from filelock import FileLock", "ToukaProxy/src/services/foo_service.py"
    )
    assert any(v["rule"] == "FILELOCK_ASYNC_SSOT" for v in violations)


def test_filelock_async_allows_non_service_layer():
    """非服務層（如 utils）import 同步 FileLock → 不在本規則管轄，不報告。"""
    violations = parse_and_check(
        "from filelock import FileLock", "ToukaProxy/src/utils/foo.py"
    )
    assert not any(v["rule"] == "FILELOCK_ASYNC_SSOT" for v in violations)


def test_filelock_async_allows_other_names():
    """服務層 import 的是 SoftFileLock（非 FileLock）→ 不報告（只抓 FileLock 名稱）。"""
    violations = parse_and_check(
        "from filelock import SoftFileLock", "ToukaProxy/src/services/foo_service.py"
    )
    assert not any(v["rule"] == "FILELOCK_ASYNC_SSOT" for v in violations)


def test_filelock_async_catches_module_attribute_call():
    """批⑤-D 0049⑤：`import filelock; filelock.FileLock(...)` 繞道也必須被抓。

    先前只抓 ImportFrom → Import＋屬性呼叫可無聲繞過同步 FileLock 禁令（守衛盲點）。
    """
    violations = parse_and_check(
        "import filelock\nlock = filelock.FileLock('x.lock')",
        "ToukaProxy/src/services/foo_service.py",
    )
    assert any(v["rule"] == "FILELOCK_ASYNC_SSOT" for v in violations)


def test_filelock_async_allows_module_asyncfilelock_call():
    """`filelock.AsyncFileLock(...)` 是被鼓勵的正解 → 不報告（陽性對照）。"""
    violations = parse_and_check(
        "import filelock\nlock = filelock.AsyncFileLock('x.lock')",
        "ToukaProxy/src/services/foo_service.py",
    )
    assert not any(v["rule"] == "FILELOCK_ASYNC_SSOT" for v in violations)
