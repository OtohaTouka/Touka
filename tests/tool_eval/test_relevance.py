"""P0-B：relevance「該不該叫工具」（BFCL irrelevance 維度）——TDD.

有些 prompt 的正解是「**不叫任何工具**」（純知識/閒聊/無對應能力）——叫了＝過度
調用。`expect_no_tool` 案例：正解＝零呼叫；`abstention_ok` 記是否克制、`abstention_accuracy`
聚合。與 selection（ANY-OF）互斥：這類案無 expected_tools。
"""

from __future__ import annotations

import pytest

from . import metrics
from .cases import SEED_CASES, EvalCase, ToolCall


def _abstain_case(**overrides) -> EvalCase:
    base = {"id": "t", "title": "t", "prompt": "p", "expect_no_tool": True}
    base.update(overrides)
    return EvalCase(**base)


def test_abstention_ok_when_no_calls():
    r = metrics.score_case(_abstain_case(), [], frozenset())
    assert r.abstention_ok is True
    assert r.evaluated_selection is False  # 非 selection 案


def test_abstention_fail_on_spurious_call():
    r = metrics.score_case(
        _abstain_case(), [ToolCall("research", {"query": "x"})], frozenset({"research"})
    )
    assert r.abstention_ok is False


def test_non_abstention_case_has_none():
    r = metrics.score_case(
        EvalCase(id="x", title="x", prompt="p", expected_tools=["read"]),
        [ToolCall("read", {"scope": "project", "path": "a"})],
        frozenset({"read"}),
    )
    assert r.abstention_ok is None


def test_aggregate_abstention_accuracy():
    ok = metrics.score_case(_abstain_case(id="a"), [], frozenset())
    bad = metrics.score_case(
        _abstain_case(id="b"), [ToolCall("research", {"query": "x"})], frozenset({"research"})
    )
    rep = metrics.aggregate([ok, bad])
    assert rep.abstention_accuracy == pytest.approx(0.5)
    assert "abstention_accuracy" in metrics.format_report(rep)


def test_no_abstention_cases_reports_none():
    r = metrics.score_case(
        EvalCase(id="x", title="x", prompt="p", expected_tools=["read"]),
        [ToolCall("read", {"scope": "project", "path": "a"})],
        frozenset({"read"}),
    )
    rep = metrics.aggregate([r])
    assert rep.abstention_accuracy is None
    assert "abstention_accuracy" not in metrics.format_report(rep)


def test_seed_has_relevance_cases():
    rel = [c for c in SEED_CASES if c.expect_no_tool]
    assert len(rel) >= 2
    assert all(c.recorded_tool_calls == [] for c in rel)  # 正解＝零呼叫
    assert all(not c.expected_tools for c in rel)  # 無 expected（非 selection 案）
