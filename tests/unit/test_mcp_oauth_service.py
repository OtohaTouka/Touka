"""P8-A ConnectorAuthService authorization-code orchestration。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr


def _modules():
    return (
        importlib.import_module("src.protocols.mcp.oauth.models"),
        importlib.import_module("src.protocols.mcp.oauth.profile"),
        importlib.import_module("src.protocols.mcp.oauth.protocol"),
        importlib.import_module("src.protocols.mcp.oauth.service"),
    )


def _slot(models):
    return models.OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=1,
        auth_profile_id="profile-a",
    )


def _profile(profile_module):
    return profile_module.OAuthProfile(
        profile_id="profile-a",
        generation=1,
        enabled=True,
        callback_binding="form_post",
        trusted_public_origin="https://chat.example.test",
        trusted_issuers=("https://issuer.example.test",),
        client_id="client-a",
        scope_rules=(
            {
                "operation": "tools/call",
                "selector": "search",
                "scopes": ["tools.read"],
            },
        ),
    )


def _metadata(service_module):
    return service_module.OAuthAuthorizationMetadata(
        issuer="https://issuer.example.test",
        authorization_endpoint="https://issuer.example.test/authorize",
        token_endpoint="https://issuer.example.test/token",
        registration_endpoint=None,
        code_challenge_methods_supported=("S256",),
        response_modes_supported=("form_post",),
        authorization_response_iss_parameter_supported=True,
        security_fingerprint="metadata-fingerprint",
    )


class _Repository:
    def __init__(self):
        self.events: list[str] = []
        self.transaction = None
        self.commit_result = True
        self.pending = None
        self.snapshot = None
        self.revoking = None

    async def find_pending_for_slot(self, _slot, **_kwargs):
        return self.pending

    async def create_transaction(self, **kwargs):
        self.events.append("created")
        self.raw_state = kwargs["raw_state"]
        payload = kwargs["payload"]
        self.transaction = SimpleNamespace(
            transaction_id="transaction-a",
            operation_id=kwargs["operation_id"],
            slot_digest="slot-digest",
            slot_version=1,
            status="pending",
            expires_at=kwargs["expires_at"],
            payload=payload,
        )
        return self.transaction

    async def cancel_transaction(self, transaction_id):
        self.events.append(f"cancelled:{transaction_id}")
        self.pending = None
        return True

    async def consume_transaction(self, raw_state, **kwargs):
        self.events.append("consumed")
        if raw_state != self.raw_state:
            return None
        return self.transaction

    async def mark_transaction_error(self, transaction_id):
        self.events.append(f"error:{transaction_id}")

    async def commit_authorization(self, **kwargs):
        self.events.append("committed")
        self.binding = kwargs["binding"]
        self.tokens = kwargs["tokens"]
        return self.commit_result

    async def get_slot_snapshot(self, _slot, **_kwargs):
        return self.snapshot

    async def begin_disconnect(self, _slot, **_kwargs):
        self.events.append("disconnect_started")
        return self.revoking

    async def complete_disconnect(self, grant_id, **_kwargs):
        self.events.append(f"disconnect_completed:{grant_id}")
        return True


class _Outbound:
    def __init__(self, models, *, returned_scope: str | None = None):
        self.models = models
        self.returned_scope = returned_scope
        self.events: list[str] = []

    async def exchange_authorization_code(self, **kwargs):
        self.events.append("exchange")
        assert kwargs["code"] == "code-a"
        assert kwargs["code_verifier"] == "verifier-value"
        return self.models.OAuthTokenSet(
            access_token=SecretStr("access-canary"),
            refresh_token=SecretStr("refresh-canary"),
            token_type="Bearer",
            expires_at=2000.0,
            granted_scopes=frozenset(
                self.returned_scope.split(" ") if self.returned_scope else {"tools.read"}
            ),
        ), self.returned_scope

    async def revoke_token(self, **kwargs):
        self.events.append("revoke")


@pytest.mark.asyncio
async def test_start_persists_before_returning_s256_authorization_url():
    models, profile_module, _protocol, service_module = _modules()
    repository = _Repository()
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=_Outbound(models),
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: (
            "raw-state" if not hasattr(repository, "raw_state") else "verifier-value"
        ),
    )

    result = await service.start(
        slot=_slot(models),
        profile=_profile(profile_module),
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )

    assert repository.events == ["created"]
    assert "state=raw-state" in result.authorization_url
    assert "code_challenge_method=S256" in result.authorization_url
    assert "verifier-value" not in result.authorization_url
    assert result.operation_id


@pytest.mark.asyncio
async def test_callback_consumes_before_exchange_and_commits_without_exposing_tokens():
    models, profile_module, protocol_module, service_module = _modules()
    repository = _Repository()
    outbound = _Outbound(models)
    random_values = iter(("raw-state", "verifier-value", "operation-a", "grant-a"))
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: next(random_values),
    )
    profile = _profile(profile_module)
    metadata = _metadata(service_module)
    await service.start(
        slot=_slot(models),
        profile=profile,
        metadata=metadata,
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )
    callback = protocol_module.OAuthCallbackPayload(
        state="raw-state",
        code="code-a",
        issuer="https://issuer.example.test",
    )

    result = await service.complete_callback(
        callback=callback,
        redirect_route="/mcp/oauth/callback",
        profile=profile,
        metadata=metadata,
        slot=_slot(models),
    )

    assert repository.events == ["created", "consumed", "committed"]
    assert outbound.events == ["exchange"]
    assert result.status == "connected"
    assert "access-canary" not in result.model_dump_json()
    assert "refresh-canary" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_issuer_mismatch_terminalizes_transaction_without_exchange():
    models, profile_module, protocol_module, service_module = _modules()
    repository = _Repository()
    outbound = _Outbound(models)
    values = iter(("raw-state", "verifier-value", "operation-a"))
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: next(values),
    )
    profile = _profile(profile_module)
    metadata = _metadata(service_module)
    await service.start(
        slot=_slot(models),
        profile=profile,
        metadata=metadata,
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )

    with pytest.raises(service_module.OAuthIssuerMismatchError):
        await service.complete_callback(
            callback=protocol_module.OAuthCallbackPayload(
                state="raw-state",
                code="code-a",
                issuer="https://evil.example.test",
            ),
            redirect_route="/mcp/oauth/callback",
            profile=profile,
            metadata=metadata,
            slot=_slot(models),
        )

    assert outbound.events == []
    assert repository.events[-1] == "error:transaction-a"


@pytest.mark.asyncio
async def test_stale_commit_never_returns_connected():
    models, profile_module, protocol_module, service_module = _modules()
    repository = _Repository()
    repository.commit_result = False
    outbound = _Outbound(models)
    values = iter(("raw-state", "verifier-value", "operation-a"))
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: next(values),
    )
    profile = _profile(profile_module)
    metadata = _metadata(service_module)
    await service.start(
        slot=_slot(models),
        profile=profile,
        metadata=metadata,
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )

    with pytest.raises(service_module.OAuthStaleAuthorizationError):
        await service.complete_callback(
            callback=protocol_module.OAuthCallbackPayload(
                state="raw-state",
                code="code-a",
                issuer="https://issuer.example.test",
            ),
            redirect_route="/mcp/oauth/callback",
            profile=profile,
            metadata=metadata,
            slot=_slot(models),
        )


@pytest.mark.asyncio
async def test_repeated_start_reuses_pending_operation_without_recovering_raw_state():
    models, profile_module, _protocol_module, service_module = _modules()
    repository = _Repository()
    repository.pending = SimpleNamespace(
        operation_id="existing-operation",
        expires_at=1500.0,
    )
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=_Outbound(models),
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: pytest.fail("must not generate a second state"),
    )
    result = await service.start(
        slot=_slot(models),
        profile=_profile(profile_module),
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )
    assert result.operation_id == "existing-operation"
    assert result.authorization_url is None
    assert result.reused is True


@pytest.mark.asyncio
async def test_start_cancels_stale_pending_binding_before_creating_replacement():
    models, profile_module, _protocol_module, service_module = _modules()
    repository = _Repository()
    values = iter(
        (
            "state-one",
            "verifier-one",
            "operation-one",
            "state-two",
            "verifier-two",
            "operation-two",
        )
    )
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=_Outbound(models),
        epoch_clock=lambda: 1000.0,
        random_urlsafe=lambda _size: next(values),
    )
    profile = _profile(profile_module)
    await service.start(
        slot=_slot(models),
        profile=profile,
        metadata=_metadata(service_module),
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )
    repository.pending = repository.transaction
    drifted = _metadata(service_module).model_copy(
        update={"security_fingerprint": "drifted-fingerprint"}
    )

    replacement = await service.start(
        slot=_slot(models),
        profile=profile,
        metadata=drifted,
        resource="https://mcp.example.test/mcp",
        operation_scopes=frozenset({"tools.read"}),
        redirect_route="/mcp/oauth/callback",
        redirect_uri="https://chat.example.test/mcp/oauth/callback",
    )

    assert replacement.operation_id == "operation-two"
    assert replacement.reused is False
    assert repository.events == [
        "created",
        "cancelled:transaction-a",
        "created",
    ]


@pytest.mark.asyncio
async def test_status_is_derived_from_repository_snapshot():
    models, _profile_module, _protocol_module, service_module = _modules()
    repository = _Repository()
    repository.snapshot = models.OAuthSlotSnapshot(
        grant_status=models.OAuthGrantStatus.REFRESHING,
        access_expires_at=2000.0,
        has_refresh=True,
    )
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=_Outbound(models),
        epoch_clock=lambda: 1000.0,
    )
    status = await service.status(slot=_slot(models), enabled=True)
    assert status.status == "refreshing"
    assert status.expires_at == 2000.0


@pytest.mark.asyncio
async def test_disconnect_revokes_then_tombstones_exact_grant():
    models, _profile_module, _protocol_module, service_module = _modules()
    repository = _Repository()
    repository.revoking = models.OAuthGrantRecord(
        grant_id="grant-a",
        slot_digest="slot-digest",
        grant_version=3,
        status=models.OAuthGrantStatus.REVOKING,
        binding=_binding_for_disconnect(models),
        tokens=models.OAuthTokenSet(
            access_token=SecretStr("access-canary"),
            refresh_token=SecretStr("refresh-canary"),
            token_type="Bearer",
            expires_at=2000.0,
            granted_scopes=frozenset({"tools.read"}),
        ),
    )
    outbound = _Outbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
    )
    result = await service.disconnect(slot=_slot(models))
    assert result.status == "auth_required"
    assert outbound.events == ["revoke"]
    assert repository.events == ["disconnect_started", "disconnect_completed:grant-a"]


@pytest.mark.asyncio
async def test_disconnect_revocation_retry_is_bounded_and_uses_injected_backoff():
    models, _profile_module, _protocol_module, service_module = _modules()
    repository = _Repository()
    repository.revoking = models.OAuthGrantRecord(
        grant_id="grant-a",
        slot_digest="slot-digest",
        grant_version=3,
        status=models.OAuthGrantStatus.REVOKING,
        binding=_binding_for_disconnect(models),
        tokens=models.OAuthTokenSet(
            access_token=SecretStr("access-canary"),
            refresh_token=SecretStr("refresh-canary"),
            token_type="Bearer",
            expires_at=2000.0,
            granted_scopes=frozenset({"tools.read"}),
        ),
    )

    class FlakyOutbound(_Outbound):
        async def revoke_token(self, **kwargs):
            self.events.append("revoke")
            if len(self.events) < 3:
                raise RuntimeError()

    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    outbound = FlakyOutbound(models)
    service = service_module.ConnectorAuthService(
        repository=repository,
        outbound=outbound,
        epoch_clock=lambda: 1000.0,
        sleeper=sleeper,
        retry_jitter=lambda: 1.0,
    )

    result = await service.disconnect(slot=_slot(models))

    assert result.status == "auth_required"
    assert outbound.events == ["revoke", "revoke", "revoke"]
    assert delays == [0.25, 0.5]
    assert repository.events[-1] == "disconnect_completed:grant-a"


def _binding_for_disconnect(models):
    return models.OAuthGrantBinding(
        issuer="https://issuer.example.test",
        resource="https://mcp.example.test/mcp",
        client_id="client-a",
        granted_scopes=frozenset({"tools.read"}),
        profile_generation=1,
        profile_fingerprint="profile-fingerprint",
        registration_version=1,
        metadata_security_fingerprint="metadata-fingerprint",
        revocation_endpoint="https://issuer.example.test/revoke",
    )
