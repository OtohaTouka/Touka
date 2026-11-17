"""本機小幫手端點（ADR-0312）：配對只給人類 admin、權杖不等於身分、工作來回。"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.local_bridge import MAX_JOB_RESULT_BYTES, PROTOCOL_VERSION, LocalPairingRecord
from src.routers import local_bridge as router_module
from src.services.identity_router import get_verified_identity
from src.services.local_bridge import OPEN_DECISION_SECONDS, LocalBridge
from src.services.local_bridge_pairing import LocalPairingStore
from src.utils.local_bridge_signing import sign_open_request, text_fingerprint

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


def _pair_code(client: TestClient) -> str:
    response = client.post("/v1/local-bridge/pairing")
    assert response.status_code == 200
    return response.json()["code"]


def _pair(client: TestClient) -> str:
    """配對碼是「權杖.鑰匙」；小幫手只拿權杖連線（ADR-0317）。"""
    return _pair_code(client).split(".", 1)[0]


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
    response = client.post("/v1/local-bridge/pairing", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_pairing_code_is_shown_once(client: TestClient) -> None:
    code = _pair_code(client)
    token, _, key_text = code.partition(".")
    assert len(key_text) == 43
    status = client.get("/v1/local-bridge/status").json()
    assert status == {"paired": True, "online": False, "helper_version": None}
    assert token not in str(status)
    assert key_text not in str(status)


def test_the_full_pairing_code_is_not_a_token(client: TestClient) -> None:
    assert _poll(client, _pair_code(client)).status_code == 401


def test_the_decision_key_never_appears_in_the_api_contract() -> None:
    schema = str(app.openapi())
    assert "decision_key" not in schema


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
        newest.append((await store.create("someone")).token)

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


def test_memory_and_heartbeat_mounts_are_sent_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module.settings, "HOST_WORKSPACE_ROOT", "")
    monkeypatch.setattr(router_module.settings, "SANCTUARY_DATA_PATH", "")
    monkeypatch.setattr(router_module.settings, "MEMORY_SYSTEM_SRC_DIR", "../memory")
    monkeypatch.setattr(router_module.settings, "HEARTBEAT_CONFIG_DIR", "../heartbeat")
    token = _pair(client)
    assert _poll(client, token).json()["host_mount_roots"] == ["../memory", "../heartbeat"]


def test_host_mount_roots_are_sent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert (
        client.post(f"/v1/local-bridge/jobs/{job_id}/confirm", headers=headers).status_code == 200
    )
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


# --- 從 Touka 請小幫手開放資料夾（ADR-0320） ---

ORIGIN = {"Origin": "http://localhost:3000"}
OPEN_URL = "/v1/local-bridge/open-requests"
# 路徑不以磁碟代號開頭：Touka 不判斷路徑，規則都在小幫手（PATH_LITERAL_SSOT）
WANTED = "相片/家 2026"


def _open_and_answer(
    client: TestClient, token: str, answer: dict[str, Any] | None, path: str = WANTED
) -> tuple[Any, dict[str, Any]]:
    """設定頁送出開放請求，測試扮小幫手拿到工作、回報 ``answer``。回傳（回應, 工作）。"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, OPEN_URL, json={"path": path}, headers=ORIGIN)
        job: dict[str, Any] | None = None
        for _ in range(200):
            body = _poll(client, token).json()
            if body["job"] is not None:
                job = body["job"]
                break
            time.sleep(0.01)
        assert job is not None
        if answer is not None:
            delivered = client.post(
                f"/v1/local-bridge/jobs/{job['id']}/result", json=answer, headers={HEADER: token}
            )
            assert delivered.status_code == 200
        return pending.result(10), job


def _key_of(code: str) -> bytes:
    key_text = code.split(".", 1)[1]
    return base64.urlsafe_b64decode(key_text + "=")


