"""插話端點安全（ADR-0249 第 4a 段）：越權、跨 stream、超量、代理身分、無痕。

`POST/GET /v1/ui/sessions/{id}/interjections`、`DELETE .../{iid}` 把使用者文字送進模型的
context，是新的提示注入入口：只收人類呼叫者（admin 代打可以），沿用
`_authorized_stream_state` 驗串流擁有者與同一串流代；請求只有 text 與 urgent。
測試直接經 HTTP 層打 router（身分以 dependency override 注入），斷言值寫字面值。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.core.exceptions import ToukaException
from src.main import app as main_app
from src.models.identity import AgentIdentity
from src.models.interjection import InterjectionCreateRequest
from src.models.session_persistence import IncognitoStreamAdmission, PersistedStreamAdmission
from src.routers import ui_interjections as interjection_mod
from src.routers import ui_sessions as ui_mod
from src.services.authorization_gateway import require_human_requester
from src.services.identity_router import get_verified_identity
from src.services.interjection_queue import InMemoryInterjectionQueue
from src.services.stream_buffer_service import StreamBufferService
from src.utils.async_db import get_async_db, init_db_async, session_service
from src.utils.history_manager import TreeMessageManager

_OWNER = AgentIdentity.for_role(agent_id="human-owner", role="normal", is_human=True)
_STRANGER = AgentIdentity.for_role(agent_id="stranger", role="normal", is_human=True)
_ADMIN = AgentIdentity.for_role(agent_id="admin-user", role="admin", is_human=True)
# X-Agent-ID 代打：admin 保留 human_role="admin"；非 admin 擁有者變成靈魂本身（代理面）。
_ADMIN_ASSUMED = AgentIdentity(agent_id="Alice", human_role="admin", is_human=False)
_OWNER_ASSUMED = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False)
_SOUL_TOKEN = AgentIdentity.for_role(agent_id="Bob", role="Base")
_DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
_DB_FORBIDDEN = AssertionError("incognito interjections must not touch the session database")


@pytest.fixture
def queue(monkeypatch) -> InMemoryInterjectionQueue:
    svc = InMemoryInterjectionQueue()
    monkeypatch.setattr(interjection_mod, "interjection_queue", svc)
    return svc


@pytest.fixture
def buffer(monkeypatch, queue) -> StreamBufferService:
    svc = StreamBufferService(interjections=queue)
    monkeypatch.setattr(ui_mod, "stream_buffer_service", svc)
    return svc


@pytest.fixture
async def sandbox_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        yield db_path


def _client(identity: AgentIdentity) -> AsyncClient:
    app = FastAPI()
    app.include_router(interjection_mod.router, prefix="/v1")
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _stream(buffer, owner, session_id="sess-1", *, incognito=True):
    if incognito:
        admission = IncognitoStreamAdmission(
            mode="incognito", user_parent_id="u", assistant_node_id="a"
        )
    else:
        admission = PersistedStreamAdmission(
            mode="persistent", revision="r1", user_parent_id="u", assistant_node_id="a"
        )
    return await buffer.create(session_id, identity=owner, admission=admission)


def _path(session_id: str, interjection_id: str | None = None) -> str:
    base = f"/v1/ui/sessions/{session_id}/interjections"
    return f"{base}/{interjection_id}" if interjection_id else base


async def _call(identity, method, session_id, stream_id, *, iid=None, body=None):
    params = {"stream_id": stream_id} if stream_id is not None else {}
    async with _client(identity) as client:
        if method == "POST":
            return await client.post(
                _path(session_id), params=params, json=body or {"text": "hello"}
            )
        if method == "GET":
            return await client.get(_path(session_id), params=params)
        return await client.delete(_path(session_id, iid), params=params)


def _code(response) -> tuple[int, str]:
    return response.status_code, response.json()["detail"]["code"]


# --- 路由登記與輸入結構 -------------------------------------------------------


def test_routes_registered_under_v1_and_root():
    routes = {
        (route.path, method)
        for route in main_app.routes
        for method in getattr(route, "methods", None) or ()
    }
    for prefix in ("/v1", ""):
        assert (f"{prefix}/ui/sessions/{{session_id}}/interjections", "POST") in routes
        assert (f"{prefix}/ui/sessions/{{session_id}}/interjections", "GET") in routes
        assert (
            f"{prefix}/ui/sessions/{{session_id}}/interjections/{{interjection_id}}",
            "DELETE",
        ) in routes


def test_request_model_has_only_text_and_urgent():
    assert set(InterjectionCreateRequest.model_fields) == {"text", "urgent"}


@pytest.mark.parametrize(
    "extra",
    [
        {"approve": True},
        {"approved_by": "auto_reviewer"},
        {"auth_level": "FullYOLO"},
        {"mode": "YOLO"},
        {"tool_choice": "none"},
        {"tool_call_id": "call_1"},
        {"role": "system"},
    ],
)
async def test_extra_fields_are_rejected(buffer, queue, extra):
    entry = await _stream(buffer, _OWNER)
    response = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "hi", **extra}
    )
    assert _code(response) == (422, "VALIDATION_ERROR")
    assert queue.list_items(entry.session_id, entry.stream_id) == []


async def test_owner_happy_path_add_list_retract(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    created = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "補充", "urgent": True}
    )
    assert created.status_code == 201
    body = created.json()
    assert (body["text"], body["urgent"], body["status"], body["step"]) == (
        "補充",
        True,
        "queued",
        None,
    )
    listed = await _call(_OWNER, "GET", entry.session_id, entry.stream_id)
    assert listed.status_code == 200
    assert [(i["id"], i["status"]) for i in listed.json()["interjections"]] == [
        (body["id"], "queued")
    ]
    retracted = await _call(_OWNER, "DELETE", entry.session_id, entry.stream_id, iid=body["id"])
    assert retracted.status_code == 200
    assert (retracted.json()["id"], retracted.json()["status"]) == (body["id"], "retracted")
    assert queue.list_items(entry.session_id, entry.stream_id) == []


async def test_retract_after_handoff_is_refused(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    item = queue.enqueue(entry.session_id, entry.stream_id, "text")
    queue.drain(entry.session_id, entry.stream_id, step=2)
    response = await _call(_OWNER, "DELETE", entry.session_id, entry.stream_id, iid=item.id)
    assert _code(response) == (409, "INTERJECTION_NOT_RETRACTABLE")
    assert [i.status for i in queue.list_items(entry.session_id, entry.stream_id)] == ["pending"]


# --- 越權 ---------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize("caller", [_STRANGER, _ADMIN], ids=["stranger", "admin-not-requester"])
async def test_non_requester_cannot_touch_the_queue(buffer, queue, method, caller):
    # admin 不是這個串流的發起者時也拒絕：沿用 _authorized_stream_state（和停止端點相同）
    entry = await _stream(buffer, _OWNER)
    item = queue.enqueue(entry.session_id, entry.stream_id, "owner text")
    with patch.object(
        session_service, "get_session_by_id", AsyncMock(return_value={"agent_id": "Alice"})
    ):
        response = await _call(caller, method, entry.session_id, entry.stream_id, iid=item.id)
    assert _code(response) == (403, "AUTH_PERMISSION_DENIED")
    assert [(i.id, i.status) for i in queue.list_items(entry.session_id, entry.stream_id)] == [
        (item.id, "queued")
    ]


async def test_owner_who_lost_the_session_is_refused(sandbox_db, buffer, queue):
    sid = "persistent-sess"
    assert await session_service.upsert_session(
        sid, "private", TreeMessageManager.create_empty_tree(), owner_user_id=_OWNER.agent_id
    )
    entry = await _stream(buffer, _OWNER, sid, incognito=False)
    created = await _call(_OWNER, "POST", sid, entry.stream_id)
    assert created.status_code == 201
    async with get_async_db() as database:
        await database.execute(
            "UPDATE user_sessions SET owner_user_id=? WHERE id=?", ("other-owner", sid)
        )
        await database.commit()
    for method in ("POST", "GET", "DELETE"):
        response = await _call(_OWNER, method, sid, entry.stream_id, iid=created.json()["id"])
        assert _code(response) == (403, "AUTH_PERMISSION_DENIED"), method
    assert [i.text for i in queue.list_items(sid, entry.stream_id)] == ["hello"]


async def test_other_sessions_interjection_id_is_not_found(buffer, queue):
    first = await _stream(buffer, _OWNER, "sess-a")
    second = await _stream(buffer, _OWNER, "sess-b")
    item = queue.enqueue(first.session_id, first.stream_id, "for session a")
    response = await _call(_OWNER, "DELETE", second.session_id, second.stream_id, iid=item.id)
    assert _code(response) == (404, "INTERJECTION_NOT_FOUND")
    listed = await _call(_OWNER, "GET", second.session_id, second.stream_id)
    assert listed.json()["interjections"] == []
    assert [i.status for i in queue.list_items(first.session_id, first.stream_id)] == ["queued"]


# --- 代理身分 -----------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize(
    "agent",
    [_OWNER_ASSUMED, _SOUL_TOKEN, _DEBUGGER],
    ids=["owner-x-agent-id", "soul-token", "debugger"],
)
async def test_agent_identities_are_refused_even_on_their_own_stream(buffer, queue, method, agent):
    entry = await _stream(buffer, agent)  # 串流由該代理身分建立：擁有者檢查本身會過
    item = queue.enqueue(entry.session_id, entry.stream_id, "text")
    response = await _call(agent, method, entry.session_id, entry.stream_id, iid=item.id)
    assert _code(response) == (403, "AUTH_PERMISSION_DENIED")
    assert [(i.id, i.status) for i in queue.list_items(entry.session_id, entry.stream_id)] == [
        (item.id, "queued")
    ]


@pytest.mark.parametrize("caller", [_ADMIN_ASSUMED, _ADMIN], ids=["admin-x-agent-id", "admin"])
async def test_admin_and_admin_assumption_may_interject_on_their_stream(buffer, queue, caller):
    entry = await _stream(buffer, caller)
    created = await _call(caller, "POST", entry.session_id, entry.stream_id)
    assert created.status_code == 201
    listed = await _call(caller, "GET", entry.session_id, entry.stream_id)
    assert [i["text"] for i in listed.json()["interjections"]] == ["hello"]
    retracted = await _call(
        caller, "DELETE", entry.session_id, entry.stream_id, iid=created.json()["id"]
    )
    assert retracted.json()["status"] == "retracted"


@pytest.mark.parametrize(
    ("identity", "allowed"),
    [
        (_OWNER, True),
        (_ADMIN, True),
        (_ADMIN_ASSUMED, True),
        (_OWNER_ASSUMED, False),
        (_SOUL_TOKEN, False),
        (_DEBUGGER, False),
        (None, False),
    ],
)
def test_human_requester_gate(identity, allowed):
    if allowed:
        assert require_human_requester(identity) is identity
        return
    with pytest.raises(ToukaException) as exc:
        require_human_requester(identity)
    assert (exc.value.status_code, exc.value.detail["code"]) == (403, "AUTH_PERMISSION_DENIED")


# --- 跨 stream ----------------------------------------------------------------


async def test_previous_stream_cannot_leak_into_the_next(buffer, queue):
    old = await _stream(buffer, _OWNER)
    created = await _call(_OWNER, "POST", old.session_id, old.stream_id, body={"text": "old"})
    assert created.status_code == 201
    new = await _stream(buffer, _OWNER)  # 同 session 開新串流
    assert new.stream_id != old.stream_id

    for method in ("POST", "GET", "DELETE"):
        response = await _call(
            _OWNER, method, old.session_id, old.stream_id, iid=created.json()["id"]
        )
        assert _code(response) == (404, "STREAM_NOT_FOUND"), method
    listed = await _call(_OWNER, "GET", new.session_id, new.stream_id)
    assert listed.json()["interjections"] == []
    response = await _call(
        _OWNER, "DELETE", new.session_id, new.stream_id, iid=created.json()["id"]
    )
    assert _code(response) == (404, "INTERJECTION_NOT_FOUND")
    assert queue.drain(new.session_id, new.stream_id, step=1) == []
    assert queue.list_items(old.session_id, old.stream_id) == []


async def test_finished_stream_accepts_no_more_interjections(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    queue.enqueue(entry.session_id, entry.stream_id, "queued before the end")
    await buffer.finish(entry.session_id, stream_id=entry.stream_id)
    response = await _call(_OWNER, "POST", entry.session_id, entry.stream_id)
    assert _code(response) == (404, "STREAM_NOT_FOUND")
    listed = await _call(_OWNER, "GET", entry.session_id, entry.stream_id)
    assert listed.status_code == 200
    assert listed.json()["interjections"] == []


async def test_stream_id_is_required(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    for method in ("POST", "GET", "DELETE"):
        response = await _call(_OWNER, method, entry.session_id, None, iid="x")
        assert _code(response) == (422, "VALIDATION_ERROR"), method
    assert queue.list_items(entry.session_id, entry.stream_id) == []


async def test_session_without_a_ui_stream_is_not_found(buffer, queue):
    with patch.object(
        session_service,
        "get_session_by_id",
        AsyncMock(return_value={"agent_id": "Alice", "owner_user_id": _OWNER.agent_id}),
    ):
        response = await _call(_OWNER, "POST", "idle-sess", "some-stream")
    assert _code(response) == (404, "STREAM_NOT_FOUND")
    assert queue.list_items("idle-sess", "some-stream") == []


# --- 超量 ---------------------------------------------------------------------


async def test_queue_cap_over_http(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    ids = []
    for n in range(10):
        response = await _call(
            _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": f"m{n}"}
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    response = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "overflow"}
    )
    assert _code(response) == (429, "INTERJECTION_QUEUE_FULL")
    await _call(_OWNER, "DELETE", entry.session_id, entry.stream_id, iid=ids[0])
    response = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "after retract"}
    )
    assert response.status_code == 201


async def test_text_length_over_http(buffer, queue):
    entry = await _stream(buffer, _OWNER)
    response = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "a" * 4001}
    )
    assert _code(response) == (422, "INTERJECTION_TOO_LONG")
    for text in ("", "   "):
        response = await _call(
            _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": text}
        )
        assert _code(response) == (422, "VALIDATION_ERROR"), repr(text)
    response = await _call(
        _OWNER, "POST", entry.session_id, entry.stream_id, body={"text": "a" * 4000}
    )
    assert response.status_code == 201
    assert len(queue.list_items(entry.session_id, entry.stream_id)) == 1


# --- 無痕 ---------------------------------------------------------------------


async def test_incognito_interjections_stay_in_memory_only(buffer, queue, monkeypatch):
    database = AsyncMock(side_effect=_DB_FORBIDDEN)
    for name in ("get_session_by_id", "read_persistence_snapshot", "upsert_session"):
        monkeypatch.setattr(session_service, name, database)
    entry = await _stream(buffer, _OWNER, "incognito-sess")
    created = await _call(_OWNER, "POST", entry.session_id, entry.stream_id)
    assert created.status_code == 201
    listed = await _call(_OWNER, "GET", entry.session_id, entry.stream_id)
    assert [i["id"] for i in listed.json()["interjections"]] == [created.json()["id"]]
    await buffer.finish(entry.session_id, stream_id=entry.stream_id)
    assert queue.list_items(entry.session_id, entry.stream_id) == []
    database.assert_not_awaited()
