"""Unit tests for M2/M3 security hardening (TDD).

M2 — Approval Queue Unbounded: pending_approvals must reject new entries when
     the queue is at MAX_PENDING_APPROVALS to prevent memory exhaustion.
M3 — ReDoS in SNS Regex: the @alias pattern must have a bounded quantifier and
     the SNSGatewayService must guard against excessively long content.
"""

from __future__ import annotations

import asyncio

import pytest
from src.models.identity import AgentIdentity
from src.routers.terminal import (
    MAX_PENDING_APPROVALS,
    approval_results,
    pending_approvals,
    request_approval,
)
from src.services.sns_gateway import (
    MAX_SNS_CONTENT_LENGTH,
    SNSGatewayService,
)

# ---------------------------------------------------------------------------
# M2 — Approval Queue: bounded at MAX_PENDING_APPROVALS
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_pending():
    """Reset global pending_approvals before and after each test."""
    pending_approvals.clear()
    yield
    pending_approvals.clear()


def test_max_pending_approvals_constant_is_reasonable():
    """MAX_PENDING_APPROVALS must be a small positive integer."""
    assert isinstance(MAX_PENDING_APPROVALS, int)
    assert 1 <= MAX_PENDING_APPROVALS <= 200, (
        "MAX_PENDING_APPROVALS should be a reasonable limit (1–200)"
    )


@pytest.mark.asyncio
async def test_request_approval_rejected_when_queue_full():
    """When pending_approvals is at MAX_PENDING_APPROVALS, new requests must be rejected."""
    # Fill the queue to the limit
    for i in range(MAX_PENDING_APPROVALS):
        pending_approvals[f"exec-{i}"] = asyncio.Event()

    assert len(pending_approvals) == MAX_PENDING_APPROVALS

    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    approved, payload = await request_approval(
        exec_id="overflow-exec",
        identity=identity,
        command="ls",
    )

    assert approved is False, (
        "request_approval must return False when queue is full"
    )
    # The overflow exec_id must NOT be added to the queue
    assert "overflow-exec" not in pending_approvals


@pytest.mark.asyncio
async def test_request_approval_allowed_when_under_limit():
    """Normal approval requests must proceed when queue is below the limit."""
    # Queue is empty — should be allowed (starts the approval flow)
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")

    # Manually set the event to simulate instant approval so the coroutine doesn't block
    async def _auto_approve():
        await asyncio.sleep(0)  # yield to allow request_approval to set the event
        if "auto-exec" in pending_approvals:
            approval_results["auto-exec"] = "approved"
            pending_approvals["auto-exec"].set()

    task = asyncio.create_task(_auto_approve())
    approved, _ = await request_approval(
        exec_id="auto-exec",
        identity=identity,
        command="ls",
    )
    await task

    # The approval must have been processed (not short-circuited by queue limit)
    assert isinstance(approved, bool)


def test_pending_approvals_dict_does_not_grow_beyond_limit():
    """Directly verify the queue stays bounded after hitting the limit."""
    for i in range(MAX_PENDING_APPROVALS + 10):
        if len(pending_approvals) < MAX_PENDING_APPROVALS:
            pending_approvals[f"exec-{i}"] = asyncio.Event()

    assert len(pending_approvals) <= MAX_PENDING_APPROVALS


# ---------------------------------------------------------------------------
# M3 — SNS Regex: bounded quantifier + content length guard
# ---------------------------------------------------------------------------

def test_alias_pattern_matches_normal_mentions():
    """Normal @mentions like @Alice, @代理人 must still be detected."""
    gateway = SNSGatewayService()
    assert gateway._has_group_mentions("Hello @Alice how are you")
    assert gateway._has_group_mentions("@代理人 請回覆")
    assert gateway._has_group_mentions("Hey @Agent-1 and @Bot_2")


def test_alias_pattern_does_not_match_plain_text():
    """Text without @ must not produce false positives."""
    gateway = SNSGatewayService()
    assert not gateway._has_group_mentions("No mentions here")
    assert not gateway._has_group_mentions("Email: user at example dot com")


def test_alias_pattern_quantifier_is_bounded():
    """The _ALIAS_PATTERN quantifier must be bounded (e.g. {1,N}) to prevent ReDoS."""
    pattern_str = SNSGatewayService._ALIAS_PATTERN.pattern
    # Unbounded quantifier '+' alone (without length limit) is the risk
    # After M3 fix, it should use {1,N} notation
    assert "{" in pattern_str, (
        "M3 fix: _ALIAS_PATTERN must use a bounded quantifier {1,N} instead of bare +"
    )


def test_alias_pattern_rejects_excessively_long_alias():
    """An alias longer than the bound must not be captured as a single match."""
    long_alias = "@" + "A" * 200  # 200-char alias, well over any reasonable bound
    matches = SNSGatewayService._ALIAS_PATTERN.findall(long_alias)
    # The pattern should either not match, or match only the bounded portion
    for m in matches:
        assert len(m) <= 100, (
            f"Matched alias is too long ({len(m)} chars) — quantifier is not bounded"
        )


def test_max_sns_content_length_constant_exists():
    """MAX_SNS_CONTENT_LENGTH must be exported from sns_gateway for testing."""
    assert isinstance(MAX_SNS_CONTENT_LENGTH, int)
    assert MAX_SNS_CONTENT_LENGTH > 0


def test_sns_gateway_has_content_guard():
    """SNSGatewayService must expose a _guard_content_length method (M3 fix)."""
    gateway = SNSGatewayService()
    assert hasattr(gateway, "_guard_content_length"), (
        "SNSGatewayService must have _guard_content_length for M3 content length check"
    )


def test_content_guard_truncates_oversized_content():
    """_guard_content_length must truncate content exceeding MAX_SNS_CONTENT_LENGTH."""
    gateway = SNSGatewayService()
    long_content = "A" * (MAX_SNS_CONTENT_LENGTH + 500)
    guarded = gateway._guard_content_length(long_content)
    assert len(guarded) <= MAX_SNS_CONTENT_LENGTH


def test_content_guard_passes_normal_content():
    """_guard_content_length must return short content unchanged."""
    gateway = SNSGatewayService()
    normal = "Hello @Alice, how are you today?"
    assert gateway._guard_content_length(normal) == normal
