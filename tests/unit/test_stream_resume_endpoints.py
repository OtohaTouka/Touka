"""ADR-0081 — resume 端點與 stop 擴充測試（先 Red 後 Green）。

- GET /sessions/{id}/stream：重播＋跟流（SSE）、starting_after 切片、404 無緩衝、IDOR 拒絕
- GET /streams/active：只回呼叫者可存取的串流（IDOR 過濾）
- POST /sessions/{id}/stop：擴充後同時取消緩衝 producer
"""

import contextlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import IncognitoStreamAdmission, PersistedStreamAdmission
from src.routers import ui_sessions as ui_mod
from src.services.stream_buffer_service import StreamBufferService
from src.utils.async_db import SessionPersistenceError, get_async_db, init_db_async, session_service
from src.utils.history_manager import TreeMessageManager

_ADMIN = AgentIdentity.for_role(agent_id="admin-user", role="admin", is_human=True)
_STRANGER = AgentIdentity.for_role(agent_id="stranger", role="Base")
_UI_OWNER = AgentIdentity.for_role(agent_id="human-owner", role="normal", is_human=True)


async def _owned_entry(buffer, *, incognito):
    model = IncognitoStreamAdmission if incognito else PersistedStreamAdmission
    options = {} if incognito else {"revision": "synthetic-revision"}
    return await buffer.create(
        "private-ui",
        identity=_UI_OWNER,
        admission=model(
            mode="incognito" if incognito else "persistent",
            user_parent_id="user",
            assistant_node_id="assistant",
            **options,
        ),
    )


@pytest.mark.asyncio
async def test_ui_incognito_buffer_resume_and_stop_require_no_database(buffer, monkeypatch):
    entry = await _owned_entry(buffer, incognito=True)
    database = AsyncMock(side_effect=AssertionError("UI buffer must not read a session database"))
    monkeypatch.setattr(session_service, "get_session_by_id", database)
    monkeypatch.setattr(session_service, "read_persistence_snapshot", database)
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", Mock())
    monkeypatch.setattr(ui_mod.process_registry, "pop_session", Mock(return_value=[]))
    monkeypatch.setattr(ui_mod, "terminate_processes", Mock(return_value=0))
    result = await ui_mod.list_active_streams(identity=_UI_OWNER)
    assert [row["session_id"] for row in result["streams"]] == [entry.session_id]
    state_handler = getattr(ui_mod, "get_session_stream_state", None)
    assert state_handler is not None, "selected-session terminal lookup is missing"
    state = await state_handler(entry.session_id, identity=_UI_OWNER)
    assert state.state.stream_id == entry.stream_id and state.state.terminal is None
    await ui_mod.stop_session_tasks(entry.session_id, identity=_UI_OWNER, stream_id=entry.stream_id)
    assert entry.status == "cancelled"
    response = await ui_mod.resume_session_stream(
        entry.session_id, identity=_UI_OWNER, stream_id=entry.stream_id
    )
    chunks = await _collect_sse(response)
    assert chunks == entry.chunks and '"generation": "cancelled"' in chunks[-1]
    assert (
        await state_handler(entry.session_id, identity=_UI_OWNER)
    ).state.terminal.generation == "cancelled"
    database.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["owner", "deleted", "read_error"])
@pytest.mark.parametrize("operation", ["resume", "stop", "state", "active"])
@pytest.mark.asyncio
async def test_persistent_ui_buffer_revalidates_current_owner(
    sandbox_db, buffer, monkeypatch, mutation, operation
):
    tree = TreeMessageManager.create_empty_tree()
    assert await session_service.upsert_session(
        "private-ui", "private", tree, owner_user_id=_UI_OWNER.agent_id
    )
    entry = await _owned_entry(buffer, incognito=False)
    if mutation == "owner":
        async with get_async_db() as database:
            await database.execute(
                "UPDATE user_sessions SET owner_user_id=? WHERE id=?",
                ("other-owner", entry.session_id),
            )
            await database.commit()
    elif mutation == "deleted":
        await session_service.delete_session(entry.session_id)
    else:
        monkeypatch.setattr(
            session_service,
            "read_persistence_snapshot",
            AsyncMock(side_effect=SessionPersistenceError()),
        )
    interrupt = Mock()
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", interrupt)
    if operation == "active":
        assert (await ui_mod.list_active_streams(identity=_UI_OWNER))["streams"] == []
    else:
        functions = {
            "resume": ui_mod.resume_session_stream,
            "stop": ui_mod.stop_session_tasks,
            "state": ui_mod.get_session_stream_state,
        }
        with pytest.raises(ToukaException):
            await functions[operation](
                entry.session_id,
                identity=_UI_OWNER,
                **({"stream_id": entry.stream_id} if operation != "state" else {}),
            )
    assert entry.status == "active" and entry.terminal is None
    interrupt.assert_not_called()


