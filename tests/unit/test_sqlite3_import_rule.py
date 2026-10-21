"""Tests for Sqlite3ImportRule — SQLITE3_IMPORT_BAN static enforcement.

ADR-0030: 禁止直接 import sqlite3，必須透過授權的非同步資料庫代理層。
授權白名單：async_db.py（ToukaProxy SSOT）、factual.py、legacy_store.py（ToukaMemory SSOT）。
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


def sqlite3_violations(violations: list[dict]) -> list[dict]:
    return [v for v in violations if v["rule"] == "SQLITE3_IMPORT_BAN"]


# ── 應觸發違規的情境 ────────────────────────────────────────────────────────


def test_import_sqlite3_triggers_violation() -> None:
    """import sqlite3 在非授權檔案中應觸發 SQLITE3_IMPORT_BAN。"""
    code = "import sqlite3"
    violations = sqlite3_violations(parse_and_check(code, "some_router.py"))
    assert len(violations) == 1


def test_from_sqlite3_import_triggers_violation() -> None:
    """from sqlite3 import connect 在非授權檔案中應觸發 SQLITE3_IMPORT_BAN。"""
    code = "from sqlite3 import connect, Row"
    violations = sqlite3_violations(parse_and_check(code, "some_service.py"))
    assert len(violations) == 1


def test_from_sqlite3_submodule_triggers_violation() -> None:
    """from sqlite3.dbapi2 import ... 在非授權檔案中應觸發。"""
    code = "from sqlite3.dbapi2 import Connection"
    violations = sqlite3_violations(parse_and_check(code, "utils.py"))
    assert len(violations) == 1


# ── 白名單豁免 ───────────────────────────────────────────────────────────────


def test_async_db_allowed() -> None:
    """async_db.py 是 ToukaProxy 授權的非同步 DB SSOT，import sqlite3 不應觸發。"""
    code = "import sqlite3"
    violations = sqlite3_violations(parse_and_check(code, "async_db.py"))
    assert len(violations) == 0


def test_factual_allowed() -> None:
    """factual.py 是 ToukaMemory 自有事實庫 SSOT，import sqlite3 不應觸發。"""
    code = "import sqlite3"
    violations = sqlite3_violations(parse_and_check(code, "factual.py"))
    assert len(violations) == 0


def test_legacy_store_allowed() -> None:
    """legacy_store.py 是 ToukaMemory 自有遺產庫 SSOT，import sqlite3 不應觸發。"""
    code = "from sqlite3 import connect"
    violations = sqlite3_violations(parse_and_check(code, "legacy_store.py"))
    assert len(violations) == 0


# ── 不相關的 import 不受影響 ─────────────────────────────────────────────────


def test_unrelated_import_no_violation() -> None:
    """import aiosqlite 等不相關模組不應觸發 SQLITE3_IMPORT_BAN。"""
    code = "import aiosqlite\nimport sqlalchemy\nfrom databases import Database"
    violations = sqlite3_violations(parse_and_check(code, "some_service.py"))
    assert len(violations) == 0


# ── noqa 豁免機制 ───────────────────────────────────────────────────────────


def test_noqa_suppresses_violation() -> None:
    """# noqa: SQLITE3_IMPORT_BAN 豁免機制：直接傳 ignored_lines 驗證抑制邏輯。"""
    code = "import sqlite3"
    ignored: dict[int, set[str]] = {1: {"SQLITE3_IMPORT_BAN"}}
    tree = ast.parse(code)
    visitor = SSOTGuard("observability.py", ignored_lines=ignored)
    visitor.visit(tree)
    assert not any(v["rule"] == "SQLITE3_IMPORT_BAN" for v in visitor.violations)
