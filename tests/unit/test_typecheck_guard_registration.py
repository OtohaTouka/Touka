"""TDD 結構性測試：Guard #25 — Frontend TypeScript Type Check Guard。

Red：在 tsconfig.guard.json 不存在、package.json 缺少腳本、
     guard_all.py 未包含任務時三個測試均 FAIL。
Green：實作後三個斷言全部通過。

ADR-0054：Frontend TypeScript Type Check Guard。
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
FRONTEND_DIR = PROJECT_ROOT / "ToukaChat"


def test_tsconfig_guard_json_exists() -> None:
    """tsconfig.guard.json 必須存在，作為守衛專用生產原始碼型別設定 SSOT。"""
    config_path = FRONTEND_DIR / "tsconfig.guard.json"
    assert config_path.exists(), (
        "缺少 ToukaChat/tsconfig.guard.json。"
        " Guard #25 需要一份只涵蓋生產原始碼（排除測試檔案）的 TypeScript 設定。"
    )


def test_package_json_has_guard_typecheck() -> None:
    """package.json 必須包含 guard:typecheck 腳本，且必須使用 tsconfig.guard.json。"""
    package_json_path = FRONTEND_DIR / "package.json"
    data = json.loads(package_json_path.read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("scripts", {})

    assert "guard:typecheck" in scripts, (
        "ToukaChat/package.json 缺少 guard:typecheck 腳本。"
        f" 現有守衛腳本：{[k for k in scripts if k.startswith('guard:')]}"
    )
    assert "tsconfig.guard.json" in scripts["guard:typecheck"], (
        "guard:typecheck 腳本必須使用 tsconfig.guard.json 設定，"
        f" 實際內容：{scripts['guard:typecheck']!r}"
    )


def test_guard_all_includes_typecheck_guard() -> None:
    """guard_all.py 的 tasks 清單必須包含 Frontend TypeScript Type Check Guard 任務。"""
    guard_all_path = PROJECT_ROOT / "scripts" / "guard_all.py"
    content = guard_all_path.read_text(encoding="utf-8")

    assert "guard:typecheck" in content, (
        "scripts/guard_all.py 未包含 npm run guard:typecheck。"
        " Guard #25 必須被加入 tasks 清單。"
    )
    assert "TypeScript Type Check" in content, (
        "scripts/guard_all.py 未包含 TypeScript Type Check 守衛名稱。"
    )
