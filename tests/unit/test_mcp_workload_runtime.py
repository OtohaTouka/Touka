"""P8-B Slice 4：ephemeral token cache 與 per-send egress permit runtime。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import (
    McpInvocationAuthorization,
    McpRequestContext,
    McpWorkloadWireAttempt,
    digest_mcp_arguments,
)
from src.protocols.mcp.oauth.workload.models import (
    WorkloadAccessToken,
    WorkloadCredentialPayload,
    WorkloadMetadataSnapshot,
)
from src.protocols.mcp.oauth.workload.target import McpOAuthTargetBinding

_TRANSPORT_URL = "https://mcp.example.test/mcp"


def _runtime() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.runtime"
    assert importlib.util.find_spec(module_name) is not None, "runtime seam is missing"
    return importlib.import_module(module_name)


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-1",
        identity=AgentIdentity.for_role("Alice", "Owner"),
        installation_id="calendar",
        auth_level="SafeAuto",
        execution_kind="heartbeat",
        origin="heartbeat",
    )


def _target_binding() -> McpOAuthTargetBinding:
    return McpOAuthTargetBinding(
        expected_resource="https://mcp.example.test",
        expected_transport_url=_TRANSPORT_URL,
        profile_id="calendar-workload",
        profile_generation=6,
        profile_fingerprint=f"sha256:{'2' * 64}",
        installation_id="calendar",
        installation_generation=4,
    )


def _ticket(arguments: dict[str, Any] | None = None) -> McpInvocationAuthorization:
    return McpInvocationAuthorization(
        authorization_id="authz-1",
        authority_kind="workload",
        workload_principal_id="heartbeat:Alice",
        owner_user_id="owner-1",
        execution_agent_id="Alice",
        origin="heartbeat",
        installation_id="calendar",
        installation_generation=4,
        auth_profile_id="calendar-workload",
        profile_generation=6,
        target_binding_digest=_target_binding().digest,
        credential_generation=2,
        operation_method="tools/call",
        raw_tool_name="calendar.read",
        arguments_digest=digest_mcp_arguments(arguments or {}),
        required_scopes=frozenset({"calendar.read"}),
        auth_level="SafeAuto",
        policy_digest=f"sha256:{'1' * 64}",
        request_id="request-1",
        issued_at=100.0,
        expires_at=200.0,
    )


def _delegation_ticket() -> McpInvocationAuthorization:
    return McpInvocationAuthorization(
        authorization_id="authz-delegation-1",
        authority_kind="delegation",
        owner_user_id="owner-1",
        human_principal_id="owner-1",
        delegation_digest=f"sha256:{'4' * 64}",
        delegation_generation=3,
        execution_agent_id="Alice",
        origin="heartbeat",
        installation_id="calendar",
        installation_generation=4,
        auth_profile_id="calendar-user",
        profile_generation=6,
        target_binding_digest=_target_binding().digest,
        credential_generation=None,
        operation_method="tools/call",
        raw_tool_name="calendar.read",
        arguments_digest=digest_mcp_arguments({}),
        required_scopes=frozenset({"calendar.read"}),
        auth_level="SafeAuto",
        policy_digest=f"sha256:{'1' * 64}",
        request_id="request-1",
        issued_at=100.0,
        expires_at=200.0,
    )


def _wire_attempt(
    ticket: McpInvocationAuthorization,
    arguments: dict[str, Any] | None = None,
) -> McpWorkloadWireAttempt:
    actual_arguments = arguments or {}
    return McpWorkloadWireAttempt(
        authorization_id=ticket.authorization_id,
        egress_kind="mcp_bearer",
        method="tools/call",
        payload_digest=digest_mcp_arguments(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": ticket.raw_tool_name,
                    "arguments": actual_arguments,
                },
            }
        ),
        target_binding_digest=ticket.target_binding_digest,
        raw_tool_name=ticket.raw_tool_name,
        arguments_digest=digest_mcp_arguments(actual_arguments),
    )


def _material() -> Any:
    return _runtime().WorkloadAuthorityMaterial(
        authority_key=_ticket().handle_principal_key,
        snapshot=WorkloadMetadataSnapshot(
            profile_id="calendar-workload",
            profile_generation=6,
            profile_fingerprint=f"sha256:{'2' * 64}",
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test",
            token_endpoint="https://issuer.example.test/oauth/token",
            token_endpoint_auth_method="client_secret_basic",
            allow_private_network=False,
            verified_scopes=frozenset({"calendar.read"}),
            metadata_security_fingerprint=f"sha256:{'3' * 64}",
            verified_at=90.0,
        ),
        credential=WorkloadCredentialPayload(
            profile_id="calendar-workload",
            credential_id="calendar-secret",
            generation=2,
            client_id=SecretStr("client-id"),
            client_secret=SecretStr("client-secret"),
        ),
        target_binding=_target_binding(),
        allowed_private_cidrs=(),
    )


class _Authorizer:
    def __init__(
        self,
        *,
        fail_revalidation_at: int | None = None,
        expected_now: float | None = 150.0,
    ) -> None:
        self.revalidations = 0
        self.fail_revalidation_at = fail_revalidation_at
        self.expected_now = expected_now

    async def authorize_operation(self, **kwargs: Any) -> McpInvocationAuthorization:
        return _ticket(kwargs["arguments"])

    async def revalidate(
        self,
        ticket: McpInvocationAuthorization,
        *,
        now: float,
    ) -> Any:
        assert ticket.authorization_id.startswith("authz-")
        if self.expected_now is not None:
            assert now == self.expected_now
        self.revalidations += 1
        if self.revalidations == self.fail_revalidation_at:
            raise PermissionError("revoked")
        return _material()


class _Exchange:
    def __init__(self) -> None:
        self.calls = 0

    async def exchange(self, **kwargs: Any) -> WorkloadAccessToken:
        self.calls += 1
        if not await kwargs["authority_revalidator"]():
            raise PermissionError("revoked")
        return WorkloadAccessToken(
            access_token=SecretStr("opaque-access-token"),
            expires_at=190.0,
            granted_scopes=frozenset({"calendar.read"}),
        )


class _Permits:
    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.acked: list[str] = []

    async def issue(self, **kwargs: Any) -> Any:
        self.issued.append(kwargs)
        return SimpleNamespace(permit_id=f"permit-{len(self.issued)}")

    async def begin_send(self, permit: Any, **_kwargs: Any) -> None:
        self.started.append(permit.permit_id)

    async def release(self, permit_id: str) -> None:
        self.acked.append(permit_id)


class _Audit:
    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []

    async def accept_egress_intent(self, **kwargs: Any) -> None:
        self.intents.append(kwargs)


class _DelegatedAuthorizer:
    def __init__(self) -> None:
        self.revalidations = 0

    async def authorize_operation(self, **_kwargs: Any) -> McpInvocationAuthorization:
        return _delegation_ticket()

    async def revalidate(self, ticket: McpInvocationAuthorization, *, now: float) -> Any:
        assert ticket == _delegation_ticket()
        assert now == 150.0
        self.revalidations += 1
        return _runtime().WorkloadDelegatedAuthorityMaterial(
            authority_key=ticket.handle_principal_key,
            source_slot=SimpleNamespace(
                human_principal_id="owner-1",
                execution_agent_id="Alice",
                installation_id="calendar-user-source",
                installation_generation=9,
                auth_profile_id="calendar-user",
            ),
            source_grant_id="grant-1",
            source_grant_version=(2 if self.revalidations == 1 else 3),
            target_binding=_target_binding(),
        )


class _DelegatedBearerProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def delegated_bearer_credential_guard(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        binding = SimpleNamespace(
            http_method="POST",
            digest=f"sha256:{'c' * 64}",
            grant_type="refresh_token",
            grant_id="grant-1",
            grant_version=2,
        )
        async with kwargs["refresh_egress_guard"](binding):
            pass
        assert await kwargs["authorization_fence"]()
        yield SimpleNamespace(
            grant_id="grant-1",
            grant_version=3,
            authorization_header=SecretStr("Bearer delegated-access-token"),
            allowed_private_cidrs=(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_kind", "origin"),
    [("foreground", "heartbeat"), ("heartbeat", "foreground")],
)
async def test_workload_requires_server_execution_kind_and_origin(
    execution_kind: str,
    origin: str,
) -> None:
    authorizer = _Authorizer()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=authorizer,
        exchange=_Exchange(),
        permits=_Permits(),
        audit=_Audit(),
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )
    context = _context().model_copy(
        update={"execution_kind": execution_kind, "origin": origin}
    )

    with pytest.raises(_runtime().WorkloadRuntimeDenied):
        await runtime.authorize_operation(
            server_name="calendar",
            request_context=context,
            installation_generation=4,
            operation="tools/call",
            selector="calendar.read",
            arguments={},
        )

    assert authorizer.revalidations == 0


@pytest.mark.asyncio
async def test_access_token_is_memory_only_and_every_send_gets_fresh_permit() -> None:
    authorizer = _Authorizer()
    exchange = _Exchange()
    permits = _Permits()
    audit = _Audit()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=authorizer,
        exchange=exchange,
        permits=permits,
        audit=audit,
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )
    ticket = await runtime.authorize_operation(
        server_name="calendar",
        request_context=_context(),
        installation_generation=4,
        operation="tools/call",
        selector="calendar.read",
        arguments={},
    )

    headers: list[str] = []
    for index in range(2):
        current_ticket = ticket.model_copy(
            update={"authorization_id": f"authz-{index + 1}"}
        )
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=current_ticket,
            wire_attempt=_wire_attempt(current_ticket),
            transport_url=_TRANSPORT_URL,
        ) as credential:
            headers.append(credential.authorization_header.get_secret_value())

    assert headers == ["Bearer opaque-access-token"] * 2
    assert exchange.calls == 1
    assert authorizer.revalidations == 7
    assert len(permits.issued) == 2
    assert permits.started == permits.acked == ["permit-1", "permit-2"]
    assert [intent["egress_kind"] for intent in audit.intents] == [
        "mcp_bearer",
        "mcp_bearer",
    ]
    await runtime.aclose()
    assert runtime.cached_token_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual_transport_url",
    [
        "https://other.example.test/mcp",
        "https://mcp.example.test:444/mcp",
        "https://mcp.example.test/other",
        "http://mcp.example.test/mcp",
    ],
)
async def test_transport_mismatch_denies_before_token_exchange_or_send(
    actual_transport_url: str,
) -> None:
    exchange = _Exchange()
    permits = _Permits()
    audit = _Audit()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=_Authorizer(),
        exchange=exchange,
        permits=permits,
        audit=audit,
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )

    with pytest.raises(_runtime().WorkloadRuntimeDenied):
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=_ticket(),
            wire_attempt=_wire_attempt(_ticket()),
            transport_url=actual_transport_url,
        ):
            pytest.fail("mismatched transport obtained a bearer")

    assert exchange.calls == 0
    assert permits.issued == []
    assert audit.intents == []


@pytest.mark.asyncio
async def test_exact_tool_ticket_cannot_authenticate_a_second_wire_send() -> None:
    permits = _Permits()
    audit = _Audit()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=_Authorizer(),
        exchange=_Exchange(),
        permits=permits,
        audit=audit,
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )
    ticket = _ticket()

    async with runtime.workload_bearer_credential_guard(
        server_name="calendar",
        authorization=ticket,
        wire_attempt=_wire_attempt(ticket),
        transport_url=_TRANSPORT_URL,
    ):
        pass
    with pytest.raises(_runtime().WorkloadRuntimeDenied):
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=ticket,
            wire_attempt=_wire_attempt(ticket),
            transport_url=_TRANSPORT_URL,
        ):
            pytest.fail("one tool ticket authenticated two wire sends")

    assert len(permits.issued) == 1
    assert len(audit.intents) == 1


@pytest.mark.asyncio
async def test_revocation_between_token_resolution_and_send_denies_without_permit() -> None:
    authorizer = _Authorizer(fail_revalidation_at=2)
    permits = _Permits()
    audit = _Audit()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=authorizer,
        exchange=_Exchange(),
        permits=permits,
        audit=audit,
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )

    with pytest.raises(PermissionError, match="revoked"):
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=_ticket(),
            wire_attempt=_wire_attempt(_ticket()),
            transport_url=_TRANSPORT_URL,
        ):
            pytest.fail("stale authorization reached the transport")

    assert permits.issued == []
    assert audit.intents == []


@pytest.mark.asyncio
async def test_revocation_after_permit_issue_denies_before_begin_send() -> None:
    authorizer = _Authorizer(fail_revalidation_at=4)
    permits = _Permits()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=authorizer,
        exchange=_Exchange(),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )

    with pytest.raises(PermissionError, match="revoked"):
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=_ticket(),
            wire_attempt=_wire_attempt(_ticket()),
            transport_url=_TRANSPORT_URL,
        ):
            pytest.fail("credential must not reach transport")

    assert len(permits.issued) == 1
    assert permits.started == []
    assert permits.acked == ["permit-1"]


@pytest.mark.asyncio
async def test_delegation_uses_p8a_bearer_owner_and_accepts_same_grant_refresh() -> None:
    authorizer = _DelegatedAuthorizer()
    provider = _DelegatedBearerProvider()
    permits = _Permits()
    audit = _Audit()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=authorizer,
        exchange=_Exchange(),
        delegated_bearer_provider=provider,
        permits=permits,
        audit=audit,
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )

    async with runtime.workload_bearer_credential_guard(
        server_name="calendar",
        authorization=_delegation_ticket(),
        wire_attempt=_wire_attempt(_delegation_ticket()),
        transport_url=_TRANSPORT_URL,
    ) as credential:
        assert credential.authorization_header.get_secret_value() == (
            "Bearer delegated-access-token"
        )

    assert runtime.cached_token_count == 0
    assert authorizer.revalidations >= 2
    assert len(provider.calls) == 1
    assert permits.started == permits.acked == ["permit-1", "permit-2"]
    assert [intent["egress_kind"] for intent in audit.intents] == [
        "delegated_refresh",
        "mcp_bearer",
    ]
    assert permits.issued[0]["method"] == "POST"
    assert permits.issued[0]["payload_digest"] == f"sha256:{'c' * 64}"


@pytest.mark.asyncio
async def test_consumed_ticket_replay_index_is_bounded_and_expiry_pruned() -> None:
    now = 150.0
    permits = _Permits()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=_Authorizer(expected_now=None),
        exchange=_Exchange(),
        permits=permits,
        audit=_Audit(),
        clock=lambda: now,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
        ticket_replay_capacity=2,
    )

    for index in range(2):
        ticket = _ticket().model_copy(
            update={
                "authorization_id": f"authz-bounded-{index}",
                "expires_at": 160.0,
            }
        )
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=ticket,
            wire_attempt=_wire_attempt(ticket),
            transport_url=_TRANSPORT_URL,
        ):
            pass
    assert runtime.consumed_ticket_count == 2

    full_ticket = _ticket().model_copy(
        update={"authorization_id": "authz-bounded-full"}
    )
    with pytest.raises(_runtime().WorkloadRuntimeDenied):
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=full_ticket,
            wire_attempt=_wire_attempt(full_ticket),
            transport_url=_TRANSPORT_URL,
        ):
            pytest.fail("replay index capacity was bypassed")

    now = 161.0
    fresh = _ticket().model_copy(
        update={
            "authorization_id": "authz-bounded-fresh",
            "issued_at": 160.0,
            "expires_at": 300.0,
        }
    )
    async with runtime.workload_bearer_credential_guard(
        server_name="calendar",
        authorization=fresh,
        wire_attempt=_wire_attempt(fresh),
        transport_url=_TRANSPORT_URL,
    ):
        pass
    assert runtime.consumed_ticket_count == 1


@pytest.mark.asyncio
async def test_runtime_cancels_registered_request_task_and_releases_permit() -> None:
    permits = _Permits()
    runtime = _runtime().WorkloadOAuthRuntime(
        authorizer=_Authorizer(),
        exchange=_Exchange(),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 150.0,
        send_permit_ttl=5.0,
        token_expiry_skew=10.0,
    )
    entered = asyncio.Event()

    async def invoke() -> None:
        async with runtime.workload_bearer_credential_guard(
            server_name="calendar",
            authorization=_ticket(),
            wire_attempt=_wire_attempt(_ticket()),
            transport_url=_TRANSPORT_URL,
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(invoke())
    await entered.wait()
    await runtime.cancel_tasks(frozenset({"request-1"}))

    assert task.cancelled()
    assert permits.acked == ["permit-1"]
