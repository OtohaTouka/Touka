"""Legacy and memory times: seconds become milliseconds; the model sees Taipei time with its zone.

Pure functions only (no I/O, no clock). The Taipei rendering must match the ``<continuity>``
block (Proxy ``format_epoch_to_taipei``) for the same instant, so every time the model sees
is in one zone (ADR-0258).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from memory_server.utils import legacy_time
from src.utils.time_utils import format_epoch_to_taipei

# 2026-05-13 00:00 Asia/Taipei == 2026-05-12 16:00 UTC
DAY_START_S = 1_778_601_600
# 2026-05-13 07:30 Asia/Taipei == 2026-05-12 23:30 UTC (the UTC date is one day earlier)
MORNING_S = 1_778_628_600


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (MORNING_S, MORNING_S * 1000),  # seconds
        (MORNING_S * 1000, MORNING_S * 1000),  # milliseconds
        (MORNING_S + 0.5, MORNING_S * 1000 + 500),  # float seconds
        (float(MORNING_S * 1000), MORNING_S * 1000),  # float milliseconds
        (99_999_999_999, 99_999_999_999_000),  # just below the cutoff: seconds
        (100_000_000_000, 100_000_000_000),  # the cutoff itself: milliseconds
        (str(MORNING_S), MORNING_S * 1000),  # numeric text
        (f" {MORNING_S * 1000} ", MORNING_S * 1000),
        ("2026-05-13T07:30:00+08:00", MORNING_S * 1000),  # ISO 8601 with an offset
        ("2026-05-12T23:30:00Z", MORNING_S * 1000),
        ("2026-05-13T07:30:00", None),  # no offset: the zone is unknown
        ("not a time", None),
        ("", None),
        (None, None),
        (True, None),
        (False, None),
        (0, None),
        (-5, None),
        (math.nan, None),
        (math.inf, None),
        ([MORNING_S], None),
        ({"timestamp": MORNING_S}, None),
    ],
)
def test_to_epoch_ms_table(value: Any, expected: int | None) -> None:
    assert legacy_time.to_epoch_ms(value) == expected


@pytest.mark.parametrize(
    ("value", "with_time", "expected"),
    [
        (MORNING_S, True, "2026-05-13 07:30 Asia/Taipei"),  # seconds row, not 1970
        (MORNING_S * 1000, True, "2026-05-13 07:30 Asia/Taipei"),  # milliseconds row
        (MORNING_S, False, "2026-05-13 Asia/Taipei"),  # the UTC date would be 05-12
        (DAY_START_S - 1, True, "2026-05-12 23:59 Asia/Taipei"),  # last second of the day
        (DAY_START_S, True, "2026-05-13 00:00 Asia/Taipei"),  # first second of the next
        (1_700_000_000_000, True, "2023-11-15 06:13 Asia/Taipei"),
        ("2026-05-12T23:30:00Z", True, "2026-05-13 07:30 Asia/Taipei"),
    ],
)
def test_format_taipei_table(value: Any, with_time: bool, expected: str) -> None:
    assert legacy_time.format_taipei(value, with_time=with_time) == expected


@pytest.mark.parametrize("value", [None, "", "garbage", 0, -1, True, 10**20, math.nan])
def test_format_taipei_unknown_uses_caller_label(value: Any) -> None:
    assert legacy_time.format_taipei(value, unknown="未知時間") == "未知時間"


@pytest.mark.parametrize(
    "epoch_s", [MORNING_S, DAY_START_S - 1, DAY_START_S, DAY_START_S + 86_399, 1_700_000_000]
)
def test_format_taipei_matches_continuity_zone(epoch_s: int) -> None:
    """Same instant, same wall-clock text as the continuity block (F3)."""
    continuity_text, _weekday = format_epoch_to_taipei(epoch_s)
    for value in (epoch_s, epoch_s * 1000):
        rendered = legacy_time.format_taipei(value)
        assert rendered == f"{continuity_text[:16]} {legacy_time.TAIPEI_TZ_NAME}"


@pytest.mark.parametrize(
    ("date_text", "expected"),
    [
        ("2026-05-13", DAY_START_S),
        ("2026-01-01", 1_767_196_800),  # 2025-12-31 16:00 UTC
    ],
)
def test_taipei_day_start(date_text: str, expected: int) -> None:
    assert legacy_time.taipei_day_start(date_text) == expected


@pytest.mark.parametrize("date_text", ["2026/05/13", "2026-13-01", "", "13-05-2026"])
def test_taipei_day_start_rejects_bad_dates(date_text: str) -> None:
    with pytest.raises(ValueError):
        legacy_time.taipei_day_start(date_text)


@pytest.mark.parametrize(
    ("start_s", "end_s", "expected"),
    [
        (
            DAY_START_S,
            DAY_START_S + 86_399,
            (DAY_START_S * 1000, (DAY_START_S + 86_400) * 1000 - 1),
        ),
        (None, None, (None, None)),
        (DAY_START_S, None, (DAY_START_S * 1000, None)),
        (None, DAY_START_S, (None, DAY_START_S * 1000 + 999)),
    ],
)
def test_seconds_range_to_ms_keeps_the_whole_last_second(
    start_s: int | None, end_s: int | None, expected: tuple[int | None, int | None]
) -> None:
    assert legacy_time.seconds_range_to_ms(start_s, end_s) == expected