@pytest.mark.parametrize(
    "identity",
    [
        None,
        AgentIdentity.for_role("Soul", "Owner"),
        AgentIdentity.for_role("User", "normal", is_human=True),
        AgentIdentity.for_role("Owner", "admin", is_human=False),
    ],
)
def test_only_human_admins_can_ask_to_open(
    wired: LocalBridge, identity: AgentIdentity | None
) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    with TestClient(app) as test_client:
        response = test_client.post(OPEN_URL, json={"path": WANTED}, headers=ORIGIN)
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
def test_asking_to_open_needs_a_trusted_origin(
    client: TestClient, headers: dict[str, str]
) -> None:
    _pair(client)
    response = client.post(OPEN_URL, json={"path": WANTED}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUTH_PERMISSION_DENIED"


def test_asking_to_open_before_pairing(client: TestClient) -> None:
    response = client.post(OPEN_URL, json={"path": WANTED}, headers=ORIGIN)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LOCAL_HELPER_NOT_PAIRED"


def test_only_the_person_who_paired_can_ask(wired: LocalBridge) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as test_client:
        _pair(test_client)
    other = AgentIdentity.for_role("Other", "admin", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: other
    with TestClient(app) as test_client:
        response = test_client.post(OPEN_URL, json={"path": WANTED}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_HELPER_NOT_PAIRED_HUMAN"


@pytest.mark.parametrize("path", ["", "a" * 1025, "相片\x00", "相片\n", "相片\x7f"])
def test_bad_paths_are_refused_before_anything_is_sent(
    client: TestClient, wired: LocalBridge, path: str
) -> None:
    token = _pair(client)
    _poll(client, token)
    response = client.post(OPEN_URL, json={"path": path}, headers=ORIGIN)
    assert response.status_code == 422
    assert _poll(client, token).json()["job"] is None


def test_an_old_pairing_without_a_key_sends_nothing(
    client: TestClient, wired: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _pair(client)
    _poll(client, token)
    store = router_module.get_local_pairing_store()
    record = store.current()
    assert record is not None
    old = record.model_copy(update={"decision_key": None})
    monkeypatch.setattr(store, "current", lambda: old)
    response = client.post(OPEN_URL, json={"path": WANTED}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json() == {
        "status": "refused",
        "code": "needs_new_pairing",
        "name": None,
        "writable": None,
    }
    assert _poll(client, token).json()["job"] is None


def test_an_open_request_is_stamped_with_the_pairing_key(client: TestClient) -> None:
    code = _pair_code(client)
    token = code.split(".", 1)[0]
    _poll(client, token)
    response, job = _open_and_answer(
        client, token, {"status": "ok", "data": {"name": "家 2026", "writable": False}}
    )
    assert job["action"] == "request_open"
    assert job["args"] == {"path": WANTED}
    assert job["decide_within"] == OPEN_DECISION_SECONDS
    decision = job["decision"]
    assert decision["decided_by"] == "owner_request"
    assert decision["digest"] == decision["shown"] == text_fingerprint(WANTED)
    expected = sign_open_request(
        _key_of(code), job_id=job["id"], nonce=decision["nonce"], path=WANTED
    )
    assert decision["signature"] == expected
    assert response.status_code == 200
    assert response.json() == {
        "status": "opened",
        "code": None,
        "name": "家 2026",
        "writable": False,
    }


def test_each_request_gets_a_new_number(client: TestClient) -> None:
    token = _pair(client)
    _poll(client, token)
    _first, one = _open_and_answer(client, token, {"status": "refused", "code": "not_opened"})
    _second, two = _open_and_answer(client, token, {"status": "refused", "code": "not_opened"})
    assert one["decision"]["nonce"] != two["decision"]["nonce"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {"status": "refused", "code": "not_opened", "message": "The user did not open it."},
            {"status": "refused", "code": "not_opened", "name": None, "writable": None},
        ),
        (
            {"status": "error", "code": None},
            {"status": "error", "code": "unknown", "name": None, "writable": None},
        ),
        (
            {"status": "ok", "data": {"name": "家", "writable": True, "path": "x"}},
            {"status": "error", "code": "bad_result", "name": None, "writable": None},
        ),
        (
            {"status": "ok", "data": {"name": "家", "writable": "yes"}},
            {"status": "error", "code": "bad_result", "name": None, "writable": None},
        ),
        (
            {"status": "ok", "data": {"name": "", "writable": True}},
            {"status": "error", "code": "bad_result", "name": None, "writable": None},
        ),
        (
            {"status": "ok", "data": None},
            {"status": "error", "code": "bad_result", "name": None, "writable": None},
        ),
    ],
)
def test_helper_answers_are_mapped(
    client: TestClient, answer: dict[str, Any], expected: dict[str, Any]
) -> None:
    token = _pair(client)
    _poll(client, token)
    response, _job = _open_and_answer(client, token, answer)
    assert response.status_code == 200
    assert response.json() == expected


def test_a_long_folder_name_still_counts_as_opened(client: TestClient) -> None:
    token = _pair(client)
    _poll(client, token)
    name = "a" * 255 + " 2"
    response, _job = _open_and_answer(
        client, token, {"status": "ok", "data": {"name": name, "writable": False}}
    )
    assert response.json()["status"] == "opened"
    assert response.json()["name"] == name


def test_without_a_helper_online_nothing_waits(client: TestClient) -> None:
    _pair(client)
    response = client.post(OPEN_URL, json={"path": WANTED}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["code"] == "helper_offline"


class _ShiftingStore:
    """每讀一次 ``current()`` 換一份紀錄：抓「人和鑰匙分兩次讀」。"""

    def __init__(self, records: list[LocalPairingRecord], channel: str) -> None:
        self._records = records
        self._channel = channel
        self.reads = 0

    def current(self) -> LocalPairingRecord:
        record = self._records[min(self.reads, len(self._records) - 1)]
        self.reads += 1
        return record

    def decision_key(self) -> bytes:
        return bytes(range(1, 33))

    def authenticate(self, token: str) -> LocalPairingRecord | None:
        return self._records[0] if token == self._channel else None

    def generation(self) -> int:
        return 1


def test_the_person_and_the_key_come_from_one_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_key = bytes(range(32))
    first = LocalPairingRecord(
        generation=1, token_hash="0" * 64, human_id=ADMIN.agent_id, decision_key=first_key.hex()
    )
    second = first.model_copy(update={"decision_key": bytes(range(1, 33)).hex()})
    channel = "shifting-store-channel"
    store = _ShiftingStore([first, second], channel)
    monkeypatch.setattr(router_module, "get_local_pairing_store", lambda: store)
    _poll(client, channel)
    _response, job = _open_and_answer(
        client, channel, {"status": "refused", "code": "not_opened"}
    )
    decision = job["decision"]
    assert decision["signature"] == sign_open_request(
        first_key, job_id=job["id"], nonce=decision["nonce"], path=WANTED
    )
    assert store.reads == 1


def test_open_requests_are_in_the_api_contract() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/local-bridge/open-requests" in paths
    assert "/local-bridge/open-requests" in paths