@pytest.mark.parametrize("operation", ["resume", "stop", "state", "active"])
@pytest.mark.asyncio
async def test_db_authorization_cannot_be_reused_for_replaced_stream(
    sandbox_db, buffer, monkeypatch, operation
):
    assert await session_service.upsert_session(
        "private-ui",
        "private",
        TreeMessageManager.create_empty_tree(),
        owner_user_id=_UI_OWNER.agent_id,
    )
    old = await _owned_entry(buffer, incognito=False)
    read = session_service.read_persistence_snapshot
    replacement = None

    async def replace_during_read(*args):
        nonlocal replacement
        snapshot = await read(*args)
        replacement = await _owned_entry(buffer, incognito=False)
        return snapshot

    monkeypatch.setattr(session_service, "read_persistence_snapshot", replace_during_read)
    interrupt = Mock()
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", interrupt)
    if operation == "active":
        assert (await ui_mod.list_active_streams(identity=_UI_OWNER))["streams"] == []
    else:
        functions = {
            "resume": ui_mod.resume_session_stream,
            "stop": ui_mod.stop_session_tasks,
            "state": ui_mod.get_session_stream_state,
        }
        with pytest.raises(ToukaException):
            await functions[operation](
                old.session_id,
                identity=_UI_OWNER,
                **({"stream_id": old.stream_id} if operation != "state" else {}),
            )
    assert replacement is not None and replacement.status == "active"
    interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_successful_delete_retires_old_buffer_before_same_id_recreate(sandbox_db, buffer):
    tree = TreeMessageManager.create_empty_tree()
    assert await session_service.upsert_session(
        "private-ui", "original", tree, owner_user_id=_UI_OWNER.agent_id
    )
    original = await session_service.read_persistence_snapshot("private-ui", _UI_OWNER)
    old = await _owned_entry(buffer, incognito=False)
    assert await ui_mod.delete_session("private-ui", if_match=original.revision, identity=_UI_OWNER)
    assert buffer.get_state("private-ui") is None
    assert old.status == "cancelled"
    assert await session_service.upsert_session(
        "private-ui", "recreated", tree, owner_user_id=_UI_OWNER.agent_id
    )
    with pytest.raises(ToukaException):
        await ui_mod.resume_session_stream(
            "private-ui", identity=_UI_OWNER, stream_id=old.stream_id
        )
    assert not await buffer.append("private-ui", "late-old-chunk", stream_id=old.stream_id)
    fresh = await _owned_entry(buffer, incognito=False)
    await buffer.finish("private-ui", stream_id=old.stream_id)
    assert fresh.status == "active"


@pytest.mark.asyncio
async def test_delete_does_not_retire_new_stream_created_after_commit(
    sandbox_db, buffer, monkeypatch
):
    tree = TreeMessageManager.create_empty_tree()
    assert await session_service.upsert_session(
        "private-ui", "original", tree, owner_user_id=_UI_OWNER.agent_id
    )
    original = await session_service.read_persistence_snapshot("private-ui", _UI_OWNER)
    old = await _owned_entry(buffer, incognito=False)
    delete = session_service.conditional_delete_session
    replacement = None

    async def delete_then_recreate(*args):
        nonlocal replacement
        result = await delete(*args)
        assert result.status == "saved"
        assert await session_service.upsert_session(
            "private-ui", "recreated", tree, owner_user_id=_UI_OWNER.agent_id
        )
        replacement = await _owned_entry(buffer, incognito=False)
        return result

    monkeypatch.setattr(session_service, "conditional_delete_session", delete_then_recreate)
    await ui_mod.delete_session("private-ui", if_match=original.revision, identity=_UI_OWNER)
    assert replacement is not None and replacement.stream_id != old.stream_id
    assert buffer.get_state("private-ui")["stream_id"] == replacement.stream_id
    assert replacement.status == "active"


