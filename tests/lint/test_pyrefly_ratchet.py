"""Python 型別檢查只減不增（ADR-0278 §7）。

範圍與設定在 pyproject.toml 的 `[tool.pyrefly]`，先從隔離與安全邊界開始。每種錯誤的數量必須和
`tests/lint/pyrefly_baseline.json` 完全相等：變多要修，變少要在同一個 PR 重拍基準。
升級 pyrefly 或第三方套件時數字可能跟著變，在同一個 PR 重拍並在 PR 裡說明。
"""

import json
import sys
import tomllib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import lint_ratchet  # scripts/ 由 tests/conftest.py 加進 sys.path
import pytest
from lint_ratchet import PROJECT_ROOT, PYREFLY_BASELINE, collect_pyrefly, compare, load_baseline

PYPROJECT = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PYREFLY_CONFIG = PYPROJECT["tool"]["pyrefly"]


def _fake_pyrefly(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str, stderr: str
) -> list:
    """替換子程序，回傳收到的 argv 清單，讓測試不必真的跑 pyrefly。"""
    calls: list = []
    done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return done

    monkeypatch.setattr(lint_ratchet, "subprocess", SimpleNamespace(run=run))
    return calls


def test_pyrefly_errors_match_the_baseline() -> None:
    problems = compare(collect_pyrefly(), load_baseline(PYREFLY_BASELINE))
    assert not problems, "\n".join(
        [*problems, "修好或確認後跑：python scripts/lint_ratchet.py pyrefly --update"]
    )


def test_pyrefly_is_a_registered_ratchet() -> None:
    assert lint_ratchet.TOOLS["pyrefly"] == (collect_pyrefly, PYREFLY_BASELINE)


def test_config_fixes_platform_and_version_without_an_interpreter_path() -> None:
    """平台固定 linux、版本對齊 .python-version；直譯器不寫死，由收集指令傳入。"""
    pinned = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert PYREFLY_CONFIG["python-platform"] == "linux"
    assert PYREFLY_CONFIG["python-version"] == pinned
    assert "python-interpreter-path" not in PYREFLY_CONFIG


@pytest.mark.parametrize("pattern", PYREFLY_CONFIG["project-includes"])
def test_every_include_pattern_still_matches_files(pattern: str) -> None:
    """pyrefly 對一個檔案也沒對到的樣式不報錯；檔案改名時範圍會悄悄變小。"""
    assert any(PROJECT_ROOT.glob(pattern)), f"{pattern} 沒有對到任何檔案"


def test_collect_runs_pyrefly_with_this_interpreter_and_the_repo_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["bad-argument-type", "missing-attribute", "bad-argument-type"]
    payload = json.dumps({"errors": [{"name": name} for name in names]})
    calls = _fake_pyrefly(monkeypatch, returncode=1, stdout=payload, stderr=" INFO 3 errors")
    assert collect_pyrefly() == Counter({"bad-argument-type": 2, "missing-attribute": 1})
    argv = calls[0]
    assert argv[:4] == [sys.executable, "-m", "pyrefly", "check"]
    assert argv[argv.index("--config") + 1] == "pyproject.toml"
    assert argv[argv.index("--python-interpreter-path") + 1] == sys.executable


NO_ERRORS = json.dumps({"errors": []})


def test_collect_accepts_a_clean_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pyrefly(monkeypatch, returncode=0, stdout=NO_ERRORS, stderr=" INFO 0 errors")
    assert collect_pyrefly() == Counter()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        # 設定壞掉時結束碼和「有型別錯誤」一樣是 1，只能從輸出分辨。
        (1, "", " INFO Fatal configuration error"),
        (1, NO_ERRORS, " WARN pyproject.toml: Extra keys found in config: pyhton-platform"),
        (1, NO_ERRORS, " WARN pyproject.toml: Invalid search-path: `ToukaMemory/src` is missing"),
        (2, NO_ERRORS, ""),
        (0, "[]", ""),
    ],
)
def test_collect_fails_instead_of_counting_zero(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, stderr: str
) -> None:
    _fake_pyrefly(monkeypatch, returncode=returncode, stdout=stdout, stderr=stderr)
    with pytest.raises(RuntimeError, match="pyrefly did not finish"):
        collect_pyrefly()


def test_update_writes_a_sorted_baseline_that_then_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = tmp_path / "pyrefly_baseline.json"
    counts = Counter({"missing-attribute": 2, "bad-assignment": 1})
    monkeypatch.setattr(lint_ratchet, "TOOLS", {"pyrefly": (lambda: counts, baseline)})
    assert lint_ratchet.main(["pyrefly", "--update"]) == 0
    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert list(written) == ["bad-assignment", "missing-attribute"]
    assert lint_ratchet.main(["pyrefly"]) == 0
    counts["bad-assignment"] -= 1
    assert lint_ratchet.main(["pyrefly"]) == 1
