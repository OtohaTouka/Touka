"""Unit tests for RateLimiter (TDD — H2 fix).

Tests cover the sliding-window rate limiting logic in isolation.
No HTTP or VS Code API dependencies.
"""

from __future__ import annotations

import pytest
from src.utils.rate_limiter import RateLimiter


@pytest.fixture
def limiter() -> RateLimiter:
    """Standard limiter: 3 attempts per 60-second window."""
    return RateLimiter(max_attempts=3, window_sec=60)


# ---------------------------------------------------------------------------
# Basic allow / deny behaviour
# ---------------------------------------------------------------------------

def test_first_request_is_allowed(limiter: RateLimiter):
    """First attempt must be allowed."""
    assert limiter.is_allowed("192.168.1.1", now=1000.0) is True


def test_attempts_below_limit_are_allowed(limiter: RateLimiter):
    """Attempts below max_attempts in the window must all be allowed."""
    ip = "10.0.0.1"
    for i in range(3):
        assert limiter.is_allowed(ip, now=float(i)) is True
        limiter.record(ip, now=float(i))


def test_exceeding_max_attempts_is_denied(limiter: RateLimiter):
    """After max_attempts records in the window, next is_allowed must return False."""
    ip = "10.0.0.2"
    for i in range(3):
        limiter.record(ip, now=1000.0 + i)

    assert limiter.is_allowed(ip, now=1005.0) is False


def test_different_ips_are_independent(limiter: RateLimiter):
    """Rate limits for different IPs must not affect each other."""
    for i in range(3):
        limiter.record("bad-ip", now=float(i))

    assert limiter.is_allowed("good-ip", now=5.0) is True


# ---------------------------------------------------------------------------
# Sliding window expiry
# ---------------------------------------------------------------------------

def test_old_attempts_outside_window_expire(limiter: RateLimiter):
    """Attempts older than window_sec must not count toward the limit."""
    ip = "10.0.0.3"
    # Three attempts far in the past (outside 60s window)
    for i in range(3):
        limiter.record(ip, now=float(i))

    # At now=200 (> 60s after last attempt at t=2), they should be gone
    assert limiter.is_allowed(ip, now=200.0) is True


def test_window_boundary_exact(limiter: RateLimiter):
    """Attempts at exactly window_sec ago are expired (exclusive boundary)."""
    ip = "10.0.0.4"
    limiter.record(ip, now=0.0)
    limiter.record(ip, now=0.0)
    limiter.record(ip, now=0.0)
    # At now=60.0, all three are at exactly window edge → expired
    assert limiter.is_allowed(ip, now=60.0) is True


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset_clears_attempts(limiter: RateLimiter):
    """reset() must clear all recorded attempts for a key."""
    ip = "10.0.0.5"
    for i in range(3):
        limiter.record(ip, now=float(i))
    assert limiter.is_allowed(ip, now=5.0) is False

    limiter.reset(ip)
    assert limiter.is_allowed(ip, now=5.0) is True


def test_reset_unknown_key_is_safe(limiter: RateLimiter):
    """reset() on an unknown key must not raise."""
    limiter.reset("no-such-ip")  # should not raise