@pytest.mark.parametrize("operation", ["resume", "stop", "state", "active"])
@pytest.mark.asyncio
async def test_ui_stream_other_principal_cannot_read_or_interrupt(buffer, monkeypatch, operation):
    entry = await _owned_entry(buffer, incognito=True)
    interrupt = Mock()
    pop = Mock(return_value=[])
    terminate = Mock(return_value=0)
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", interrupt)
    # /stop 在事件迴圈上取出行程、再到執行緒裡殺：授權失敗時兩步都不能發生。
    monkeypatch.setattr(ui_mod.process_registry, "pop_session", pop)
    monkeypatch.setattr(ui_mod, "terminate_processes", terminate)
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    if operation == "active":
        assert (await ui_mod.list_active_streams(identity=_ADMIN))["streams"] == []
    else:
        functions = {
            "resume": ui_mod.resume_session_stream,
            "stop": ui_mod.stop_session_tasks,
            "state": getattr(ui_mod, "get_session_stream_state", None),
        }
        assert functions[operation] is not None
        with pytest.raises(ToukaException) as exc:
            await functions[operation](entry.session_id, identity=_ADMIN)
        assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    assert entry.status == "active" and entry.terminal is None
    interrupt.assert_not_called()
    pop.assert_not_called()
    terminate.assert_not_called()


@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize("stream_id", [None, "old-generation"])
@pytest.mark.asyncio
async def test_ui_stop_without_exact_generation_cannot_stop_new_producer(
    buffer, monkeypatch, stream_id, waiting
):
    entry = await _owned_entry(buffer, incognito=True)
    interrupt = Mock()
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", interrupt)
    # 同一人另一則還在等 hook 的請求也不能讓沒指名的停止停到進行中的那一代
    other = (
        buffer.pending_request(entry.session_id, _UI_OWNER) if waiting else contextlib.nullcontext()
    )
    with other, pytest.raises(ToukaException) as exc:
        await ui_mod.stop_session_tasks(entry.session_id, identity=_UI_OWNER, stream_id=stream_id)
    assert exc.value.code == ErrorCode.STREAM_NOT_FOUND
    assert entry.status == "active"
    interrupt.assert_not_called()


def _stop_spies(monkeypatch) -> tuple[Mock, AsyncMock]:
    interrupt = Mock()
    database = AsyncMock(side_effect=AssertionError("a waiting request is stopped without the DB"))
    monkeypatch.setattr(ui_mod.rhythm_service, "interrupt_session", interrupt)
    monkeypatch.setattr(ui_mod.process_registry, "pop_session", Mock(return_value=[]))
    monkeypatch.setattr(ui_mod, "terminate_processes", Mock(return_value=0))
    monkeypatch.setattr(session_service, "get_session_by_id", database)
    monkeypatch.setattr(session_service, "read_persistence_snapshot", database)
    return interrupt, database


@pytest.mark.parametrize("earlier", ["none", "finished_incognito", "finished_persistent"])
@pytest.mark.asyncio
async def test_ui_stop_reaches_a_request_whose_stream_has_not_started(buffer, monkeypatch, earlier):
    """ADR-0292：hook 執行中按停止。無痕查不到對話、上一則的結束登記還留著也要停得下來。"""
    previous = None
    if earlier != "none":
        previous = await _owned_entry(buffer, incognito=earlier == "finished_incognito")
        assert await buffer.cancel(previous.session_id, stream_id=previous.stream_id)
    interrupt, database = _stop_spies(monkeypatch)
    with buffer.pending_request("private-ui", _UI_OWNER):
        result = await ui_mod.stop_session_tasks("private-ui", identity=_UI_OWNER, stream_id=None)
    assert result["status"] == "success"
    interrupt.assert_called_once_with("private-ui")
    database.assert_not_awaited()
    if previous is not None:
        assert previous.status == "cancelled" and buffer.get_state("private-ui") is not None


