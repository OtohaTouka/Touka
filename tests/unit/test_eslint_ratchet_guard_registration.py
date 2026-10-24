"""TDD 結構性測試：Guard #35 — ESLint Ratchet Guard。

ADR-0098：ESLint error 級違規「只降不升」棘輪。
驗證守衛腳本、基準檔與 guard_all.py 註冊三者齊備。
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def test_ratchet_guard_script_exists() -> None:
    """guard_eslint_ratchet.py 必須存在（Guard #35 腳本 SSOT）。"""
    assert (SCRIPTS_DIR / "guard_eslint_ratchet.py").exists(), (
        "缺少 scripts/guard_eslint_ratchet.py（Guard #35）。"
    )


def test_eslint_baseline_exists_and_valid() -> None:
    """eslint_baseline.json 必須存在且為 {規則: 整數計數} 結構。"""
    baseline_path = SCRIPTS_DIR / "eslint_baseline.json"
    assert baseline_path.exists(), "缺少 scripts/eslint_baseline.json 基準檔。"
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "eslint_baseline.json 必須是物件。"
    assert all(isinstance(v, int) for v in data.values()), (
        "eslint_baseline.json 的每個值必須是整數計數。"
    )


def test_guard_all_includes_ratchet_guard() -> None:
    """guard_all.py 的 tasks 清單必須註冊 ESLint Ratchet Guard。"""
    content = (SCRIPTS_DIR / "guard_all.py").read_text(encoding="utf-8")
    assert "guard_eslint_ratchet.py" in content, (
        "scripts/guard_all.py 未包含 guard_eslint_ratchet.py。Guard #35 必須加入 tasks。"
    )
    assert "ESLint Ratchet Guard" in content, (
        "scripts/guard_all.py 未包含 ESLint Ratchet Guard 守衛名稱。"
    )


def test_collect_violations_runs_only_eslint(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0282: Guard #35 no longer runs a braces verifier before ESLint."""
    spec = importlib.util.spec_from_file_location(
        "eslint_ratchet_under_test", SCRIPTS_DIR / "guard_eslint_ratchet.py"
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    calls: list[tuple[object, dict[str, Any]]] = []

    def fake_run(command: object, **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="[]", stderr="", returncode=0)

    monkeypatch.setattr(guard, "subprocess", SimpleNamespace(run=fake_run))

    assert guard.collect_violations() == Counter()
    assert [command for command, _ in calls] == [guard.ESLINT_CMD]
    assert calls[0][1]["cwd"] == PROJECT_ROOT / "ToukaChat"


def _load_guard() -> Any:
    spec = importlib.util.spec_from_file_location(
        "eslint_ratchet_baseline_under_test", SCRIPTS_DIR / "guard_eslint_ratchet.py"
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


@pytest.mark.parametrize(("baseline_text", "expected_exit"), [("{}\n", 0), (None, 1)])
def test_empty_baseline_passes_but_missing_baseline_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, baseline_text: str | None, expected_exit: int
) -> None:
    """違規修到 0 後重拍出 `{}` 仍要能通過；只有基準檔不存在才算沒設定（ADR-0278）。"""
    guard = _load_guard()
    baseline = tmp_path / "eslint_baseline.json"
    if baseline_text is not None:
        baseline.write_text(baseline_text, encoding="utf-8")
    monkeypatch.setattr(guard, "BASELINE_PATH", baseline)
    monkeypatch.setattr(guard, "collect_violations", lambda: guard.Counter())
    monkeypatch.setattr(guard.sys, "argv", ["guard_eslint_ratchet.py"])
    with pytest.raises(SystemExit) as exit_info:
        guard.main()
    assert exit_info.value.code == expected_exit
