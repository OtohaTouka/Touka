"""P1-B：time_utils 新增單調時鐘 helper（供 duration 量測）——TDD.

duration 必須用單調時鐘（perf_counter），wall-clock epoch 會因 NTP 校時倒退。
此 helper 收進時間 SSOT（time_utils 在 lint_ssot 的 TIME_WHITELIST），避免業務層
直接觸碰 time.*。
"""

from __future__ import annotations

from src.utils.time_utils import get_monotonic_ms


def test_get_monotonic_ms_returns_float():
    assert isinstance(get_monotonic_ms(), float)


def test_get_monotonic_ms_non_decreasing():
    a = get_monotonic_ms()
    b = get_monotonic_ms()
    assert b >= a
