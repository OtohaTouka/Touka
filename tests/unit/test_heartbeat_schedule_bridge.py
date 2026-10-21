"""Synthetic human JWT -> Proxy bridge -> real HB service auth and transactions."""

import asyncio
import json
from pathlib import Path

import httpx
import jwt
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient
from src.core.exceptions import ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.user import User
from src.routers import heartbeat as router
from src.services import auth_service
from src.services import heartbeat_schedule_bridge as bridge_module
from src.utils.time_utils import get_current_epoch

from ToukaHeartbeat import awaken
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

SIGNING_KEY = "synthetic-heartbeat-bridge-signing-key"
SERVICE_SECRET = "synthetic-heartbeat-bridge-service"  # noqa: S105
ORIGIN = "http://heartbeat-fixture.invalid:8082"


@pytest.fixture
def bridge_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    users = [
        User(
            id="alice",
            email="alice@example.com",
            role="normal",
            owned_agents=["Alice"],
            hashed_password="unused",
        ),
        User(
            id="bob",
            email="bob@example.com",
            role="normal",
            owned_agents=["Bob"],
            hashed_password="unused",
        ),
        User(id="admin", email="admin@example.com", role="admin", hashed_password="unused"),
    ]
    users_path = tmp_path / "users.json"
    users_path.write_text(
        json.dumps({"users": [user.model_dump() for user in users]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth_service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(AuthSecretManager, "get_secret", staticmethod(lambda: SIGNING_KEY))
    monkeypatch.setattr(router, "HEARTBEAT_EXTERNAL_URL", ORIGIN)
    monkeypatch.setattr(router, "HEARTBEAT_TOKEN", SERVICE_SECRET)
    storage = StorageService(tmp_path / "heartbeat.json", env_token=SERVICE_SECRET)
    storage.save_config(
        {
            "heartbeat_token": SERVICE_SECRET,
            "schedules": [
                {"id": "alice-job", "agent_id": "Alice"},
                {"id": "bob-job", "agent_id": "Bob"},
            ],
        }
    )
    monkeypatch.setattr(awaken, "storage", storage)
    provider = StorageTokenProvider(storage)
    monkeypatch.setattr(awaken, "token_provider", provider)
    monkeypatch.setattr(awaken, "proxy_client", ProxyClient(provider))
    monkeypatch.setattr(awaken, "scheduler", AsyncIOScheduler(timezone=awaken.TAIPEI_TZ))
    requests = []
    controls = {}
    real_client = httpx.AsyncClient

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if controls.get("stall"):
                await asyncio.Event().wait()
            if controls.get("timeout"):
                raise httpx.ReadTimeout("owned_timeout", request=request)
            if controls.get("status"):
                return httpx.Response(controls["status"], request=request)
            hook = controls.get("before_mutation")
            if hook and request.method != "GET":
                hook()
            return await httpx.ASGITransport(app=awaken.app).handle_async_request(request)

    def client_factory(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("timeout") is not None
        return real_client(*args, **kwargs, transport=Transport())

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    yield TestClient(app), storage, requests, controls, users_path


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_outbound_has_overall_deadline_and_is_not_retried(
    bridge_app, monkeypatch, method
) -> None:
    _client, _storage, requests, controls, _users_path = bridge_app
    controls["stall"] = True
    monkeypatch.setattr(bridge_module, "_OUTBOUND_TIMEOUT_SECONDS", 0.001, raising=False)
    bridge = bridge_module.HeartbeatScheduleBridge(ORIGIN, SERVICE_SECRET, lambda _agent: True)
    with pytest.raises(ToukaException) as error:
        await asyncio.wait_for(bridge._request(method), timeout=0.2)
    expected = "HEARTBEAT_UPSTREAM_FAILED" if method == "GET" else "HEARTBEAT_OUTCOME_UNKNOWN"
    assert error.value.code.value == expected
    assert len(requests) == 1


def auth(user: str = "alice", kind: str = "access") -> dict[str, str]:
    now = get_current_epoch()
    token = jwt.encode(
        {"sub": user, "type": kind, "iat": now, "exp": now + 3600}, SIGNING_KEY, algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


def revision(client: TestClient, user: str = "alice") -> str:
    response = client.get("/v1/heartbeat/schedules", headers=auth(user))
    assert response.status_code == 200, response.text
    assert response.headers.get("ETag")
    return response.headers["ETag"]


@pytest.mark.parametrize(
    ("user", "expected"),
    [("alice", {"alice-job"}), ("bob", {"bob-job"}), ("admin", {"alice-job", "bob-job"})],
)
def test_human_list_uses_fresh_ownership_and_only_forwards_service_secret(
    bridge_app, user, expected
) -> None:
    client, storage, requests, _controls, _users_path = bridge_app
    before = storage.config_file.read_bytes()
    response = client.get("/v1/heartbeat/schedules", headers={**auth(user), "X-Agent-ID": "admin"})
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == expected
    assert response.headers["ETag"]
    assert all(row["active"] is True and row["mode"] == "interval" for row in response.json())
    assert storage.config_file.read_bytes() == before
    original_revision = bridge_module.schedules_revision(json.loads(before)["schedules"])
    assert response.headers["ETag"] == original_revision
    assert SERVICE_SECRET not in response.text
    assert requests and all(
        request.headers["Authorization"] == f"Bearer {SERVICE_SECRET}" for request in requests
    )
    assert all(str(request.url) == ORIGIN + "/v1/schedules" for request in requests)


@pytest.mark.parametrize("kind", ["service", "refresh"])
def test_nonhuman_tokens_are_rejected_before_outbound(bridge_app, kind) -> None:
    client, _storage, requests, _controls, _users_path = bridge_app
    response = client.get("/v1/heartbeat/schedules", headers=auth("alice", kind))
    assert response.status_code == 401
    assert requests == []


def test_invalid_human_schedule_never_echoes_input_or_validation_context(bridge_app) -> None:
    client, _storage, requests, _controls, _users_path = bridge_app
    sentinel = "synthetic-private-schedule-input"
    response = client.post(
        "/v1/heartbeat/schedules",
        headers=auth(),
        json={
            "agent_id": "Alice",
            "interval_minutes": sentinel,
            "init_prompt": sentinel,
        },
    )
    assert response.status_code == 422
    assert sentinel not in response.text
    assert "HEARTBEAT_SCHEDULE_INVALID" in response.text
    assert "input" not in response.json()["detail"]
    assert "ctx" not in response.json()["detail"]
    assert requests == []


def test_suspended_user_is_rejected_using_fresh_user_data(bridge_app) -> None:
    client, _storage, requests, _controls, users_path = bridge_app
    headers = auth()
    contents = json.loads(users_path.read_text(encoding="utf-8"))
    contents["users"][0]["status"] = "suspended"
    users_path.write_text(json.dumps(contents), encoding="utf-8")
    assert client.get("/v1/heartbeat/schedules", headers=headers).status_code == 401
    assert requests == []


@pytest.mark.parametrize("target", ["bob-job", "missing-job"])
def test_delete_invisible_and_missing_schedule_are_indistinguishable(bridge_app, target) -> None:
    client, storage, requests, _controls, _users_path = bridge_app
    etag = revision(client)
    old = storage.config_file.read_bytes()
    response = client.delete(
        f"/v1/heartbeat/schedules/{target}", headers={**auth(), "If-Match": etag}
    )
    assert response.status_code == 404
    assert storage.config_file.read_bytes() == old
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    ("identifier", "target_agent", "status"), [("bob-job", "Alice", 404), ("alice-job", "Bob", 403)]
)
def test_upsert_checks_both_old_and_new_agent_ownership(
    bridge_app, identifier, target_agent, status
) -> None:
    client, storage, _requests, _controls, _users_path = bridge_app
    etag = revision(client)
    old = storage.config_file.read_bytes()
    response = client.post(
        "/v1/heartbeat/schedules",
        headers={**auth(), "If-Match": etag},
        json={"id": identifier, "agent_id": target_agent},
    )
    assert response.status_code == status
    assert storage.config_file.read_bytes() == old


def test_admin_can_reassign_existing_schedule(bridge_app) -> None:
    client, storage, requests, _controls, _users_path = bridge_app
    etag = revision(client, "admin")
    response = client.post(
        "/v1/heartbeat/schedules",
        headers={**auth("admin"), "If-Match": etag},
        json={"id": "alice-job", "agent_id": "Bob"},
    )
    assert response.status_code == 200, response.text
    assert storage.load_config()["schedules"][0]["agent_id"] == "Bob"
    assert requests[-1].headers["If-Match"] == etag


def test_missing_revision_is_rejected_before_any_outbound(bridge_app) -> None:
    client, _storage, requests, _controls, _users_path = bridge_app
    response = client.post("/v1/heartbeat/schedules", headers=auth(), json={"agent_id": "Alice"})
    assert response.status_code == 428
    assert requests == []


def test_revision_race_cannot_overwrite_concurrent_other_owner_creation(bridge_app) -> None:
    client, storage, _requests, controls, _users_path = bridge_app
    etag = revision(client)

    def concurrent_create():
        config = storage.load_config()
        config["schedules"].append({"id": "new-id", "agent_id": "Bob"})
        storage.save_config(config)

    controls["before_mutation"] = concurrent_create
    response = client.post(
        "/v1/heartbeat/schedules",
        headers={**auth(), "If-Match": etag},
        json={"id": "new-id", "agent_id": "Alice"},
    )
    assert response.status_code == 409
    assert storage.load_config()["schedules"][-1] == {"id": "new-id", "agent_id": "Bob"}


def test_empty_shared_secret_does_not_issue_outbound(
    bridge_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _storage, requests, _controls, _users_path = bridge_app
    monkeypatch.setattr(router, "HEARTBEAT_TOKEN", " ")
    assert client.get("/v1/heartbeat/schedules", headers=auth()).status_code == 503
    assert requests == []


def test_upstream_auth_failure_does_not_expire_human_login(bridge_app) -> None:
    client, _storage, _requests, controls, _users_path = bridge_app
    controls["status"] = 401
    assert client.get("/v1/heartbeat/schedules", headers=auth()).status_code == 502


def test_mutation_timeout_reports_unknown_and_is_never_resent(bridge_app) -> None:
    client, _storage, requests, controls, _users_path = bridge_app
    etag = revision(client)

    def timeout_after_validation():
        raise httpx.ReadTimeout("owned_mutation_timeout")

    controls["before_mutation"] = timeout_after_validation
    response = client.delete(
        "/v1/heartbeat/schedules/alice-job", headers={**auth(), "If-Match": etag}
    )
    assert response.status_code == 503
    assert "HEARTBEAT_OUTCOME_UNKNOWN" in response.text
    assert len([request for request in requests if request.method == "DELETE"]) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://user@heartbeat-fixture.invalid",
        "http://heartbeat-fixture.invalid?x=1",
        "http://heartbeat-fixture.invalid#fragment",
    ],
)
def test_bridge_rejects_ambiguous_fixed_origin_before_outbound(
    bridge_app, monkeypatch: pytest.MonkeyPatch, origin
) -> None:
    client, _storage, requests, _controls, _users_path = bridge_app
    monkeypatch.setattr(router, "HEARTBEAT_EXTERNAL_URL", origin)
    assert client.get("/v1/heartbeat/schedules", headers=auth()).status_code == 502
    assert requests == []
