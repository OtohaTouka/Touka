"""P8-A cross-process refresh lease convergence with explicit IPC barriers."""

from __future__ import annotations

import asyncio
import base64
import multiprocessing
from pathlib import Path

from pydantic import SecretStr
from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.models import (
    OAuthAuthorizationSlotKey,
    OAuthGrantBinding,
    OAuthTokenSet,
    OAuthTransactionPayload,
)
from src.protocols.mcp.oauth.store import OAuthSqliteStore


def _key_value() -> str:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return f"v1:multiprocess-refresh:{encoded}"


def _refresh_worker(
    database: str,
    key_value: str,
    grant_id: str,
    grant_version: int,
    worker_id: str,
    ready,
    release,
    results,
) -> None:
    async def run() -> None:
        store = OAuthSqliteStore(
            Path(database),
            OAuthKeyRing(current=OAuthKey.parse(key_value)),
        )
        await store.prepare_schema()
        ready.put(worker_id)
        await asyncio.to_thread(release.wait)
        lease = await store.acquire_refresh_lease(
            grant_id,
            grant_version,
            worker_id,
            now=1100.0,
            lease_seconds=30.0,
        )
        results.put((worker_id, lease is not None))

    asyncio.run(run())


async def _prepare_authorized_store(database: Path) -> tuple[str, int]:
    store = OAuthSqliteStore(
        database,
        OAuthKeyRing(current=OAuthKey.parse(_key_value())),
    )
    await store.prepare_schema()
    slot = OAuthAuthorizationSlotKey(
        human_principal_id="human-a",
        execution_agent_id="agent-a",
        installation_id="server-a",
        installation_generation=1,
        auth_profile_id="profile-a",
    )
    transaction = await store.create_transaction(
        slot=slot,
        operation_id="operation-a",
        raw_state="multiprocess-state",
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
            execution_agent_id="agent-a",
            installation_id="server-a",
            installation_generation=1,
            authorization_endpoint="https://issuer.example.test/authorize",
            token_endpoint="https://issuer.example.test/token",
            code_challenge_methods_supported=("S256",),
            response_modes_supported=("form_post",),
            authorization_response_iss_parameter_supported=True,
            metadata_security_fingerprint="metadata-fingerprint",
        ),
        now=1000.0,
        expires_at=1600.0,
    )
    consumed = await store.consume_transaction(
        "multiprocess-state",
        now=1001.0,
        redirect_route="/mcp/oauth/callback",
    )
    assert consumed is not None
    assert await store.commit_authorization(
        transaction_id=transaction.transaction_id,
        expected_slot_version=transaction.slot_version,
        binding=OAuthGrantBinding(
            issuer="https://issuer.example.test",
            resource="https://mcp.example.test/mcp",
            client_id="client-a",
            granted_scopes=frozenset({"tools.read"}),
            profile_generation=1,
            profile_fingerprint="profile-fingerprint",
            registration_version=1,
            metadata_security_fingerprint="metadata-fingerprint",
        ),
        tokens=OAuthTokenSet(
            access_token=SecretStr("access-canary"),
            refresh_token=SecretStr("refresh-canary"),
            token_type="Bearer",
            expires_at=2000.0,
            granted_scopes=frozenset({"tools.read"}),
        ),
        now=1002.0,
    )
    grant = await store.get_active_grant(slot, now=1003.0)
    assert grant is not None
    return grant.grant_id, grant.grant_version


async def _queue_get(queue):
    return await asyncio.to_thread(queue.get, True, 15.0)


async def _join(process) -> None:
    await asyncio.to_thread(process.join, 15.0)
    assert not process.is_alive()
    assert process.exitcode == 0


async def test_refresh_lease_has_one_network_winner_across_spawned_workers(tmp_path):
    database = tmp_path / "mcp_oauth.sqlite3"
    grant_id, grant_version = await _prepare_authorized_store(database)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_refresh_worker,
            args=(
                str(database),
                _key_value(),
                grant_id,
                grant_version,
                worker_id,
                ready,
                release,
                results,
            ),
        )
        for worker_id in ("worker-a", "worker-b")
    ]
    for worker in workers:
        worker.start()
    assert {await _queue_get(ready), await _queue_get(ready)} == {
        "worker-a",
        "worker-b",
    }
    release.set()
    outcomes = [await _queue_get(results), await _queue_get(results)]
    for worker in workers:
        await _join(worker)

    assert sum(won for _worker, won in outcomes) == 1
