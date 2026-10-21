"""ADR-0062：useConfirmToast SSOT 結構性測試 (TDD).

驗證：
  - useConfirmToast hook 存在且使用 toast.warning
  - 5 個元件不再使用原生 confirm() 瀏覽器對話框
  - 5 個元件均引入 useConfirmToast
"""

from __future__ import annotations

import re
from pathlib import Path

HOOK_PATH = Path("ToukaChat/src/hooks/useConfirmToast.ts")

COMPONENTS: dict[str, Path] = {
    "SkillsTab": Path("ToukaChat/src/components/settings/tabs/SkillsTab.tsx"),
    "ModelsTab": Path("ToukaChat/src/components/settings/tabs/ModelsTab.tsx"),
    "UsersTab": Path("ToukaChat/src/components/settings/tabs/UsersTab.tsx"),
    "ProjectsView": Path("ToukaChat/src/components/projects/ProjectsView.tsx"),
    "HeartbeatScheduler": Path("ToukaChat/src/components/dashboard/HeartbeatScheduler.tsx"),
}


def test_use_confirm_toast_hook_exists() -> None:
    """useConfirmToast hook 必須存在。"""
    assert HOOK_PATH.exists(), f"{HOOK_PATH} must be created"


def test_use_confirm_toast_uses_warning_style() -> None:
    """useConfirmToast 必須使用 toast.warning 給予破壞性操作適當的視覺警示。"""
    content = HOOK_PATH.read_text(encoding="utf-8")
    assert "confirmToast" in content, "useConfirmToast must export confirmToast"
    assert "toast.warning" in content, (
        "useConfirmToast must use toast.warning for destructive actions"
    )


def test_no_native_confirm_in_components() -> None:
    """5 個元件均不得呼叫原生 confirm() 瀏覽器對話框。"""
    violations = []
    for name, path in COMPONENTS.items():
        content = path.read_text(encoding="utf-8")
        if re.search(r"\bconfirm\s*\(", content):
            violations.append(name)
    assert not violations, (
        f"These components still use native confirm(): {violations}. "
        "Replace with useConfirmToast()."
    )


def test_all_components_use_confirm_toast() -> None:
    """5 個元件均必須引入並使用 useConfirmToast hook。"""
    violations = []
    for name, path in COMPONENTS.items():
        content = path.read_text(encoding="utf-8")
        if "useConfirmToast" not in content:
            violations.append(name)
    assert not violations, (
        f"These components do not import useConfirmToast: {violations}"
    )
