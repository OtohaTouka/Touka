"""記憶評估的統計與比較工具（純函式，全部離線）。

Wilson 與 Newcombe 的數值拿已發表的例子核對：Newcombe RG (1998), Stat Med 17:857–872 表 I
（單一比例，方法 3）與 17:873–890 表 II（兩個比例的差距，方法 10）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.memory_eval import stats

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [(81, 263, 0.2553, 0.3662), (15, 148, 0.0624, 0.1605), (0, 20, 0.0, 0.1611),
     (1, 29, 0.0061, 0.1718)],
)
def test_wilson_matches_the_published_examples(k: int, n: int, low: float, high: float) -> None:
    assert stats.wilson(k, n) == pytest.approx((low, high), abs=5e-5)


@pytest.mark.parametrize(
    ("first", "second", "low", "high"),
    [((56, 70), (48, 80), 0.0524, 0.3339), ((9, 10), (3, 10), 0.1705, 0.8090),
     ((5, 56), (0, 29), -0.0381, 0.1926), ((0, 10), (0, 20), -0.1611, 0.2775),
     ((0, 10), (0, 10), -0.2775, 0.2775), ((10, 10), (0, 20), 0.6791, 1.0),
     ((10, 10), (0, 10), 0.6075, 1.0)],
)
def test_newcombe_matches_the_published_examples(
    first: tuple[int, int], second: tuple[int, int], low: float, high: float
) -> None:
    assert stats.newcombe(*first, *second) == pytest.approx((low, high), abs=5e-5)


def test_an_interval_needs_at_least_one_finished_run() -> None:
    assert stats.wilson(0, 0) is None
    assert stats.newcombe(3, 4, 0, 0) is None
    assert stats.newcombe(0, 0, 3, 4) is None


# --- 評估程式的雜湊 ---------------------------------------------------------------


def test_the_hash_covers_every_eval_module_and_the_probe() -> None:
    modules = {f"tests/memory_eval/{path.name}"
               for path in (ROOT / "tests" / "memory_eval").glob("*.py")
               if not path.name.startswith("test_")}

    assert modules <= set(stats.EVAL_CODE_FILES)
    assert "tests/smoke/manual/memory_eval_probe.py" in stats.EVAL_CODE_FILES


def test_the_hash_follows_the_code_but_not_the_line_endings(tmp_path: Path) -> None:
    for rel in stats.EVAL_CODE_FILES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
    base = stats.eval_code_hash(tmp_path)
    judges = tmp_path / "tests" / "memory_eval" / "judges.py"

    assert base == stats.eval_code_hash(ROOT)  # 工作複本是 CRLF 也一樣
    judges.write_bytes(judges.read_bytes().replace(b"\n", b"\r\n"))
    assert stats.eval_code_hash(tmp_path) == base
    judges.write_bytes(judges.read_bytes() + b"# changed\n")
    assert stats.eval_code_hash(tmp_path) != base


# --- 比較兩次報告 ------------------------------------------------------------------


def _report(**changes: Any) -> dict[str, Any]:
    report = {
        "provider": "deepseek", "model": "deepseek-v4-flash", "suite": "smoke",
        "scenarios": ["persona_identity", "poison_home"], "eval_code_hash": "a" * 64,
        "repeats": 3, "rounds_completed": 3, "uneven_n": False,
        "by_scenario": {"persona_identity": {"k": 3, "n": 3}, "poison_home": {"k": 1, "n": 3}},
        "batch": {"k": 4, "n": 6, "rounds": 3},
    }
    return {**report, **changes}


_NONE_PASSED = {
    "by_scenario": {"persona_identity": {"k": 0, "n": 3}, "poison_home": {"k": 0, "n": 3}},
    "batch": {"k": 0, "n": 6, "rounds": 3},
}


def test_compare_lines_up_each_scenario_and_the_difference_range() -> None:
    other = _report(provider="anthropic", model="claude-haiku-5-5", **_NONE_PASSED)

    out = "\n".join(stats.compare_reports(_report(), other))

    assert "deepseek/deepseek-v4-flash" in out
    assert "anthropic/claude-haiku-5-5" in out  # 型號不同照常比（比較模型本來就是用途）
    assert re.search(r"persona_identity\s+3/3\s+0/3", out)
    assert re.search(r"poison_home\s+1/3\s+0/3", out)
    low, high = stats.newcombe(4, 6, 0, 6)
    assert f"[{low:+.3f}, {high:+.3f}]" in out
    assert low > 0
    assert "excludes 0" in out


def test_a_range_that_includes_zero_is_not_called_a_difference() -> None:
    out = "\n".join(stats.compare_reports(_report(), _report()))

    assert "includes 0" in out
    assert "excludes 0" not in out


@pytest.mark.parametrize("field", ["eval_code_hash", "suite", "scenarios"])
def test_reports_that_differ_in_code_suite_or_scenarios_are_not_compared(field: str) -> None:
    missing = {key: value for key, value in _report().items() if key != field}

    with pytest.raises(stats.ReportsNotComparable, match=field):
        stats.compare_reports(_report(), _report(**{field: "other"}))
    with pytest.raises(stats.ReportsNotComparable, match=field):
        stats.compare_reports(missing, _report())


def test_unfinished_rounds_and_uneven_n_are_shown() -> None:
    stopped = _report(
        rounds_completed=1, uneven_n=True,
        by_scenario={"persona_identity": {"k": 2, "n": 2}, "poison_home": {"k": 1, "n": 1}},
        batch={"k": 2, "n": 2, "rounds": 1},
    )

    out = "\n".join(stats.compare_reports(_report(), stopped))

    assert re.search(r"rounds\s+3/3\s+1/3", out)
    assert "uneven n: B (a round stopped part-way)" in out
    low, high = stats.newcombe(4, 6, 2, 2)
    assert f"[{low:+.3f}, {high:+.3f}]" in out


@pytest.mark.parametrize("field", ["repeats", "rounds_completed", "by_scenario", "batch"])
def test_a_report_missing_a_required_field_is_not_compared(field: str) -> None:
    missing = {key: value for key, value in _report().items() if key != field}

    with pytest.raises(stats.ReportsNotComparable, match=field):
        stats.compare_reports(_report(), missing)


def test_a_scenario_without_counts_or_an_empty_list_is_not_compared() -> None:
    partial = _report(by_scenario={"persona_identity": {"k": 3, "n": 3}})

    with pytest.raises(stats.ReportsNotComparable, match="by_scenario"):
        stats.compare_reports(_report(), partial)
    with pytest.raises(stats.ReportsNotComparable, match="scenarios"):
        stats.compare_reports(_report(scenarios=[]), _report(scenarios=[]))


def test_settings_that_change_the_scores_are_shown_and_flagged_when_they_differ() -> None:
    stub = {"model": "touka-eval-stub-ngram", "query_floor": 0.3, "dedup_threshold": 0.92}
    first = _report(wire_max_tokens={"per_request": 512}, embedding_stub=stub)
    second = _report(wire_max_tokens={"per_request": 1024}, embedding_stub=stub)

    out = "\n".join(stats.compare_reports(first, second))

    assert "wire_max_tokens: A=512 B=1024 (differs)" in out
    same = "touka-eval-stub-ngram/0.3/0.92"
    assert f"embedding_stub: A={same} B={same}" in out.splitlines()  # 相同就不標


def test_a_side_without_a_finished_round_has_no_range() -> None:
    empty = _report(rounds_completed=0, batch={"k": 0, "n": 0, "rounds": 0},
                    by_scenario={"persona_identity": {"k": 0, "n": 0},
                                 "poison_home": {"k": 0, "n": 0}})

    out = "\n".join(stats.compare_reports(_report(), empty))

    assert "difference A-B: n/a" in out


def test_the_report_is_found_after_the_scope_lines() -> None:
    printed = ("[HUMAN-APPROVED LIVE API SCOPE]\noperation=manual_memory_eval_probe\n"
               "limits=suite=smoke, repeats=3\nexpected_requests=2x3=6\nsandbox=x\n"
               + json.dumps(_report(), ensure_ascii=False, indent=2) + "\n")

    assert stats.load_report(printed) == _report()
    assert stats.load_report(json.dumps(_report())) == _report()
    with pytest.raises(stats.NoReportFound):
        stats.load_report("expected_requests=2x3=6\n")


def test_main_compares_two_saved_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("sandbox=x\n" + json.dumps(_report(), indent=2), encoding="utf-8")
    # Windows PowerShell 5 的 > 存成 UTF-16
    second.write_text(json.dumps(_report(**_NONE_PASSED)), encoding="utf-16")

    assert stats.main([str(first), str(second)]) == 0
    assert "difference A-B" in capsys.readouterr().out


def test_main_refuses_reports_that_cannot_be_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text(json.dumps(_report()), encoding="utf-8")
    second.write_text(json.dumps(_report(eval_code_hash="b" * 64)), encoding="utf-8")

    assert stats.main([str(first), str(second)]) == 1
    captured = capsys.readouterr()
    assert "eval_code_hash" in captured.err
    assert not captured.out
    assert stats.main([str(first)]) == 2  # 只給一份
    assert stats.main([str(first), str(tmp_path / "missing.txt")]) == 2  # 讀不到檔
    (tmp_path / "ansi.txt").write_bytes("報告".encode("cp950"))
    assert stats.main([str(first), str(tmp_path / "ansi.txt")]) == 2  # 不是 UTF-8／UTF-16
