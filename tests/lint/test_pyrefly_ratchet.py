"""Python 型別檢查只減不增（ADR-0278 §7）。

範圍與設定在 pyproject.toml 的 `[tool.pyrefly]`，先從隔離與安全邊界開始。每種錯誤的數量必須和
`tests/lint/pyrefly_baseline.json` 完全相等：變多要修，變少要在同一個 PR 重拍基準。
升級 pyrefly 或第三方套件時數字可能跟著變，在同一個 PR 重拍並在 PR 裡說明。
"""

import json
import re
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
# 審查過的範圍。縮小範圍、排除檔案、關掉錯誤種類、換平台都會讓數字悄悄變好看，
# 所以整份設定釘在這裡；要調整就改這裡，改動會出現在審查的差異裡。
REVIEWED_CONFIG = {
    "project-includes": [
        "ToukaProxy/src/sandbox_kernel/**/*.py",
        "ToukaProxy/src/services/isolation/**/*.py",
        "ToukaProxy/src/services/authorization_gateway.py",
        "ToukaProxy/src/services/execution_broker.py",
    ],
    "search-path": ["ToukaProxy", "ToukaMemory/src"],
    "python-platform": "linux",
    "use-ignore-files": False,
}
# 這個註解會壓掉整個檔案的錯誤（10/23 實測；`# type: ignore`、`# pyre-ignore-all-errors` 不會）。
WHOLE_FILE_IGNORE = re.compile(r"pyrefly:\s*ignore-errors")
TINY_PROJECT_SOURCE = 'x: int = "a"\n'


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


def test_config_is_exactly_the_reviewed_scope() -> None:
    """平台固定 linux、版本對齊 .python-version；沒有排除、錯誤開關，也不寫死直譯器。"""
    pinned = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert {**REVIEWED_CONFIG, "python-version": pinned} == PYREFLY_CONFIG


@pytest.mark.parametrize("pattern", REVIEWED_CONFIG["project-includes"])
def test_every_include_pattern_still_matches_files(pattern: str) -> None:
    """pyrefly 對一個檔案也沒對到的樣式不報錯；檔案改名時範圍會悄悄變小。"""
    assert any(PROJECT_ROOT.glob(pattern)), f"{pattern} 沒有對到任何檔案"


def test_no_file_in_scope_switches_off_type_checking() -> None:
    """整檔壓制會讓錯誤數變少，卻不是真的修好。"""
    patterns = REVIEWED_CONFIG["project-includes"]
    files = {path for pattern in patterns for path in PROJECT_ROOT.glob(pattern)}
    offenders = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in files
        if WHOLE_FILE_IGNORE.search(path.read_text(encoding="utf-8"))
    )
    assert not offenders, f"範圍內不要整檔壓制型別檢查：{offenders}"


def test_collect_runs_pyrefly_with_this_interpreter_and_the_repo_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["bad-argument-type", "missing-attribute", "bad-argument-type"]
    payload = json.dumps({"errors": [{"name": name} for name in names]})
    calls = _fake_pyrefly(monkeypatch, returncode=1, stdout=payload, stderr=" INFO 3 errors")
    assert collect_pyrefly() == Counter({"bad-argument-type": 2, "missing-attribute": 1})
    # 整組釘住：多一個 --ignore 或 --project-excludes 都會讓數字悄悄變好看。
    assert calls == [
        [
            sys.executable,
            "-m",
            "pyrefly",
            "check",
            "--config",
            "pyproject.toml",
            "--python-interpreter-path",
            sys.executable,
            "--output-format",
            "json",
        ]
    ]


NO_ERRORS = json.dumps({"errors": []})


def test_collect_accepts_a_clean_run(monkeypatch: pytest.MonkeyPatch) -> None:
    logs = " INFO Checking project configured at `pyproject.toml`\n INFO 0 errors\n\n"
    _fake_pyrefly(monkeypatch, returncode=0, stdout=NO_ERRORS, stderr=logs)
    assert collect_pyrefly() == Counter()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        # 設定壞掉時結束碼和「有型別錯誤」一樣是 1，只能從輸出分辨。
        (1, "", " WARN pyproject.toml: failed to parse\nFatal configuration error"),
        (1, NO_ERRORS, " WARN pyproject.toml: Extra keys found in config: pyhton-platform"),
        (1, NO_ERRORS, " WARN pyproject.toml: Invalid search-path: `ToukaMemory/src` is missing"),
        # 升級後日誌格式變了，也不能當成正常跑完。
        (1, NO_ERRORS, " INFO 0 errors\nWARNING something changed"),
        (1, NO_ERRORS, " ERROR something failed"),
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


def _tiny_project(monkeypatch: pytest.MonkeyPatch, root: Path, extra_config: str) -> None:
    """在暫存資料夾放一份設定和一個有型別錯誤的檔案，讓收集指令改去檢查它。"""
    config = '[tool.pyrefly]\nproject-includes = ["m.py"]\n' + extra_config
    (root / "pyproject.toml").write_text(config, encoding="utf-8")
    (root / "m.py").write_text(TINY_PROJECT_SOURCE, encoding="utf-8")
    monkeypatch.setattr(lint_ratchet, "PROJECT_ROOT", root)


def test_real_pyrefly_output_is_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """用真的 pyrefly 核對輸出格式；升級後格式變了，這裡會先失敗。"""
    _tiny_project(monkeypatch, tmp_path, "")
    assert collect_pyrefly() == Counter({"bad-assignment": 1})


@pytest.mark.parametrize(
    "extra_config",
    [
        'python-platfrom = "linux"\n',  # 欄位打錯：只印 WARN 就照跑
        'search-path = ["missing"]\n',  # 資料夾不存在：只印 WARN 就照跑
        "project-excludes = [1]\n",  # 設定讀不懂
    ],
)
def test_real_pyrefly_config_mistakes_fail_the_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_config: str
) -> None:
    _tiny_project(monkeypatch, tmp_path, extra_config)
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
