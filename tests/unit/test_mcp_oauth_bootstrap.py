"""P8-A feature-gated lifespan composition never loads secrets when disabled。"""

from __future__ import annotations

import asyncio
import base64

import pytest
from src.protocols.mcp.oauth.bootstrap import (
    McpOAuthAuthorizationFence,
    McpOAuthBootstrapConfig,
    McpOAuthRuntimeBundle,
    build_mcp_oauth_runtime_bundle,
)
from src.protocols.mcp.oauth.models import OAuthAuthorizationSlotKey
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _key_value() -> str:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return f"v1:external-test:{encoded}"


def _registry(auth_mode: str | None = None) -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_oauth_bootstrap_registry__.json")
    if auth_mode is not None:
        values = {
            "url": "https://mcp.example.test/mcp",
            "auth_mode": auth_mode,
            "auth_profile_id": "profile-a" if auth_mode == "oauth_user" else None,
        }
        if auth_mode == "oauth_client_credentials":
            values.update(
                auth_profile_id="profile-a",
                workload_authority_kind="owner_delegation",
                workload_delegation_id="delegation-a",
                workload_source_installation_id="calendar-user-source",
            )
        registry.servers = {"server-a": ProtocolServerConfig(**values)}
    return registry


@pytest.mark.asyncio
async def test_disabled_gate_does_not_load_key_create_db_or_validate_callback(tmp_path):
    config = McpOAuthBootstrapConfig(
        enabled=False,
        config_dir=tmp_path,
        key_source="external",
        current_key="definitely-invalid",
        retiring_keys_json="not-json",
        callback_base_url="not-a-url",
        bootstrap_enabled=False,
    )

    bundle = await build_mcp_oauth_runtime_bundle(
        config=config,
        registry=_registry("oauth_user"),
        authorization_fence=lambda _slot: True,
        persist_secret=lambda _name, _value: pytest.fail("must not persist"),
    )

    assert bundle is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_enabled_external_key_builds_runtime_only_in_config_directory(tmp_path):
    config = McpOAuthBootstrapConfig(
        enabled=True,
        config_dir=tmp_path,
        key_source="external",
        current_key=_key_value(),
        retiring_keys_json="[]",
        callback_base_url="https://proxy.example.test",
        bootstrap_enabled=False,
    )

    bundle = await build_mcp_oauth_runtime_bundle(
        config=config,
        registry=_registry("oauth_user"),
        authorization_fence=lambda _slot: True,
        persist_secret=lambda _name, _value: False,
    )

    assert bundle is not None
    assert bundle.runtime.get_callback_policy("/mcp/oauth/callback").redirect_uri == (
        "https://proxy.example.test/mcp/oauth/callback"
    )
    assert (tmp_path / "mcp_oauth.sqlite3").exists()
    assert not (tmp_path / "mcp_oauth_profiles.json").exists()
    await bundle.aclose()


@pytest.mark.asyncio
async def test_delegation_only_builds_p8a_token_owner_without_enabling_interactive(
    tmp_path,
) -> None:
    config = McpOAuthBootstrapConfig(
        enabled=False,
        delegation_enabled=True,
        config_dir=tmp_path,
        key_source="external",
        current_key=_key_value(),
        retiring_keys_json="[]",
        callback_base_url="https://proxy.example.test",
        bootstrap_enabled=False,
    )

    bundle = await build_mcp_oauth_runtime_bundle(
        config=config,
        registry=_registry("oauth_client_credentials"),
        authorization_fence=lambda _slot: True,
        persist_secret=lambda _name, _value: False,
    )

    assert bundle is not None
    assert (tmp_path / "mcp_oauth.sqlite3").exists()
    await bundle.aclose()


