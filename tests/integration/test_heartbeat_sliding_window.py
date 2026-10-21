"""Integration tests for Heartbeat PostHeartbeat sliding-window hook (TDD — RED first).

Verifies that:
1. Sub-sessions never trigger check_and_trim.
2. Home sessions trigger check_and_trim after all pulses complete.
3. check_and_trim is called with the correct identity and session_id.
4. Heartbeat is not affected if the sliding-window hook raises an exception.

Tests invoke _process_heartbeat_loop directly with all dependencies mocked to
avoid real I/O. Only DebugAgent is used as identity; no production data is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.routers.heartbeat import _process_heartbeat_loop
from src.services.sliding_window_service import SlidingWindowService

DEBUG_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
_SESSION_ID = "home_DebugAgent_test"


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        agent_id="DebugAgent",
        session_id=_SESSION_ID,
        auth_level="SafeAuto",
        enable_autonomy=True,
    )


def _patch_pulse(turns: int = 1):
    """Patch _execute_single_pulse to return a fixed turn count without real I/O."""
    return patch(
        "src.routers.heartbeat._execute_single_pulse",
        new=AsyncMock(return_value=turns),
    )


def _patch_health():
    """Patch model health check to be a no-op."""
    return patch(
        "src.routers.heartbeat.model_manager.check_models_health",
        new=AsyncMock(),
    )


# ── Sub-session: no sliding-window ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sub_session_does_not_trigger_check_and_trim():
    """When is_home_mode=False, check_and_trim must NOT be called."""
    with (
        _patch_pulse(),
        _patch_health(),
        patch(
            "src.routers.heartbeat.sliding_window_service.check_and_trim",
            new=AsyncMock(return_value=False),
        ) as mock_trim,
    ):
        await _process_heartbeat_loop(
            final_session_id=_SESSION_ID,
            identity=DEBUG_IDENTITY,
            model_override=None,
            ctx=_make_ctx(),
            pulse_count=1,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=False,  # ← sub-session
        )

    mock_trim.assert_not_called()


# ── Home session: sliding-window runs ──────────────────────────────────────


@pytest.mark.asyncio
async def test_home_session_triggers_check_and_trim():
    """When is_home_mode=True, check_and_trim must be called after all pulses."""
    with (
        _patch_pulse(),
        _patch_health(),
        patch(
            "src.routers.heartbeat.sliding_window_service.check_and_trim",
            new=AsyncMock(return_value=False),
        ) as mock_trim,
    ):
        await _process_heartbeat_loop(
            final_session_id=_SESSION_ID,
            identity=DEBUG_IDENTITY,
            model_override=None,
            ctx=_make_ctx(),
            pulse_count=1,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=True,  # ← home session
        )

    mock_trim.assert_awaited_once_with(DEBUG_IDENTITY, _SESSION_ID)


@pytest.mark.asyncio
async def test_check_and_trim_called_with_correct_args():
    """check_and_trim receives the correct identity and session_id."""
    captured: list[tuple] = []

    async def _fake_trim(identity, session_id):
        captured.append((identity, session_id))
        return False

    with (
        _patch_pulse(turns=3),
        _patch_health(),
        patch(
            "src.routers.heartbeat.sliding_window_service.check_and_trim",
            side_effect=_fake_trim,
        ),
    ):
        await _process_heartbeat_loop(
            final_session_id=_SESSION_ID,
            identity=DEBUG_IDENTITY,
            model_override=None,
            ctx=_make_ctx(),
            pulse_count=2,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=True,
        )

    assert len(captured) == 1
    used_identity, used_session_id = captured[0]
    assert used_identity.agent_id == "DebugAgent"
    assert used_session_id == _SESSION_ID


@pytest.mark.asyncio
async def test_check_and_trim_called_after_all_pulses():
    """check_and_trim must fire AFTER all pulses complete, not during."""
    call_order: list[str] = []

    async def _fake_pulse(*args, **kwargs):
        call_order.append("pulse")
        return 1

    async def _fake_trim(identity, session_id):
        call_order.append("trim")
        return False

    with (
        patch("src.routers.heartbeat._execute_single_pulse", side_effect=_fake_pulse),
        _patch_health(),
        patch("src.routers.heartbeat.asyncio.sleep", new=AsyncMock()),
        patch(
            "src.routers.heartbeat.sliding_window_service.check_and_trim",
            side_effect=_fake_trim,
        ),
    ):
        await _process_heartbeat_loop(
            final_session_id=_SESSION_ID,
            identity=DEBUG_IDENTITY,
            model_override=None,
            ctx=_make_ctx(),
            pulse_count=3,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=True,
        )

    # All 3 pulses must run before trim
    assert call_order == ["pulse", "pulse", "pulse", "trim"]


# ── Hook failure isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_failure_does_not_fail_heartbeat():
    """If check_and_trim raises, _process_heartbeat_loop must complete normally."""
    with (
        _patch_pulse(),
        _patch_health(),
        patch(
            "src.routers.heartbeat.sliding_window_service.check_and_trim",
            side_effect=RuntimeError("simulated hook failure"),
        ),
    ):
        # Must not raise
        await _process_heartbeat_loop(
            final_session_id=_SESSION_ID,
            identity=DEBUG_IDENTITY,
            model_override=None,
            ctx=_make_ctx(),
            pulse_count=1,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=True,
        )


# ── Window disabled ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_disabled_trim_not_called_with_trim_result():
    """When config.enabled=False, check_and_trim returns False (no-op)."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=False, keep_rounds=2, upper_limit_rounds=3)
    with patch.object(svc, "load_config", return_value=cfg):
        result = await svc.check_and_trim(DEBUG_IDENTITY, "any-session")
    assert result is False
