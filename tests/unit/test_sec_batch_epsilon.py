"""Unit tests for P24/P28 security hardening (Batch 5 Epsilon — TDD).

P24 — LDH auto-token-issue must be rate-limited.
P28 — Withdrawn: conversations are never deleted automatically; last_activity_at stays.

P26 (30s grace on the owner's refresh token) and P29 (the "checks passed" line is DEBUG)
are behaviour tests in test_heartbeat_awaken_hardening_security.py (舊審查報告丙-6).
"""

from __future__ import annotations

import pytest
from src import config as config_mod
from src.config import settings
from src.services import identity_router as identity_router_mod
from src.utils import async_db as async_db_mod
from src.utils.rate_limiter import RateLimiter
from src.utils.time_utils import get_current_epoch, get_current_epoch_ms

# ---------------------------------------------------------------------------
# P24 — LDH rate limiter
# ---------------------------------------------------------------------------

def test_ldh_rate_limiter_exists():
    """identity_router.py must define _ldh_limiter for LDH rate limiting."""
    assert hasattr(identity_router_mod, "_ldh_limiter"), (
        "identity_router.py must define module-level _ldh_limiter"
    )


def test_ldh_rate_limiter_is_rate_limiter():
    """_ldh_limiter must be an instance of RateLimiter."""
    assert isinstance(identity_router_mod._ldh_limiter, RateLimiter), (
        "_ldh_limiter must be a RateLimiter instance"
    )


def test_ldh_rate_limited_after_30_requests():
    """After 30 LDH requests from the same IP, the limiter must block."""
    limiter = identity_router_mod._ldh_limiter
    ip = "192.168.1.99"
    now = get_current_epoch()

    for _ in range(limiter._max + 1):
        limiter.record(ip, now)

    assert not limiter.is_allowed(ip, now), (
        "LDH limiter must block after exceeding max_attempts"
    )
    limiter.reset(ip)


# ---------------------------------------------------------------------------
# P28 — Session TTL (withdrawn by ADR-0259: conversations are never deleted automatically)
# ---------------------------------------------------------------------------

def test_session_ttl_setting_is_removed():
    """No session TTL setting is left to turn automatic deletion back on."""
    assert "SESSION_TTL_DAYS" not in type(settings).model_fields
    assert not hasattr(config_mod, "SESSION_TTL_DAYS")


def test_no_automatic_session_cleanup():
    """No cleanup function is left to call."""
    assert not hasattr(async_db_mod, "cleanup_expired_sessions")


@pytest.mark.asyncio
async def test_startup_db_init_keeps_old_conversations():
    """Old, inactive non-home conversations survive the startup database step."""
    year_ago_ms = get_current_epoch_ms() - 400 * 86_400_000
    rows = {"p28-old-1970s": 1, "p28-old-year": year_ago_ms}
    await async_db_mod.init_db_async()
    async with async_db_mod.get_async_db() as db:
        await db.executemany(
            "INSERT INTO user_sessions (id, title, messages, updatedAt, is_home,"
            " last_activity_at) VALUES (?, 'old', '{}', ?, 0, ?)",
            [(session_id, stamp, stamp) for session_id, stamp in rows.items()],
        )
        await db.commit()
    try:
        await async_db_mod.init_db_async()
        async with async_db_mod.get_async_db() as db, db.execute(
            "SELECT id FROM user_sessions WHERE id IN (?, ?)", tuple(rows)
        ) as cursor:
            kept = {row["id"] for row in await cursor.fetchall()}
    finally:
        async with async_db_mod.get_async_db() as db:
            await db.execute("DELETE FROM user_sessions WHERE id IN (?, ?)", tuple(rows))
            await db.commit()
    assert kept == set(rows)
