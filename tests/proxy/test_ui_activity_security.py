"""GET /v1/ui/activity：側欄工作狀態的查詢（ADR-0300）。

只看記憶體、不碰對話資料庫；依身分過濾，只白名單輸出。背景活動與結束紀錄的規則和 /stats 相同
（``authorization_gateway.can_access_session``）；串流只回呼叫者自己送出的，admin 也看不到別人的。

擁有權判斷全用真的；沙箱沒有 users.json，只替換 ``AuthService.get_user_by_id``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.session_persistence import IncognitoStreamAdmission, PersistedStreamAdmission
from src.models.user import User
from src.services.activity_registry import MAX_FINISHED_ACTIVITY, activity_registry
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.stream_buffer_service import stream_buffer_service
from src.utils.async_db import session_service
from src.utils.time_utils import get_current_epoch_ms

if TYPE_CHECKING:
    from collections.abc import Iterator

USERS = {
    "user-a": User(id="user-a", email="a@test.com", hashed_password="...", owned_agents=["Alice"]),
    "user-b": User(id="user-b", email="b@test.com", hashed_password="...", owned_agents=["Bob"]),
}

HUMAN_ADMIN = AgentIdentity.for_role("admin-user", "admin", is_human=True)
DEBUGGER = AgentIdentity.for_role("DebugAgent", "Debugger")
OWNER_AGENT = AgentIdentity.for_role("Alice", "Owner")


def _human(user_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(
        user_id, "normal", is_human=True, owned_agents=list(USERS[user_id].owned_agents)
    )


@pytest.fixture
def user_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _get_user_by_id(self: AuthService, user_id: str) -> User | None:
        calls.append(user_id)
        return USERS.get(user_id)

    monkeypatch.setattr(AuthService, "get_user_by_id", _get_user_by_id)
    return calls


@pytest.fixture
def client(user_lookups: list[str]) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        activity_registry.reload_all()
        stream_buffer_service._streams.clear()
        try:
            yield test_client
        finally:
            activity_registry.reload_all()
            stream_buffer_service._streams.clear()
            app.dependency_overrides.clear()


def _activity(
    client: TestClient, identity: AgentIdentity, since: int | None = None
) -> dict[str, Any]:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    resp = client.get("/v1/ui/activity", params={} if since is None else {"since": since})
    assert resp.status_code == 200
    return resp.json()


def _background_ids(body: dict[str, Any]) -> set[str]:
    return {row["session_id"] for row in body["background"]}


def _finished_ids(body: dict[str, Any]) -> list[str]:
    return [row["session_id"] for row in body["finished"]]


def _seed_background() -> None:
    activity_registry.set_activity(
        "sess-alice", {"kind": "heartbeat", "current": 2, "total": 5, "agent": "Alice"}
    )
    activity_registry.set_activity(
        "sess-bob", {"kind": "heartbeat", "current": 1, "total": 3, "agent": "Bob"}
    )


async def _start_stream(session_id: str, owner: AgentIdentity, *, incognito: bool = False) -> str:
    model = IncognitoStreamAdmission if incognito else PersistedStreamAdmission
    options = {} if incognito else {"revision": "synthetic-revision"}
    entry = await stream_buffer_service.create(
        session_id,
        identity=owner,
        admission=model(
            mode="incognito" if incognito else "persistent",
            user_parent_id="user",
            assistant_node_id="assistant",
            **options,
        ),
    )
    return entry.stream_id


@pytest.mark.asyncio
async def test_old_path_streams_without_admission_are_not_reported(client: TestClient) -> None:
    await stream_buffer_service.create("stream-old", identity=_human("user-a"))
    assert _activity(client, _human("user-a"))["streams"] == []


def test_empty_session_id_is_not_recorded(client: TestClient) -> None:
    activity_registry.record_finished(None, "heartbeat", "Alice")
    activity_registry.record_finished("", "heartbeat", "Alice")
    assert activity_registry.finished_since(0) == []


def test_requires_login(client: TestClient) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: None
    assert client.get("/v1/ui/activity").status_code == 403


def test_background_only_for_souls_the_caller_can_see(client: TestClient) -> None:
    _seed_background()
    assert _background_ids(_activity(client, _human("user-a"))) == {"sess-alice"}
    assert _background_ids(_activity(client, OWNER_AGENT)) == {"sess-alice"}
    assert _background_ids(_activity(client, HUMAN_ADMIN)) == {"sess-alice", "sess-bob"}
    assert _background_ids(_activity(client, DEBUGGER)) == {"sess-alice", "sess-bob"}


def test_records_without_soul_only_for_admin(client: TestClient) -> None:
    activity_registry.set_activity("sess-none", {"kind": "heartbeat", "current": 1, "total": 1})
    activity_registry.record_finished("done-none", "heartbeat", None)
    body = _activity(client, _human("user-a"), since=0)
    assert body["background"] == []
    assert body["finished"] == []
    admin = _activity(client, HUMAN_ADMIN, since=0)
    assert _background_ids(admin) == {"sess-none"}
    assert _finished_ids(admin) == ["done-none"]


def test_malformed_background_record_does_not_break_the_query(client: TestClient) -> None:
    activity_registry.set_activity("sess-bad", {"kind": "heartbeat", "agent": "Alice"})
    activity_registry.set_activity("sess-odd", "heartbeat")  # type: ignore[arg-type]
    _seed_background()
    assert _background_ids(_activity(client, HUMAN_ADMIN)) == {"sess-alice", "sess-bob"}


def test_only_whitelisted_fields(client: TestClient) -> None:
    activity_registry.set_activity(
        "sess-alice",
        {"kind": "heartbeat", "current": 2, "total": 5, "agent": "Alice", "title": "秘密標題"},
    )
    activity_registry.record_finished("done-alice", "heartbeat", "Alice")
    body = _activity(client, HUMAN_ADMIN, since=0)
    assert set(body) == {"streams", "background", "finished", "cursor"}
    assert body["background"] == [
        {"session_id": "sess-alice", "kind": "heartbeat", "current": 2, "total": 5}
    ]
    assert list(body["finished"][0]) == ["session_id", "finished_at"]
    assert "Alice" not in str(body)
    assert "秘密標題" not in str(body)


@pytest.mark.asyncio
async def test_streams_only_for_the_requester_even_for_admin(client: TestClient) -> None:
    await _start_stream("stream-a", _human("user-a"))
    await _start_stream("stream-admin", HUMAN_ADMIN)
    assert _activity(client, _human("user-a"))["streams"] == ["stream-a"]
    assert _activity(client, _human("user-b"))["streams"] == []
    assert _activity(client, HUMAN_ADMIN)["streams"] == ["stream-admin"]


@pytest.mark.asyncio
async def test_incognito_stream_is_not_reported(client: TestClient) -> None:
    await _start_stream("stream-incognito", _human("user-a"), incognito=True)
    assert _activity(client, _human("user-a"))["streams"] == []


@pytest.mark.asyncio
async def test_old_path_stream_without_owner_is_not_reported(client: TestClient) -> None:
    """舊路徑的串流沒有開始座標：它的對話要靠別的方式認，這裡一律不回報。"""
    await stream_buffer_service.create("stream-legacy")
    assert _activity(client, HUMAN_ADMIN)["streams"] == []


@pytest.mark.asyncio
async def test_finished_stream_is_not_reported(client: TestClient) -> None:
    stream_id = await _start_stream("stream-a", _human("user-a"))
    await stream_buffer_service.finish("stream-a", stream_id=stream_id)
    assert _activity(client, _human("user-a"))["streams"] == []


@pytest.mark.asyncio
async def test_query_does_not_touch_the_session_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = AsyncMock(side_effect=AssertionError("activity must not read the session database"))
    monkeypatch.setattr(session_service, "get_session_by_id", database)
    monkeypatch.setattr(session_service, "read_persistence_snapshot", database)
    _seed_background()
    activity_registry.record_finished("done-alice", "heartbeat", "Alice")
    await _start_stream("stream-a", _human("user-a"))
    assert _activity(client, _human("user-a"), since=0)["streams"] == ["stream-a"]
    database.assert_not_awaited()


def test_ownership_checked_once_per_soul(client: TestClient, user_lookups: list[str]) -> None:
    activity_registry.set_activity(
        "sess-1", {"kind": "heartbeat", "current": 1, "total": 1, "agent": "Alice"}
    )
    activity_registry.set_activity(
        "sess-2", {"kind": "heartbeat", "current": 1, "total": 1, "agent": "Alice"}
    )
    activity_registry.record_finished("done-1", "heartbeat", "Alice")
    user_lookups.clear()
    _activity(client, _human("user-a"), since=0)
    assert user_lookups == ["user-a"]


def test_first_query_gets_a_cursor_and_no_finished(client: TestClient) -> None:
    activity_registry.record_finished("done-alice", "heartbeat", "Alice")
    before = get_current_epoch_ms()
    body = _activity(client, HUMAN_ADMIN)
    assert body["finished"] == []
    assert body["cursor"] >= before


def test_since_returns_only_later_finished(client: TestClient) -> None:
    activity_registry.record_finished("done-1", "heartbeat", "Alice")
    cursor = _activity(client, HUMAN_ADMIN, since=0)["cursor"]
    activity_registry.record_finished("done-2", "heartbeat", "Alice")
    body = _activity(client, HUMAN_ADMIN, since=cursor)
    assert _finished_ids(body) == ["done-2"]
    assert body["cursor"] > cursor
    assert _activity(client, HUMAN_ADMIN, since=body["cursor"])["finished"] == []


def test_two_finishes_in_the_same_millisecond_are_both_seen_in_order(client: TestClient) -> None:
    for index in range(50):
        activity_registry.record_finished(f"done-{index}", "heartbeat", "Alice")
    rows = _activity(client, HUMAN_ADMIN, since=0)["finished"]
    times = [row["finished_at"] for row in rows]
    assert times == sorted(set(times))
    assert len(times) == 50


def test_others_finishing_does_not_change_the_response(client: TestClient) -> None:
    """別人的心跳結束時，回應逐字不變：cursor 也不能洩漏次數。"""
    activity_registry.record_finished("done-alice", "heartbeat", "Alice")
    since = _activity(client, _human("user-a"), since=0)["cursor"]
    before = _activity(client, _human("user-a"), since=since)
    activity_registry.record_finished("done-bob-1", "heartbeat", "Bob")
    activity_registry.record_finished("done-bob-2", "heartbeat", "Bob")
    assert _activity(client, _human("user-a"), since=since) == before
    assert before["cursor"] == since


def test_finished_records_are_capped(client: TestClient) -> None:
    for index in range(MAX_FINISHED_ACTIVITY + 5):
        activity_registry.record_finished(f"done-{index}", "heartbeat", "Alice")
    rows = _finished_ids(_activity(client, HUMAN_ADMIN, since=0))
    assert len(rows) == MAX_FINISHED_ACTIVITY
    assert rows[0] == "done-5"
    assert rows[-1] == f"done-{MAX_FINISHED_ACTIVITY + 4}"


@pytest.mark.parametrize("since", [-1, "abc"])
def test_bad_since_is_rejected(client: TestClient, since: Any) -> None:
    app.dependency_overrides[get_verified_identity] = lambda: HUMAN_ADMIN
    assert client.get("/v1/ui/activity", params={"since": since}).status_code == 422


def test_reload_all_clears_finished(client: TestClient) -> None:
    activity_registry.record_finished("done-alice", "heartbeat", "Alice")
    activity_registry.reload_all()
    assert _activity(client, HUMAN_ADMIN, since=0)["finished"] == []
