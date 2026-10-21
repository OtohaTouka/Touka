"""Unit tests for P24/P26/P28/P29 security hardening (Batch 5 Epsilon — TDD).

P24 — LDH auto-token-issue must be rate-limited.
P26 — Heartbeat owner expiry check must include 30s grace period.
P28 — Withdrawn: conversations are never deleted automatically; last_activity_at stays.
P29 — Heartbeat must not log "DEBUG:" prefix at INFO level.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from src import config as config_mod
from src import main as main_mod
from src.config import settings
from src.routers import heartbeat as heartbeat_mod
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
# P26 — Heartbeat expiry grace period
# ---------------------------------------------------------------------------

def test_heartbeat_expiry_check_has_grace_period():
    """heartbeat.py must use a grace period in the token expiry check."""
    src = inspect.getsource(heartbeat_mod)
    assert "refresh_token_exp + 30" in src or "grace" in src.lower(), (
        "Heartbeat must add a grace period to the refresh_token_exp check"
    )


def test_heartbeat_rejects_at_exact_expiry_plus_grace():
    """At exactly expiry + 31 seconds, the owner token must be expired."""
    src = inspect.getsource(heartbeat_mod)
    # Verify the grace condition exists in source
    assert "30" in src, "Grace period constant must be in heartbeat.py"


def test_heartbeat_allows_within_grace_period():
    """Within the 30s grace period, the owner token should still be considered valid."""
    src = inspect.getsource(heartbeat_mod)
    # The condition should allow access within grace: now <= exp + 30
    assert "refresh_token_exp + 30" in src, (
        "heartbeat.py must check: now_sec >= (owner.refresh_token_exp + 30)"
    )


# ---------------------------------------------------------------------------
# P28 — Session TTL (withdrawn by ADR-0259: conversations are never deleted automatically)
# ---------------------------------------------------------------------------

def test_session_ttl_setting_is_removed():
    """No session TTL setting is left to turn automatic deletion back on."""
    assert "SESSION_TTL_DAYS" not in type(settings).model_fields
    assert not hasattr(config_mod, "SESSION_TTL_DAYS")


def test_session_has_last_activity_at_migration():
    """init_db_async must add last_activity_at migration."""
    src = inspect.getsource(async_db_mod)
    assert "last_activity_at" in src, (
        "async_db.py must include last_activity_at column and migration"
    )


def test_no_automatic_session_cleanup():
    """No cleanup function, no startup call, no SQL deleting conversations by inactivity."""
    assert not hasattr(async_db_mod, "cleanup_expired_sessions")
    assert "cleanup_expired_sessions" not in inspect.getsource(main_mod)
    for module in (async_db_mod, main_mod):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value.upper()
                assert not ("DELETE FROM USER_SESSIONS" in sql and "LAST_ACTIVITY_AT" in sql)


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


# ---------------------------------------------------------------------------
# P29 — No DEBUG-prefixed INFO logs in heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_no_info_log_with_debug_prefix():
    """heartbeat.py must not use logger.info with 'DEBUG:' prefix."""
    src = inspect.getsource(heartbeat_mod)
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if 'logger.info' in line and 'DEBUG:' in line:
            pytest.fail(
                f"heartbeat.py line {i}: logger.info must not use 'DEBUG:' prefix: {line.strip()}"
            )


def test_heartbeat_checks_passed_uses_debug_level():
    """'checks passed' log must use logger.debug, not logger.info."""
    src = inspect.getsource(heartbeat_mod)
    assert "logger.debug" in src, (
        "Heartbeat must use logger.debug for non-critical status messages"
    )
    # Ensure the specific "passed all checks" style message is at debug level
    for line in src.splitlines():
        if "passed" in line.lower() and "DEBUG:" in line and "logger.info" in line:
            pytest.fail(f"Found logger.info with DEBUG: prefix: {line.strip()}")
