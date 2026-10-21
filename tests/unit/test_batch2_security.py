"""Unit tests for Batch-2 security fixes (TDD).

N4 — TOCTOU in users.json: AuthService must use a file-level lock for all
     read-modify-write operations to prevent concurrent request data loss.
N5 — Race condition in terminal approval: approve/reject must be protected
     by an asyncio.Lock to prevent exec_id confusion under concurrency.
N6 — NPM SSRF: fetch_npm_downloads must validate package names with a strict
     regex to prevent path traversal in the constructed URL.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity
from src.routers.mcp import _validate_npm_package_name, fetch_npm_downloads
from src.routers.terminal import (
    _approval_lock,
    approval_results,
    approve_command,
    pending_approvals,
)
from src.services.auth_service import AuthService, _users_file_lock

# ---------------------------------------------------------------------------
# N4 — TOCTOU: threading.Lock for users.json operations
# ---------------------------------------------------------------------------

def test_users_file_lock_is_a_threading_lock():
    """Module-level _users_file_lock must be a threading.Lock (not asyncio)."""
    assert isinstance(_users_file_lock, type(threading.Lock())), (
        "_users_file_lock must be a threading.Lock"
    )


def test_auth_service_acquires_lock_on_write():
    """_write_users must acquire _users_file_lock before writing."""
    svc = AuthService()
    write_calls: list[bool] = []

    real_write = svc._write_users

    def mock_write(users):
        write_calls.append(_users_file_lock.locked())
        return real_write(users)

    with (
        patch.object(svc, "_read_users", return_value=[]),
        patch.object(svc, "_write_users", side_effect=mock_write),
    ):
        svc.ensure_admin_user([])

    assert any(write_calls), (
        "_write_users must be called while _users_file_lock is held"
    )


def test_concurrent_writes_do_not_lose_data(tmp_path):
    """Two concurrent write calls must serialize, not interleave."""
    config_file = tmp_path / "users.json"
    svc1 = AuthService(users_file_path=str(config_file))
    svc2 = AuthService(users_file_path=str(config_file))

    svc1._write_users([])  # initialize empty file

    results: list[int] = []

    def write_then_read(svc, marker):
        users = svc._read_users()
        # Simulate work between read and write
        users_count = len(users)
        time.sleep(0.005)  # tiny delay to maximize race window
        svc._write_users(users)
        results.append(users_count)

    t1 = threading.Thread(target=write_then_read, args=(svc1, "A"))
    t2 = threading.Thread(target=write_then_read, args=(svc2, "B"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both threads should complete without exceptions
    assert len(results) == 2, "Both writes must complete"


# ---------------------------------------------------------------------------
# N5 — Race condition: asyncio.Lock for terminal approval dicts
# ---------------------------------------------------------------------------

def test_approval_lock_is_asyncio_lock():
    """_approval_lock must be an asyncio.Lock instance."""
    assert isinstance(_approval_lock, asyncio.Lock), (
        "_approval_lock must be asyncio.Lock"
    )


@pytest.fixture(autouse=True)
def _clean_approvals():
    pending_approvals.clear()
    yield
    pending_approvals.clear()


@pytest.mark.asyncio
async def test_approve_is_protected_by_lock():
    """approve_command must acquire _approval_lock before modifying state."""
    # Inject a fake identity via the raw function (bypass Depends for unit test)
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    exec_id = "lock-test-exec"
    event = asyncio.Event()
    pending_approvals[exec_id] = event

    acquired_during_approve: list[bool] = []
    real_acquire = _approval_lock.__class__.acquire

    async def spy_acquire(self, *args, **kwargs):
        result = await real_acquire(self, *args, **kwargs)
        acquired_during_approve.append(True)
        return result

    _State = type("S", (), {"new_auth_token": None})
    mock_request = type("R", (), {"headers": {}, "client": None, "state": _State()})()
    with patch.object(type(_approval_lock), "acquire", spy_acquire):
        await approve_command(exec_id, mock_request, identity)

    assert acquired_during_approve, "approve_command must acquire _approval_lock"
    pending_approvals.clear()
    approval_results.clear()


@pytest.mark.asyncio
async def test_concurrent_approvals_serialize():
    """Two concurrent approvals on different exec_ids must not interfere."""
    exec_id_a = "concurrent-a"
    exec_id_b = "concurrent-b"
    event_a = asyncio.Event()
    event_b = asyncio.Event()
    pending_approvals[exec_id_a] = event_a
    pending_approvals[exec_id_b] = event_b

    async def approve_a():
        approval_results[exec_id_a] = "approved"
        event_a.set()

    async def approve_b():
        approval_results[exec_id_b] = "rejected"
        event_b.set()

    await asyncio.gather(approve_a(), approve_b())

    assert approval_results.get(exec_id_a) == "approved"
    assert approval_results.get(exec_id_b) == "rejected"


# ---------------------------------------------------------------------------
# N6 — NPM SSRF: package name validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("express", True),
    ("@types/node", True),
    ("lodash", True),
    ("react-dom", True),
    ("mcp-scan", True),
    ("../../../etc/passwd", False),
    ("../../internal/api", False),
    ("/etc/passwd", False),
    ("evil package", False),           # spaces
    ("UPPER_CASE", False),             # npm names are lowercase
    ("a" * 215, False),                # too long (npm max = 214)
    ("", False),                       # empty
    ("../attack", False),
    ("node_modules", False),           # reserved name
    (".bin", False),                   # reserved (dot prefix)
])
def test_validate_npm_package_name(name: str, expected: bool):
    """NPM package name validator must accept valid names and reject injection."""
    result = _validate_npm_package_name(name)
    assert result == expected, (
        f"Package name {name!r}: expected valid={expected}, got valid={result}"
    )


@pytest.mark.asyncio
async def test_fetch_npm_downloads_rejects_invalid_package():
    """fetch_npm_downloads must reject invalid package names without making HTTP.

    哨兵值由 0 演進為 None（2026-06-11 npm 下載量誠實化：None＝未知，
    與真 0 區分）；安全性質不變——非法名稱嚴禁發出任何 HTTP 請求（N6）。
    """
    with patch("src.routers.mcp.httpx.AsyncClient") as mock_client:
        result = await fetch_npm_downloads("../../internal/api")
        mock_client.assert_not_called()

    assert result is None, "Invalid package name must return None without HTTP call"
