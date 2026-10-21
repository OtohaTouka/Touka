"""P8-A cross-process DCR registration lease convergence."""

from __future__ import annotations

import asyncio
import base64
import multiprocessing
from pathlib import Path

from src.protocols.mcp.oauth.keyring import OAuthKey, OAuthKeyRing
from src.protocols.mcp.oauth.registration import OAuthRegistrationKey
from src.protocols.mcp.oauth.store import OAuthSqliteStore


def _key_value() -> str:
    encoded = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
    return f"v1:multiprocess-dcr:{encoded}"


def _dcr_worker(database, key_value, worker_id, ready, release, results) -> None:
    async def run() -> None:
        store = OAuthSqliteStore(
            Path(database),
            OAuthKeyRing(current=OAuthKey.parse(key_value)),
        )
        await store.prepare_schema()
        key = OAuthRegistrationKey(
            profile_id="profile-a",
            profile_generation=1,
            installation_id="server-a",
            installation_generation=1,
            issuer="https://issuer.example.test",
        )
        ready.put(worker_id)
        await asyncio.to_thread(release.wait)
        lease = await store.acquire_registration_lease(
            key,
            worker_id,
            now=1000.0,
            lease_seconds=30.0,
        )
        results.put((worker_id, lease is not None))

    asyncio.run(run())


async def _queue_get(queue):
    return await asyncio.to_thread(queue.get, True, 15.0)


async def test_dcr_lease_has_one_network_winner_across_spawned_workers(tmp_path):
    database = tmp_path / "mcp_oauth.sqlite3"
    store = OAuthSqliteStore(
        database,
        OAuthKeyRing(current=OAuthKey.parse(_key_value())),
    )
    await store.prepare_schema()
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_dcr_worker,
            args=(
                str(database),
                _key_value(),
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
        await asyncio.to_thread(worker.join, 15.0)
        assert not worker.is_alive()
        assert worker.exitcode == 0

    assert sum(won for _worker, won in outcomes) == 1
