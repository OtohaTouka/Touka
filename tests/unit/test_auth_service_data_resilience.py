"""Unit tests for AuthService data-resilience fixes (TDD).

Covers two complementary bugs in auth_service.py:
1. _read_users silently swallowed all exceptions and returned [] — the caller
   (_atomic_users_write) then overwrote users.json with {"users": []}, wiping
   all accounts.  Fix: raise ToukaException(PERSISTENCE_ERROR) instead.
2. _write_users used direct write_bytes() on the target file — a mid-write
   crash produced a partial / zero-byte file, which triggered bug 1 on next
   boot.  Fix: write to .tmp first, then Path.replace() (atomic rename).

Red/Green table (before fix → after fix):
  test_read_users_raises_on_corrupted_json              RED → GREEN
  test_read_users_raises_on_empty_file                  RED → GREEN
  test_read_users_returns_empty_when_missing            GREEN → GREEN  (regression guard)
  test_atomic_write_does_not_overwrite_on_corrupted_read  RED → GREEN
  test_write_leaves_no_tmp_file_on_success              RED → GREEN
  test_write_does_not_corrupt_file_when_exception_in_tmp_write  RED → GREEN
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.services.auth_service import AuthService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_auth(tmp_path: Path) -> tuple[AuthService, Path]:
    """Return (AuthService, users_file_path) isolated under tmp_path."""
    users_file = tmp_path / "users.json"
    return AuthService(users_file_path=str(users_file)), users_file


def _write_valid_users(users_file: Path) -> None:
    """Write a minimal valid users.json with one user (round-trip safe)."""
    data = {
        "users": [
            {
                "id": "test-uuid-001",
                "email": "survivor@touka.io",
                "role": "normal",
                "hashed_password": "$2b$12$fakehashfakehashfakehash",
                "status": "active",
                "owned_agents": [],
            }
        ]
    }
    users_file.write_bytes(json.dumps(data).encode("utf-8"))


# ---------------------------------------------------------------------------
# Test 1 — _read_users raises on corrupted JSON
# ---------------------------------------------------------------------------


def test_read_users_raises_on_corrupted_json(tmp_path: Path) -> None:
    """_read_users must raise ToukaException(PERSISTENCE_ERROR) when JSON is malformed.

    Before fix: silently returns [].
    After fix:  raises so _atomic_users_write never reaches _write_users.
    """
    auth, users_file = _make_auth(tmp_path)
    users_file.write_bytes(b"{ this is not valid json !!!")

    with pytest.raises(ToukaException) as exc_info:
        auth._read_users()  # noqa: SLF001

    assert exc_info.value.code == ErrorCode.PERSISTENCE_ERROR


# ---------------------------------------------------------------------------
# Test 2 — _read_users raises on empty file
# ---------------------------------------------------------------------------


def test_read_users_raises_on_empty_file(tmp_path: Path) -> None:
    """Empty file (e.g. mid-write crash) is not a valid JSON document — must raise.

    Before fix: silently returns [].
    After fix:  raises ToukaException(PERSISTENCE_ERROR).
    """
    auth, users_file = _make_auth(tmp_path)
    users_file.write_bytes(b"")

    with pytest.raises(ToukaException) as exc_info:
        auth._read_users()  # noqa: SLF001

    assert exc_info.value.code == ErrorCode.PERSISTENCE_ERROR


# ---------------------------------------------------------------------------
# Test 3 — _read_users returns [] when file does not exist (regression guard)
# ---------------------------------------------------------------------------


def test_read_users_returns_empty_when_missing(tmp_path: Path) -> None:
    """File-not-exists is the legitimate fresh-install state — must return [].

    This is existing correct behaviour and must not be broken by the fix.
    """
    auth, users_file = _make_auth(tmp_path)
    assert not users_file.exists()

    result = auth._read_users()  # noqa: SLF001

    assert result == []


# ---------------------------------------------------------------------------
# Test 4 — _atomic_users_write does NOT overwrite file on corrupted read
# ---------------------------------------------------------------------------


def test_atomic_write_does_not_overwrite_on_corrupted_read(tmp_path: Path) -> None:
    """When users.json is corrupted, _atomic_users_write must not silently erase it.

    Before fix:
      _read_users() returns [] → mutate_fn([]) runs → _write_users([]) overwrites
      with {"users": []} → all accounts gone.
    After fix:
      _read_users() raises ToukaException → Python propagates before _write_users
      is reached → file content is untouched.
    """
    auth, users_file = _make_auth(tmp_path)
    original_content = b"{ corrupted json !!!"
    users_file.write_bytes(original_content)

    with pytest.raises(ToukaException) as exc_info:
        auth._atomic_users_write(lambda users: None)  # noqa: SLF001

    assert users_file.read_bytes() == original_content, (
        "users.json must not be overwritten when _read_users raises"
    )
    assert exc_info.value.code == ErrorCode.PERSISTENCE_ERROR


# ---------------------------------------------------------------------------
# Test 5 — _write_users leaves no .tmp file on success
# ---------------------------------------------------------------------------


def test_write_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    """After a successful _write_users, the sibling .tmp file must not exist.

    Path.replace() moves .tmp → target atomically, so no orphaned .tmp remains.
    Before fix: no .tmp was ever created (direct write), so this also fails if
    the new mechanism is absent.
    """
    auth, users_file = _make_auth(tmp_path)
    _write_valid_users(users_file)

    users = auth._read_users()  # noqa: SLF001
    auth._write_users(users)  # noqa: SLF001

    tmp_file = users_file.with_suffix(".tmp")
    assert not tmp_file.exists(), (
        f"Sibling .tmp must be cleaned up after successful write, but {tmp_file} exists"
    )
    assert users_file.exists(), "Target users.json must exist after write"


# ---------------------------------------------------------------------------
# Test 6 — _write_users does not corrupt target when tmp write raises
# ---------------------------------------------------------------------------


def test_write_does_not_corrupt_file_when_exception_in_tmp_write(tmp_path: Path) -> None:
    """If write_bytes() raises during the .tmp step, the target file is untouched.

    Before fix (direct write_bytes on target): a mid-write OSError leaves the
    target partially written / zero-byte.
    After fix (tmp + replace): the target is replaced ONLY after tmp write
    succeeds, so an I/O error on the tmp step leaves the original intact.
    """
    auth, users_file = _make_auth(tmp_path)
    _write_valid_users(users_file)
    original_content = users_file.read_bytes()

    users = auth._read_users()  # noqa: SLF001

    with (
        patch.object(Path, "write_bytes", side_effect=OSError("Simulated disk full")),
        pytest.raises(OSError, match="Simulated disk full"),
    ):
        auth._write_users(users)  # noqa: SLF001

    assert users_file.read_bytes() == original_content, (
        "Target users.json must remain unchanged when the .tmp write step raises"
    )


@pytest.mark.parametrize("existing_users", [False, True])
def test_admin_password_delivery_failure_never_commits_users(
    tmp_path: Path,
    existing_users: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An undelivered credential must not commit an unreachable administrator."""
    auth, users_file = _make_auth(tmp_path)
    if existing_users:
        _write_valid_users(users_file)
    before = users_file.read_bytes() if existing_users else None
    error: ToukaException | None = None
    with (
        patch(
            "src.services.auth_service.get_initial_admin_password_file",
            return_value=tmp_path / "INITIAL_ADMIN_PASSWORD",
        ),
        patch(
            "src.services.auth_service.resource_manager.write_file_text",
            side_effect=OSError("private-path-sentinel"),
        ),
    ):
        try:
            auth.ensure_admin_user(["owned-synthetic-agent"])
        except ToukaException as caught:
            error = caught
    # Observable bytes/absence come first: the RED must demonstrate the unwanted commit.
    if existing_users:
        assert users_file.read_bytes() == before
    else:
        assert not users_file.exists()
    assert error is not None
    assert error.code == ErrorCode.PERSISTENCE_ERROR
    assert "private-path-sentinel" not in str(error)
    assert "private-path-sentinel" not in caplog.text
