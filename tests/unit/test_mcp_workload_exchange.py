"""P8-B Slice 3：client-credentials exchange fail-closed contract。"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from src.protocols.mcp.canonical import (
    McpInvocationAuthorization,
    McpRequestOrigin,
)
from src.protocols.mcp.oauth.outbound import OAuthHttpClient
from src.protocols.mcp.oauth.workload.models import (
    WorkloadCredentialPayload,
    WorkloadMetadataSnapshot,
)


def _exchange() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.exchange"
    assert importlib.util.find_spec(module_name) is not None, "exchange seam is missing"
    return importlib.import_module(module_name)


def _snapshot(*, method: str = "client_secret_basic") -> WorkloadMetadataSnapshot:
    return WorkloadMetadataSnapshot(
        profile_id="calendar-workload",
        profile_generation=3,
        profile_fingerprint=f"sha256:{'1' * 64}",
        issuer="https://issuer.example.test",
        resource="https://mcp.example.test",
        token_endpoint="https://issuer.example.test/oauth/token",
        token_endpoint_auth_method=method,
        allow_private_network=False,
        verified_scopes=frozenset({"calendar.read", "calendar.write"}),
        metadata_security_fingerprint=f"sha256:{'2' * 64}",
        verified_at=100.0,
    )


def _credential() -> WorkloadCredentialPayload:
    return WorkloadCredentialPayload(
        profile_id="calendar-workload",
        credential_id="calendar-secret",
        generation=4,
        client_id=SecretStr("client-1"),
        client_secret=SecretStr("secret-1"),
    )


class _Endpoint:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def post_token(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _Permits:
    def __init__(
        self,
        *,
        deny_attempt: int | None = None,
        fail_begin_send: bool = False,
    ) -> None:
        self.deny_attempt = deny_attempt
        self.fail_begin_send = fail_begin_send
        self.issued: list[Any] = []
        self.acknowledged: list[str] = []

    async def issue(self, **kwargs: Any) -> Any:
        if kwargs["attempt_number"] == self.deny_attempt:
            raise importlib.import_module(
                "src.protocols.mcp.oauth.workload.permit"
            ).WorkloadEgressDenied()
        permit_type = importlib.import_module(
            "src.protocols.mcp.oauth.workload.permit"
        ).WorkloadEgressPermit
        permit = permit_type(
            permit_id=f"permit-{kwargs['attempt_number']}",
            boot_id="boot-a",
            authority_epoch=7,
            **kwargs,
        )
        self.issued.append(permit)
        return permit

    async def begin_send(self, permit: Any, **kwargs: Any) -> None:
        if self.fail_begin_send:
            raise RuntimeError("begin-send-denied")
        assert permit.task_id == kwargs["task_id"]
        assert permit.ticket_id == kwargs["ticket_id"]
        assert permit.method == kwargs["method"]
        assert permit.payload_digest == kwargs["payload_digest"]

    async def release(self, permit_id: str) -> None:
        self.acknowledged.append(permit_id)


class _Audit:
    def __init__(self, *, fail_attempt: int | None = None) -> None:
        self.fail_attempt = fail_attempt
        self.attempts: list[int] = []
        self.intents: list[dict[str, Any]] = []

    async def accept_egress_intent(self, **kwargs: Any) -> None:
        attempt = kwargs["attempt_number"]
        self.attempts.append(attempt)
        self.intents.append(kwargs)
        if attempt == self.fail_attempt:
            raise RuntimeError("audit-unavailable")


def _ticket(**updates: Any) -> McpInvocationAuthorization:
    values: dict[str, Any] = {
        "authorization_id": "ticket-1",
        "authority_kind": "workload",
        "workload_principal_id": "workload-1",
        "owner_user_id": "owner-1",
        "execution_agent_id": "Alice",
        "origin": McpRequestOrigin.HEARTBEAT,
        "installation_id": "calendar-server",
        "installation_generation": 5,
        "auth_profile_id": "calendar-workload",
        "profile_generation": 3,
        "target_binding_digest": f"sha256:{'c' * 64}",
        "credential_generation": 4,
        "raw_tool_name": "calendar.read",
        "arguments_digest": f"sha256:{'a' * 64}",
        "required_scopes": frozenset({"calendar.read"}),
        "auth_level": "SafeAuto",
        "policy_digest": f"sha256:{'b' * 64}",
        "request_id": "request-1",
        "issued_at": 190.0,
        "expires_at": 300.0,
    }
    values.update(updates)
    return McpInvocationAuthorization(**values)


async def _async_true() -> bool:
    return True


@pytest.mark.asyncio
async def test_missing_verified_snapshot_never_calls_token_endpoint() -> None:
    endpoint = _Endpoint([])
    exchanger = _exchange().WorkloadTokenExchanger(endpoint=endpoint)

    with pytest.raises(_exchange().WorkloadTokenExchangeDenied):
        await exchanger.exchange(
            snapshot=None,
            credential=_credential(),
            requested_scopes=frozenset({"calendar.read"}),
            now=200.0,
        )

    assert endpoint.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_exact_client_auth_and_opaque_bearer_are_preserved(method: str) -> None:
    response = _exchange().WorkloadTokenEndpointResponse(
        status_code=200,
        payload={
            "access_token": "opaque.not-a-jwt",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "calendar.read",
        },
    )
    endpoint = _Endpoint([response])
    token = await _exchange().WorkloadTokenExchanger(endpoint=endpoint).exchange(
        snapshot=_snapshot(method=method),
        credential=_credential(),
        requested_scopes=frozenset({"calendar.read"}),
        now=200.0,
    )

    assert token.access_token.get_secret_value() == "opaque.not-a-jwt"
    assert token.expires_at == 500.0
    assert token.granted_scopes == frozenset({"calendar.read"})
    call = endpoint.calls[0]
    if method == "client_secret_basic":
        assert call["basic_auth"] == ("client-1", "secret-1")
        assert "client_secret" not in call["form"]
    else:
        assert call["basic_auth"] is None
        assert call["form"]["client_id"] == "client-1"
        assert (
            call["form"]["client_secret"]
            == _credential().client_secret.get_secret_value()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "access_token": "token",
            "refresh_token": "forbidden",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "calendar.read",
        },
        {
            "access_token": "token",
            "token_type": "DPoP",
            "expires_in": 300,
            "scope": "calendar.read",
        },
        {
            "access_token": "token",
            "token_type": "Bearer",
            "expires_in": 0,
            "scope": "calendar.read",
        },
        {
            "access_token": "token",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "calendar.write",
        },
    ],
)
async def test_invalid_or_under_scoped_response_fails_closed(
    payload: dict[str, Any],
) -> None:
    endpoint = _Endpoint(
        [_exchange().WorkloadTokenEndpointResponse(status_code=200, payload=payload)]
    )

    with pytest.raises(_exchange().WorkloadTokenResponseError):
        await _exchange().WorkloadTokenExchanger(endpoint=endpoint).exchange(
            snapshot=_snapshot(),
            credential=_credential(),
            requested_scopes=frozenset({"calendar.read"}),
            now=200.0,
        )


@pytest.mark.asyncio
async def test_4xx_is_not_retryable_and_5xx_or_network_is_retryable() -> None:
    rejected = _Endpoint(
        [_exchange().WorkloadTokenEndpointResponse(status_code=401, payload={})]
    )
    unavailable = _Endpoint(
        [_exchange().WorkloadTokenEndpointResponse(status_code=503, payload={})]
    )

    with pytest.raises(_exchange().WorkloadTokenExchangeRejected):
        await _exchange().WorkloadTokenExchanger(endpoint=rejected).exchange(
            snapshot=_snapshot(),
            credential=_credential(),
            requested_scopes=frozenset({"calendar.read"}),
            now=200.0,
        )
    with pytest.raises(_exchange().WorkloadTokenExchangeRetryable):
        await _exchange().WorkloadTokenExchanger(endpoint=unavailable).exchange(
            snapshot=_snapshot(),
            credential=_credential(),
            requested_scopes=frozenset({"calendar.read"}),
            now=200.0,
        )

    assert len(rejected.calls) == 1
    assert len(unavailable.calls) == 1


@pytest.mark.asyncio
async def test_exchanger_does_not_hide_or_retry_transport_failure() -> None:
    endpoint = _Endpoint([_exchange().WorkloadTokenTransportError()])

    with pytest.raises(_exchange().WorkloadTokenExchangeRetryable):
        await _exchange().WorkloadTokenExchanger(endpoint=endpoint).exchange(
            snapshot=_snapshot(),
            credential=_credential(),
            requested_scopes=frozenset({"calendar.read"}),
            now=200.0,
        )

    assert len(endpoint.calls) == 1


@pytest.mark.asyncio
async def test_retry_coordinator_uses_a_fresh_permit_for_single_5xx_retry() -> None:
    endpoint = _Endpoint(
        [
            _exchange().WorkloadTokenEndpointResponse(status_code=503, payload={}),
            _exchange().WorkloadTokenEndpointResponse(
                status_code=200,
                payload={
                    "access_token": "opaque-token",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "scope": "calendar.read",
                },
            ),
        ]
    )
    permits = _Permits()
    audit = _Audit()
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=audit,
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    token = await coordinator.exchange(
        ticket=_ticket(),
        task_id="task-1",
        snapshot=_snapshot(),
        credential=_credential(),
        authority_revalidator=lambda: _async_true(),
    )

    assert token.access_token.get_secret_value() == "opaque-token"
    assert [permit.attempt_number for permit in permits.issued] == [1, 2]
    assert permits.issued[0].permit_id != permits.issued[1].permit_id
    assert permits.acknowledged == ["permit-1", "permit-2"]
    assert audit.attempts == [1, 2]
    assert len(endpoint.calls) == 2


@pytest.mark.asyncio
async def test_token_exchange_permit_binds_final_http_request_not_tool_arguments() -> None:
    async def run(
        *,
        snapshot: WorkloadMetadataSnapshot,
        credential: WorkloadCredentialPayload,
        ticket: McpInvocationAuthorization,
    ) -> tuple[Any, Any, dict[str, Any]]:
        endpoint = _Endpoint(
            [
                _exchange().WorkloadTokenEndpointResponse(
                    status_code=200,
                    payload={
                        "access_token": "opaque-token",
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "scope": " ".join(sorted(ticket.required_scopes)),
                    },
                )
            ]
        )
        permits = _Permits()
        audit = _Audit()
        coordinator = _exchange().WorkloadTokenExchangeCoordinator(
            exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
            permits=permits,
            audit=audit,
            clock=lambda: 200.0,
            attempt_ttl=10.0,
        )
        await coordinator.exchange(
            ticket=ticket,
            task_id="task-1",
            snapshot=snapshot,
            credential=credential,
            authority_revalidator=lambda: _async_true(),
        )
        return permits.issued[0], audit.intents[0]["wire_attempt"], endpoint.calls[0]

    cases = [
        (_snapshot(), _credential(), _ticket()),
        (
            _snapshot().model_copy(
                update={"token_endpoint": "https://issuer.example.test/oauth/token-v2"}
            ),
            _credential(),
            _ticket(),
        ),
        (
            _snapshot(method="client_secret_post"),
            _credential(),
            _ticket(),
        ),
        (
            _snapshot(),
            _credential().model_copy(update={"generation": 5}),
            _ticket(credential_generation=5),
        ),
    ]
    results = [
        await run(snapshot=snapshot, credential=credential, ticket=ticket)
        for snapshot, credential, ticket in cases
    ]

    digests = set()
    for permit, wire_attempt, request in results:
        assert permit.method == wire_attempt.method == "POST"
        assert permit.payload_digest == wire_attempt.payload_digest
        assert permit.payload_digest != _ticket().arguments_digest
        assert request["endpoint"].startswith("https://issuer.example.test/")
        digests.add(permit.payload_digest)
    assert len(digests) == len(cases)


@pytest.mark.asyncio
async def test_retry_stops_if_fresh_second_permit_is_denied() -> None:
    endpoint = _Endpoint(
        [_exchange().WorkloadTokenEndpointResponse(status_code=503, payload={})]
    )
    permits = _Permits(deny_attempt=2)
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    with pytest.raises(
        importlib.import_module(
            "src.protocols.mcp.oauth.workload.permit"
        ).WorkloadEgressDenied
    ):
        await coordinator.exchange(
            ticket=_ticket(),
            task_id="task-1",
            snapshot=_snapshot(),
            credential=_credential(),
            authority_revalidator=lambda: _async_true(),
        )

    assert len(endpoint.calls) == 1
    assert permits.acknowledged == ["permit-1"]


@pytest.mark.asyncio
async def test_generation_mismatch_denies_before_permit_or_network() -> None:
    endpoint = _Endpoint([])
    permits = _Permits()
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    with pytest.raises(_exchange().WorkloadTokenExchangeDenied):
        await coordinator.exchange(
            ticket=_ticket(profile_generation=99),
            task_id="task-1",
            snapshot=_snapshot(),
            credential=_credential(),
            authority_revalidator=lambda: _async_true(),
        )

    assert permits.issued == []
    assert endpoint.calls == []


@pytest.mark.asyncio
async def test_audit_failure_denies_before_token_permit_or_network() -> None:
    endpoint = _Endpoint([])
    permits = _Permits()
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=_Audit(fail_attempt=1),
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    with pytest.raises(RuntimeError, match="audit-unavailable"):
        await coordinator.exchange(
            ticket=_ticket(),
            task_id="task-1",
            snapshot=_snapshot(),
            credential=_credential(),
            authority_revalidator=lambda: _async_true(),
        )

    assert permits.issued == []
    assert endpoint.calls == []


@pytest.mark.asyncio
async def test_begin_send_failure_releases_issued_permit_without_network() -> None:
    endpoint = _Endpoint([])
    permits = _Permits(fail_begin_send=True)
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    with pytest.raises(RuntimeError, match="begin-send-denied"):
        await coordinator.exchange(
            ticket=_ticket(),
            task_id="task-1",
            snapshot=_snapshot(),
            credential=_credential(),
            authority_revalidator=lambda: _async_true(),
        )

    assert permits.acknowledged == ["permit-1"]
    assert endpoint.calls == []


@pytest.mark.asyncio
async def test_authority_revoked_after_permit_issue_denies_before_begin_send() -> None:
    endpoint = _Endpoint([])
    permits = _Permits()
    coordinator = _exchange().WorkloadTokenExchangeCoordinator(
        exchanger=_exchange().WorkloadTokenExchanger(endpoint=endpoint),
        permits=permits,
        audit=_Audit(),
        clock=lambda: 200.0,
        attempt_ttl=10.0,
    )

    async def revoked() -> bool:
        return False

    with pytest.raises(_exchange().WorkloadTokenExchangeDenied):
        await coordinator.exchange(
            ticket=_ticket(),
            task_id="task-1",
            snapshot=_snapshot(),
            credential=_credential(),
            authority_revalidator=revoked,
        )

    assert len(permits.issued) == 1
    assert permits.acknowledged == ["permit-1"]
    assert endpoint.calls == []


@pytest.mark.asyncio
async def test_http_endpoint_uses_basic_auth_and_bounded_json_adapter() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "access_token": "opaque-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "calendar.read",
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        adapter = _exchange().WorkloadOAuthHttpEndpoint(
            outbound=OAuthHttpClient(client=client)
        )
        response = await adapter.post_token(
            endpoint="https://issuer.example.test/token",
            form={"grant_type": "client_credentials"},
            basic_auth=("client-1", "secret-1"),
            private_network_cidrs=(),
        )

    assert response.status_code == 200
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert b"client_secret" not in requests[0].content


@pytest.mark.asyncio
async def test_http_endpoint_rejects_duplicate_success_fields() -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"access_token":"a","access_token":"b"}',
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        adapter = _exchange().WorkloadOAuthHttpEndpoint(
            outbound=OAuthHttpClient(client=client)
        )
        with pytest.raises(_exchange().WorkloadTokenEndpointProtocolError):
            await adapter.post_token(
                endpoint="https://issuer.example.test/token",
                form={"grant_type": "client_credentials"},
                basic_auth=("client-1", "secret-1"),
                private_network_cidrs=(),
            )


@pytest.mark.asyncio
async def test_http_endpoint_maps_pre_response_transport_failure_only() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(type(request).__name__, request=request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        adapter = _exchange().WorkloadOAuthHttpEndpoint(
            outbound=OAuthHttpClient(client=client)
        )
        with pytest.raises(_exchange().WorkloadTokenTransportError):
            await adapter.post_token(
                endpoint="https://issuer.example.test/token",
                form={"grant_type": "client_credentials"},
                basic_auth=("client-1", "secret-1"),
                private_network_cidrs=(),
            )


@pytest.mark.asyncio
async def test_private_network_profile_passes_only_exact_cidr_policy() -> None:
    class _CapturingOutbound:
        private_networks: tuple[str, ...] = ()

        async def post_client_credentials_token(self, **kwargs: Any) -> tuple[dict, int]:
            policy = kwargs["private_network_policy"]
            self.private_networks = tuple(
                str(network) for network in policy(kwargs["token_endpoint"])
            )
            return {}, 500

    outbound = _CapturingOutbound()
    adapter = _exchange().WorkloadOAuthHttpEndpoint(outbound=outbound)

    response = await adapter.post_token(
        endpoint="https://issuer.example.test/token",
        form={"grant_type": "client_credentials"},
        basic_auth=("client-1", "secret-1"),
        private_network_cidrs=("10.0.0.0/8",),
    )

    assert response.status_code == 500
    assert outbound.private_networks == ("10.0.0.0/8",)
