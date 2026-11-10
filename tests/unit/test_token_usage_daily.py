"""每天的用量與近 7 天的快取命中率（ADR-0311）。

``token_usage.json`` 每個用途底下多一個 ``daily``，鍵是 UTC 的日序號，保留 14 天。累計數字與
成本照舊；``daily`` 壞掉也不能擋住累計，否則預算上限看不到花費。
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.services import telemetry
from src.services.resource_manager import resource_manager
from src.services.telemetry import accumulate_token_usage, cache_hit_rates

_TODAY = 20_402
_SECONDS_PER_DAY = 86_400
_USAGE = {
    "prompt_tokens": 10, "completion_tokens": 5,
    "cache_read_input_tokens": 30, "cache_creation_input_tokens": 2,
}
_names = count()


def _on_day(monkeypatch: pytest.MonkeyPatch, day: int) -> None:
    monkeypatch.setattr(telemetry, "get_current_epoch", lambda: day * _SECONDS_PER_DAY + 3_600)


@pytest.fixture
def ident(monkeypatch: pytest.MonkeyPatch) -> AgentIdentity:
    _on_day(monkeypatch, _TODAY)
    return AgentIdentity.for_role(agent_id=f"DailyUsage{next(_names)}", role="Owner")


def _path(ident: AgentIdentity) -> Path:
    return Path(resource_manager.get_stats_path(ident)) / "token_usage.json"


def _read(ident: AgentIdentity) -> dict[str, Any]:
    return resource_manager.read_json(_path(ident))


def _write(ident: AgentIdentity, data: dict[str, Any]) -> None:
    resource_manager.make_dir(_path(ident).parent)
    resource_manager.write_json(_path(ident), data, indent=2)


def _old_file(daily: object) -> dict[str, Any]:
    return {"ProvX": {
        "prompt_tokens": 100, "completion_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "total_cost_usd": 1.5, "daily": daily,
    }}


def _bucket(prompt: int = 0, read: int = 0, write: int = 0, completion: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
    }


def test_a_new_day_gets_its_own_bucket_with_all_four_counts(ident: AgentIdentity) -> None:
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="claude-opus-5-5")
    assert _read(ident)["ProvX"]["daily"] == {str(_TODAY): _USAGE}


def test_the_same_day_adds_up(ident: AgentIdentity) -> None:
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="claude-opus-5-5")
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="claude-opus-5-5")
    assert _read(ident)["ProvX"]["daily"] == {str(_TODAY): {k: v * 2 for k, v in _USAGE.items()}}


def test_the_next_day_starts_a_new_bucket_and_totals_keep_growing(
    ident: AgentIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="claude-opus-5-5")
    _on_day(monkeypatch, _TODAY + 1)
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="claude-opus-5-5")
    stats = _read(ident)["ProvX"]
    assert stats["daily"] == {str(_TODAY): _USAGE, str(_TODAY + 1): _USAGE}
    assert stats["prompt_tokens"] == 20


def test_each_label_keeps_its_own_days(ident: AgentIdentity) -> None:
    accumulate_token_usage(ident, "Anthropic", dict(_USAGE), model="claude-opus-5-5")
    accumulate_token_usage(ident, "summarizer", dict(_USAGE), model="claude-opus-5-5")
    data = _read(ident)
    assert data["Anthropic"]["daily"] == {str(_TODAY): _USAGE}
    assert data["summarizer"]["daily"] == {str(_TODAY): _USAGE}


def test_old_key_names_land_in_the_same_buckets(ident: AgentIdentity) -> None:
    accumulate_token_usage(ident, "ProvX", {"input_tokens": 7, "output_tokens": 3}, model="m")
    assert _read(ident)["ProvX"]["daily"] == {str(_TODAY): _bucket(prompt=7, completion=3)}


def test_buckets_older_than_14_days_are_dropped(ident: AgentIdentity) -> None:
    _write(ident, _old_file({
        str(_TODAY - 14): _bucket(prompt=1), str(_TODAY - 13): _bucket(prompt=2),
    }))
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="m")
    assert _read(ident)["ProvX"]["daily"] == {
        str(_TODAY - 13): _bucket(prompt=2), str(_TODAY): _USAGE,
    }


def test_a_file_from_before_this_feature_keeps_its_totals(ident: AgentIdentity) -> None:
    old = _old_file(None)
    del old["ProvX"]["daily"]
    old["ProvX"]["e_index_history"] = [1, 2]
    _write(ident, old)
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="m")
    stats = _read(ident)["ProvX"]
    assert stats["prompt_tokens"] == 110
    assert stats["total_cost_usd"] > 1.5
    assert stats["e_index_history"] == [1, 2]
    assert stats["daily"] == {str(_TODAY): _USAGE}


@pytest.mark.parametrize(
    "bad",
    ["x", ["a"], 7, None, {"abc": _bucket(prompt=1)}, {"9" * 5_000: _bucket(prompt=1)},
     {str(_TODAY): "x"}, {str(_TODAY + 1): _bucket(prompt=1)}],
    ids=["text", "list", "number", "null", "bad-day", "huge-day", "bad-bucket", "future-day"],
)
def test_a_broken_daily_never_blocks_the_cost_counter(ident: AgentIdentity, bad: object) -> None:
    _write(ident, _old_file(bad))
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="m")
    stats = _read(ident)["ProvX"]
    assert stats["prompt_tokens"] == 110
    assert stats["total_cost_usd"] > 1.5
    assert stats["daily"] == {str(_TODAY): _USAGE}


@pytest.mark.parametrize("value", ["x", None, True, 1.5], ids=["text", "null", "bool", "float"])
def test_a_broken_number_in_todays_bucket_counts_as_zero(
    ident: AgentIdentity, value: object
) -> None:
    _write(ident, _old_file({str(_TODAY): {**_bucket(read=4), "prompt_tokens": value}}))
    accumulate_token_usage(ident, "ProvX", dict(_USAGE), model="m")
    assert _read(ident)["ProvX"]["daily"][str(_TODAY)] == {**_USAGE, "cache_read_input_tokens": 34}


# ---- 命中率 ----


def _rates(monkeypatch: pytest.MonkeyPatch, usage: object) -> object:
    _on_day(monkeypatch, _TODAY)
    return cache_hit_rates(usage)


def test_rate_is_cache_reads_over_all_input_and_ignores_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = _bucket(prompt=10, read=30, write=10, completion=999)
    usage = {"Anthropic": {"daily": {str(_TODAY): bucket}}}
    assert _rates(monkeypatch, usage) == {"Anthropic": 0.6}


def test_rate_keeps_four_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = {"P": {"daily": {str(_TODAY): _bucket(prompt=2, read=1)}}}
    assert _rates(monkeypatch, usage) == {"P": 0.3333}


def test_window_is_today_and_the_six_days_before(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = {"P": {"daily": {
        str(_TODAY): _bucket(read=1),
        str(_TODAY - 6): _bucket(read=1),
        str(_TODAY - 7): _bucket(prompt=1000),
        str(_TODAY + 1): _bucket(prompt=1000),
    }}}
    assert _rates(monkeypatch, usage) == {"P": 1.0}


@pytest.mark.parametrize(
    "stats",
    [{"prompt_tokens": 50}, {"daily": {}}, {"daily": "x"},
     {"daily": {str(_TODAY): _bucket(completion=9)}},
     {"daily": {str(_TODAY - 7): _bucket(read=9)}},
     {"daily": {"abc": _bucket(read=9)}},
     {"daily": {str(_TODAY): "x"}}],
    ids=["no-daily", "empty", "not-a-dict", "output-only", "too-old", "bad-day", "bad-bucket"],
)
def test_no_input_in_the_window_is_none(
    monkeypatch: pytest.MonkeyPatch, stats: dict[str, Any]
) -> None:
    assert _rates(monkeypatch, {"P": stats}) == {"P": None}


def test_a_broken_number_counts_as_zero_in_the_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = {**_bucket(read=3, prompt=1), "cache_creation_input_tokens": "x"}
    usage = {"P": {"daily": {str(_TODAY): bucket}}}
    assert _rates(monkeypatch, usage) == {"P": 0.75}


def test_entries_that_are_not_usage_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _rates(monkeypatch, {"total_tokens": 5, "P": {"daily": {}}}) == {"P": None}


@pytest.mark.parametrize("usage", [None, "x", [1]], ids=["null", "text", "list"])
def test_a_file_that_is_not_an_object_has_no_rates(
    monkeypatch: pytest.MonkeyPatch, usage: object
) -> None:
    assert _rates(monkeypatch, usage) == {}
