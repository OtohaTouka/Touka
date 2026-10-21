"""P8-B Slice 1：workload OAuth 設定與 request scope 邊界。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from src.config import Settings
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def test_client_credentials_is_principal_scoped_and_deferred() -> None:
    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_client_credentials",
        auth_profile_id="workload-profile",
        workload_authority_kind="direct",
    )

    assert config.is_principal_scoped is True
    assert config.requires_deferred_auth is True


def test_client_credentials_rejects_stdio_transport() -> None:
    with pytest.raises(ValidationError, match="remote"):
        ProtocolServerConfig(
            command="local-mcp-server",
            auth_mode="oauth_client_credentials",
            auth_profile_id="workload-profile",
            workload_authority_kind="direct",
        )


def test_client_credentials_requires_explicit_auth_profile() -> None:
    with pytest.raises(ValidationError, match="auth_profile_id"):
        ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            workload_authority_kind="direct",
        )


def test_client_credentials_requires_explicit_authority_kind() -> None:
    with pytest.raises(ValidationError, match="workload_authority_kind"):
        ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            auth_profile_id="workload-profile",
        )


def test_owner_delegation_requires_exact_delegation_id() -> None:
    with pytest.raises(ValidationError, match="workload_delegation_id"):
        ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            auth_profile_id="interactive-profile",
            workload_authority_kind="owner_delegation",
            workload_source_installation_id="calendar-user-source",
        )


def test_owner_delegation_requires_a_distinct_source_installation() -> None:
    with pytest.raises(ValidationError, match="workload_source_installation_id"):
        ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            auth_profile_id="interactive-profile",
            workload_authority_kind="owner_delegation",
            workload_delegation_id="delegation-1",
        )

    with pytest.raises(ValidationError, match="workload_source_installation_id"):
        ProtocolServerConfig(
            url="https://mcp.example.test/mcp",
            auth_mode="oauth_client_credentials",
            auth_profile_id="workload-profile",
            workload_authority_kind="direct",
            workload_source_installation_id="calendar-user-source",
        )


def _write_registry(path: Path, servers: dict[str, object]) -> ProtocolRegistry:
    config_path = path / "server_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )
    return ProtocolRegistry(str(config_path))


def _source_config(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "url": "https://mcp.example.test/mcp",
        "auth_mode": "oauth_user",
        "auth_profile_id": "interactive-profile",
    }
    values.update(updates)
    return values


def _delegated_target(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "url": "https://mcp.example.test/mcp",
        "auth_mode": "oauth_client_credentials",
        "auth_profile_id": "interactive-profile",
        "workload_authority_kind": "owner_delegation",
        "workload_delegation_id": "delegation-1",
        "workload_source_installation_id": "calendar-user-source",
    }
    values.update(updates)
    return values


def test_registry_accepts_distinct_enabled_oauth_user_source(tmp_path: Path) -> None:
    registry = _write_registry(
        tmp_path,
        {
            "calendar-user-source": _source_config(),
            "calendar-workload-target": _delegated_target(),
        },
    )

    assert set(registry.get_all_servers()) == {
        "calendar-user-source",
        "calendar-workload-target",
    }


@pytest.mark.parametrize(
    ("source", "target_updates"),
    [
        (None, {}),
        (_source_config(enabled=False), {}),
        (_source_config(auth_mode="none", auth_profile_id=None), {}),
        (_source_config(url="https://other.example.test/mcp"), {}),
        (_source_config(auth_profile_id="other-profile"), {}),
        (_source_config(), {"workload_source_installation_id": "calendar-workload-target"}),
    ],
)
def test_registry_rejects_invalid_owner_delegation_source_graph(
    tmp_path: Path,
    source: dict[str, object] | None,
    target_updates: dict[str, object],
) -> None:
    servers: dict[str, object] = {
        "calendar-workload-target": _delegated_target(**target_updates),
    }
    if source is not None:
        servers["calendar-user-source"] = source

    registry = _write_registry(tmp_path, servers)

    assert registry.get_all_servers() == {}


def test_workload_feature_and_key_namespace_default_fail_closed(monkeypatch) -> None:
    for name in (
        "TOUKA_MCP_OAUTH_WORKLOAD_ENABLED",
        "TOUKA_MCP_OAUTH_DELEGATION_ENABLED",
        "TOUKA_MCP_OAUTH_WORKLOAD_KEY",
        "TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND",
        "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE",
        "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH",
    ):
        monkeypatch.delenv(name, raising=False)
    config = Settings(_env_file=None)

    assert config.TOUKA_MCP_OAUTH_WORKLOAD_ENABLED is False
    assert config.TOUKA_MCP_OAUTH_DELEGATION_ENABLED is False
    assert config.TOUKA_MCP_OAUTH_WORKLOAD_KEY == ""
    assert config.TOUKA_MCP_OAUTH_WORKLOAD_FILESYSTEM_KIND == "unknown"
    assert config.TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE == "stable"
    assert config.TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH == 1
