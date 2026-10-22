"""完整 ruff 只減不增（ADR-0278）。

CI 本來就跑 pytest，所以這支測試就是完整 ruff 在 CI 上的關卡。每條規則的數量必須和
`tests/lint/ruff_baseline.json` 完全相等：變多要修，變少要在同一個 PR 重拍基準，
改動才會出現在差異裡。範圍和 CI 的 `ruff check --select F` 相同，是四個產品資料夾。
"""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import lint_ratchet  # scripts/ 由 tests/conftest.py 加進 sys.path
import pytest
import yaml
from lint_ratchet import PRODUCT_SOURCE_ROOTS, RUFF_BASELINE, collect_ruff, compare, load_baseline

WORKFLOW = Path(__file__).absolute().parents[2] / ".github/workflows/native-tool-contracts.yml"


def test_ruff_violations_match_the_baseline() -> None:
    problems = compare(collect_ruff(), load_baseline(RUFF_BASELINE))
    assert not problems, "\n".join(
        [*problems, "修好或確認後跑：python scripts/lint_ratchet.py ruff --update"]
    )


def test_ruff_ratchet_scans_the_same_roots_as_the_ci_pyflakes_step() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    runs = [step["run"].split() for job in jobs.values() for step in job["steps"] if "run" in step]
    pyflakes = [argv for argv in runs if "ruff" in argv and "--select" in argv and "F" in argv]
    assert len(pyflakes) == 1
    ci_roots = {arg for arg in pyflakes[0] if arg.startswith("Touka")}
    assert ci_roots == set(PRODUCT_SOURCE_ROOTS)


def test_compare_requires_exact_counts() -> None:
    baseline = {"E501": 3, "ERA001": 1}
    assert compare(Counter({"E501": 3, "ERA001": 1}), baseline) == []

    more = compare(Counter({"E501": 4, "ERA001": 1}), baseline)
    assert len(more) == 1 and "E501" in more[0] and "變多" in more[0]

    fewer = compare(Counter({"E501": 3}), baseline)
    assert len(fewer) == 1 and "ERA001" in fewer[0] and "變少" in fewer[0]

    new_rule = compare(Counter({"E501": 3, "ERA001": 1, "B904": 1}), baseline)
    assert len(new_rule) == 1 and "B904" in new_rule[0] and "變多" in new_rule[0]


def test_collect_fails_when_ruff_does_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """設定壞掉等異常結束不能被當成 0 個違規。"""
    broken = SimpleNamespace(returncode=2, stdout="", stderr="ruff failed: bad config")
    fake = SimpleNamespace(run=lambda *_args, **_kwargs: broken)
    monkeypatch.setattr(lint_ratchet, "subprocess", fake)
    with pytest.raises(RuntimeError, match="exit 2"):
        collect_ruff()


def test_update_writes_a_sorted_baseline_that_then_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = tmp_path / "baseline.json"
    counts = Counter({"TRY003": 2, "E501": 5})
    monkeypatch.setattr(lint_ratchet, "TOOLS", {"ruff": (lambda: counts, baseline)})
    assert lint_ratchet.main(["ruff", "--update"]) == 0
    assert list(json.loads(baseline.read_text(encoding="utf-8"))) == ["E501", "TRY003"]
    assert lint_ratchet.main(["ruff"]) == 0
    counts["E501"] += 1
    assert lint_ratchet.main(["ruff"]) == 1


@pytest.mark.parametrize("argv", [[], ["eslint"], ["ruff", "--updat"], ["ruff", "--update", "x"]])
def test_main_rejects_unknown_tools_and_flags(argv: list[str]) -> None:
    """打錯的參數要報錯，不能默默照常檢查。"""
    assert lint_ratchet.main(argv) == 2
