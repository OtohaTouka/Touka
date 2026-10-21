"""Unit tests for ProtocolManager.cleanup() graceful shutdown (TDD).

Tests that cleanup() handles all expected exception types during
uvicorn/ASGI lifespan shutdown without propagating exceptions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.protocols.manager import ProtocolManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """Create a ProtocolManager with a mock registry."""
    registry = MagicMock()
    return ProtocolManager(registry)


def _add_mock_stack(manager: ProtocolManager, side_effect=None) -> AsyncMock:
    """Inject a mock AsyncExitStack into the manager's _exit_stacks."""
    stack = AsyncMock()
    if side_effect is not None:
        stack.aclose.side_effect = side_effect
    manager._exit_stacks["test-server"] = stack
    return stack


# ---------------------------------------------------------------------------
# Tests: cleanup() exception handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_handles_cancelled_error(manager):
    """CancelledError during aclose() must be silently suppressed.

    This is the primary bug fix: uvicorn lifespan cancels tasks during
    shutdown, which propagates CancelledError into MCP stack cleanup.
    """
    _add_mock_stack(manager, side_effect=asyncio.CancelledError)
    # Must not raise — CancelledError should be caught and logged at DEBUG level
    await manager.cleanup()
    assert len(manager._exit_stacks) == 0  # stacks cleared regardless


@pytest.mark.asyncio
async def test_cleanup_handles_timeout_error(manager):
    """TimeoutError (from asyncio.wait_for) must be suppressed with WARNING."""
    _add_mock_stack(manager, side_effect=asyncio.TimeoutError)
    await manager.cleanup()
    assert len(manager._exit_stacks) == 0


@pytest.mark.asyncio
async def test_cleanup_handles_runtime_cancel_scope(manager):
    """RuntimeError containing 'cancel scope' must still be suppressed (existing behaviour)."""
    _add_mock_stack(manager, side_effect=RuntimeError("Cancelled in cancel scope"))
    await manager.cleanup()
    assert len(manager._exit_stacks) == 0


@pytest.mark.asyncio
async def test_cleanup_handles_other_runtime_error(manager):
    """RuntimeError NOT related to cancel scope must be suppressed with WARNING."""
    _add_mock_stack(manager, side_effect=RuntimeError("Some other runtime error"))
    await manager.cleanup()
    assert len(manager._exit_stacks) == 0


@pytest.mark.asyncio
async def test_cleanup_handles_generic_exception(manager):
    """Any generic Exception must be suppressed with WARNING."""
    _add_mock_stack(manager, side_effect=ValueError("Unexpected error"))
    await manager.cleanup()
    assert len(manager._exit_stacks) == 0


@pytest.mark.asyncio
async def test_cleanup_succeeds_normally(manager):
    """Happy path: aclose() succeeds, stacks cleared cleanly."""
    stack = _add_mock_stack(manager)
    await manager.cleanup()
    stack.aclose.assert_called_once()
    assert len(manager._exit_stacks) == 0


@pytest.mark.asyncio
async def test_cleanup_clears_active_sessions(manager):
    """cleanup() must clear _active_sessions regardless of stack errors."""
    manager._active_sessions["session-1"] = MagicMock()
    _add_mock_stack(manager, side_effect=asyncio.CancelledError)
    await manager.cleanup()
    assert len(manager._active_sessions) == 0


@pytest.mark.asyncio
async def test_cleanup_clears_multiple_stacks_with_mixed_errors(manager):
    """All stacks are closed even if some raise errors (no early abort)."""
    stack_ok = _add_mock_stack(manager)
    manager._exit_stacks["server-a"] = stack_ok

    stack_cancel = AsyncMock()
    stack_cancel.aclose.side_effect = asyncio.CancelledError
    manager._exit_stacks["server-b"] = stack_cancel

    stack_timeout = AsyncMock()
    stack_timeout.aclose.side_effect = asyncio.TimeoutError
    manager._exit_stacks["server-c"] = stack_timeout

    await manager.cleanup()
    assert len(manager._exit_stacks) == 0
    # All stacks should have had aclose called (loop doesn't short-circuit)
