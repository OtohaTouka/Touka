"""TDD 結構性測試：Guard #31 — Frontend Orphaned Tests Guard。

確保「孤兒測試守衛」已建立並接線：守衛腳本與純邏輯模組存在、
package.json 有 guard:orphaned-tests、guard_all.py 納入 Guard #31，
且 vitest 與守衛共用同一份 include SSOT（TEST_INCLUDE_GLOBS）。
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
FRONTEND_DIR = PROJECT_ROOT / "ToukaChat"


def test_orphaned_tests_guard_scripts_exist() -> None:
    """守衛腳本與其純邏輯模組必須存在。"""
    assert (FRONTEND_DIR / "scripts" / "guard_orphaned_tests.ts").exists(), (
        "缺少 ToukaChat/scripts/guard_orphaned_tests.ts。"
    )
    assert (FRONTEND_DIR / "scripts" / "lib" / "orphaned_tests_logic.ts").exists(), (
        "缺少 ToukaChat/scripts/lib/orphaned_tests_logic.ts（findOrphans 純邏輯 SSOT）。"
    )


def test_package_json_has_guard_orphaned_tests() -> None:
    """package.json 必須包含 guard:orphaned-tests 腳本。"""
    data = json.loads((FRONTEND_DIR / "package.json").read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("scripts", {})
    assert "guard:orphaned-tests" in scripts, (
        "ToukaChat/package.json 缺少 guard:orphaned-tests 腳本。"
        f" 現有守衛腳本：{[k for k in scripts if k.startswith('guard:')]}"
    )


def test_guard_all_includes_orphaned_tests_guard() -> None:
    """guard_all.py 的 tasks 清單必須包含 Guard #31。"""
    content = (PROJECT_ROOT / "scripts" / "guard_all.py").read_text(encoding="utf-8")
    assert "guard:orphaned-tests" in content, (
        "scripts/guard_all.py 未包含 npm run guard:orphaned-tests。"
    )
    assert "Guard #31" in content, "scripts/guard_all.py 未包含 Guard #31。"
    assert "Orphaned Tests" in content, (
        "scripts/guard_all.py 未包含 Orphaned Tests 守衛名稱。"
    )


def test_vitest_include_is_single_source_of_truth() -> None:
    """vitest.config.ts 必須匯入共用的 TEST_INCLUDE_GLOBS 作為 include（SSOT）。"""
    content = (FRONTEND_DIR / "vitest.config.ts").read_text(encoding="utf-8")
    assert "TEST_INCLUDE_GLOBS" in content, (
        "vitest.config.ts 應使用共用的 TEST_INCLUDE_GLOBS 作為 include，"
        " 以與孤兒測試守衛保持單一事實來源。"
    )
