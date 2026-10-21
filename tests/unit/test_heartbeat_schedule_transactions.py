"""Real HB auth, storage and scheduler transitions against owned fixtures."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient

from ToukaHeartbeat import awaken
from ToukaHeartbeat.errors import HeartbeatError
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

SERVICE_TOKEN = "synthetic-heartbeat-transaction"  # noqa: S105


@pytest.fixture
def transaction_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.json"
    storage = StorageService(path, env_token=SERVICE_TOKEN)
    storage.save_config({"heartbeat_token": SERVICE_TOKEN, "schedules": []})
    engine = AsyncIOScheduler(timezone=awaken.TAIPEI_TZ)
    monkeypatch.setattr(awaken, "storage", storage)
    provider = StorageTokenProvider(storage)
    monkeypatch.setattr(awaken, "token_provider", provider)
    monkeypatch.setattr(awaken, "proxy_client", ProxyClient(provider))
    monkeypatch.setattr(awaken, "scheduler", engine)
    outbound = AsyncMock(return_value=200)
    monkeypatch.setattr(awaken.proxy_client, "trigger_heartbeat", outbound)
    client = TestClient(awaken.app, raise_server_exceptions=False)
    yield client, storage, engine, outbound
    outbound.assert_not_called()


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def create(client: TestClient, identifier: str = "one", **fields) -> dict:
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"id": identifier, "agent_id": "DebugAgent", "interval_minutes": 10, **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


def error_reason(response) -> str:
    assert response.status_code == 503, response.text
    return response.json()["detail"]["reason"]


def test_engine_failure_restores_original_bytes_and_exact_jobs(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, engine, _outbound = transaction_app
    create(client)
    old = storage.config_file.read_bytes()
    old_jobs = [(job.id, job.trigger.interval, job.kwargs) for job in engine.get_jobs()]
    real_add = engine.add_job
    attempts = []

    def fail_first(*args, **kwargs):
        attempts.append(kwargs["id"])
        if len(attempts) == 1:
            raise RuntimeError("synthetic-engine-secret")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(engine, "add_job", fail_first)
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"id": "one", "agent_id": "DebugAgent", "interval_minutes": 30},
    )
    assert error_reason(response) == "schedule_apply_failed"
    assert "synthetic-engine-secret" not in response.text
    assert storage.config_file.read_bytes() == old
    assert [(job.id, job.trigger.interval, job.kwargs) for job in engine.get_jobs()] == old_jobs


def test_failed_engine_compensation_degrades_and_blocks_further_mutations(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, engine, _outbound = transaction_app
    create(client)
    monkeypatch.setattr(engine, "add_job", Mock(side_effect=RuntimeError("owned-engine-failure")))
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"id": "one", "agent_id": "DebugAgent", "interval_minutes": 30},
    )
    assert error_reason(response) == "schedule_recovery_required"
    assert client.get("/status").json()["status"] == "degraded"
    retained = storage.config_file.read_bytes()
    assert (
        error_reason(client.delete("/v1/schedules/one", headers=headers()))
        == "schedule_recovery_required"
    )
    assert storage.config_file.read_bytes() == retained


@pytest.mark.asyncio
async def test_failed_persistence_compensation_keeps_unknown_and_blocks_job_dispatch(
    transaction_app, monkeypatch
) -> None:
    client, storage, engine, outbound = transaction_app
    create(client)
    old = storage.config_file.read_bytes()
    old_job = engine.get_jobs()[0]
    real_add = engine.add_job
    calls = []

    def fail_first(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("owned-apply-failure")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(engine, "add_job", fail_first)
    monkeypatch.setattr(
        storage, "restore_bytes", Mock(side_effect=OSError("owned-restore-failure"))
    )
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"id": "one", "agent_id": "DebugAgent", "interval_minutes": 40},
    )
    assert error_reason(response) == "schedule_recovery_required"
    assert response.json()["detail"]["persisted"] == "UNKNOWN"
    assert storage.config_file.read_bytes() != old
    assert await old_job.func(**old_job.kwargs) == 503
    assert outbound.await_count == 0


def test_precommit_save_failure_has_structured_failure_and_no_engine_changes(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, engine, _outbound = transaction_app
    create(client)
    old = storage.config_file.read_bytes()
    jobs = engine.get_jobs()
    monkeypatch.setattr(
        storage,
        "save_config",
        Mock(side_effect=HeartbeatError("config_write_failed", phase="write")),
    )
    response = client.delete("/v1/schedules/one", headers=headers())
    assert error_reason(response) == "config_write_failed"
    assert storage.config_file.read_bytes() == old
    assert engine.get_jobs() == jobs


def test_similar_schedule_ids_never_remove_each_others_jobs(transaction_app) -> None:
    client, _storage, engine, _outbound = transaction_app
    create(client, "foo")
    create(client, "foo_bar")
    before = {job.id for job in engine.get_jobs()}
    assert len(before) == 2
    response = client.delete("/v1/schedules/foo", headers=headers())
    assert response.status_code == 200
    assert len(engine.get_jobs()) == 1
    assert {job.id for job in engine.get_jobs()} < before
    assert client.get("/v1/schedules", headers=headers()).json()[0]["id"] == "foo_bar"


def test_revision_conflict_has_no_storage_or_engine_side_effect(transaction_app) -> None:
    client, storage, engine, _outbound = transaction_app
    before = client.get("/v1/schedules", headers=headers())
    revision = before.headers.get("ETag")
    assert revision is not None
    create(client)
    current = storage.config_file.read_bytes()
    jobs = engine.get_jobs()
    response = client.post(
        "/v1/schedules",
        headers={**headers(), "If-Match": revision},
        json={"id": "another", "agent_id": "DebugAgent"},
    )
    assert response.status_code == 409
    assert storage.config_file.read_bytes() == current
    assert engine.get_jobs() == jobs


def test_invalid_model_errors_do_not_echo_original_input(transaction_app) -> None:
    client, storage, engine, _outbound = transaction_app
    old = storage.config_file.read_bytes()
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"agent_id": "DebugAgent", "interval_minutes": "synthetic-secret-input"},
    )
    assert response.status_code == 422
    assert "synthetic-secret-input" not in response.text
    assert storage.config_file.read_bytes() == old
    assert engine.get_jobs() == []


def test_invalid_existing_configuration_starts_degraded_without_jobs(transaction_app) -> None:
    _client, storage, engine, _outbound = transaction_app
    original = b'{"heartbeat_token":"synthetic-retained",'
    storage.config_file.write_bytes(original)
    with TestClient(awaken.app, raise_server_exceptions=False) as client:
        response = client.get("/status")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
    assert storage.config_file.read_bytes() == original
    assert engine.get_jobs() == []


def test_update_retains_compatible_schedule_fields_and_presence(transaction_app) -> None:
    client, storage, _engine, _outbound = transaction_app
    create(client, extension={"retain": None})
    response = client.post(
        "/v1/schedules",
        headers=headers(),
        json={"id": "one", "agent_id": "DebugAgent", "active": False},
    )
    assert response.status_code == 200
    persisted = json.loads(storage.config_file.read_bytes())["schedules"][0]
    assert persisted["extension"] == {"retain": None}
    assert persisted["interval_minutes"] == 10


def test_running_job_restoration_keeps_trigger_next_run_time_and_options(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _storage, _engine, _outbound = transaction_app
    engine = BackgroundScheduler(timezone=awaken.TAIPEI_TZ)
    monkeypatch.setattr(awaken, "scheduler", engine)
    create(client)
    engine.start(paused=True)
    try:
        previous = engine.get_jobs()[0]
        snapshot = (
            previous.trigger,
            previous.next_run_time,
            previous.kwargs,
            previous.coalesce,
            previous.max_instances,
            previous.misfire_grace_time,
        )
        real_add = engine.add_job
        attempts = []

        def fail_once(*args, **kwargs):
            attempts.append(kwargs["id"])
            if len(attempts) == 1:
                raise RuntimeError("owned-apply-failure")
            return real_add(*args, **kwargs)

        monkeypatch.setattr(engine, "add_job", fail_once)
        response = client.post(
            "/v1/schedules",
            headers=headers(),
            json={"id": "one", "agent_id": "DebugAgent", "interval_minutes": 30},
        )
        assert error_reason(response) == "schedule_apply_failed"
        restored = engine.get_jobs()[0]
        assert (
            restored.trigger,
            restored.next_run_time,
            restored.kwargs,
            restored.coalesce,
            restored.max_instances,
            restored.misfire_grace_time,
        ) == snapshot
    finally:
        engine.shutdown(wait=False)


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_directory_sync_failure_reports_compensation_result(
    transaction_app, monkeypatch: pytest.MonkeyPatch, recovery_fails: bool
) -> None:
    client, storage, engine, _outbound = transaction_app
    create(client)
    old = storage.config_file.read_bytes()
    old_ids = [job.id for job in engine.get_jobs()]
    calls = []

    def fail_sync():
        calls.append(True)
        if recovery_fails or len(calls) == 1:
            raise OSError("owned-directory-sync")
        return "DIRECTORY_FSYNC_UNSUPPORTED"

    monkeypatch.setattr(storage, "_sync_directory", fail_sync)
    response = client.delete("/v1/schedules/one", headers=headers())
    assert error_reason(response) == (
        "schedule_recovery_required" if recovery_fails else "config_write_failed"
    )
    assert response.json()["detail"]["committed"] is True
    assert storage.config_file.read_bytes() == old
    assert [job.id for job in engine.get_jobs()] == old_ids
    if recovery_fails:
        assert response.json()["detail"]["persisted"] == "UNKNOWN"
        assert client.get("/status").json()["status"] == "degraded"
    else:
        assert response.json()["detail"]["persisted"] == "OLD"


def test_parallel_local_updates_keep_both_successful_changes(transaction_app) -> None:
    client, storage, engine, _outbound = transaction_app
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda identifier: create(client, identifier), ["first", "second"]))
    assert {row["id"] for row in results} == {"first", "second"}
    assert {row["id"] for row in storage.load_config()["schedules"]} == {"first", "second"}
    assert len(engine.get_jobs()) == 2


def test_revision_preserves_returned_presence_and_ignores_service_token(transaction_app) -> None:
    client, storage, _engine, _outbound = transaction_app
    config = {
        "heartbeat_token": SERVICE_TOKEN,
        "schedules": [{"id": "one", "agent_id": "DebugAgent", "init_prompt": None}],
    }
    storage.save_config(config)
    response = client.get("/v1/schedules", headers=headers())
    assert response.json() == config["schedules"]
    previous_revision = response.headers["ETag"]
    config["heartbeat_token"] = "synthetic-another-token"  # noqa: S105
    storage.save_config(config)
    assert client.get("/v1/schedules", headers=headers()).headers["ETag"] == previous_revision


def test_scheduler_start_failure_keeps_api_degraded_and_starts_no_jobs(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, storage, engine, _outbound = transaction_app
    storage.save_config({"schedules": [{"id": "one", "agent_id": "DebugAgent"}]})
    original = storage.config_file.read_bytes()
    monkeypatch.setattr(engine, "start", Mock(side_effect=RuntimeError("owned-start-failure")))
    with TestClient(awaken.app, raise_server_exceptions=False) as client:
        assert client.get("/status").json()["status"] == "degraded"
    assert storage.config_file.read_bytes() == original
    assert engine.get_jobs() == []


@pytest.mark.asyncio
async def test_failed_physical_pause_still_blocks_new_dispatch_from_real_job(
    transaction_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, storage, _engine, outbound = transaction_app
    engine = BackgroundScheduler(timezone=awaken.TAIPEI_TZ)
    monkeypatch.setattr(awaken, "scheduler", engine)
    create(client)
    engine.start(paused=True)
    try:
        job = engine.get_jobs()[0]
        monkeypatch.setattr(engine, "pause", Mock(side_effect=RuntimeError("owned-pause-failure")))
        storage.config_file.write_bytes(b"broken owned config")
        response = client.get("/v1/schedules", headers=headers())
        assert response.status_code == 503
        assert client.get("/status").json()["engine"] == "UNKNOWN"
        result = await job.func(**job.kwargs)
        calls = outbound.await_count
        outbound.reset_mock()
        assert result == 503
        assert calls == 0
    finally:
        engine.shutdown(wait=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("failure", ["parse", "read"])
async def test_mutation_read_failure_degrades_existing_engine(
    transaction_app, monkeypatch, method, failure
) -> None:
    client, storage, engine, outbound = transaction_app
    create(client)
    assert client.get("/status").json()["persisted"] == "CURRENT"
    job = engine.get_jobs()[0]
    original = b"broken owned config"
    if failure == "parse":
        storage.config_file.write_bytes(original)
    else:
        monkeypatch.setattr(
            storage, "read_snapshot", Mock(side_effect=HeartbeatError("config_read_failed"))
        )
    if method == "POST":
        response = client.post(
            "/v1/schedules", headers=headers(), json={"id": "one", "agent_id": "DebugAgent"}
        )
    elif method == "DELETE":
        response = client.delete("/v1/schedules/one", headers=headers())
    else:
        response = client.get("/v1/schedules", headers=headers())
    assert response.status_code == 503
    assert client.get("/status").json()["status"] == "degraded"
    assert client.get("/status").json()["persisted"] == "UNKNOWN"
    assert await job.func(**job.kwargs) == 503
    assert outbound.await_count == 0
    if failure == "parse":
        assert storage.config_file.read_bytes() == original
