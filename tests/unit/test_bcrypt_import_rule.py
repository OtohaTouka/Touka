"""Tests for BcryptImportRule — BCRYPT_IMPORT_SSOT static enforcement.

ADR-0028: 嚴禁繞過 AuthService 直接使用 bcrypt 或 passlib。
唯一授權匯入點：ToukaProxy/src/services/auth_service.py
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from lint_ssot import SSOTGuard  # noqa: E402


def parse_and_check(code_str: str, filename: str = "dummy.py") -> list[dict]:
    """Helper: parse code and return all violations."""
    tree = ast.parse(code_str)
    visitor = SSOTGuard(filename)
    visitor.visit(tree)
    return visitor.violations


def bcrypt_violations(violations: list[dict]) -> list[dict]:
    return [v for v in violations if v["rule"] == "BCRYPT_IMPORT_SSOT"]


# ── 應觸發違規的情境 ────────────────────────────────────────────────────────


def test_import_bcrypt_triggers_violation():
    """import bcrypt 在非授權檔案中應觸發 BCRYPT_IMPORT_SSOT。"""
    code = "import bcrypt"
    violations = bcrypt_violations(parse_and_check(code, "some_service.py"))
    assert len(violations) == 1


def test_import_passlib_triggers_violation():
    """import passlib 在非授權檔案中應觸發 BCRYPT_IMPORT_SSOT。"""
    code = "import passlib"
    violations = bcrypt_violations(parse_and_check(code, "some_service.py"))
    assert len(violations) == 1


def test_from_bcrypt_import_triggers_violation():
    """from bcrypt import gensalt 在非授權檔案中應觸發 BCRYPT_IMPORT_SSOT。"""
    code = "from bcrypt import gensalt, hashpw"
    violations = bcrypt_violations(parse_and_check(code, "user_service.py"))
    assert len(violations) == 1


def test_from_passlib_hash_import_triggers_violation():
    """from passlib.hash import bcrypt 在非授權檔案中應觸發 BCRYPT_IMPORT_SSOT。"""
    code = "from passlib.hash import bcrypt"
    violations = bcrypt_violations(parse_and_check(code, "auth_helper.py"))
    assert len(violations) == 1


def test_from_passlib_submodule_triggers_violation():
    """from passlib.context import CryptContext 在非授權檔案中應觸發。"""
    code = "from passlib.context import CryptContext"
    violations = bcrypt_violations(parse_and_check(code, "middleware.py"))
    assert len(violations) == 1


# ── 白名單豁免：auth_service.py ─────────────────────────────────────────────


def test_auth_service_import_bcrypt_allowed():
    """auth_service.py 是唯一授權點，import bcrypt 不應觸發違規。"""
    code = "import bcrypt"
    violations = bcrypt_violations(parse_and_check(code, "auth_service.py"))
    assert len(violations) == 0


def test_auth_service_from_bcrypt_allowed():
    """auth_service.py 中 from bcrypt import ... 不應觸發違規。"""
    code = "from bcrypt import checkpw, gensalt, hashpw"
    violations = bcrypt_violations(parse_and_check(code, "auth_service.py"))
    assert len(violations) == 0


# ── 不相關的 import 不受影響 ─────────────────────────────────────────────────


def test_unrelated_import_no_violation():
    """import hashlib 等不相關模組不應觸發 BCRYPT_IMPORT_SSOT。"""
    code = "import hashlib\nimport hmac\nfrom cryptography.fernet import Fernet"
    violations = bcrypt_violations(parse_and_check(code, "crypto_util.py"))
    assert len(violations) == 0


# ── noqa 豁免機制 ───────────────────────────────────────────────────────────


def test_noqa_suppresses_violation():
    """# noqa: BCRYPT_IMPORT_SSOT 豁免機制：直接傳 ignored_lines 驗證抑制邏輯。"""
    code = "import bcrypt"
    ignored: dict[int, set[str]] = {1: {"BCRYPT_IMPORT_SSOT"}}
    tree = ast.parse(code)
    visitor = SSOTGuard("some_service.py", ignored_lines=ignored)
    visitor.visit(tree)
    assert not any(v["rule"] == "BCRYPT_IMPORT_SSOT" for v in visitor.violations)
