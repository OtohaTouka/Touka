"""人工實測的送前最壞花費與報告去敏（純函式，離線）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.memory_eval import live_bounds as lb
from tests.memory_eval.scenarios import CANARY, canary_fragments

_USAGE = {"prompt_tokens": 1_200, "cache_read_input_tokens": 300,
          "cache_creation_input_tokens": 40, "completion_tokens": 900}


def _ledger(budget: float) -> lb.SpendLedger:
    return lb.SpendLedger(budget_usd=budget, input_price=1.0, output_price=10.0)


def test_input_estimate_counts_every_character_plus_a_vendor_margin() -> None:
    request = {"messages": [{"role": "user", "content": "我的貓叫麻糬"}]}

    estimate = lb.estimate_input_tokens(request)

    assert estimate == len(json.dumps(request, ensure_ascii=False)) + lb.VENDOR_OVERHEAD_TOKENS
    assert estimate - lb.VENDOR_OVERHEAD_TOKENS > len("我的貓叫麻糬")  # 中文每字至少 1 token


def test_input_estimate_includes_the_tool_schema() -> None:
    bare = {"messages": [{"role": "user", "content": "嗨"}]}
    tools = [{"name": "memory_tools", "description": "查詢或儲存記憶" * 200,
              "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}}}]

    grown = lb.estimate_input_tokens({**bare, "tools": tools}) - lb.estimate_input_tokens(bare)

    assert grown >= len(json.dumps(tools, ensure_ascii=False))


def test_input_estimate_survives_values_json_cannot_encode() -> None:
    assert lb.estimate_input_tokens({"blob": object()}) > lb.VENDOR_OVERHEAD_TOKENS


def test_worst_case_fills_the_whole_output_cap() -> None:
    worst = lb.worst_case_usd(input_tokens=30_000, max_output_tokens=8_192,
                              input_price=0.625, output_price=2.5)

    assert worst == pytest.approx((30_000 * 0.625 + 8_192 * 2.5) / 1_000_000)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (SimpleNamespace(input=0.5, output=2.5, cache_write=0.625), 0.625),
        (SimpleNamespace(input=1.0, output=5.0, cache_write=None), 1.0),
        (SimpleNamespace(input=1.0, output=5.0, cache_write=0.3), 1.0),
    ],
)
def test_input_price_is_the_dearer_of_input_and_cache_write(rate: object, expected: float) -> None:
    assert lb.worst_case_input_price(rate) == expected


def test_ledger_refuses_once_spent_plus_worst_case_passes_the_budget() -> None:
    ledger = _ledger(0.0)
    worst = lb.worst_case_usd(input_tokens=lb.estimate_input_tokens({"m": "x"}),
                              max_output_tokens=1_000, input_price=1.0, output_price=10.0)
    ledger.budget_usd = worst * 2.5

    ledger.reserve({"m": "x"}, 1_000)
    ledger.reserve({"m": "x"}, 1_000)
    with pytest.raises(lb.WorstCaseOverBudget):
        ledger.reserve({"m": "x"}, 1_000)

    assert len(ledger.records) == 2
    assert ledger.spent_usd == pytest.approx(worst * 2)
    assert "budget" in ledger.refused


def test_a_request_that_exactly_fits_the_budget_is_sent() -> None:
    ledger = _ledger(0.0)
    ledger.budget_usd = lb.worst_case_usd(input_tokens=lb.estimate_input_tokens({}),
                                          max_output_tokens=10, input_price=1.0, output_price=10.0)

    ledger.reserve({}, 10)

    assert not ledger.refused


def test_reported_usage_replaces_the_worst_case_with_the_actual_cost() -> None:
    ledger = _ledger(1.0)
    ledger.reserve({"m": "x"}, 1_000)

    ledger.settle(_USAGE, cost_usd=0.0021)

    assert ledger.spent_usd == pytest.approx(0.0021)
    assert ledger.records[0].usage == _USAGE


def test_missing_usage_keeps_the_worst_case() -> None:
    ledger = _ledger(1.0)
    record = ledger.reserve({"m": "x"}, 1_000)

    ledger.settle({}, cost_usd=0.0)

    assert record.cost_usd is None
    assert ledger.spent_usd == pytest.approx(record.worst_case_usd)


def test_report_lists_every_request_with_cache_and_thinking_tokens() -> None:
    ledger = _ledger(1.0)
    ledger.reserve({"m": "x"}, 8_192)
    ledger.settle(_USAGE, cost_usd=0.0021)
    ledger.reserve({"m": "y"}, 8_192)

    report = ledger.to_dict()

    first, second = report["requests"]
    assert first["input_tokens"] == 1_200
    assert first["cache_read_tokens"] == 300
    assert first["cache_write_tokens"] == 40
    assert first["output_tokens_incl_thinking"] == 900
    assert first["max_output_tokens"] == 8_192
    assert first["cost_usd"] == pytest.approx(0.0021)
    assert second["cost_usd"] is None and second["worst_case_usd"] > 0
    assert report["usage_totals"]["output_tokens_incl_thinking"] == 900
    assert report["ledger_spent_usd"] == pytest.approx(0.0021 + second["worst_case_usd"])


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        (f"口令是{CANARY}", CANARY),
        (f"口令是{CANARY.lower()}", CANARY.lower()),
        (f"口令中間是{canary_fragments(CANARY)[1]}", canary_fragments(CANARY)[1]),
        ("金鑰sk-ant-api03-abcdefghijklmnop", "sk-ant-api03-abcdefghijklmnop"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("api_key=AIzaSyA1234567890abcdefghijk", "AIzaSyA1234567890abcdefghijk"),
        ("token: 0123456789abcdef0123456789abcdef99", "0123456789abcdef0123456789abcdef99"),
        ("檔案在C:\\Users\\someone\\notes.txt裡", "Users\\someone"),
        ("見 E:/work/notes.txt。", "work/notes.txt"),
        ("\\\\server\\share\\x", "\\\\server\\share"),
    ],
)
def test_excerpt_masks_canary_keys_and_windows_paths(raw: str, hidden: str) -> None:
    excerpt = lb.redact_excerpt(raw, CANARY)

    assert hidden not in excerpt
    assert "[REDACTED" in excerpt


def test_excerpt_masks_before_cutting_to_200_chars() -> None:
    excerpt = lb.redact_excerpt("好" * 195 + CANARY, CANARY)

    assert len(excerpt) == lb.EXCERPT_CHARS + 1
    assert excerpt.startswith("好" * 195)
    assert excerpt.endswith("…")
    assert CANARY[:5] not in excerpt


def test_short_plain_text_is_kept() -> None:
    assert lb.redact_excerpt("我是澪。今天下雨。", CANARY) == "我是澪。今天下雨。"


def test_excerpt_masks_a_spaced_out_canary() -> None:
    """判定把拆開寫的口令算成洩漏，報告也要遮拆開寫的."""
    first, second, _third = canary_fragments(CANARY)
    excerpt = lb.redact_excerpt(f"{' '.join(first)} 和 {'-'.join(second.lower())}", CANARY)
    assert first not in excerpt.replace(" ", "")
    assert second.lower() not in excerpt.replace("-", "")


def test_excerpt_masks_this_runs_canary() -> None:
    other = "BCDFG-HJKLM-NPQRS"
    assert other not in lb.redact_excerpt(f"口令是{other}", other)
    assert "HJKLM" not in lb.redact_excerpt("中間那段是 hjklm", other)
