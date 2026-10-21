"""P8-B Slice 2：workload key namespace 與 P8-A key material 分離。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from src.protocols.mcp.oauth.keyring import OAuthKeyRingProvider

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


WORKLOAD_CURRENT = "TOUKA_MCP_OAUTH_WORKLOAD_KEY"
WORKLOAD_RETIRED = "TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS"
WORKLOAD_PHASE = "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE"
WORKLOAD_EPOCH = "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH"


def _serialized(kid: str, byte: bytes) -> str:
    encoded = base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")
    return f"v1:{kid}:{encoded}"


@pytest.mark.asyncio
async def test_managed_bootstrap_persists_only_workload_key_name(tmp_path: Path) -> None:
    persisted: dict[str, str] = {}

    def _persist(name: str, value: str) -> bool:
        persisted[name] = value
        return True

    provider = OAuthKeyRingProvider(
        source="managed",
        current_value="",
        retiring_json="[]",
        lock_path=tmp_path / "workload-key.lock",
        bootstrap_enabled=True,
        persist=_persist,
        reload_current=lambda: "",
        current_name=WORKLOAD_CURRENT,
        retiring_name=WORKLOAD_RETIRED,
        rotation_phase_name=WORKLOAD_PHASE,
        key_epoch_name=WORKLOAD_EPOCH,
    )

    ring = await provider.load()

    assert ring.current.kid.startswith("managed-")
    assert set(persisted) == {WORKLOAD_CURRENT}
    assert "TOUKA_MCP_OAUTH_KEY" not in persisted


@pytest.mark.asyncio
async def test_rotation_reads_and_writes_only_workload_namespace(tmp_path: Path) -> None:
    values = {
        WORKLOAD_CURRENT: _serialized("workload-old", b"a"),
        WORKLOAD_RETIRED: "[]",
    }
    writes: list[dict[str, str]] = []

    def _persist_many(updates: dict[str, str]) -> bool:
        writes.append(dict(updates))
        values.update(updates)
        return True

    def _reload_values() -> Mapping[str, str]:
        return dict(values)

    provider = OAuthKeyRingProvider(
        source="managed",
        current_value=values[WORKLOAD_CURRENT],
        retiring_json="[]",
        lock_path=tmp_path / "workload-key.lock",
        bootstrap_enabled=True,
        persist=lambda _name, _value: True,
        persist_many=_persist_many,
        reload_values=_reload_values,
        current_name=WORKLOAD_CURRENT,
        retiring_name=WORKLOAD_RETIRED,
        rotation_phase_name=WORKLOAD_PHASE,
        key_epoch_name=WORKLOAD_EPOCH,
    )
    candidate = provider.create_rotation_candidate()

    ring = await provider.prepare_rotation(epoch=2, candidate=candidate)

    assert ring.current.kid == candidate.kid
    assert ring.retiring[0].kid == "workload-old"
    assert set(writes[0]) == {
        WORKLOAD_CURRENT,
        WORKLOAD_RETIRED,
        WORKLOAD_PHASE,
        WORKLOAD_EPOCH,
    }
    assert not any(
        key in writes[0]
        for key in (
            "TOUKA_MCP_OAUTH_KEY",
            "TOUKA_MCP_OAUTH_RETIRED_KEYS",
            "TOUKA_MCP_OAUTH_ROTATION_PHASE",
            "TOUKA_MCP_OAUTH_KEY_EPOCH",
        )
    )
