"""完整 ruff 只減不增（ADR-0278）。

CI 本來就跑 pytest，所以這支測試就是完整 ruff 在 CI 上的關卡。每條規則的數量必須和
`tests/lint/ruff_baseline.json` 完全相等：變多要修，變少要在同一個 PR 重拍基準，
改動才會出現在差異裡。範圍和 CI 的 `ruff check --select F` 相同，是四個產品資料夾。
"""

from collections import Counter

from lint_ratchet import (  # scripts/ 由 tests/conftest.py 加進 sys.path
    PRODUCT_SOURCE_ROOTS,
    RUFF_BASELINE,
    collect_ruff,
    compare,
    load_baseline,
)


def test_ruff_violations_match_the_baseline() -> None:
    problems = compare(collect_ruff(), load_baseline(RUFF_BASELINE))
    assert not problems, "\n".join(
        [*problems, "修好或確認後跑：python scripts/lint_ratchet.py ruff --update"]
    )


def test_ruff_ratchet_scans_the_same_roots_as_the_ci_pyflakes_step() -> None:
    assert PRODUCT_SOURCE_ROOTS == (
        "ToukaProxy/src",
        "ToukaMemory/src",
        "ToukaHeartbeat",
        "ToukaDispatch",
    )


def test_compare_requires_exact_counts() -> None:
    baseline = {"E501": 3, "ERA001": 1}
    assert compare(Counter({"E501": 3, "ERA001": 1}), baseline) == []

    more = compare(Counter({"E501": 4, "ERA001": 1}), baseline)
    assert len(more) == 1 and "E501" in more[0] and "變多" in more[0]

    fewer = compare(Counter({"E501": 3}), baseline)
    assert len(fewer) == 1 and "ERA001" in fewer[0] and "變少" in fewer[0]

    new_rule = compare(Counter({"E501": 3, "ERA001": 1, "B904": 1}), baseline)
    assert len(new_rule) == 1 and "B904" in new_rule[0] and "變多" in new_rule[0]
