"""
Structural guard: verifies that npm run test covers the full frontend test suite.

ADR-0055: vitest.config.ts is the SSOT for test discovery; package.json test script
must not hardcode a specific sub-directory.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
FRONTEND_DIR = REPO_ROOT / "ToukaChat"


def test_package_json_test_script_not_restricted_to_auth():
    """'test' script must not hard-restrict discovery to tests/auth."""
    pkg = json.loads((FRONTEND_DIR / "package.json").read_text(encoding="utf-8"))
    script = pkg["scripts"]["test"]
    assert "tests/auth" not in script, (
        f"package.json 'test' script is restricted to tests/auth only: {script!r}. "
        "Update it so the full test suite runs (see ADR-0055)."
    )


# SSOT for vitest test discovery globs（ADR-0055 + 孤兒測試守衛 Guard #31）。
# include 已從 vitest.config.ts 的字面陣列改為共用常數，與 guard_orphaned_tests.ts 同源。
_INCLUDE_SSOT = FRONTEND_DIR / "scripts" / "lib" / "orphaned_tests_logic.ts"


def test_vitest_config_uses_shared_include_ssot():
    """vitest.config.ts 必須以共用的 TEST_INCLUDE_GLOBS 作為 include（與孤兒測試守衛同源）。"""
    config = (FRONTEND_DIR / "vitest.config.ts").read_text(encoding="utf-8")
    assert "include" in config, (
        "vitest.config.ts must have an 'include'. "
        "Test discovery is the SSOT responsibility of vitest.config.ts (ADR-0055)."
    )
    assert "TEST_INCLUDE_GLOBS" in config, (
        "vitest.config.ts 'include' 必須使用共用的 TEST_INCLUDE_GLOBS "
        "（scripts/lib/orphaned_tests_logic.ts），讓 vitest 與孤兒測試守衛共用單一事實來源。"
    )


def test_include_ssot_covers_src_component_tests():
    """共用 include SSOT 必須涵蓋 src/ 下的測試（含元件測試）。"""
    ssot = _INCLUDE_SSOT.read_text(encoding="utf-8")
    assert "src/**" in ssot, (
        "TEST_INCLUDE_GLOBS 必須涵蓋 src/ 下的測試，元件測試才會被收錄執行。"
    )


def test_include_ssot_covers_tests_dir():
    """共用 include SSOT 必須涵蓋 tests/ 目錄（unit/guard/auth）。"""
    ssot = _INCLUDE_SSOT.read_text(encoding="utf-8")
    assert "tests/**" in ssot, (
        "TEST_INCLUDE_GLOBS 必須涵蓋 tests/ 目錄（tests/auth, tests/unit, tests/guard）。"
    )
