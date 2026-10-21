"""P8-B Slice 5：feature-gated single-process workload lifespan composition。"""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from src.protocols.mcp.oauth.workload.topology import McpWorkloadTopologyError
from src.protocols.registry import (
    ProtocolRegistry,
    ProtocolRegistryReloadError,
    ProtocolServerConfig,
)
from src.services.resource_manager import resource_manager


def _bootstrap() -> Any:
    return importlib.import_module("src.protocols.mcp.oauth.workload.bootstrap")


def _key_value() -> str:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return f"v1:workload-test:{encoded}"


def _registry() -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_workload_bootstrap_registry__.json")
    registry.servers = {
        "calendar": ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            auth_profile_id="calendar-workload",
            workload_authority_kind="direct",
        )
    }
    return registry


class _Owners:
    async def active_owner_for_agent(self, _agent_id: str) -> str | None:
        return "owner-1"


class _Policy:
    async def authorize(self, **_kwargs: Any) -> str:
        return f"sha256:{'1' * 64}"

    async def revalidate(self, **_kwargs: Any) -> str:
        return f"sha256:{'1' * 64}"


@pytest.mark.asyncio
async def test_disabled_workload_gate_performs_no_key_or_filesystem_io(tmp_path) -> None:
    config = _bootstrap().McpWorkloadOAuthBootstrapConfig(
        direct_enabled=False,
        delegation_enabled=False,
        config_dir=tmp_path,
        filesystem_kind="unknown",
        key_source="external",
        current_key="invalid",
        retiring_keys_json="not-json",
        bootstrap_enabled=False,
    )

    bundle = await _bootstrap().build_mcp_workload_oauth_runtime_bundle(
        config=config,
        registry=_registry(),
        installation_generation_lookup=lambda _name: 1,
        owners=_Owners(),
        policy=_Policy(),
        persist_secret=lambda _name, _value: pytest.fail("must not persist"),
        audit_sink=lambda _event: None,
    )

    assert bundle is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_enabled_workload_rejects_nonlocal_topology_before_key_load(tmp_path) -> None:
    config = _bootstrap().McpWorkloadOAuthBootstrapConfig(
        direct_enabled=True,
        delegation_enabled=False,
        config_dir=tmp_path,
        filesystem_kind="remote",
        key_source="external",
        current_key="invalid",
        retiring_keys_json="not-json",
        bootstrap_enabled=False,
    )

    with pytest.raises(McpWorkloadTopologyError):
        await _bootstrap().build_mcp_workload_oauth_runtime_bundle(
            config=config,
            registry=_registry(),
            installation_generation_lookup=lambda _name: 1,
            owners=_Owners(),
            policy=_Policy(),
            persist_secret=lambda _name, _value: pytest.fail("must not persist"),
            audit_sink=lambda _event: None,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_enabled_direct_runtime_owns_lock_store_and_http_lifespan(tmp_path) -> None:
    config = _bootstrap().McpWorkloadOAuthBootstrapConfig(
        direct_enabled=True,
        delegation_enabled=False,
        config_dir=tmp_path,
        filesystem_kind="local",
        key_source="external",
        current_key=_key_value(),
        retiring_keys_json="[]",
        bootstrap_enabled=False,
    )

    bundle = await _bootstrap().build_mcp_workload_oauth_runtime_bundle(
        config=config,
        registry=_registry(),
        installation_generation_lookup=lambda _name: 1,
        owners=_Owners(),
        policy=_Policy(),
        persist_secret=lambda _name, _value: False,
        audit_sink=lambda _event: None,
    )

    assert bundle is not None
    assert bundle.topology.acquired
    assert (tmp_path / "mcp_oauth_workload.sqlite3").exists()
    assert bundle.runtime.cached_token_count == 0
    assert bundle.control._mutation_coordinator is bundle.rotation._mutation_coordinator
    await bundle.aclose()
    assert not bundle.topology.acquired


def test_frozen_workload_registry_rejects_reload_and_keeps_last_known_good(
    tmp_path,
) -> None:
    path = tmp_path / "server_config.json"
    initial = {
        "mcpServers": {
            "calendar": {
                "url": "https://mcp.example.test/mcp",
                "auth_mode": "oauth_client_credentials",
                "auth_profile_id": "calendar-workload",
                "workload_authority_kind": "direct",
            }
        }
    }
    resource_manager.write_json(path, initial)
    registry = ProtocolRegistry(path)
    registry.freeze_workload_oauth_config()
    changed = {
        "mcpServers": {
            "calendar": {
                **initial["mcpServers"]["calendar"],
                "auth_profile_id": "different-profile",
            }
        }
    }
    resource_manager.write_json(path, changed)

    with pytest.raises(ProtocolRegistryReloadError):
        registry.load_config()

    assert registry.get_server("calendar").auth_profile_id == "calendar-workload"


@pytest.mark.asyncio
async def test_audit_maintenance_retries_transient_sink_failure_without_task_death(
    caplog,
) -> None:
    class _TransientDrainer:
        def __init__(self) -> None:
            self.calls = 0

        async def drain_once(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise OSError()
            return 1

    drainer = _TransientDrainer()
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        if drainer.calls == 2:
            raise asyncio.CancelledError()

    bundle = SimpleNamespace(
        audit_drainer=drainer,
        sleeper=sleeper,
        audit_drain_interval_seconds=5.0,
    )
    with (
        caplog.at_level(
            logging.ERROR,
            logger="src.protocols.mcp.oauth.workload.bootstrap",
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _bootstrap().McpWorkloadOAuthRuntimeBundle.run_maintenance(bundle)

    assert drainer.calls == 2
    assert delays == [5.0, 5.0]
    assert "audit outbox drain failed; retrying" in caplog.text
