"""Offline (replay) tests for the tool-call eval harness scaffold.

Runs green in CI with no API keys. LIVE mode against the real proxy is a
documented seam (skipped here) — see README.md.
"""

from __future__ import annotations

import pytest

from . import harness, metrics
from .cases import SEED_CASES, EvalCase

# Small explicit known-set keeps the metric assertions independent of whether the
# real proxy package can be imported in this environment.
_KNOWN = frozenset(
    {
        "update_my_soul", "mission_tools", "research", "project_tools",
        "vault_tools", "bash", "list", "write",
        # P2b：長指令監控案的三工具皆為真實工具（非幻覺）
        "execute_command", "get_command_result", "stop_command",
    }
)


def test_all_seed_cases_have_recordings():
    assert all(
        c.recorded_tool_calls is not None or c.recorded_turns is not None
        for c in SEED_CASES
    )


def test_fallback_covers_registry():
    assert "research" in harness._FALLBACK_BUILTIN_TOOLS
    assert len(harness._FALLBACK_BUILTIN_TOOLS) >= 20


def test_known_builtin_tool_names_returns_frozenset():
    known = harness.known_builtin_tool_names()
    assert isinstance(known, frozenset)
    assert "research" in known
    assert len(known) >= 20


def test_offline_suite_metrics():
    report = harness.run_suite(SEED_CASES, mode="offline", known=_KNOWN)
    assert report.n_cases == 12  # 9 ＋ 2 relevance ＋ long-run-monitor（P2b 轉正）
    assert report.total_calls == 13  # +3（監控案：execute_command/get_command_result/stop_command）
    # 8 個 selection 案例中 7 個 ok（relevance 案非 selection、不計入分母；監控案達標）
    assert report.tool_selection_accuracy == pytest.approx(7 / 8)
    assert report.wrong_tool_rate == pytest.approx(1 / 13)
    assert report.hallucinated_tool_rate == pytest.approx(1 / 13)
    assert report.invalid_param_rate == pytest.approx(1 / 13)
    assert report.tool_error_rate == pytest.approx(1 / 13)
    assert report.avg_tool_calls_per_case == pytest.approx(13 / 12)
    assert report.policy_denial_accuracy == pytest.approx(1.0)
    # 達標案例：5 單回合（t=1）＋explore-then-write（t=2）＋監控案（t=1，首回合即選到）→ 8/7
    assert report.avg_turns_to_success == pytest.approx(8 / 7)
    # 2 個 relevance 案錄的都是零呼叫 → 克制率 100%
    assert report.abstention_accuracy == pytest.approx(1.0)


def test_hallucination_flagged():
    case = next(c for c in SEED_CASES if c.id == "hallucinated-dream-tool")
    result = metrics.score_case(case, harness.run_case(case), _KNOWN)
    assert result.hallucinated_calls == 1


def test_policy_denial_scored():
    case = next(c for c in SEED_CASES if c.id == "bash-denied-base")
    result = metrics.score_case(case, harness.run_case(case), _KNOWN)
    assert result.denial_ok is True


def test_wrong_tool_is_not_hallucination():
    case = next(c for c in SEED_CASES if c.id == "wrong-tool-read-file")
    result = metrics.score_case(case, harness.run_case(case), _KNOWN)
    assert result.wrong_tool_calls == 1
    assert result.hallucinated_calls == 0  # bash is a real tool, just the wrong one


def test_format_report_is_text():
    report = harness.run_suite(SEED_CASES, known=_KNOWN)
    text = metrics.format_report(report)
    assert "tool_selection_accuracy" in text


def test_offline_requires_recording():
    empty = EvalCase(id="x", title="x", prompt="x")
    with pytest.raises(ValueError, match="recorded_tool_calls"):
        harness.run_case(empty)


def test_live_mode_requires_explicit_model():
    """LIVE seam 已接通（ADR-0136）：未帶 live_model＝明確設定錯誤（非 NotImplementedError）。

    真實 LIVE 跑仍為擁有者手動（帶金鑰＋顯式 live_model）；接線本體的離線測試見
    test_live_episode.py。
    """
    with pytest.raises(RuntimeError, match="live_model"):
        harness.run_suite(SEED_CASES, mode="live")
