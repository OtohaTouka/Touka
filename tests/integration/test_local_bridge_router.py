"""本機小幫手端點（ADR-0311）：配對只給人類 admin、權杖不等於身分、工作來回。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.local_bridge import MAX_JOB_RESULT_BYTES, PROTOCOL_VERSION
from src.routers import local_bridge as router_module
from src.services.identity_router import get_verified_identity
from src.services.local_bridge import LocalBridge
from src.services.local_bridge_pairing import LocalPairingStore

ADMIN = AgentIdentity.for_role("Owner", "admin", is_human=True)
POLL = {"protocol": PROTOCOL_VERSION, "helper_version": "1.0"}
HEADER = "X-Touka-Local-Token"


@pytest.fixture
def wired(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalBridge]:
    bridge = LocalBridge()
    store = LocalPairingStore(path=tmp_path / "pairing.json")
    monkeypatch.setattr(router_module, "get_local_bridge", lambda: bridge)
    monkeypatch.setattr(router_module, "get_local_pairing_store", lambda: store)
    monkeypatch.setattr(router_module, "POLL_HOLD_SECONDS", 0)
    yield bridge
    app.dependency_overrides.clear()


@pytest.fixture
def client(wired: LocalBridge) -> Iterator[TestClient]:
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as test_client:
        yield test_client


def _pair(client: TestClient) -> str:
    response = client.post("/v1/local-bridge/pairing")
    assert response.status_code == 200
    return response.json()["code"]


def _poll(client: TestClient, token: str | None, body: dict[str, Any] | None = None) -> Any:
    headers = {HEADER: token} if token is not None else {}
    return client.post("/v1/local-bridge/poll", json=body or POLL, headers=headers)


@pytest.mark.parametrize(
    "identity",
    [
        None,
        AgentIdentity.for_role("Soul", "Owner"),
        AgentIdentity.for_role("User", "normal", is_human=True),
        AgentIdentity.for_role("Owner", "admin", is_human=False),
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/local-bridge/pairing"),
        ("delete", "/v1/local-bridge/pairing"),
        ("get", "/v1/local-bridge/status"),
    ],
)
def test_human_endpoints_are_for_human_admins_only(
    wired: LocalBridge, identity: AgentIdentity | None, method: str, path: str
) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    with TestClient(app) as test_client:
        response = getattr(test_client, method)(path)
    assert response.status_code in (401, 403)


def test_pairing_refuses_untrusted_origin(client: TestClient) -> None:
    response = client.post(
        "/v1/local-bridge/pairing", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_pairing_code_is_shown_once(client: TestClient) -> None:
    code = _pair(client)
    status = client.get("/v1/local-bridge/status").json()
    assert status == {"paired": True, "online": False, "helper_version": None}
    assert code not in str(status)


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_poll_refuses_bad_token(client: TestClient, token: str | None) -> None:
    _pair(client)
    assert _poll(client, token).status_code == 401


def test_poll_refused_before_pairing(client: TestClient) -> None:
    assert _poll(client, "anything").status_code == 401


def test_poll_marks_online(client: TestClient) -> None:
    token = _pair(client)
    response = _poll(client, token)
    assert response.status_code == 200
    assert response.json()["job"] is None
    status = client.get("/v1/local-bridge/status").json()
    assert status == {"paired": True, "online": True, "helper_version": "1.0"}


def test_rotated_token_is_refused(client: TestClient) -> None:
    old = _pair(client)
    new = _pair(client)
    assert _poll(client, old).status_code == 401
    assert _poll(client, new).status_code == 200


def test_revoked_token_is_refused_and_goes_offline(client: TestClient) -> None:
    token = _pair(client)
    _poll(client, token)
    assert client.delete("/v1/local-bridge/pairing").status_code == 200
    assert _poll(client, token).status_code == 401
    assert client.get("/v1/local-bridge/status").json()["paired"] is False


def test_pairing_again_after_the_file_was_lost_connects(client: TestClient) -> None:
    _pair(client)
    _pair(client)
    router_module.get_local_pairing_store()._file().unlink()
    token = _pair(client)
    assert _poll(client, token).status_code == 200


def test_unpairing_uses_the_newest_generation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解除配對和重新配對交錯時，晚到的不能把世代倒回去，否則新的配對連不上。"""
    _pair(client)
    store = router_module.get_local_pairing_store()
    real_revoke = store.revoke
    newest: list[str] = []

    async def revoke_then_someone_pairs() -> None:
        await real_revoke()
        newest.append(await store.create("someone"))

    monkeypatch.setattr(store, "revoke", revoke_then_someone_pairs)
    assert client.delete("/v1/local-bridge/pairing").status_code == 200
    assert _poll(client, newest[0]).status_code == 200


