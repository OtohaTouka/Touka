"""P8-B Slice 4：版本化 non-secret workload profile SSOT。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

import pytest
from src.protocols.mcp.oauth.workload.models import WorkloadOAuthProfile

if TYPE_CHECKING:
    from pathlib import Path


def _profile_module() -> Any:
    module_name = "src.protocols.mcp.oauth.workload.profile"
    assert importlib.util.find_spec(module_name) is not None, "profile repository is missing"
    return importlib.import_module(module_name)


def _profile(generation: int = 1) -> WorkloadOAuthProfile:
    return WorkloadOAuthProfile(
        profile_id="calendar-workload",
        enabled=True,
        expected_issuer="https://issuer.example.test",
        expected_resource="https://mcp.example.test",
        expected_transport_url="https://mcp.example.test/mcp",
        token_endpoint_auth_method="client_secret_basic",
        requested_scopes=frozenset({"calendar.read"}),
        workload_principal_id="heartbeat:Alice",
        owner_user_id="human-1",
        execution_agent_id="Alice",
        tool_scopes={"calendar.read": frozenset({"calendar.read"})},
        allow_private_network=False,
        generation=generation,
        credential_id="calendar-secret",
    )


@pytest.mark.asyncio
async def test_profile_repository_round_trip_and_generation_cas(tmp_path: Path) -> None:
    repository = _profile_module().WorkloadProfileRepository(
        tmp_path / "mcp_oauth_workload_profiles.json"
    )

    await repository.put(_profile(), version="1.0.0", author="admin-1")
    assert await repository.get("calendar-workload") == _profile()

    with pytest.raises(_profile_module().WorkloadProfileConflictError):
        await repository.put(_profile(), version="1.0.1", author="admin-1")

    await repository.put(_profile(2), version="1.0.1", author="admin-1")
    document = await repository.load()
    assert document.version == "1.0.1"
    assert document.profiles == {"calendar-workload": _profile(2)}


@pytest.mark.asyncio
async def test_same_generation_profile_contention_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    module = _profile_module()
    path = tmp_path / "mcp_oauth_workload_profiles.json"
    first = module.WorkloadProfileRepository(path)
    second = module.WorkloadProfileRepository(path)
    candidates = (
        _profile(),
        _profile().model_copy(update={"enabled": False}),
    )

    results = await asyncio.gather(
        first.put(candidates[0], version="1.0.0", author="admin-1"),
        second.put(candidates[1], version="1.0.0", author="admin-2"),
        return_exceptions=True,
    )

    winners = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [
        result
        for result in results
        if isinstance(result, module.WorkloadProfileConflictError)
    ]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert await first.get("calendar-workload") == winners[0]
