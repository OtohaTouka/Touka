"""P0-A：pass^k 可靠度指標（τ-bench 式）——TDD.

pass^k＝跨 k 次獨立試驗「全部成功」的機率（可靠度，比平均選對率嚴格；多腦切換
最有意義）。單案例＝C(c,k)/C(n,k)（c＝成功試驗數、n＝總試驗數），聚合＝各案例平均。
"""

from __future__ import annotations

import pytest

from tests.live_api_policy import LiveApiCallBudget

from . import live_select_only as lso
from . import metrics
from .cases import EvalCase, ToolCall


def test_pass_hat_k_all_success():
    assert metrics.pass_hat_k(3, 3) == {1: 1.0, 2: 1.0, 3: 1.0}


def test_pass_hat_k_partial():
    r = metrics.pass_hat_k(2, 3)
    assert r[1] == pytest.approx(2 / 3)
    assert r[2] == pytest.approx(1 / 3)
    assert r[3] == 0.0


def test_pass_hat_k_zero_success():
    assert metrics.pass_hat_k(0, 3) == {1: 0.0, 2: 0.0, 3: 0.0}


def test_pass_hat_k_single_trial():
    assert metrics.pass_hat_k(1, 1) == {1: 1.0}
    assert metrics.pass_hat_k(0, 1) == {1: 0.0}


def test_mean_pass_hat_k_averages_cases():
    # case A: 3/3, case B: 2/3 → pass^1=(1+2/3)/2=5/6；pass^2=(1+1/3)/2=2/3；pass^3=(1+0)/2=0.5
    r = metrics.mean_pass_hat_k([3, 2], 3)
    assert r[1] == pytest.approx(5 / 6)
    assert r[2] == pytest.approx(2 / 3)
    assert r[3] == pytest.approx(0.5)


def test_mean_pass_hat_k_empty():
    assert metrics.mean_pass_hat_k([], 3) == {}


def test_report_reliability_render():
    result = metrics.score_episode(
        EvalCase(id="x", title="x", prompt="x", expected_tools=["read"]),
        [[ToolCall("read", {"scope": "project", "path": "a"})]],
        frozenset({"read"}),
    )
    report = metrics.aggregate([result], reliability={1: 0.83, 2: 0.5})
    assert report.reliability == {1: 0.83, 2: 0.5}
    text = metrics.format_report(report)
    assert "pass^1" in text
    assert "pass^2" in text


def test_aggregate_reliability_defaults_none():
    result = metrics.score_episode(
        EvalCase(id="x", title="x", prompt="x", expected_tools=["read"]),
        [[ToolCall("read", {"scope": "project", "path": "a"})]],
        frozenset({"read"}),
    )
    report = metrics.aggregate([result])
    assert report.reliability is None
    assert "pass^" not in metrics.format_report(report)


def test_record_baseline_multisample_reports_reliability(monkeypatch):
    """samples>1 單回合模式：逐試分開評分，回傳 pass^k reliability。"""
    case_hit = EvalCase(
        id="hit", title="", prompt="", context={"role": "Owner"}, expected_tools=["read"]
    )
    case_miss = EvalCase(
        id="miss", title="", prompt="", context={"role": "Owner"}, expected_tools=["read"]
    )

    async def _fake_assemble(_identity):
        return []

    def _fake_select(_url, _key, _model, case, _tools, **_kwargs):
        # hit：每試都選對；miss：每試都選錯（bash 是真工具但非 expected）
        if case.id == "hit":
            return [ToolCall("read", {"scope": "project", "path": "a"})]
        return [ToolCall("bash", {"command": "x"})]

    monkeypatch.setattr(lso, "resolve_chat_endpoint", lambda _p: ("u", "k"))
    monkeypatch.setattr(lso, "assemble_advertised_tools", _fake_assemble)
    monkeypatch.setattr(lso, "select_tools_for_case", _fake_select)

    payload = lso.record_baseline(
        "m",
        provider_id="deepseek",
        samples=3,
        cases=[case_hit, case_miss],
        max_tokens=64,
        request_budget=LiveApiCallBudget(max_requests=6),
    )
    rel = payload["report"]["reliability"]
    # hit 3/3→pass^1=1；miss 0/3→pass^1=0；mean pass^1=0.5
    assert rel[1] == pytest.approx(0.5)
    assert rel[3] == pytest.approx(0.5)  # hit 全對→pass^3=1、miss→0，平均 0.5


def test_record_baseline_single_sample_has_no_reliability(monkeypatch):
    """samples=1：維持原樣、無 reliability（單樣本路徑逐位不變）。"""
    case = EvalCase(id="c", title="", prompt="", context={"role": "Owner"}, expected_tools=["read"])

    async def _fake_assemble(_identity):
        return []

    monkeypatch.setattr(lso, "resolve_chat_endpoint", lambda _p: ("u", "k"))
    monkeypatch.setattr(lso, "assemble_advertised_tools", _fake_assemble)
    monkeypatch.setattr(
        lso,
        "select_tools_for_case",
        lambda *_a, **_k: [ToolCall("read", {"scope": "project", "path": "a"})],
    )

    payload = lso.record_baseline(
        "m",
        provider_id="deepseek",
        samples=1,
        cases=[case],
        max_tokens=64,
        request_budget=LiveApiCallBudget(max_requests=1),
    )
    assert payload["report"]["reliability"] is None