def test_protocol_mismatch(client: TestClient) -> None:
    token = _pair(client)
    response = _poll(client, token, {"protocol": PROTOCOL_VERSION + 1, "helper_version": "1"})
    assert response.status_code == 409


def test_poll_body_is_strict(client: TestClient) -> None:
    token = _pair(client)
    response = _poll(client, token, {**POLL, "extra": 1})
    assert response.status_code == 422


def test_token_does_not_become_an_identity(wired: LocalBridge) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as test_client:
        token = _pair(test_client)
    app.dependency_overrides[get_verified_identity] = lambda: None
    with TestClient(app) as test_client:
        response = test_client.get("/v1/local-bridge/status", headers={HEADER: token})
    assert response.status_code in (401, 403)


def test_host_mount_roots_are_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module.settings, "HOST_WORKSPACE_ROOT", "../work")
    monkeypatch.setattr(router_module.settings, "SANCTUARY_DATA_PATH", "./data")
    token = _pair(client)
    assert _poll(client, token).json()["host_mount_roots"] == ["../work", "./data"]


def _deliver_flow(
    client: TestClient, wired: LocalBridge, token: str, action: str
) -> tuple[str, Any]:
    """在 TestClient 的事件迴圈上放一件工作，回傳工作編號與 future。"""
    portal = client.portal
    assert portal is not None
    future = portal.start_task_soon(wired.submit, action, {"path": "a"}, "Soul")
    for _ in range(50):
        body = _poll(client, token).json()
        if body["job"] is not None:
            return body["job"]["id"], future
        portal.call(asyncio.sleep, 0.01)
    raise AssertionError


def test_result_round_trip(client: TestClient, wired: LocalBridge) -> None:
    token = _pair(client)
    _poll(client, token)
    job_id, future = _deliver_flow(client, wired, token, "read")
    response = client.post(
        f"/v1/local-bridge/jobs/{job_id}/result",
        json={"status": "ok", "data": {"text": "x"}},
        headers={HEADER: token},
    )
    assert response.status_code == 200
    assert future.result(2).data == {"text": "x"}


def test_confirm_then_result(client: TestClient, wired: LocalBridge) -> None:
    token = _pair(client)
    _poll(client, token)
    job_id, future = _deliver_flow(client, wired, token, "propose")
    headers = {HEADER: token}
    assert client.post(f"/v1/local-bridge/jobs/{job_id}/confirm", headers=headers).status_code == 200
    second = client.post(f"/v1/local-bridge/jobs/{job_id}/confirm", headers=headers)
    assert second.status_code == 410
    done = client.post(
        f"/v1/local-bridge/jobs/{job_id}/result",
        json={"status": "ok", "data": {"applied": 1}},
        headers=headers,
    )
    assert done.status_code == 200
    assert future.result(2).data == {"applied": 1}


@pytest.mark.parametrize("path", ["confirm", "result"])
def test_job_endpoints_need_the_token(client: TestClient, path: str) -> None:
    _pair(client)
    response = client.post(f"/v1/local-bridge/jobs/x/{path}", json={"status": "ok"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [("confirm", None), ("result", {"status": "ok"})],
)
def test_unknown_job_is_gone(client: TestClient, path: str, body: Any) -> None:
    token = _pair(client)
    response = client.post(
        f"/v1/local-bridge/jobs/unknown/{path}", json=body, headers={HEADER: token}
    )
    assert response.status_code == 410


def test_oversized_result_is_refused(client: TestClient, wired: LocalBridge) -> None:
    token = _pair(client)
    _poll(client, token)
    job_id, future = _deliver_flow(client, wired, token, "read")
    response = client.post(
        f"/v1/local-bridge/jobs/{job_id}/result",
        json={"status": "ok", "data": {"text": "x" * (MAX_JOB_RESULT_BYTES + 1)}},
        headers={HEADER: token},
    )
    assert response.status_code == 413
    future.cancel()


def test_result_body_is_strict(client: TestClient) -> None:
    token = _pair(client)
    response = client.post(
        "/v1/local-bridge/jobs/x/result",
        json={"status": "maybe"},
        headers={HEADER: token},
    )
    assert response.status_code == 422