@pytest.mark.asyncio
async def test_query_callback_is_fail_closed_until_ingress_canary_is_explicit(tmp_path):
    config = McpOAuthBootstrapConfig(
        enabled=True,
        config_dir=tmp_path,
        key_source="external",
        current_key=_key_value(),
        retiring_keys_json="[]",
        callback_base_url="https://proxy.example.test",
        bootstrap_enabled=False,
        callback_response_mode="query",
    )

    with pytest.raises(ValueError, match="verified ingress canary"):
        await build_mcp_oauth_runtime_bundle(
            config=config,
            registry=_registry("oauth_user"),
            authorization_fence=lambda _slot: True,
            persist_secret=lambda _name, _value: False,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_verified_query_callback_binds_both_parity_routes_to_get(tmp_path):
    config = McpOAuthBootstrapConfig(
        enabled=True,
        config_dir=tmp_path,
        key_source="external",
        current_key=_key_value(),
        retiring_keys_json="[]",
        callback_base_url="https://proxy.example.test",
        bootstrap_enabled=False,
        callback_response_mode="query",
        query_callback_canary_verified=True,
    )

    bundle = await build_mcp_oauth_runtime_bundle(
        config=config,
        registry=_registry("oauth_user"),
        authorization_fence=lambda _slot: True,
        persist_secret=lambda _name, _value: False,
    )

    assert bundle is not None
    assert bundle.runtime.get_callback_policy("/mcp/oauth/callback").method == "GET"
    assert bundle.runtime.get_callback_policy("/v1/mcp/oauth/callback").method == "GET"
    await bundle.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["none", "static_headers"])
async def test_non_oauth_registry_does_not_load_key_create_db_or_validate_callback(
    tmp_path,
    auth_mode: str,
):
    config = McpOAuthBootstrapConfig(
        enabled=True,
        config_dir=tmp_path,
        key_source="external",
        current_key="definitely-invalid",
        retiring_keys_json="not-json",
        callback_base_url="not-a-url",
        bootstrap_enabled=False,
    )

    bundle = await build_mcp_oauth_runtime_bundle(
        config=config,
        registry=_registry(auth_mode),
        authorization_fence=lambda _slot: True,
        persist_secret=lambda _name, _value: pytest.fail("must not persist"),
    )

    assert bundle is None
    assert list(tmp_path.iterdir()) == []


def test_authorization_fence_revalidates_human_config_profile_and_generation():
    registry = _registry()
    registry.servers = {
        "server-a": ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_user",
            auth_profile_id="profile-a",
        )
    }
    slot = OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="soul-a",
        installation_id="server-a",
        installation_generation=3,
        auth_profile_id="profile-a",
    )
    fence = McpOAuthAuthorizationFence(
        registry=registry,
        feature_enabled=lambda: True,
        current_generation=lambda _name: 3,
        human_authorizer=lambda candidate: candidate.human_principal_id == "human-a",
        installation_authorizer=lambda candidate: candidate.execution_agent_id == "soul-a",
    )

    assert fence(slot)
    assert not fence(slot.model_copy(update={"installation_generation": 2}))
    assert not fence(slot.model_copy(update={"auth_profile_id": "profile-b"}))
    registry.servers["server-a"] = registry.servers["server-a"].model_copy(
        update={"enabled": False}
    )
    assert not fence(slot)


@pytest.mark.asyncio
async def test_maintenance_loop_uses_injected_clock_and_sleep() -> None:
    events: list[tuple[str, float]] = []

    class Store:
        async def observe_clock(self, *, now: float, max_rollback_seconds: float) -> bool:
            assert max_rollback_seconds > 0
            events.append(("clock", now))
            return True

        async def cleanup(self, *, now: float) -> None:
            events.append(("cleanup", now))

    class Rotation:
        async def maintain(self, *, now: float) -> None:
            events.append(("rotation", now))

    async def stop_after_one_interval(seconds: float) -> None:
        events.append(("sleep", seconds))
        raise asyncio.CancelledError

    bundle = McpOAuthRuntimeBundle(
        runtime=None,
        outbound=None,
        store=Store(),
        rotation=Rotation(),
        clock=lambda: 1234.5,
        sleeper=stop_after_one_interval,
        cleanup_interval_seconds=7.0,
    )  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await bundle.run_maintenance()

    assert events == [
        ("clock", 1234.5),
        ("cleanup", 1234.5),
        ("rotation", 1234.5),
        ("sleep", 7.0),
    ]
