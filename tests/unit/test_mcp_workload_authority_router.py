"""P8-B Slice 5：direct／owner-delegation authority 不得混線。"""

from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest
from src.protocols.registry import ProtocolServerConfig


def _router() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.authority_router"
    assert importlib.util.find_spec(module_name) is not None, "authority router seam is missing"
    return importlib.import_module(module_name)


class _Authorizer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.authorized = 0
        self.revalidated = 0

    async def authorize_operation(self, **_kwargs: Any) -> str:
        self.authorized += 1
        return self.name

    async def revalidate(self, _ticket: Any, *, now: float) -> str:
        assert now == 10.0
        self.revalidated += 1
        return self.name


def _config(kind: str) -> ProtocolServerConfig:
    values: dict[str, Any] = {
        "url": "https://mcp.example.test/mcp",
        "auth_mode": "oauth_client_credentials",
        "auth_profile_id": "profile-a",
        "workload_authority_kind": kind,
    }
    if kind == "owner_delegation":
        values["workload_delegation_id"] = "delegation-a"
        values["workload_source_installation_id"] = "calendar-user-source"
    return ProtocolServerConfig(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [("direct", "direct"), ("owner_delegation", "delegated")],
)
async def test_router_selects_only_configured_authority(kind: str, expected: str) -> None:
    direct = _Authorizer("direct")
    delegated = _Authorizer("delegated")
    router = _router().WorkloadAuthorityRouter(
        config_lookup=lambda _name: _config(kind),
        direct=direct,
        delegated=delegated,
    )

    result = await router.authorize_operation(server_name="server-a")

    assert result == expected
    assert direct.authorized == (1 if kind == "direct" else 0)
    assert delegated.authorized == (1 if kind == "owner_delegation" else 0)


@pytest.mark.asyncio
async def test_router_revalidation_uses_immutable_ticket_authority() -> None:
    direct = _Authorizer("direct")
    delegated = _Authorizer("delegated")
    router = _router().WorkloadAuthorityRouter(
        config_lookup=lambda _name: _config("direct"),
        direct=direct,
        delegated=delegated,
    )

    result = await router.revalidate(
        SimpleNamespace(authority_kind="workload", installation_id="server-a"),
        now=10.0,
    )

    assert result == "direct"
    assert direct.revalidated == 1
