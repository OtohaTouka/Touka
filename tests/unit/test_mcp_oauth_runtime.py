# ruff: noqa: S105
"""P8-A runtime composition and verified authorization-slot tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import McpRequestContext, McpRequestOrigin
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey, OAuthTransactionPayload
from src.protocols.mcp.oauth.profile import OAuthProfile
from src.protocols.mcp.oauth.protocol import OAuthCallbackPayload, OAuthCallbackPolicy
from src.protocols.mcp.oauth.registration import OAuthClientBinding
from src.protocols.mcp.oauth.runtime import McpOAuthRuntimeService, OAuthUnsupportedModeError
from src.protocols.mcp.oauth.service import (
    OAuthAuthorizationMetadata,
    OAuthAuthorizationRequiredError,
    OAuthBearerCredential,
    OAuthCallbackResult,
    OAuthClockRollbackError,
    OAuthDisconnectResult,
    OAuthStartResult,
    OAuthStatusResult,
)
from src.protocols.registry import ProtocolServerConfig


def _profile() -> OAuthProfile:
    return OAuthProfile(
        profile_id="profile-a",
        generation=1,
        enabled=True,
        callback_binding="form_post",
        trusted_public_origin="https://chat.example.test",
        trusted_issuers=("https://issuer.example.test",),
        client_id="client-a",
        scope_rules=(
            {"operation": "tools/call", "selector": "*", "scopes": ["tools.read"]},
            {
                "operation": "resources/read",
                "selector": "*",
                "scopes": ["resources.read"],
            },
        ),
    )


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="request-a",
        identity=AgentIdentity.for_role("soul-a", "Owner"),
        human_principal_id="human-a",
        installation_id="server-a",
        auth_level="Manual",
        origin=McpRequestOrigin.FOREGROUND,
    )


def _delegated_slot() -> OAuthAuthorizationSlotKey:
    return OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="soul-a",
        installation_id="calendar-user-source",
        installation_generation=9,
        auth_profile_id="profile-a",
    )


class _Registry:
    def __init__(self, auth_mode: str = "oauth_user", *, enabled: bool = True) -> None:
        values = {
            "url": "https://mcp.example.test/mcp",
            "auth_mode": auth_mode,
            "auth_profile_id": "profile-a",
            "enabled": enabled,
        }
        if auth_mode == "oauth_client_credentials":
            values.update(
                workload_authority_kind="owner_delegation",
                workload_delegation_id="delegation-a",
                workload_source_installation_id="calendar-user-source",
            )
        self.config = ProtocolServerConfig(**values)
        self.source_config = ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_user",
            auth_profile_id="profile-a",
            enabled=enabled,
        )

    def get_server(self, name: str):
        if name == "server-a":
            return self.config
        if name == "calendar-user-source":
            return self.source_config
        return None


class _Profiles:
    def __init__(self) -> None:
        self.profile = _profile()
        self.events: list[str] = []

    async def get(self, profile_id: str):
        assert profile_id == "profile-a"
        return self.profile

    async def list_profiles(self):
        self.events.append("list")
        return [self.profile]

    async def put(self, profile: OAuthProfile):
        self.events.append("put")
        self.profile = profile
        return profile

    async def delete(self, profile_id: str):
        self.events.append(f"delete:{profile_id}")

    @asynccontextmanager
    async def profile_guard(self, profile_id: str):
        self.events.append(f"guard-enter:{profile_id}")
        try:
            yield
        finally:
            self.events.append(f"guard-exit:{profile_id}")


class _Metadata:
    def __init__(self) -> None:
        self.calls = 0

    async def discover(self, **kwargs):
        self.calls += 1
        assert kwargs["resource"] == "https://mcp.example.test/mcp"
        assert kwargs["selected_issuer"] == "https://issuer.example.test"
        return OAuthAuthorizationMetadata(
            issuer="https://issuer.example.test",
            authorization_endpoint="https://issuer.example.test/authorize",
            token_endpoint="https://issuer.example.test/token",
            registration_endpoint="https://issuer.example.test/register",
            revocation_endpoint="https://issuer.example.test/revoke",
            code_challenge_methods_supported=("S256",),
            response_modes_supported=("form_post",),
            authorization_response_iss_parameter_supported=True,
            security_fingerprint="metadata-fingerprint",
        )


class _Connector:
    def __init__(self) -> None:
        self.calls = []
        self.status_result = OAuthStatusResult(
            status="connected",
            expires_at=2000.0,
        )

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return OAuthStartResult(
            operation_id="operation-a",
            authorization_url="https://issuer.example.test/authorize?opaque=yes",
            expires_at=1600.0,
        )

    async def status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return self.status_result

    async def disconnect(self, **kwargs):
        self.calls.append(("disconnect", kwargs))
        return OAuthDisconnectResult(status="auth_required")

    async def complete_callback(self, **kwargs):
        self.calls.append(("callback", kwargs))
        return OAuthCallbackResult(operation_id="operation-a", status="connected")

    async def get_bearer_credential(self, **kwargs):
        self.calls.append(("bearer", kwargs))
        return OAuthBearerCredential(
            grant_id="grant-a",
            grant_version=3,
            authorization_header=SecretStr("Bearer access-canary"),
        )


class _Store:
    transaction = None
    mutation_events = []
    active_grant = SimpleNamespace(
        binding=SimpleNamespace(
            issuer="https://issuer.example.test",
            granted_scopes=frozenset({"tools.read"}),
        )
    )
    clock_allowed = True
    updating_profiles = []
    frozen_grants = []

    async def observe_clock(self, **_kwargs):
        return self.clock_allowed

    async def find_transaction(self, _state: str, **_kwargs):
        return self.transaction

    async def get_active_grant(self, _slot, **_kwargs):
        return self.active_grant

    usable = True

    async def has_usable_grant(self, slot, *, now):
        self.usable_checks.append((slot, now))
        return self.usable

    async def begin_profile_mutation(self, profile_id, **_kwargs):
        self.mutation_events.append(f"begin:{profile_id}")
        return 4

    async def complete_profile_mutation(self, profile_id, epoch, *, applied_fingerprint, **_kwargs):
        self.mutation_events.append(
            f"complete:{profile_id}:{epoch}:{applied_fingerprint is not None}"
        )
        return True

    async def list_updating_profile_fences(self):
        return [(profile_id, 4) for profile_id in self.updating_profiles]

    async def freeze_grant(self, grant_id, expected_version, *, now):
        self.frozen_grants.append((grant_id, expected_version, now))
        return True


class _Registration:
    def __init__(self) -> None:
        self.calls = []

    async def resolve(self, **kwargs):
        self.calls.append(kwargs)
        return OAuthClientBinding(
            client_id="dynamic-client-a",
            token_endpoint_auth_method="none",
            registration_version=4,
        )


def _runtime(
    *,
    auth_mode: str = "oauth_user",
    enabled: bool = True,
    client_registration=None,
    authorization_fence=lambda _slot: True,
    connector=None,
):
    connector = connector or _Connector()
    store = _Store()
    store.mutation_events = []
    store.updating_profiles = []
    store.frozen_grants = []
    store.usable_checks = []
    profiles = _Profiles()
    metadata = _Metadata()
    runtime = McpOAuthRuntimeService(
        registry=_Registry(auth_mode, enabled=enabled),
        profiles=profiles,
        store=store,
        connector=connector,
        metadata_provider=metadata,
        client_registration=client_registration,
        callback_policies=(
            OAuthCallbackPolicy(
                route="/mcp/oauth/callback",
                redirect_uri="https://proxy.example.test/mcp/oauth/callback",
                response_mode="form_post",
            ),
            OAuthCallbackPolicy(
                route="/v1/mcp/oauth/callback",
                redirect_uri="https://proxy.example.test/v1/mcp/oauth/callback",
                response_mode="form_post",
            ),
        ),
        primary_callback_route="/mcp/oauth/callback",
        feature_enabled=True,
        delegated_feature_enabled=auth_mode == "oauth_client_credentials",
        epoch_clock=lambda: 1000.0,
        authorization_fence=authorization_fence,
    )
    runtime.test_metadata = metadata
    return runtime, connector, store, profiles


@pytest.mark.asyncio
async def test_runtime_builds_full_slot_only_from_verified_context_and_generation():
    runtime, connector, _store, _profiles = _runtime()
    response = await runtime.start_authorization(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
        selected_issuer="https://issuer.example.test",
    )
    assert response.operation_id == "operation-a"
    _name, kwargs = connector.calls[-1]
    slot = kwargs["slot"]
    assert slot.human_principal_id == "human-a"
    assert slot.execution_agent_id == "soul-a"
    assert slot.installation_generation == 7
    assert slot.auth_profile_id == "profile-a"
    assert kwargs["operation_scopes"] == frozenset({"tools.read", "resources.read"})


@pytest.mark.asyncio
async def test_disconnect_fails_before_p8a_revocation_when_delegation_fence_fails():
    runtime, connector, _store, _profiles = _runtime()

    @asynccontextmanager
    async def reject(_slot: OAuthAuthorizationSlotKey, _request_id: str):
        raise RuntimeError()
        yield

    runtime.set_delegation_disconnect_fence(reject)

    with pytest.raises(RuntimeError):
        await runtime.disconnect(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
        )

    assert connector.calls == []


@pytest.mark.asyncio
async def test_disconnect_fence_is_held_through_actual_connector_disconnect():
    trace: list[str] = []

    class _BlockingConnector(_Connector):
        def __init__(self) -> None:
            super().__init__()
            self.disconnect_entered = asyncio.Event()
            self.finish_disconnect = asyncio.Event()

        async def disconnect(self, **kwargs):
            trace.append("connector-enter")
            self.disconnect_entered.set()
            await self.finish_disconnect.wait()
            trace.append("connector-exit")
            return await super().disconnect(**kwargs)

    connector = _BlockingConnector()
    runtime, _connector, _store, _profiles = _runtime(connector=connector)

    @asynccontextmanager
    async def fence(_slot: OAuthAuthorizationSlotKey, _request_id: str):
        trace.append("fence-enter")
        try:
            yield
        finally:
            trace.append("fence-exit")

    runtime.set_delegation_disconnect_fence(fence)
    task = asyncio.create_task(
        runtime.disconnect(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
        )
    )
    await asyncio.wait_for(connector.disconnect_entered.wait(), timeout=0.5)

    assert trace == ["fence-enter", "connector-enter"]
    connector.finish_disconnect.set()
    await asyncio.wait_for(task, timeout=0.5)
    assert trace == [
        "fence-enter",
        "connector-enter",
        "connector-exit",
        "fence-exit",
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_non_user_oauth_modes_before_store_or_network():
    runtime, connector, _store, _profiles = _runtime(auth_mode="enterprise")
    with pytest.raises(OAuthUnsupportedModeError):
        await runtime.get_status(
            server_name="server-a",
            request_context=_context(),
            installation_generation=1,
        )
    assert connector.calls == []


@pytest.mark.asyncio
async def test_runtime_clock_rollback_blocks_before_metadata_or_registration_egress():
    runtime, connector, store, _profiles = _runtime()
    store.clock_allowed = False

    with pytest.raises(OAuthClockRollbackError):
        await runtime.start_authorization(
            server_name="server-a",
            request_context=_context(),
            installation_generation=1,
            selected_issuer="https://issuer.example.test",
        )

    assert runtime.test_metadata.calls == 0
    assert connector.calls == []


@pytest.mark.asyncio
async def test_delegated_guard_refreshes_headlessly_with_exact_source_grant() -> None:
    fences = 0

    async def workload_fence() -> bool:
        nonlocal fences
        fences += 1
        return True

    runtime, connector, store, _profiles = _runtime(
        auth_mode="oauth_client_credentials"
    )
    store.frozen_grants = []
    store.active_grant = SimpleNamespace(
        grant_id="grant-a",
        grant_version=3,
        binding=SimpleNamespace(
            issuer="https://issuer.example.test",
            profile_generation=1,
            granted_scopes=frozenset({"tools.read"}),
        ),
        tokens=SimpleNamespace(granted_scopes=frozenset({"tools.read"})),
    )

    async with runtime.delegated_bearer_credential_guard(
        server_name="server-a",
        source_slot=_delegated_slot(),
        expected_grant_id="grant-a",
        minimum_grant_version=2,
        required_scopes=frozenset({"tools.read"}),
        authorization_fence=workload_fence,
    ) as credential:
        assert credential.grant_id == "grant-a"
        assert credential.grant_version == 3

    assert fences >= 2
    assert [name for name, _kwargs in connector.calls] == ["bearer"]


@pytest.mark.asyncio
async def test_delegated_guard_rejects_stale_grant_before_connector_network() -> None:
    runtime, connector, store, _profiles = _runtime(
        auth_mode="oauth_client_credentials"
    )
    store.active_grant = SimpleNamespace(
        grant_id="other-grant",
        grant_version=3,
        binding=SimpleNamespace(
            issuer="https://issuer.example.test",
            profile_generation=1,
            granted_scopes=frozenset({"tools.read"}),
        ),
        tokens=SimpleNamespace(granted_scopes=frozenset({"tools.read"})),
    )

    with pytest.raises(OAuthAuthorizationRequiredError):
        async with runtime.delegated_bearer_credential_guard(
            server_name="server-a",
            source_slot=_delegated_slot(),
            expected_grant_id="grant-a",
            minimum_grant_version=2,
            required_scopes=frozenset({"tools.read"}),
            authorization_fence=lambda: True,
        ):
            pytest.fail("stale source grant reached transport")

    assert connector.calls == []


@pytest.mark.asyncio
async def test_delegated_guard_freezes_grant_when_final_fence_closes() -> None:
    fence_calls = 0

    async def closes_after_refresh() -> bool:
        nonlocal fence_calls
        fence_calls += 1
        return fence_calls == 1

    runtime, connector, store, _profiles = _runtime(
        auth_mode="oauth_client_credentials"
    )
    store.frozen_grants = []
    store.active_grant = SimpleNamespace(
        grant_id="grant-a",
        grant_version=3,
        binding=SimpleNamespace(
            issuer="https://issuer.example.test",
            profile_generation=1,
            granted_scopes=frozenset({"tools.read"}),
        ),
        tokens=SimpleNamespace(granted_scopes=frozenset({"tools.read"})),
    )

    with pytest.raises(OAuthAuthorizationRequiredError):
        async with runtime.delegated_bearer_credential_guard(
            server_name="server-a",
            source_slot=_delegated_slot(),
            expected_grant_id="grant-a",
            minimum_grant_version=2,
            required_scopes=frozenset({"tools.read"}),
            authorization_fence=closes_after_refresh,
        ):
            pytest.fail("closed final fence reached transport")

    assert [name for name, _kwargs in connector.calls] == ["bearer"]
    assert store.frozen_grants == [("grant-a", 3, 1000.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_target", ["installation", "profile"])
async def test_runtime_disabled_authority_blocks_before_metadata_or_registration_egress(
    disabled_target: str,
):
    registration = _Registration()
    runtime, connector, _store, profiles = _runtime(
        enabled=disabled_target != "installation",
        client_registration=registration,
    )
    if disabled_target == "profile":
        profiles.profile = profiles.profile.model_copy(update={"enabled": False})

    with pytest.raises((OAuthUnsupportedModeError, OAuthAuthorizationRequiredError)):
        await runtime.start_authorization(
            server_name="server-a",
            request_context=_context(),
            installation_generation=1,
            selected_issuer="https://issuer.example.test",
        )

    assert runtime.test_metadata.calls == 0
    assert registration.calls == []
    assert connector.calls == []


@pytest.mark.asyncio
async def test_runtime_dynamic_authority_blocks_start_bearer_and_operation_before_egress():
    runtime, connector, _store, _profiles = _runtime(
        authorization_fence=lambda _slot: False,
    )

    with pytest.raises(OAuthAuthorizationRequiredError):
        await runtime.start_authorization(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
            selected_issuer="https://issuer.example.test",
        )
    with pytest.raises(OAuthAuthorizationRequiredError):
        await runtime.get_bearer_credential(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
        )
    with pytest.raises(Exception) as operation_error:
        await runtime.authorize_operation(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
            operation="tools/call",
            selector="search",
        )

    assert type(operation_error.value).__name__ == "OAuthScopeDeniedError"
    assert runtime.test_metadata.calls == 0
    assert connector.calls == []


@pytest.mark.asyncio
async def test_runtime_projects_multiple_trusted_issuers_as_explicit_selection():
    runtime, connector, _store, profiles = _runtime()
    profiles.profile = profiles.profile.model_copy(
        update={
            "trusted_issuers": (
                "https://issuer-a.example.test",
                "https://issuer-b.example.test",
            )
        }
    )
    connector.status_result = OAuthStatusResult(status="auth_required")

    response = await runtime.get_status(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
    )

    assert response.status.value == "selection_required"
    assert response.authorization_servers == [
        "https://issuer-a.example.test",
        "https://issuer-b.example.test",
    ]


@pytest.mark.asyncio
async def test_runtime_does_not_project_selection_over_existing_oauth_state():
    runtime, connector, _store, profiles = _runtime()
    profiles.profile = profiles.profile.model_copy(
        update={
            "trusted_issuers": (
                "https://issuer-a.example.test",
                "https://issuer-b.example.test",
            )
        }
    )
    connector.status_result = OAuthStatusResult(
        status="awaiting_user",
        operation_id="operation-a",
        expires_at=1600.0,
    )

    response = await runtime.get_status(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
    )

    assert response.status.value == "awaiting_user"
    assert response.authorization_servers == []


@pytest.mark.asyncio
async def test_runtime_discovers_exact_active_issuer_before_resolving_bearer():
    runtime, connector, _store, _profiles = _runtime()
    credential = await runtime.get_bearer_credential(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
    )

    assert credential.authorization_header.get_secret_value() == "Bearer access-canary"
    name, kwargs = connector.calls[-1]
    assert name == "bearer"
    assert kwargs["slot"].execution_agent_id == "soul-a"
    assert kwargs["metadata"].issuer == "https://issuer.example.test"


@pytest.mark.asyncio
async def test_runtime_projects_current_profile_cidrs_into_each_bearer_request():
    runtime, _connector, _store, profiles = _runtime()
    document = profiles.profile.model_dump(mode="json")
    document["private_network_exceptions"] = [
        {
            "installation_id": "server-a",
            "origin": "https://mcp.example.test",
            "cidrs": ["10.20.0.0/16"],
        }
    ]
    profiles.profile = OAuthProfile.model_validate(document)

    credential = await runtime.get_bearer_credential(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
    )

    assert credential.allowed_private_cidrs == ("10.20.0.0/16",)


@pytest.mark.asyncio
async def test_runtime_freezes_exact_grant_when_authority_is_revoked_at_return_boundary():
    decisions = iter((True, False))
    runtime, _connector, store, _profiles = _runtime(
        authorization_fence=lambda _slot: next(decisions)
    )

    with pytest.raises(OAuthAuthorizationRequiredError):
        await runtime.get_bearer_credential(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
        )

    assert store.frozen_grants == [("grant-a", 3, 1000.0)]


@pytest.mark.asyncio
async def test_runtime_scope_policy_is_exact_default_deny_and_cannot_expand_rbac():
    runtime, _connector, _store, _profiles = _runtime()
    await runtime.authorize_operation(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
        operation="tools/call",
        selector="search",
    )
    with pytest.raises(Exception) as caught:
        await runtime.authorize_operation(
            server_name="server-a",
            request_context=_context(),
            installation_generation=7,
            operation="prompts/get",
            selector="unconfigured",
        )
    assert type(caught.value).__name__ == "OAuthScopeDeniedError"


@pytest.mark.asyncio
async def test_profile_put_freezes_sqlite_before_json_write_and_completes_exact_epoch():
    runtime, _connector, store, profiles = _runtime()
    updated = _profile().model_copy(update={"generation": 2})
    await runtime.put_profile(updated)

    assert store.mutation_events[0] == "begin:profile-a"
    assert profiles.events == ["guard-enter:profile-a", "put", "guard-exit:profile-a"]
    assert store.mutation_events[1].startswith("complete:profile-a:4:True")


@pytest.mark.asyncio
async def test_profile_reconciler_completes_orphaned_updating_fence_under_guard():
    runtime, _connector, store, profiles = _runtime()
    store.updating_profiles = ["profile-a"]

    await runtime.reconcile_profile_mutations()

    assert profiles.events == ["guard-enter:profile-a", "guard-exit:profile-a"]
    assert store.mutation_events == ["complete:profile-a:4:True"]


@pytest.mark.asyncio
async def test_runtime_resolves_dcr_binding_before_persisting_authorization_transaction():
    registration = _Registration()
    runtime, connector, _store, profiles = _runtime(client_registration=registration)
    profiles.profile = profiles.profile.model_copy(
        update={
            "client_id": None,
            "dcr_enabled": True,
            "dcr_trusted_issuers": ("https://issuer.example.test",),
        }
    )
    await runtime.start_authorization(
        server_name="server-a",
        request_context=_context(),
        installation_generation=7,
        selected_issuer="https://issuer.example.test",
    )

    assert registration.calls[0]["registration_endpoint"] == (
        "https://issuer.example.test/register"
    )
    assert connector.calls[-1][1]["client_binding"].registration_version == 4


@pytest.mark.asyncio
async def test_callback_reconstructs_frozen_binding_from_encrypted_transaction():
    runtime, connector, store, _profiles = _runtime()
    store.transaction = SimpleNamespace(
        payload=OAuthTransactionPayload(
            pkce_verifier=SecretStr("verifier-canary"),
            requested_scopes=frozenset({"tools.read"}),
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test/mcp",
            redirect_uri="https://proxy.example.test/mcp/oauth/callback",
            redirect_route="/mcp/oauth/callback",
            response_mode="form_post",
            profile_generation=1,
            profile_fingerprint="profile-fingerprint",
            auth_profile_id="profile-a",
            human_principal_id="human-a",
            execution_agent_id="soul-a",
            installation_id="server-a",
            installation_generation=7,
            authorization_endpoint="https://issuer.example.test/authorize",
            token_endpoint="https://issuer.example.test/token",
            revocation_endpoint="https://issuer.example.test/revoke",
            code_challenge_methods_supported=("S256",),
            response_modes_supported=("form_post",),
            authorization_response_iss_parameter_supported=True,
            metadata_security_fingerprint="metadata-fingerprint",
        )
    )
    outcome = await runtime.complete_callback(
        callback=OAuthCallbackPayload(
            state="raw-state",
            code="raw-code",
            issuer="https://issuer.example.test",
        ),
        redirect_route="/mcp/oauth/callback",
    )
    assert outcome.operation_id == "operation-a"
    assert outcome.client_origin == "https://chat.example.test"
    _name, kwargs = connector.calls[-1]
    assert kwargs["slot"].installation_generation == 7
    assert kwargs["metadata"].token_endpoint == "https://issuer.example.test/token"


def test_callback_policy_is_exact_route_bound():
    runtime, _connector, _store, _profiles = _runtime()
    assert runtime.get_callback_policy("/mcp/oauth/callback").method == "POST"
    with pytest.raises(KeyError):
        runtime.get_callback_policy("/unknown/callback")


def _listing_check(runtime):
    return runtime.has_usable_grant(
        server_name="server-a", request_context=_context(), installation_generation=7
    )


@pytest.mark.asyncio
async def test_listing_grant_check_reads_the_store_without_advancing_the_clock():
    """ADR-0281 T1：組工具清單的授權查詢不推進時鐘高水位；查的是這個人＋靈魂的授權位置。"""
    runtime, _connector, store, _profiles = _runtime()

    async def _no_clock(**_kwargs):
        raise AssertionError

    store.observe_clock = _no_clock

    assert await _listing_check(runtime) is True
    (slot, _now), = store.usable_checks
    assert (slot.human_principal_id, slot.execution_agent_id, slot.installation_generation) == (
        "human-a",
        "soul-a",
        7,
    )


@pytest.mark.asyncio
async def test_listing_grant_check_is_false_when_the_fence_or_server_refuses():
    fenced, _c, fenced_store, _p = _runtime(authorization_fence=lambda _slot: False)
    disabled, _c, disabled_store, _p = _runtime(enabled=False)

    assert await _listing_check(fenced) is False
    assert fenced_store.usable_checks == []  # 授權條件不符時不查儲存
    assert await _listing_check(disabled) is False
