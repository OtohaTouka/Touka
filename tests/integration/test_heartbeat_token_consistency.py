"""Synthetic credentials through real Heartbeat ingress and outbound boundaries."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from src import config as proxy_config
from src.core.exceptions import ToukaException
from src.routers import heartbeat as proxy_router
from starlette.requests import Request

from ToukaHeartbeat import awaken, proxy_client
from ToukaHeartbeat.config import HeartbeatSettings
from ToukaHeartbeat.errors import HeartbeatError
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import Credential, StorageTokenProvider

# Each restart child imports the whole Proxy and Heartbeat in a fresh interpreter: about
# 20 seconds here, over 30 on a busy machine. The result is only its exit code and pass
# marker, so this limit catches a hung child and is not a speed check.
CHILD_HANG_GUARD_SECONDS = 300


def _install_storage(tmp_path, monkeypatch, shared: str, saved: str):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"heartbeat_token": saved, "schedules": []}), encoding="utf-8")
    storage = StorageService(path, env_token=shared)
    monkeypatch.setattr(awaken, "storage", storage)
    provider = StorageTokenProvider(storage)
    monkeypatch.setattr(awaken, "token_provider", provider)
    monkeypatch.setattr(awaken, "proxy_client", proxy_client.ProxyClient(provider))
    return storage


def _capture_tasks(monkeypatch):
    tasks = Mock(side_effect=lambda coroutine: coroutine.close())
    monkeypatch.setattr(awaken, "asyncio", SimpleNamespace(create_task=tasks))
    return tasks


def test_env_token_wins_for_manual_trigger(tmp_path, monkeypatch):
    """Shared token B authenticates even when the saved local fallback is A."""
    _install_storage(tmp_path, monkeypatch, "synthetic-shared-B", "synthetic-local-A")
    tasks = _capture_tasks(monkeypatch)
    response = TestClient(awaken.app).post(
        "/trigger", json={"token": "synthetic-shared-B", "agent_id": "DebugAgent"}
    )
    assert response.status_code == 200
    tasks.assert_called_once()


def test_saved_token_is_rejected_when_shared_token_exists(tmp_path, monkeypatch):
    """The lower-priority saved token cannot authorize a trigger."""
    _install_storage(tmp_path, monkeypatch, "synthetic-shared-B", "synthetic-local-A")
    tasks = _capture_tasks(monkeypatch)
    response = TestClient(awaken.app).post(
        "/trigger", json={"token": "synthetic-local-A", "agent_id": "DebugAgent"}
    )
    assert response.status_code == 401
    tasks.assert_not_called()


@pytest.mark.parametrize("shared", ["", "   "])
def test_local_fallback_does_not_dispatch_without_shared_setting(tmp_path, monkeypatch, shared):
    """Management fallback does not establish Proxy readiness or create a task."""
    _install_storage(tmp_path, monkeypatch, shared, "synthetic-local-A")
    tasks = _capture_tasks(monkeypatch)
    response = TestClient(awaken.app).post(
        "/trigger", json={"token": "synthetic-local-A", "agent_id": "DebugAgent"}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HEARTBEAT_SHARED_CREDENTIAL_NOT_READY"
    tasks.assert_not_called()


@pytest.mark.asyncio
async def test_empty_shared_client_does_not_send_http(tmp_path, monkeypatch):
    """A client with no shared credential must fail before opening transport."""
    requests = []
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))
    monkeypatch.setattr(
        proxy_client.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    provider = StorageTokenProvider(StorageService(tmp_path / "config.json"))
    client = proxy_client.ProxyClient(provider, api_url="http://proxy.invalid/v1/heartbeat/awaken")
    result = await client.trigger_heartbeat("DebugAgent", 1, None)
    assert result == 503
    assert requests == []
    assert not (tmp_path / "config.json").exists()


def _verify_at_proxy(value: str) -> str:
    return proxy_router.verify_heartbeat_token(
        Request({"type": "http", "headers": []}),
        proxy_router.HeartbeatAwakenRequest(token=value, agent_id="DebugAgent"),
    )


@pytest.mark.asyncio
async def test_env_token_wins_for_management_trigger_and_real_outbound(tmp_path, monkeypatch):
    """Real API -> real client -> captured transport -> real Proxy validator."""
    storage = _install_storage(tmp_path, monkeypatch, "synthetic-shared-B", "synthetic-local-A")
    monkeypatch.setattr(proxy_router, "HEARTBEAT_TOKEN", "synthetic-shared-B")
    original_client = httpx.AsyncClient
    requests = []
    completed = asyncio.Event()

    def receive(request):
        body = json.loads(request.content)
        assert _verify_at_proxy(body["token"]) == body["token"]
        requests.append(body)
        completed.set()
        return httpx.Response(200)

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(proxy_client.httpx, "AsyncClient", factory)
    payload = {
        "token": "synthetic-shared-B",
        "agent_id": "DebugAgent",
        "pulse_count": 3,
        "pulse_slots": 9,
        "init_prompt": "synthetic prompt",
        "model_override": "synthetic-model",
        "security_level": "safe",
        "session_mode": "independent",
        "session_id": "owned-session",
    }
    before = storage.config_file.read_bytes()
    async with original_client(
        transport=httpx.ASGITransport(app=awaken.app), base_url="http://hb.invalid"
    ) as api:
        good = await api.get(
            "/v1/schedules", headers={"Authorization": "Bearer synthetic-shared-B"}
        )
        bad = await api.get("/v1/schedules", headers={"Authorization": "Bearer synthetic-local-A"})
        result = await api.post("/trigger", json=payload)
        await asyncio.wait_for(completed.wait(), timeout=0.2)
    assert good.status_code == result.status_code == 200
    assert bad.status_code == 401
    assert requests == [payload]
    assert storage.config_file.read_bytes() == before
    with pytest.raises(ToukaException):
        _verify_at_proxy("synthetic-local-A")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shared", "saved", "ready"),
    [
        ("same-value", "same-value", True),
        (" shared-B ", "local-A", True),
        ("shared-B", "", True),
        ("shared-B", None, True),
        ("", "local-A", False),
        ("  ", "local-A", False),
        ("", "", False),
        ("", None, False),
        ("  ", "  ", False),
    ],
)
async def test_credential_source_matrix(tmp_path, monkeypatch, shared, saved, ready):
    """Source presence never implies agreement with the remote service."""
    path = tmp_path / "config.json"
    if saved is not None:
        path.write_text(json.dumps({"heartbeat_token": saved, "schedules": []}), encoding="utf-8")
    storage = StorageService(path, env_token=shared)
    provider = StorageTokenProvider(storage)
    result = provider.resolve()
    assert result.shared_ready is ready
    assert result.source == ("shared_setting" if ready else "local_fallback")
    assert result.value and result.value.strip() == result.value
    assert result.value not in repr(result)
    assert result.accepts(result.value) and not result.accepts("")
    assert not result.accepts("不相符的合成值")
    if ready:
        assert result.value == shared.strip()
        assert provider.resolve(require_shared=True) == result
    else:
        with pytest.raises(HeartbeatError) as error:
            provider.resolve(require_shared=True)
        assert error.value.reason == "shared_credential_not_ready"
        opened = Mock(side_effect=AssertionError("transport must not be opened"))
        monkeypatch.setattr(proxy_client.httpx, "AsyncClient", opened)
        assert (
            await proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None) == 503
        )
        opened.assert_not_called()
        if saved and saved.strip():
            assert result.value == saved.strip()
        assert storage.get_token() == result.value


@pytest.mark.parametrize("value", ["", "   ", "不相符"])
def test_empty_or_invalid_trigger_never_creates_a_task(tmp_path, monkeypatch, value):
    _install_storage(tmp_path, monkeypatch, "", "")
    tasks = _capture_tasks(monkeypatch)
    response = TestClient(awaken.app).post(
        "/trigger", json={"token": value, "agent_id": "DebugAgent"}
    )
    assert response.status_code == 401
    tasks.assert_not_called()


def test_local_generation_failure_preserves_file_and_creates_no_task(tmp_path, monkeypatch):
    storage = _install_storage(tmp_path, monkeypatch, "", "")
    before = storage.config_file.read_bytes()
    tasks = _capture_tasks(monkeypatch)
    failure = Mock(side_effect=HeartbeatError("config_write_failed", phase="replace"))
    monkeypatch.setattr(storage, "save_config", failure)
    response = TestClient(awaken.app).post("/trigger", json={"token": "", "agent_id": "DebugAgent"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HEARTBEAT_CONFIG_WRITE_FAILED"
    assert storage.config_file.read_bytes() == before
    failure.assert_called_once()
    tasks.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [401, 403, 307, "timeout", "connect"])
async def test_proxy_client_single_attempt_and_safe_failure(tmp_path, monkeypatch, caplog, outcome):
    provider = StorageTokenProvider(StorageService(tmp_path / "config.json", "synthetic-shared"))
    original_client = httpx.AsyncClient
    requests = []

    def receive(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("sensitive-synthetic-request-detail", request=request)
        if outcome == "connect":
            raise httpx.ConnectError("sensitive-synthetic-request-detail", request=request)
        return httpx.Response(outcome, headers={"Location": "http://other.invalid/"})

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setattr(proxy_client.httpx, "AsyncClient", factory)
    result = await proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None)
    assert result == (500 if isinstance(outcome, str) else outcome)
    assert len(requests) == 1
    assert "sensitive-synthetic-request-detail" not in caplog.text
    assert "synthetic-shared" not in caplog.text
    assert all(
        record.exc_info is None
        for record in caplog.records
        if record.name == "ToukaHeartbeat.ProxyClient"
    )


@pytest.mark.asyncio
async def test_proxy_client_has_overall_deadline(tmp_path, monkeypatch):
    provider = StorageTokenProvider(StorageService(tmp_path / "config.json", "synthetic-shared"))
    original_client = httpx.AsyncClient
    requests = []

    async def receive(request):
        requests.append(request)
        await asyncio.Event().wait()

    monkeypatch.setattr(proxy_client, "_OUTBOUND_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        proxy_client.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(receive), **kwargs),
    )
    result = await asyncio.wait_for(
        proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None), timeout=0.2
    )
    assert result == 500
    assert len(requests) == 1


def _owned_proxy_persistence(tmp_path, monkeypatch):
    env = tmp_path / "shared.env"
    env.write_text("HEARTBEAT_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(proxy_config, "resolve_env_file_path", lambda: env)
    monkeypatch.setattr(proxy_config, "is_bootstrap_enabled", lambda: True)
    monkeypatch.setenv("HEARTBEAT_TOKEN", "")
    monkeypatch.setattr(proxy_config, "HEARTBEAT_TOKEN", "")
    monkeypatch.setattr(proxy_config.settings, "HEARTBEAT_TOKEN", "")
    return env


def test_integrated_startup_resolves_shared_token_after_real_persist(tmp_path, monkeypatch):
    """Real managed persistence precedes fresh HB settings, without a live service."""
    env = _owned_proxy_persistence(tmp_path, monkeypatch)
    before = HeartbeatSettings(_env_file=env)
    assert before.HEARTBEAT_TOKEN == ""
    token = proxy_config._resolve_heartbeat_token("")
    assert token and f"HEARTBEAT_TOKEN={token}" in env.read_text(encoding="utf-8")
    monkeypatch.delenv("HEARTBEAT_TOKEN")
    after = HeartbeatSettings(_env_file=env)
    assert before.HEARTBEAT_TOKEN == ""  # An already-created snapshot did not reload.
    provider = StorageTokenProvider(
        StorageService(tmp_path / "heartbeat.json", after.HEARTBEAT_TOKEN)
    )
    monkeypatch.setattr(proxy_router, "HEARTBEAT_TOKEN", token)
    credential = provider.resolve(require_shared=True)
    assert credential.accepts(token)
    assert _verify_at_proxy(credential.value) == token
    assert not (tmp_path / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_supported_restart_replaces_all_secret_snapshots(tmp_path, monkeypatch):
    """Two owned setting applications recreate both consumers; no hot-rotation claim."""
    env = _owned_proxy_persistence(tmp_path, monkeypatch)
    prior = None
    for value in ["synthetic-rotation-A", "synthetic-rotation-B", "synthetic-rotation-C"]:
        assert proxy_config.persist_generated_secret("HEARTBEAT_TOKEN", value)
        if prior:
            old_provider, old_value = prior
            assert old_provider.resolve(require_shared=True).value == old_value
            assert _verify_at_proxy(old_value) == old_value
            with pytest.raises(ToukaException):
                _verify_at_proxy(value)
        monkeypatch.delenv("HEARTBEAT_TOKEN")
        hb_settings = HeartbeatSettings(_env_file=env)
        selected = proxy_config._resolve_heartbeat_token(hb_settings.HEARTBEAT_TOKEN)
        monkeypatch.setattr(proxy_router, "HEARTBEAT_TOKEN", selected)
        storage = StorageService(tmp_path / "heartbeat.json", hb_settings.HEARTBEAT_TOKEN)
        storage.save_config({"heartbeat_token": "unchanged-local-fallback", "schedules": []})
        provider = StorageTokenProvider(storage)
        monkeypatch.setattr(awaken, "token_provider", provider)
        tasks = _capture_tasks(monkeypatch)
        assert provider.resolve(require_shared=True).accepts(value)
        awaken.verify_schedule_token(
            Request({"type": "http", "headers": [(b"authorization", f"Bearer {value}".encode())]})
        )
        assert (
            await awaken.handle_manual_trigger(
                awaken.TriggerPayload(token=value, agent_id="DebugAgent")
            )
        )["status"] == "dispatched"
        tasks.assert_called_once()
        original_client = httpx.AsyncClient
        sent = []

        def receive(request, observed=sent):
            body = json.loads(request.content)
            observed.append(_verify_at_proxy(body["token"]))
            return httpx.Response(200)

        with monkeypatch.context() as outbound_patch:
            outbound_patch.setattr(
                proxy_client.httpx,
                "AsyncClient",
                lambda _client=original_client, _handler=receive, **kwargs: _client(
                    transport=httpx.MockTransport(_handler), **kwargs
                ),
            )
            assert (
                await proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None)
                == 200
            )
        assert sent == [value]
        if prior:
            assert not provider.resolve().accepts(prior[1])
            with pytest.raises(ToukaException):
                _verify_at_proxy(prior[1])
        prior = provider, value


@pytest.mark.asyncio
async def test_persistence_failure_never_enables_shared_outbound(tmp_path, monkeypatch):
    env = _owned_proxy_persistence(tmp_path, monkeypatch)
    before = env.read_bytes()
    monkeypatch.setattr(proxy_config, "set_key", Mock(side_effect=OSError("owned-write-failure")))
    assert proxy_config._resolve_heartbeat_token("") == ""
    assert env.read_bytes() == before
    settings = HeartbeatSettings(_env_file=env)
    provider = StorageTokenProvider(StorageService(tmp_path / "hb.json", settings.HEARTBEAT_TOKEN))
    opened = Mock(side_effect=AssertionError("no HTTP transport"))
    monkeypatch.setattr(proxy_client.httpx, "AsyncClient", opened)
    assert await proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None) == 503
    opened.assert_not_called()


def test_local_fallback_management_remains_available(tmp_path, monkeypatch):
    """Local management is compatible; it does not imply shared outbound readiness."""
    _install_storage(tmp_path, monkeypatch, "", "synthetic-local-only")
    tasks = _capture_tasks(monkeypatch)
    api = TestClient(awaken.app)
    result = api.get("/v1/schedules", headers={"Authorization": "Bearer synthetic-local-only"})
    blocked = api.post("/trigger", json={"token": "synthetic-local-only", "agent_id": "DebugAgent"})
    assert result.status_code == 200
    assert blocked.status_code == 503
    tasks.assert_not_called()


@pytest.mark.asyncio
async def test_each_boundary_uses_one_resolution_snapshot(tmp_path, monkeypatch):
    """Readiness and the compared/sent value are from one result per boundary."""
    storage = _install_storage(tmp_path, monkeypatch, "synthetic-shared", "local")
    provider = StorageTokenProvider(storage)
    actual_resolve = provider.resolve
    resolver = Mock(side_effect=lambda **kwargs: actual_resolve(**kwargs))
    monkeypatch.setattr(provider, "resolve", resolver)
    monkeypatch.setattr(awaken, "token_provider", provider)
    tasks = _capture_tasks(monkeypatch)
    await awaken.handle_manual_trigger(
        awaken.TriggerPayload(token="synthetic-shared", agent_id="DebugAgent")
    )
    resolver.assert_called_once_with()
    tasks.assert_called_once()
    resolver.reset_mock()
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        proxy_client.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kwargs
        ),
    )
    assert await proxy_client.ProxyClient(provider).trigger_heartbeat("DebugAgent", 1, None) == 200
    resolver.assert_called_once_with(require_shared=True)


def test_empty_resolved_credential_never_authenticates():
    """A faulty/custom storage returning empty cannot authenticate empty input."""
    result = Credential("", "local_fallback", False)
    assert not result.accepts("")
    assert not result.accepts("anything")


def test_fresh_processes_load_persisted_rotation_without_assigning_consumers(tmp_path, monkeypatch):
    """Actual fresh imports reload the shared file; no consumer token is patched."""
    env = _owned_proxy_persistence(tmp_path, monkeypatch)
    probe = Path(__file__).resolve().parents[1] / "unit" / "test_heartbeat_env_selection.py"
    old = None
    for index, value in enumerate(["synthetic-fresh-A", "synthetic-fresh-B", "synthetic-fresh-C"]):
        assert proxy_config.persist_generated_secret("HEARTBEAT_TOKEN", value)
        owned = tmp_path / f"restart-{index}"
        owned.mkdir()
        (owned / "probe.json").write_text(
            json.dumps({"env_file": str(env), "expected": value, "old": old}), encoding="utf-8"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-B", str(probe), "--restart-child", str(owned)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CHILD_HANG_GUARD_SECONDS,
            check=False,
        )
        (owned / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (owned / "stderr.log").write_text(result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        assert "FRESH_RESTART_IMPORT_AND_TRANSPORT_PASS" in result.stdout
        old = value
