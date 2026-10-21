"""Phase 3 前置（互審設計文件 §7.1-B）：episode 級 gate 指標——TDD.

互審要降的是 **artifact 內容／宣稱型幻覺**——與既有 `hallucinated_tool_rate`
（只測工具名捏造）／`selection_ok`（只測選對工具）**不同軸**。本模組新增：
- `EvalCase` 注錯欄位：`injected_failure`／`expected_artifact_check`／`ground_truth`；
- episode 級 gate 指標：`gate_recall`（有注錯者被擋比例）／`gate_precision`
  （擋下者真的有錯比例）／`false_block_rate`（誤擋乾淨案比例）；
- 明文廢用註記：舊指標不可作為降幻覺主指標（設計文件 §6.2-7）。
全部純函式、離線、CI-safe；不動既有 aggregate／baseline。
"""

from __future__ import annotations

from . import metrics
from .cases import EvalCase
from .metrics import (
    GateOutcome,
    aggregate_gate,
    false_block_rate,
    format_gate_report,
    gate_precision,
    gate_recall,
)

# --------------------------------------------------------------------------- #
# 1. EvalCase 注錯欄位（additive、預設不影響既有案例）
# --------------------------------------------------------------------------- #


def test_evalcase_new_fields_default():
    case = EvalCase(id="x", title="t", prompt="p")
    assert case.injected_failure == "none"
    assert case.ground_truth == ""
    assert case.expected_artifact_check == {}


def test_evalcase_injected_failure_carries_kind():
    case = EvalCase(
        id="x",
        title="t",
        prompt="p",
        injected_failure="fabrication",
        expected_artifact_check={"must_exist": ["out.md"]},
        ground_truth="正確完成＝out.md 存在且內容含摘要",
    )
    assert case.injected_failure == "fabrication"
    assert case.expected_artifact_check["must_exist"] == ["out.md"]


# --------------------------------------------------------------------------- #
# 2. gate 指標純函式
# --------------------------------------------------------------------------- #

_OUTCOMES = [
    GateOutcome(case_id="a", injected_failure="fabrication", blocked=True),   # 真陽
    GateOutcome(case_id="b", injected_failure="goal_drift", blocked=False),   # 漏放
    GateOutcome(case_id="c", injected_failure="none", blocked=True),          # 誤擋
    GateOutcome(case_id="d", injected_failure="none", blocked=False),         # 真陰
]


def test_gate_recall_counts_injected_blocked():
    assert gate_recall(_OUTCOMES) == 0.5  # 2 注錯、1 被擋


def test_gate_precision_counts_blocked_truly_bad():
    assert gate_precision(_OUTCOMES) == 0.5  # 2 被擋、1 真的有錯


def test_false_block_rate_counts_clean_blocked():
    assert false_block_rate(_OUTCOMES) == 0.5  # 2 乾淨、1 被誤擋


def test_empty_denominators_return_none():
    clean_only = [GateOutcome(case_id="d", injected_failure="none", blocked=False)]
    assert gate_recall(clean_only) is None  # 無注錯案
    assert gate_precision(clean_only) is None  # 無被擋案
    injected_only = [
        GateOutcome(case_id="a", injected_failure="fabrication", blocked=True)
    ]
    assert false_block_rate(injected_only) is None  # 無乾淨案


def test_aggregate_gate_report_and_format():
    report = aggregate_gate(_OUTCOMES)
    assert report.total == 4
    assert report.injected == 2
    assert report.blocked == 2
    assert report.gate_recall == 0.5
    assert report.gate_precision == 0.5
    assert report.false_block_rate == 0.5

    text = format_gate_report(report)
    assert "gate_recall" in text
    assert "false_block_rate" in text


# --------------------------------------------------------------------------- #
# 3. 廢用註記（設計文件 §6.2-7：舊指標與 artifact 內容幻覺不同軸）
# --------------------------------------------------------------------------- #


def test_hallucination_axis_note_pinned():
    note = metrics.HALLUCINATION_AXIS_NOTE
    assert "hallucinated_tool_rate" in note
    assert "selection_ok" in note
    assert "artifact" in note
    assert "gate_recall" in note
