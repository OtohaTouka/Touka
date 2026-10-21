# ruff: noqa: TC002, TC003
"""Feature-gated single-process P8-B workload OAuth lifespan composition。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.protocols.mcp.oauth.keyring import OAuthKeyRingProvider
from src.protocols.mcp.oauth.outbound import OAuthHttpClient, create_hardened_oauth_client
from src.protocols.mcp.oauth.workload.audit import (
    WorkloadAuditOutboxDrainer,
    WorkloadEgressAuditAccepter,
)
from src.protocols.mcp.oauth.workload.authority import WorkloadDirectAuthorizer
from src.protocols.mcp.oauth.workload.authority_router import WorkloadAuthorityRouter
from src.protocols.mcp.oauth.workload.control import WorkloadOAuthControlService
from src.protocols.mcp.oauth.workload.delegated_authority import (
    WorkloadDelegatedAuthorizer,
)
from src.protocols.mcp.oauth.workload.delegation import (
    WorkloadDelegationRepository,
    WorkloadDelegationResolver,
)
from src.protocols.mcp.oauth.workload.exchange import (
    WorkloadOAuthHttpEndpoint,
    WorkloadTokenExchangeCoordinator,
    WorkloadTokenExchanger,
)
from src.protocols.mcp.oauth.workload.mutation import (
    WorkloadAuthorityMutationCoordinator,
)
from src.protocols.mcp.oauth.workload.permit import WorkloadEgressPermitManager
from src.protocols.mcp.oauth.workload.profile import WorkloadProfileRepository
from src.protocols.mcp.oauth.workload.rotation import WorkloadKeyRotationService
from src.protocols.mcp.oauth.workload.runtime import WorkloadOAuthRuntime
from src.protocols.mcp.oauth.workload.store import WorkloadOAuthStore
from src.protocols.mcp.oauth.workload.topology import SingleProxyProcessGate
from src.protocols.registry import ProtocolRegistry
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpWorkloadOAuthBootstrapConfig:
    """Resolved non-secret P8-B paths, feature flags, and safety limits。"""

    direct_enabled: bool
    delegation_enabled: bool
    config_dir: Path
    filesystem_kind: Literal["local", "remote", "unknown"]
    key_source: str
    current_key: str
    retiring_keys_json: str
    bootstrap_enabled: bool
    ticket_ttl_seconds: float = 30.0
    send_permit_ttl_seconds: float = 10.0
    token_expiry_skew_seconds: float = 60.0
    audit_high_watermark: int = 1000
    audit_drain_interval_seconds: float = 5.0


@dataclass
class McpWorkloadOAuthRuntimeBundle:
    """Lifespan owner for P8-B runtime, lock, store, audit, and HTTP client。"""

    runtime: WorkloadOAuthRuntime
    topology: SingleProxyProcessGate
    outbound: OAuthHttpClient
    store: WorkloadOAuthStore
    audit_drainer: WorkloadAuditOutboxDrainer
    control: WorkloadOAuthControlService
    permits: WorkloadEgressPermitManager
    rotation: WorkloadKeyRotationService
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    audit_drain_interval_seconds: float = 5.0

    async def run_maintenance(self) -> None:
        """Drain the durable security outbox until lifespan cancellation。"""
        while True:
            try:
                await self.audit_drainer.drain_once()
            except Exception as exc:
                logger.log(
                    logging.ERROR,
                    "audit outbox drain failed; retrying",
                    extra={"error_type": type(exc).__name__},
                )
            await self.sleeper(self.audit_drain_interval_seconds)

    async def aclose(self) -> None:
        """Erase tokens, close network resources, then release the process lock。"""
        try:
            await self.runtime.aclose()
        finally:
            try:
                await self.outbound.aclose()
            finally:
                await self.topology.aclose()


class _UnavailableDelegationResolver:
    async def resolve(self, **_kwargs: Any) -> Any:
        raise PermissionError()


class McpWorkloadOAuthBootstrapError(ValueError):
    """The enabled workload runtime cannot be composed safely。"""


async def _close_no_handles(_authority_key: str) -> None:
    return None


async def _close_no_workload_handles() -> None:
    return None


async def build_mcp_workload_oauth_runtime_bundle(
    *,
    config: McpWorkloadOAuthBootstrapConfig,
    registry: ProtocolRegistry,
    installation_generation_lookup: Callable[[str], int],
    owners: Any,
    policy: Any,
    persist_secret: Callable[[str, str], bool],
    audit_sink: Callable[[Any], None],
    p8a_runtime: Any | None = None,
    p8a_store: Any | None = None,
    persist_secrets: Callable[[dict[str, str]], bool] | None = None,
    read_secrets: Callable[[], dict[str, str]] | None = None,
    close_handles: Callable[[str], Awaitable[Any]] | None = None,
    close_all_handles: Callable[[], Awaitable[Any]] | None = None,
) -> McpWorkloadOAuthRuntimeBundle | None:
    """Build only for explicitly enabled installation kinds; otherwise do no I/O。"""
    servers = registry.get_all_servers().values()
    direct_present = any(
        server.enabled
        and server.auth_mode == "oauth_client_credentials"
        and server.workload_authority_kind == "direct"
        for server in servers
    )
    servers = registry.get_all_servers().values()
    delegation_present = any(
        server.enabled
        and server.auth_mode == "oauth_client_credentials"
        and server.workload_authority_kind == "owner_delegation"
        for server in servers
    )
    direct_active = config.direct_enabled and direct_present
    delegation_active = config.delegation_enabled and delegation_present
    if not direct_active and not delegation_active:
        return None
    if delegation_active and (p8a_runtime is None or p8a_store is None):
        raise McpWorkloadOAuthBootstrapError()

    config_dir = Path(config.config_dir)
    topology = SingleProxyProcessGate(
        lock_path=config_dir / "mcp_oauth_p8b_runtime.lock",
        filesystem_kind=config.filesystem_kind,
    )
    await topology.acquire()
    outbound: OAuthHttpClient | None = None
    try:
        key_provider = OAuthKeyRingProvider(
            source=config.key_source,
            current_value=config.current_key,
            retiring_json=config.retiring_keys_json,
            lock_path=config_dir / "mcp_oauth_workload_secret_writer.lock",
            bootstrap_enabled=config.bootstrap_enabled,
            persist=persist_secret,
            persist_many=persist_secrets,
            reload_values=read_secrets,
            current_name="TOUKA_MCP_OAUTH_WORKLOAD_KEY",
            retiring_name="TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS",
            rotation_phase_name="TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE",
            key_epoch_name="TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH",
        )
        keyring = await key_provider.load()
        store = WorkloadOAuthStore(
            config_dir / "mcp_oauth_workload.sqlite3",
            keyring,
        )
        await store.prepare_schema()
        profiles = WorkloadProfileRepository(config_dir / "mcp_oauth_workload_profiles.json")
        delegations = WorkloadDelegationRepository(
            config_dir / "mcp_oauth_workload_delegations.json"
        )
        if delegation_active:
            delegation_resolver: Any = WorkloadDelegationResolver(
                policy_lookup=delegations.get,
                mirror=store,
                grants=p8a_store,
            )
        else:
            delegation_resolver = _UnavailableDelegationResolver()
        direct_authorizer = WorkloadDirectAuthorizer(
            config_lookup=registry.get_server,
            installation_generation_lookup=installation_generation_lookup,
            profiles=profiles,
            store=store,
            owners=owners,
            policy=policy,
            feature_enabled=lambda: direct_active,
            clock=get_current_epoch,
            ticket_ttl=config.ticket_ttl_seconds,
        )
        delegated_authorizer = WorkloadDelegatedAuthorizer(
            config_lookup=registry.get_server,
            installation_generation_lookup=installation_generation_lookup,
            delegations=delegations,
            resolver=delegation_resolver,
            owners=owners,
            policy=policy,
            feature_enabled=lambda: delegation_active,
            clock=get_current_epoch,
            ticket_ttl=config.ticket_ttl_seconds,
        )
        authorizer = WorkloadAuthorityRouter(
            config_lookup=registry.get_server,
            direct=direct_authorizer,
            delegated=delegated_authorizer,
        )
        audit = WorkloadEgressAuditAccepter(store=store)
        permits = WorkloadEgressPermitManager(
            boot_id=topology.boot_id,
            runtime_state=store,
            audit_high_watermark=config.audit_high_watermark,
        )
        outbound = OAuthHttpClient(client=create_hardened_oauth_client())
        exchanger = WorkloadTokenExchangeCoordinator(
            exchanger=WorkloadTokenExchanger(endpoint=WorkloadOAuthHttpEndpoint(outbound=outbound)),
            permits=permits,
            audit=audit,
            clock=get_current_epoch,
            attempt_ttl=config.send_permit_ttl_seconds,
        )
        runtime = WorkloadOAuthRuntime(
            authorizer=authorizer,
            exchange=exchanger,
            delegated_bearer_provider=p8a_runtime,
            permits=permits,
            audit=audit,
            clock=get_current_epoch,
            send_permit_ttl=config.send_permit_ttl_seconds,
            token_expiry_skew=config.token_expiry_skew_seconds,
        )
        workload_close_all = close_all_handles or _close_no_workload_handles
        mutation_coordinator = WorkloadAuthorityMutationCoordinator(
            permits=permits,
            store=store,
        )
        rotation = WorkloadKeyRotationService(
            store=store,
            provider=key_provider,
            permits=permits,
            runtime=runtime,
            close_all_handles=workload_close_all,
            clock=get_current_epoch,
            mutation_coordinator=mutation_coordinator,
        )
        await rotation.maintain()
        control = WorkloadOAuthControlService(
            profiles=profiles,
            delegations=delegations,
            store=store,
            outbound=outbound,
            permits=permits,
            runtime=runtime,
            owners=owners,
            source_grants=p8a_store,
            config_lookup=registry.get_server,
            installation_generation_lookup=installation_generation_lookup,
            close_handles=close_handles or _close_no_handles,
            close_all_handles=workload_close_all,
            clock=get_current_epoch,
            mutation_coordinator=mutation_coordinator,
        )
        drainer = WorkloadAuditOutboxDrainer(
            store=store,
            sink=audit_sink,
            clock=get_current_epoch,
        )
        return McpWorkloadOAuthRuntimeBundle(
            runtime=runtime,
            topology=topology,
            outbound=outbound,
            store=store,
            audit_drainer=drainer,
            control=control,
            permits=permits,
            rotation=rotation,
            audit_drain_interval_seconds=config.audit_drain_interval_seconds,
        )
    except Exception:
        if outbound is not None:
            await outbound.aclose()
        await topology.aclose()
        raise
