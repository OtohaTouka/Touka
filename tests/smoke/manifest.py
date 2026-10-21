"""Standalone smoke execution classification SSOT (ADR-0214)."""

from __future__ import annotations

from typing import NamedTuple


class SmokeSpec(NamedTuple):
    """One explicitly classified standalone smoke/probe entry."""

    path: str
    execution_class: str
    data_access: str
    metered_api: bool
    provider_surface: str


def _automated(path: str, *, provider_surface: str = "none") -> SmokeSpec:
    return SmokeSpec(
        path=path,
        execution_class="AUTOMATED_SANDBOXED",
        data_access="TEMP_SANDBOX",
        metered_api=False,
        provider_surface=provider_surface,
    )


# Explicit rather than glob-derived: adding a new smoke requires a conscious data/API
# classification and the governance meta-test proves that this list remains complete.
AUTOMATED_SANDBOXED: tuple[SmokeSpec, ...] = (
    _automated("smoke_agent_interaction.py", provider_surface="mock_provider"),
    _automated("smoke_ask_user.py"),
    _automated("smoke_auth_identity.py"),
    _automated("smoke_autonomy_enforcement.py", provider_surface="mock_provider"),
    _automated("smoke_bash_terminal.py"),
    _automated("smoke_chat.py", provider_surface="mock_provider"),
    _automated("smoke_context_snapshot.py"),
    _automated("smoke_creation_engine.py"),
    _automated("smoke_dispatch_callback.py"),
    _automated("smoke_frontend_persistence.py"),
    _automated("smoke_group_dispatcher.py", provider_surface="mock_provider"),
    _automated("smoke_incognito.py"),
    _automated("smoke_io_prison.py"),
    _automated("smoke_ldh_handshake.py"),
    _automated("smoke_mcp_lifecycle.py"),
    _automated("smoke_memory_tools.py"),
    _automated("smoke_mission_awareness.py"),
    _automated("smoke_model_governance.py", provider_surface="mock_discovery"),
    _automated("smoke_provider_routing.py"),
    _automated("smoke_pulse_rhythm.py"),
    _automated("smoke_router.py"),
    _automated("smoke_skill_activation.py"),
    _automated("smoke_soul_recovery.py"),
    _automated("smoke_tool_coverage.py"),
)


# These entries are reporting-only. The automated runner never executes them.
MANUAL_LIVE_SPECS: tuple[SmokeSpec, ...] = (
    SmokeSpec(
        path="manual/live_chat_probe.py",
        execution_class="MANUAL_LIVE",
        data_access="TEMP_SANDBOX",
        metered_api=True,
        provider_surface="configured_provider_chat",
    ),
    SmokeSpec(
        path="manual/memory_eval_probe.py",
        execution_class="MANUAL_LIVE",
        data_access="TEMP_SANDBOX",
        metered_api=True,
        provider_surface="configured_provider_chat_with_memory_tools",
    ),
    # r55: Claude 5.5 latency by thinking profile through complete_once (Anthropic only).
    SmokeSpec(
        path="manual/thinking_latency_probe.py",
        execution_class="MANUAL_LIVE",
        data_access="TEMP_SANDBOX",
        metered_api=True,
        provider_surface="anthropic_complete_once",
    ),
    # Hugging Face model download only (free, about 1.13 GB, no provider keys). Marked
    # metered so it is listed as human-approval-only like the other manual probes.
    SmokeSpec(
        path="manual/embedding_threshold_probe.py",
        execution_class="MANUAL_LIVE",
        data_access="TEMP_SANDBOX",
        metered_api=True,
        provider_surface="huggingface_model_download",
    ),
)
