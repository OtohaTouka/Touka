# ruff: noqa: D102, TC002, TC003
"""Feature-gated P8-A runtime composition for the application lifespan。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from src.config import (
    OAUTH_CLEANUP_INTERVAL_SECONDS,
    OAUTH_KEY_ROTATION_BATCH_SIZE,
    OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
    OAUTH_STATE_TTL_SECONDS,
    OAUTH_WORKER_LIVENESS_SECONDS,
)
from src.protocols.mcp.oauth.keyring import OAuthKeyRingProvider
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey, OAuthKeyRotationPhase
from src.protocols.mcp.oauth.outbound import (
    OAuthHttpClient,
    OAuthMetadataDiscovery,
    create_hardened_oauth_client,
    validate_oauth_endpoint,
)
from src.protocols.mcp.oauth.profile import OAuthProfileRepository
from src.protocols.mcp.oauth.protocol import OAuthCallbackPolicy
from src.protocols.mcp.oauth.registration import OAuthClientRegistrationService
from src.protocols.mcp.oauth.rotation import OAuthKeyRotationService
from src.protocols.mcp.oauth.runtime import McpOAuthRuntimeService
from src.protocols.mcp.oauth.service import ConnectorAuthService
from src.protocols.mcp.oauth.store import OAuthSqliteStore
from src.protocols.registry import ProtocolRegistry
from src.utils.time_utils import get_current_epoch


@dataclass(frozen=True)
class McpOAuthBootstrapConfig:
    """Resolved non-secret paths plus key-source values for one process。"""

    enabled: bool
    config_dir: Path
    key_source: str
    current_key: str
    retiring_keys_json: str
    callback_base_url: str
    bootstrap_enabled: bool
    delegation_enabled: bool = False
    rotation_phase: str = OAuthKeyRotationPhase.STABLE.value
    key_epoch: int = 1
    callback_response_mode: Literal["form_post", "query"] = "form_post"
    query_callback_canary_verified: bool = False


@dataclass
class McpOAuthRuntimeBundle:
    """Lifespan-owned OAuth runtime and its only network resource。"""

    runtime: McpOAuthRuntimeService
    outbound: OAuthHttpClient
    store: OAuthSqliteStore
    rotation: OAuthKeyRotationService
    clock: Callable[[], float] = get_current_epoch
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    cleanup_interval_seconds: float = OAUTH_CLEANUP_INTERVAL_SECONDS

    async def run_maintenance(self) -> None:
        """Run durable clock validation and bounded cleanup until lifespan cancellation。"""
        while True:
            now = self.clock()
            if await self.store.observe_clock(
                now=now,
                max_rollback_seconds=OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
            ):
                await self.store.cleanup(now=now)
                await self.rotation.maintain(now=now)
            await self.sleeper(self.cleanup_interval_seconds)

    async def aclose(self) -> None:
        await self.outbound.aclose()


class McpOAuthAuthorizationFence:
    """Revalidate every mutable authority used by callback and bearer egress。"""

    def __init__(
        self,
        *,
        registry: ProtocolRegistry,
        feature_enabled: Callable[[], bool],
        current_generation: Callable[[str], int],
        human_authorizer: Callable[[OAuthAuthorizationSlotKey], bool],
        installation_authorizer: Callable[[OAuthAuthorizationSlotKey], bool],
    ) -> None:
        self._registry = registry
        self._feature_enabled = feature_enabled
        self._current_generation = current_generation
        self._human_authorizer = human_authorizer
        self._installation_authorizer = installation_authorizer

    def __call__(self, slot: OAuthAuthorizationSlotKey) -> bool:
        if not self._feature_enabled():
            return False
        config = self._registry.get_server(slot.installation_id)
        if (
            config is None
            or not config.enabled
            or config.auth_mode != "oauth_user"
            or config.auth_profile_id != slot.auth_profile_id
            or self._current_generation(slot.installation_id) != slot.installation_generation
        ):
            return False
        return self._human_authorizer(slot) and self._installation_authorizer(slot)


def _callback_origin(value: str) -> str:
    validated = validate_oauth_endpoint(value)
    parsed = urlsplit(validated)
    if parsed.path not in {"", "/"} or parsed.query:
        message = "OAuth callback base URL must be an HTTPS origin."
        raise ValueError(message)
    return f"{parsed.scheme}://{parsed.netloc}"


async def build_mcp_oauth_runtime_bundle(
    *,
    config: McpOAuthBootstrapConfig,
    registry: ProtocolRegistry,
    authorization_fence: Callable[[OAuthAuthorizationSlotKey], bool | Awaitable[bool]],
    persist_secret: Callable[[str, str], bool],
    persist_secrets: Callable[[dict[str, str]], bool] | None = None,
    read_secrets: Callable[[], dict[str, str]] | None = None,
) -> McpOAuthRuntimeBundle | None:
    """Build P8-A only when enabled; disabled mode performs no OAuth I/O。"""
    interactive_present = any(
        server.enabled and server.auth_mode == "oauth_user"
        for server in registry.get_all_servers().values()
    )
    delegation_present = any(
        server.enabled
        and server.auth_mode == "oauth_client_credentials"
        and server.workload_authority_kind == "owner_delegation"
        for server in registry.get_all_servers().values()
    )
    if not (
        config.enabled
        and interactive_present
        or config.delegation_enabled
        and delegation_present
    ):
        return None
    if OAUTH_WORKER_LIVENESS_SECONDS < OAUTH_CLEANUP_INTERVAL_SECONDS * 3:
        message = "OAuth worker liveness must cover three maintenance intervals."
        raise ValueError(message)
    if (
        config.enabled
        and config.callback_response_mode == "query"
        and not config.query_callback_canary_verified
    ):
        message = "OAuth query callback requires a verified ingress canary."
        raise ValueError(message)
    config_dir = Path(config.config_dir)
    callback_origin = _callback_origin(config.callback_base_url)
    key_provider = OAuthKeyRingProvider(
        source=config.key_source,
        current_value=config.current_key,
        retiring_json=config.retiring_keys_json,
        lock_path=config_dir / "mcp_oauth_secret_writer.lock",
        bootstrap_enabled=config.bootstrap_enabled,
        persist=persist_secret,
        persist_many=persist_secrets,
        reload_values=read_secrets,
    )
    keyring = await key_provider.load()
    rotation_phase = OAuthKeyRotationPhase(config.rotation_phase)
    managed_ring_disagrees = (
        rotation_phase == OAuthKeyRotationPhase.STABLE and bool(keyring.retiring)
    ) or (
        rotation_phase
        not in {
            OAuthKeyRotationPhase.STABLE,
            OAuthKeyRotationPhase.PREPARE,
        }
        and len(keyring.retiring) != 1
    )
    if config.key_source == "managed" and managed_ring_disagrees:
        message = "OAuth managed key ring and rotation phase disagree."
        raise ValueError(message)
    store = OAuthSqliteStore(
        config_dir / "mcp_oauth.sqlite3",
        keyring,
        initial_rotation_phase=rotation_phase,
        initial_key_epoch=config.key_epoch,
    )
    await store.prepare_schema()
    now = get_current_epoch()
    if not await store.observe_clock(
        now=now,
        max_rollback_seconds=OAUTH_MAX_CLOCK_ROLLBACK_SECONDS,
    ):
        message = "OAuth clock rollback fence is active."
        raise ValueError(message)
    await store.cleanup(now=now)
    profiles = OAuthProfileRepository(config_dir / "mcp_oauth_profiles.json")
    outbound = OAuthHttpClient(client=create_hardened_oauth_client())
    connector = ConnectorAuthService(repository=store, outbound=outbound)
    callback_policies = tuple(
        OAuthCallbackPolicy(
            route=route,
            redirect_uri=f"{callback_origin}{route}",
            response_mode=config.callback_response_mode,
        )
        for route in ("/mcp/oauth/callback", "/v1/mcp/oauth/callback")
    )
    runtime = McpOAuthRuntimeService(
        registry=registry,
        profiles=profiles,
        store=store,
        connector=connector,
        metadata_provider=OAuthMetadataDiscovery(outbound),
        client_registration=OAuthClientRegistrationService(
            repository=store,
            outbound=outbound,
        ),
        callback_policies=callback_policies,
        primary_callback_route="/mcp/oauth/callback",
        feature_enabled=config.enabled,
        delegated_feature_enabled=config.delegation_enabled,
        authorization_fence=authorization_fence,
    )
    await runtime.reconcile_profile_mutations()
    rotation = OAuthKeyRotationService(
        store=store,
        provider=key_provider,
        worker_id=f"oauth-worker-{secrets.token_urlsafe(12)}",
        state_ttl_seconds=OAUTH_STATE_TTL_SECONDS,
        worker_liveness_seconds=OAUTH_WORKER_LIVENESS_SECONDS,
        batch_size=OAUTH_KEY_ROTATION_BATCH_SIZE,
    )
    await rotation.maintain(now=now)
    return McpOAuthRuntimeBundle(
        runtime=runtime,
        outbound=outbound,
        store=store,
        rotation=rotation,
    )