@pytest.mark.asyncio
async def test_ui_stop_does_not_take_over_another_persons_waiting_request(buffer, monkeypatch):
    interrupt, _database = _stop_spies(monkeypatch)
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    with buffer.pending_request("private-ui", _UI_OWNER), pytest.raises(ToukaException) as exc:
        await ui_mod.stop_session_tasks("private-ui", identity=_ADMIN, stream_id=None)
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_waiting_requests_are_tracked_per_request_and_cleared_on_errors(buffer):
    with buffer.pending_request("private-ui", _UI_OWNER):
        with pytest.raises(RuntimeError), buffer.pending_request("private-ui", _UI_OWNER):
            raise RuntimeError
        # 同一對話的另一則還在等：先結束的那則只移除自己
        assert buffer.has_pending_request("private-ui", _UI_OWNER)
        assert not buffer.has_pending_request("private-ui", _ADMIN)
        assert not buffer.has_pending_request("other-session", _UI_OWNER)
    assert not buffer.has_pending_request("private-ui", _UI_OWNER)
    assert buffer._pending == {}


@pytest.fixture
async def sandbox_db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        yield db_path


@pytest.fixture
def buffer(monkeypatch) -> StreamBufferService:
    svc = StreamBufferService()
    monkeypatch.setattr(ui_mod, "stream_buffer_service", svc)
    return svc


async def _seed_session(sid: str, agent_id: str = "DebugAgent") -> None:
    tree = TreeMessageManager.create_empty_tree()
    tree = TreeMessageManager.append_node(tree, "user", "hi", name="User")
    await session_service.upsert_session(sid, "t", tree, identity=AgentIdentity(agent_id=agent_id))


async def _collect_sse(resp) -> list[str]:
    return [c async for c in resp.body_iterator]


@pytest.mark.asyncio
async def test_resume_endpoint_replays_with_starting_after(sandbox_db, buffer):
    sid = "sess-ep-1"
    await _seed_session(sid)
    await buffer.create(sid)
    for i in range(3):
        await buffer.append(sid, f"data: {i}\n\n")
    await buffer.finish(sid, "done")

    resp = await ui_mod.resume_session_stream(sid, starting_after=0, identity=_ADMIN)
    chunks = await _collect_sse(resp)
    assert chunks == ["data: 1\n\n", "data: 2\n\n"]
    assert resp.media_type == "text/event-stream"


@pytest.mark.asyncio
async def test_resume_endpoint_404_when_no_buffer(sandbox_db, buffer):
    sid = "sess-ep-2"
    await _seed_session(sid)
    with pytest.raises(ToukaException) as exc:
        await ui_mod.resume_session_stream(sid, starting_after=-1, identity=_ADMIN)
    assert exc.value.code == ErrorCode.STREAM_NOT_FOUND


@pytest.mark.asyncio
async def test_resume_endpoint_denies_non_owner(sandbox_db, buffer):
    """IDOR：非擁有者（非 admin/Debugger）不得接回他人串流。"""
    sid = "sess-ep-3"
    await _seed_session(sid, agent_id="DebugAgent")
    await buffer.create(sid)

    with pytest.raises(ToukaException) as exc:
        await ui_mod.resume_session_stream(sid, starting_after=-1, identity=_STRANGER)
    assert exc.value.code == ErrorCode.AUTH_PERMISSION_DENIED


@pytest.mark.asyncio
async def test_active_streams_list_filters_by_ownership(sandbox_db, buffer):
    """active 清單逐條過 _can_access_session：陌生人看不到、admin 全看。"""
    sid = "sess-ep-4"
    await _seed_session(sid, agent_id="DebugAgent")
    await buffer.create(sid)

    res_admin = await ui_mod.list_active_streams(identity=_ADMIN)
    assert [s["session_id"] for s in res_admin["streams"]] == [sid]
    assert res_admin["streams"][0]["status"] == "active"
    assert "stream_id" in res_admin["streams"][0]

    res_stranger = await ui_mod.list_active_streams(identity=_STRANGER)
    assert res_stranger["streams"] == []


@pytest.mark.asyncio
async def test_stop_endpoint_cancels_buffer_stream(sandbox_db, buffer):
    """stop 擴充：原有中斷動作之外，必須取消緩衝串流（producer + 終態）。"""
    sid = "sess-ep-5"
    await _seed_session(sid)
    await buffer.create(sid)
    assert buffer.get_state(sid)["status"] == "active"

    result = await ui_mod.stop_session_tasks(sid, identity=_ADMIN)
    assert result["status"] == "success"
    assert buffer.get_state(sid)["status"] == "cancelled"
