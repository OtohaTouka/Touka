"""P8-B Slice 2：authority expansion/reduction crash FSM。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.workload.models import WorkloadAuditEvent
from src.protocols.mcp.oauth.workload.store import (
    WorkloadAuthorityConflictError,
    WorkloadOAuthStore,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal


def _store(tmp_path: Path) -> WorkloadOAuthStore:
    return WorkloadOAuthStore(
        tmp_path / "workload.sqlite3",
        OAuthKeyRing(current=OAuthKey(kid="workload-k1", material=b"k" * 32)),
    )


def _event(
    event_id: str,
    *,
    action: str,
    outcome: Literal["pending", "success"] = "pending",
) -> WorkloadAuditEvent:
    return WorkloadAuditEvent(
        event_id=event_id,
        action=action,
        actor_id="human-1",
        authority_id="profile-1",
        authority_generation=2,
        request_id="request-1",
        outcome=outcome,
        created_at=100.0,
        details_digest=f"sha256:{'6' * 64}",
    )


@pytest.mark.asyncio
async def test_expansion_requires_exact_replay_before_becoming_active(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.prepare_schema()
    request_digest = f"sha256:{'7' * 64}"
    transition = await store.begin_authority_transition(
        operation_id="operation-1",
        direction="expansion",
        authority_kind="profile",
        authority_id="profile-1",
        authority_generation=2,
        actor_id="human-1",
        request_digest=request_digest,
        now=100.0,
        event=_event("event-intent", action="profile.expand.intent"),
    )

    assert transition.status == "pending_json"
    assert await store.authority_epoch() == 1

    replay = await store.begin_authority_transition(
        operation_id="operation-1",
        direction="expansion",
        authority_kind="profile",
        authority_id="profile-1",
        authority_generation=2,
        actor_id="human-1",
        request_digest=request_digest,
        now=101.0,
        event=_event("event-intent", action="profile.expand.intent"),
    )
    assert replay == transition
    assert await store.pending_audit_count() == 1

    await store.mark_transition_recovery_required("operation-1", now=102.0)
    with pytest.raises(WorkloadAuthorityConflictError):
        await store.confirm_authority_transition(
            operation_id="operation-1",
            actor_id="other-human",
            request_digest=request_digest,
            now=103.0,
            event=_event(
                "event-result-wrong",
                action="profile.expand.result",
                outcome="success",
            ),
        )

    completed = await store.confirm_authority_transition(
        operation_id="operation-1",
        actor_id="human-1",
        request_digest=request_digest,
        now=104.0,
        event=_event(
            "event-result",
            action="profile.expand.result",
            outcome="success",
        ),
    )
    assert completed.status == "active"
    assert await store.authority_epoch() == 2


@pytest.mark.asyncio
async def test_reduction_invalidates_old_epoch_before_json_completion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.prepare_schema()
    request_digest = f"sha256:{'8' * 64}"

    pending = await store.begin_authority_transition(
        operation_id="operation-reduce",
        direction="reduction",
        authority_kind="profile",
        authority_id="profile-1",
        authority_generation=2,
        actor_id="human-1",
        request_digest=request_digest,
        now=100.0,
        event=_event("event-reduce-intent", action="profile.reduce.intent"),
    )

    assert pending.status == "pending_json"
    assert await store.authority_epoch() == 2

    completed = await store.confirm_authority_transition(
        operation_id="operation-reduce",
        actor_id="human-1",
        request_digest=request_digest,
        now=101.0,
        event=_event(
            "event-reduce-result",
            action="profile.reduce.result",
            outcome="success",
        ),
    )
    assert completed.status == "completed"
    assert await store.authority_epoch() == 2


@pytest.mark.asyncio
async def test_operation_id_cannot_be_retargeted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.prepare_schema()
    await store.begin_authority_transition(
        operation_id="operation-1",
        direction="expansion",
        authority_kind="profile",
        authority_id="profile-1",
        authority_generation=2,
        actor_id="human-1",
        request_digest=f"sha256:{'9' * 64}",
        now=100.0,
        event=_event("event-1", action="profile.expand.intent"),
    )

    with pytest.raises(WorkloadAuthorityConflictError):
        await store.begin_authority_transition(
            operation_id="operation-1",
            direction="expansion",
            authority_kind="profile",
            authority_id="profile-1",
            authority_generation=2,
            actor_id="human-1",
            request_digest=f"sha256:{'0' * 64}",
            now=101.0,
            event=_event("event-2", action="profile.expand.intent"),
        )
