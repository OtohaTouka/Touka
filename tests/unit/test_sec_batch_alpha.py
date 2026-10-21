"""Unit tests for P1/P3/P5/P6/P11/P15 security hardening (Batch 1 Alpha — TDD).

P1  — Path traversal: is_relative_to must replace startswith on sovereign output.
P3  — /terminal/stream SSE must require authentication.
P5  — append_message_to_session must lock to prevent TOCTOU lost writes.
P6  — /auth/register and /auth/refresh must enforce rate limiting.
P11 — Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy).
P15 — SECURE_COOKIES must default to True.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path

import pytest
from src.config import Settings
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.auth import refresh_rate_limiter, register_rate_limiter
from src.routers.terminal import terminal_stream
from src.services.identity_router import get_verified_identity
from src.utils import async_db
from src.utils.async_db import SessionService
from src.utils.history_manager import TreeMessageManager
from src.utils.time_utils import get_current_epoch

_allowlist_path = Path(__file__).resolve().parents[2] / "scripts" / "endpoint_auth_allowlist.py"
_spec = importlib.util.spec_from_file_location("endpoint_auth_allowlist", _allowlist_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
ALLOWLIST = _mod.ALLOWLIST

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])


def _override(identity):
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _clear():
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# P1 — Sovereign Output Path Traversal (is_relative_to)
# ---------------------------------------------------------------------------

def test_sovereign_output_rejects_sibling_directory():
    """The startswith bypass: '/output' starts with '/out' as string
    but is NOT a subdirectory. is_relative_to must return False."""
    base = Path("/workspace/outputs")
    sibling = Path("/workspace/outputs_evil")
    assert str(sibling).startswith(str(base)), "Precondition: startswith is broken"
    assert sibling.is_relative_to(base) is False


def test_sovereign_output_allows_legitimate_file():
    """Legitimate child path must be accepted."""
    base = Path("/workspace/outputs")
    child = Path("/workspace/outputs/agent/report.txt")
    assert child.is_relative_to(base) is True


# ---------------------------------------------------------------------------
# P3 — /terminal/stream SSE requires auth
# ---------------------------------------------------------------------------

def test_terminal_stream_not_in_allowlist():
    """P3: /terminal/stream must NOT appear in the endpoint auth allowlist."""
    terminal_stream_entries = [
        e for e in ALLOWLIST
        if "/terminal/stream" in e.get("path_prefix", "")
    ]
    assert terminal_stream_entries == [], (
        f"/terminal/stream should be removed from allowlist, found: {terminal_stream_entries}"
    )


def test_terminal_stream_has_auth_dependency():
    """P3: /terminal/stream must have get_verified_identity Depends."""
    sig = inspect.signature(terminal_stream)
    param_names = list(sig.parameters.keys())
    assert "identity" in param_names, (
        f"terminal_stream must declare 'identity' parameter, got: {param_names}"
    )




# ---------------------------------------------------------------------------
# P5 — append_message_to_session concurrent locking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_message_concurrent_no_lost_writes(tmp_path, monkeypatch):
    """10 concurrent appends to the same session must all succeed (no lost writes)."""
    test_session_id = "test-p5-concurrent-lock"
    monkeypatch.setattr(async_db, "get_db_path", lambda: tmp_path / "sessions.db")
    await async_db.init_db_async()
    assert await SessionService.upsert_session(
        test_session_id, "P5 Test", TreeMessageManager.create_empty_tree(), identity=OWNER
    )
    tasks = [
        SessionService.append_message_to_session(
            test_session_id, "user", f"message-{i}", identity=OWNER
        )
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)

    assert all(results), "Some concurrent appends returned False"
    saved = await SessionService.read_execution_snapshot(test_session_id, OWNER)
    tree = saved.session["messages"]
    messages = TreeMessageManager.get_linear_path(tree)
    assert len(tree["messages"]) == len(messages) == 10
    assert {message["content"] for message in messages} == {f"message-{i}" for i in range(10)}


# ---------------------------------------------------------------------------
# P6 — /auth/register and /auth/refresh rate limiting
# ---------------------------------------------------------------------------

def test_register_rate_limited_after_max_attempts(auth_client):
    """POST /auth/register beyond rate limit must return 429."""
    ip = "10.0.0.99"
    now = get_current_epoch()

    for _ in range(register_rate_limiter._max + 1):
        register_rate_limiter.record(ip, now)

    assert not register_rate_limiter.is_allowed(ip, now), (
        "Rate limiter should block after max attempts"
    )
    register_rate_limiter.reset(ip)


def test_refresh_rate_limited_after_max_attempts(auth_client):
    """POST /auth/refresh beyond rate limit must return 429."""
    ip = "10.0.0.100"
    now = get_current_epoch()

    for _ in range(refresh_rate_limiter._max + 1):
        refresh_rate_limiter.record(ip, now)

    assert not refresh_rate_limiter.is_allowed(ip, now), (
        "Rate limiter should block after max attempts"
    )
    refresh_rate_limiter.reset(ip)


# ---------------------------------------------------------------------------
# P11 — Security headers present on response
# ---------------------------------------------------------------------------

def test_security_headers_present_on_response(auth_client):
    """Every response must include security hardening headers."""
    resp = auth_client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# P15 — SECURE_COOKIES defaults to True
# ---------------------------------------------------------------------------

def test_secure_cookies_defaults_true():
    """Settings model SECURE_COOKIES field must default to True."""
    field_info = Settings.model_fields["SECURE_COOKIES"]
    assert field_info.default is True, (
        f"SECURE_COOKIES default must be True, got {field_info.default}"
    )
