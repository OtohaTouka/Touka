"""ADR-0081 — dispatcher 可恢復串流路徑測試（先 Red 後 Green）。

驗證生成與 HTTP 連線解耦：
- ``resumable_stream=True`` ＋ session_id → producer 寫入緩衝、回應改為訂閱者
- 訂閱者不消費／中途斷線 → producer 仍跑完並「伺服器端持久化」assistant 回覆
- 無旗標（heartbeat／外部 API 模擬）→ 原路徑直通、零緩衝、零雙寫
"""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from src.adapters.schemas import OpenAIChatRequest
from src.handlers import react_core
from src.handlers.adapters import openai_adapter
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import SessionChanges
from src.routers import proxy as proxy_router
from src.services import dispatcher as dispatcher_mod
from src.services import proxy_service
from src.services.dispatchers import factory
from src.services.identity_router import get_verified_identity
from src.services.models.routes import CustomRouteDisposition
from src.services.stream_buffer_service import StreamBufferService
from src.utils.async_db import SessionSnapshot, get_async_db, init_db_async, session_service
from src.utils.history_manager import TreeMessageManager

UI_OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
UI_TREE = {
    "messages": {"user": {"id": "user", "role": "user", "content": "hi", "parentId": None}},
    "currentId": "user",
}


def _shared_stream_contract():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures" / "reliability_session_stream.json").read_text(
            encoding="utf-8"
        )
    )


def _assert_contract_frames(chunks, contract, *, incognito, stream_id, revision=None):
    # Only the two per-run opaque identifiers have explicit aliases. Frame bytes,
    # extension presence, owner and rich payload are otherwise compared unchanged.
    actual = [chunk.replace(stream_id, contract["aliases"]["stream_id"]) for chunk in chunks]
    if revision is not None:
        actual = [chunk.replace(revision, contract["aliases"]["revision"]) for chunk in actual]
    terminal = contract["incognito_terminal" if incognito else "terminal"]
    expected = [
        f"data: {json.dumps(contract['started'], ensure_ascii=False)}\n\n",
        *contract["generator_frames"],
        f"data: {json.dumps(terminal, ensure_ascii=False)}\n\n",
    ]
    assert actual == expected


@pytest.mark.parametrize("incognito", [False, True])
@pytest.mark.asyncio
async def test_shared_ui_contract_real_producer_and_owned_sqlite(
    sandbox_db, fresh_buffer, monkeypatch, incognito
):
    contract = _shared_stream_contract()
    assert UI_OWNER.agent_id == contract["owner_user_id"]
    if not incognito:
        assert await session_service.upsert_session(
            contract["session_id"],
            contract["title"],
            contract["user_tree"],
            identity=AgentIdentity(agent_id=contract["agent_id"]),
            owner_user_id=UI_OWNER.agent_id,
        )
    request = await _ui_request(contract["session_id"], incognito=incognito)
    read = AsyncMock(wraps=session_service.read_persistence_snapshot)
    save = AsyncMock(wraps=session_service.conditional_save_session)
    monkeypatch.setattr(session_service, "read_persistence_snapshot", read)
    monkeypatch.setattr(session_service, "conditional_save_session", save)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)

    async def generate():
        for frame in contract["generator_frames"]:
            yield frame

    _patch_generator(monkeypatch, generate)
    response = await _ui_dispatch(request)
    chunks = [chunk async for chunk in response.body_iterator]
    state = fresh_buffer.get_state(contract["session_id"])
    revision = state["terminal"]["persistence"].get("revision")
    _assert_contract_frames(
        chunks, contract, incognito=incognito, stream_id=state["stream_id"], revision=revision
    )
    # The same assertion rejects a dropped terminal; it is not a self-reported fixture claim.
    with pytest.raises(AssertionError):
        _assert_contract_frames(
            chunks[:-1],
            contract,
            incognito=incognito,
            stream_id=state["stream_id"],
            revision=revision,
        )
    if incognito:
        read.assert_not_awaited()
        save.assert_not_awaited()
        trim.assert_not_awaited()
        return
    persisted = await session_service.read_persistence_snapshot(contract["session_id"], UI_OWNER)
    assert persisted.session["owner_user_id"] == contract["owner_user_id"]
    assert persisted.session["agent_id"] == contract["agent_id"]
    assert persisted.revision == revision
    # The TS test compares the actual writer POST to this exact shared full payload.
    payload = copy.deepcopy(contract["full_payload"])
    payload.pop("id")
    payload["agent_id"] = AgentIdentity(agent_id=payload["agent_id"])
    result = await session_service.conditional_save_session(
        contract["session_id"],
        revision,
        SessionChanges(**payload),
        UI_OWNER,
    )
    assert result.status == "saved" and result.representation == "full"
    final = await session_service.read_persistence_snapshot(contract["session_id"], UI_OWNER)
    assert final.session["messages"] == contract["full_payload"]["messages"]
    assert final.session["owner_user_id"] == contract["owner_user_id"]
    assert final.session["agent_id"] == contract["agent_id"]
    assert final.revision == result.revision


@pytest.fixture
async def ui_router_client(monkeypatch):
    application = FastAPI()
    application.state.context = {}
    application.include_router(proxy_router.router, prefix="/v1")
    application.dependency_overrides[get_verified_identity] = lambda: UI_OWNER

    async def pipeline(request, ctx):
        ctx["session_id"] = request.session_id

    monkeypatch.setattr(proxy_service.pipeline, "execute", AsyncMock(side_effect=pipeline))
    monkeypatch.setattr(proxy_service, "log_payload", Mock())
    monkeypatch.setattr(proxy_router.model_manager, "mark_model_seen", Mock())
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client


def _ui_body(request):
    return {
        **request.model_dump(mode="json", exclude_none=True),
        "ui_persistence": request.ui_persistence.model_dump(),
    }


@pytest.mark.parametrize("protocol", ["chat/completions", "messages"])
@pytest.mark.asyncio
async def test_router_binds_authorization_to_same_revision(
    sandbox_db, fresh_buffer, monkeypatch, ui_router_client, protocol
):
    await _ui_seed("ui")
    first = await session_service.read_persistence_snapshot("ui", UI_OWNER)
    second = SessionSnapshot({**first.session, "owner_user_id": "other-owner"}, "other-revision")
    request = await _ui_request("ui", revision=second.revision)
    monkeypatch.setattr(
        session_service, "read_persistence_snapshot", AsyncMock(side_effect=[first, second, second])
    )
    generate = AsyncMock(return_value=_fake_gen())
    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", generate)
    response = await ui_router_client.post(
        f"/v1/{protocol}", json={**_ui_body(request), "max_tokens": 40}
    )
    assert response.status_code == 409
    generate.assert_not_awaited()
    proxy_service.pipeline.execute.assert_not_awaited()
    assert fresh_buffer.get_state("ui") is None


@pytest.mark.parametrize("explicit_ui", [True, False])
@pytest.mark.asyncio
async def test_router_cannot_replace_foreign_ui_buffer(
    sandbox_db, fresh_buffer, monkeypatch, ui_router_client, explicit_ui
):
    request = await _ui_request("ui", incognito=True)
    other = AgentIdentity.for_role(agent_id="private-owner", role="normal", is_human=True)
    entry = await fresh_buffer.create("ui", identity=other, admission=request.ui_persistence)
    before = list(entry.chunks)
    generate = AsyncMock(return_value=_fake_gen())
    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", generate)
    body = _ui_body(request) if explicit_ui else request.model_dump(mode="json", exclude_none=True)
    response = await ui_router_client.post("/v1/chat/completions", json=body)
    assert response.status_code == 403
    assert fresh_buffer._streams["ui"] is entry
    assert entry.status == "active" and entry.chunks == before and entry.terminal is None
    generate.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["chat/completions", "messages"])
@pytest.mark.parametrize("explicit_ui", [True, False])
@pytest.mark.asyncio
async def test_actual_router_factory_upstream_payload_excludes_ui_contract(
    sandbox_db, fresh_buffer, monkeypatch, ui_router_client, protocol, explicit_ui
):
    await _ui_seed("ui")
    request = await _ui_request("ui")
    observed = []

    def transport(outbound):
        observed.append(json.loads(outbound.content))
        return Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "actual mock transport"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    state_class = openai_adapter._State

    def state_factory(**kwargs):
        return state_class(**kwargs, client=AsyncClient(transport=MockTransport(transport)))

    monkeypatch.setattr(openai_adapter, "_State", state_factory)
    monkeypatch.setattr(
        factory,
        "model_manager",
        SimpleNamespace(
            get_models=Mock(return_value=[]),
            resolve_custom_route=Mock(
                return_value=SimpleNamespace(disposition=CustomRouteDisposition.UNKNOWN)
            ),
        ),
    )
    monkeypatch.setattr(
        factory,
        "get_provider_info",
        Mock(return_value=("https://synthetic.invalid/v1/chat/completions", "", "test")),
    )
    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", factory.get_stream_generator)
    monkeypatch.setattr(react_core.rhythm_service, "is_interrupted", Mock(return_value=False))
    monkeypatch.setattr(react_core.rhythm_service, "initialize_session", Mock())
    monkeypatch.setattr(react_core.rhythm_service, "get_effective_rounds", Mock(return_value=0))

    async def pipeline(request, ctx):
        ctx["session_id"] = request.session_id
        ctx["identity"] = AgentIdentity(agent_id="synthetic-soul")
        ctx["t0"] = 0.0

    proxy_service.pipeline.execute.side_effect = pipeline
    body = _ui_body(request) if explicit_ui else request.model_dump(mode="json", exclude_none=True)
    response = await ui_router_client.post(f"/v1/{protocol}", json={**body, "max_tokens": 40})
    assert response.status_code == 200
    assert len(observed) == 1
    assert observed[0]["messages"][0]["content"] == "hi"
    assert all(
        key not in json.dumps(observed)
        for key in (
            "ui_persistence",
            "ui_authorized",
            "ui_requester",
            "user_parent_id",
            "assistant_node_id",
            request.ui_persistence.revision,
        )
    )
    if explicit_ui:
        terminal = fresh_buffer.get_state("ui")["terminal"]
        assert (
            terminal["generation"] == "completed" and terminal["persistence"]["status"] == "saved"
        )
        snapshot = await session_service.read_persistence_snapshot("ui", UI_OWNER)
        assert snapshot.revision == terminal["persistence"]["revision"]
        assert snapshot.session["owner_user_id"] == UI_OWNER.agent_id
    else:
        assert (
            "touka_stream_started" not in response.text
            and "touka_stream_terminal" not in response.text
        )
        assert "data: [DONE]" in response.text


@pytest.mark.asyncio
async def test_actual_router_rechecks_owner_change_during_pipeline(
    sandbox_db, fresh_buffer, monkeypatch, ui_router_client
):
    await _ui_seed("ui")
    request = await _ui_request("ui")
    original = session_service.read_persistence_snapshot

    async def change_owner(*args):
        snapshot = await original(*args)
        async with get_async_db() as db:
            await db.execute(
                "UPDATE user_sessions SET owner_user_id = ? WHERE id = ?", ("other-owner", "ui")
            )
            await db.commit()
        return snapshot

    monkeypatch.setattr(session_service, "read_persistence_snapshot", change_owner)
    generate = AsyncMock(return_value=_fake_gen())
    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", generate)
    response = await ui_router_client.post("/v1/chat/completions", json=_ui_body(request))
    assert response.status_code == 409 and fresh_buffer.get_state("ui") is None
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_producer_compare_rejects_writer_change_during_generation(
    sandbox_db, fresh_buffer, monkeypatch
):
    await _ui_seed("ui")
    request = await _ui_request("ui")

    async def generator():
        yield _sse("old response")
        assert await session_service.upsert_session(
            "ui", "newer writer", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
        )

    _patch_generator(monkeypatch, generator)
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    events = await _response_events(await _ui_dispatch(request))
    terminal = next(event for event in events if event.get("type") == "touka_stream_terminal")
    assert terminal["generation"] == "completed" and terminal["persistence"]["status"] == "failed"
    saved = await session_service.read_persistence_snapshot("ui", UI_OWNER)
    assert (
        saved.session["title"] == "newer writer"
        and "assistant" not in saved.session["messages"]["messages"]
    )
    trim.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_completed_commit_stays_unknown(sandbox_db, fresh_buffer, monkeypatch):
    await _ui_seed("ui")
    request = await _ui_request("ui")
    entered = asyncio.Event()
    save = session_service.conditional_save_session

    async def committed_then_wait(*args, **kwargs):
        result = await save(*args, **kwargs)
        assert result.status == "saved"
        entered.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(session_service, "conditional_save_session", committed_then_wait)
    _patch_generator(monkeypatch, _fake_gen)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)
    response = await _ui_dispatch(request)
    await asyncio.wait_for(entered.wait(), timeout=1)
    state = fresh_buffer.get_state("ui")
    await fresh_buffer.cancel("ui", stream_id=state["stream_id"])
    events = await _response_events(response)
    terminal = next(event for event in events if event.get("type") == "touka_stream_terminal")
    assert terminal["generation"] == "cancelled" and terminal["persistence"]["status"] == "unknown"
    assert (
        "assistant"
        in (await session_service.read_persistence_snapshot("ui", UI_OWNER)).session["messages"][
            "messages"
        ]
    )
    trim.assert_not_awaited()


@pytest.mark.asyncio
async def test_incognito_text_producer_never_reads_or_writes_session(
    sandbox_db, fresh_buffer, monkeypatch
):
    request = await _ui_request("ui", incognito=True)
    read = AsyncMock(side_effect=AssertionError("incognito database read"))
    save = AsyncMock(side_effect=AssertionError("incognito database write"))
    monkeypatch.setattr(session_service, "read_persistence_snapshot", read)
    monkeypatch.setattr(session_service, "conditional_save_session", save)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)
    _patch_generator(monkeypatch, _fake_gen)
    events = await _response_events(await _ui_dispatch(request))
    terminal = next(event for event in events if event.get("type") == "touka_stream_terminal")
    assert terminal["persistence"] == {"status": "skipped", "reason": "incognito"}
    read.assert_not_awaited()
    save.assert_not_awaited()
    trim.assert_not_awaited()


async def _ui_request(sid, *, incognito=False, revision=None, **admission_changes):
    if not incognito and revision is None:
        snapshot = await session_service.read_persistence_snapshot(sid, UI_OWNER)
        revision = snapshot.revision
    admission = {
        "mode": "incognito" if incognito else "persistent",
        "user_parent_id": "user",
        "assistant_node_id": "assistant",
    }
    if not incognito:
        admission["revision"] = revision
    admission.update(admission_changes)
    return OpenAIChatRequest(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        session_id=sid,
        incognito=incognito,
        ui_persistence=admission,
    )


async def _ui_seed(sid):
    assert await session_service.upsert_session(
        sid, "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )


async def _ui_dispatch(request):
    ctx = PipelineContext(
        session_id=request.session_id, identity=UI_OWNER, incognito=request.incognito
    )
    ctx["resumable_stream"] = True
    ctx["ui_requester"] = UI_OWNER
    ctx["ui_authorized"] = True
    return await dispatcher_mod.dispatch_request(
        "test-model", request, UI_OWNER, "ui-request", 0.0, ctx
    )


async def _response_events(response):
    if not hasattr(response, "body_iterator"):
        return []
    chunks = [chunk async for chunk in response.body_iterator]
    return [
        json.loads(chunk.removeprefix("data: ")) for chunk in chunks if chunk.startswith("data: {")
    ]


@pytest.mark.asyncio
async def test_ui_real_producer_saves_fixed_node_before_typed_terminal(
    sandbox_db, fresh_buffer, monkeypatch
):
    await _ui_seed("ui")
    request = await _ui_request("ui")
    _patch_generator(monkeypatch, _fake_gen)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)
    response = await _ui_dispatch(request)
    events = await _response_events(response)
    terminal = next(
        (event for event in events if event.get("type") == "touka_stream_terminal"), None
    )
    assert terminal is not None, "producer lacks typed terminal"
    # 剪裁身分由家對話歸屬決定；UI 請求者（人類）不傳進去
    trim.assert_called_once_with("ui")
    assert events[0]["type"] == "touka_stream_started"
    assert (
        terminal["generation"] == "completed"
        and terminal["persistence"]["representation"] == "text"
    )
    saved = await session_service.read_persistence_snapshot("ui", UI_OWNER)
    assert saved.revision == terminal["persistence"]["revision"]
    assert saved.session["messages"]["messages"]["assistant"]["content"] == "Hello world"
    assert saved.session["messages"]["messages"]["assistant"]["parentId"] == "user"
    assert saved.session["messages"]["currentId"] == "assistant"
    assert saved.session["messages"]["messages"]["user"]["childrenIds"] == ["assistant"]
    assert saved.session["owner_user_id"] == UI_OWNER.agent_id


@pytest.mark.parametrize("fault", ["false", "raise", "generation_create", "generation_iter"])
@pytest.mark.asyncio
async def test_ui_failure_never_reports_saved_or_trims_partial(
    sandbox_db, fresh_buffer, monkeypatch, fault
):
    await _ui_seed("ui")
    request = await _ui_request("ui")
    before = await session_service.read_persistence_snapshot("ui", UI_OWNER)
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", trim)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    if fault in {"false", "raise"}:
        replacement = (
            AsyncMock(return_value=False)
            if fault == "false"
            else AsyncMock(side_effect=OSError("synthetic-secret-store"))
        )
        monkeypatch.setattr(session_service, "conditional_save_session", replacement)
        _patch_generator(monkeypatch, _fake_gen)
    elif fault == "generation_create":
        monkeypatch.setattr(
            dispatcher_mod,
            "get_stream_generator",
            AsyncMock(side_effect=OSError("synthetic-secret-generator")),
        )
    else:

        async def fails_after_text():
            yield _sse("partial")
            raise OSError("synthetic-secret-generator")

        _patch_generator(monkeypatch, fails_after_text)
    events = await _response_events(await _ui_dispatch(request))
    terminal = next(
        (event for event in events if event.get("type") == "touka_stream_terminal"), None
    )
    assert terminal is not None
    assert terminal["generation"] == ("error" if fault.startswith("generation") else "completed")
    assert terminal["persistence"]["status"] == "unknown"
    assert "synthetic-secret" not in json.dumps(events)
    assert (
        await session_service.read_persistence_snapshot("ui", UI_OWNER)
    ).revision == before.revision
    trim.assert_not_awaited()


@pytest.mark.parametrize("fault", ["stale", "wrong_parent", "existing_assistant", "missing"])
@pytest.mark.asyncio
async def test_ui_admission_rejects_stale_or_wrong_base_before_model(
    sandbox_db, fresh_buffer, monkeypatch, fault
):
    if fault != "missing":
        tree = copy.deepcopy(UI_TREE)
        if fault == "existing_assistant":
            tree["messages"]["assistant"] = {
                "id": "assistant",
                "role": "assistant",
                "content": "existing",
            }
        assert await session_service.upsert_session(
            "ui", "UI", tree, owner_user_id=UI_OWNER.agent_id
        )
    request = await _ui_request(
        "ui", **({"user_parent_id": "wrong"} if fault == "wrong_parent" else {})
    )
    if fault == "stale":
        assert await session_service.upsert_session(
            "ui", "newer", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
        )
    calls = Mock()

    async def generate():
        calls()
        yield _sse("must-not-run")

    _patch_generator(monkeypatch, generate)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    response = await _ui_dispatch(request)
    await _response_events(response)
    calls.assert_not_called()
    assert response.status_code == 409 and fresh_buffer.get_state("ui") is None


@pytest.mark.parametrize("incognito", [False, True])
@pytest.mark.asyncio
async def test_ui_empty_generation_has_explicit_skip_not_saved(
    sandbox_db, fresh_buffer, monkeypatch, incognito
):
    if not incognito:
        await _ui_seed("ui")
    request = await _ui_request("ui", incognito=incognito)

    async def tools_only():
        yield 'data: {"tool_result":{"error":"recoverable tool failure"}}\n\n'
        yield "data: [DONE]\n\n"

    _patch_generator(monkeypatch, tools_only)
    monkeypatch.setattr(dispatcher_mod, "record_working_brain", Mock())
    events = await _response_events(await _ui_dispatch(request))
    terminal = next(
        (event for event in events if event.get("type") == "touka_stream_terminal"), None
    )
    assert terminal is not None and terminal["generation"] == "completed"
    assert terminal["persistence"] == {
        "status": "skipped",
        "reason": "incognito" if incognito else "no_text",
    }
    if incognito:
        assert await session_service.get_session_by_id("ui") is None


@pytest.fixture
async def sandbox_db(tmp_path):
    """隔離的 user_sessions 測試 DB（沿用既有 get_db_path patch 範式）。"""
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        yield db_path


class _FakeRequest:
    stream = True


def _sse(text: str) -> str:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"


async def _fake_gen():
    yield _sse("Hello ")
    yield _sse("world")
    yield "data: [DONE]\n\n"


@pytest.fixture
def fresh_buffer(monkeypatch) -> StreamBufferService:
    """以全新緩衝單例隔離測試（不污染模組級單例）。"""
    svc = StreamBufferService()
    monkeypatch.setattr(dispatcher_mod, "stream_buffer_service", svc)
    return svc


def _patch_generator(monkeypatch, gen_factory) -> None:
    async def fake_get_stream_generator(*_a, **_k):
        return gen_factory()

    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", fake_get_stream_generator)


async def _seed_user_turn(session_id: str, identity: AgentIdentity) -> None:
    tree = TreeMessageManager.create_empty_tree()
    tree = TreeMessageManager.append_node(tree, "user", "hi", name="User")
    await session_service.upsert_session(session_id, "測試對話", tree, identity=identity)


async def _wait_status(svc: StreamBufferService, sid: str, status: str) -> dict:
    for _ in range(200):
        st = svc.get_state(sid)
        if st and st["status"] == status:
            return st
        await asyncio.sleep(0.02)
    pytest.fail(f"stream {sid} never reached {status}: {svc.get_state(sid)}")


@pytest.mark.asyncio
async def test_resumable_path_buffers_and_persists_server_side(
    sandbox_db, fresh_buffer, monkeypatch
):
    """旗標開啟：producer 入緩衝；即使回應從未被消費，生成仍完成並持久化。"""
    sid = "sess-resume-1"
    identity = AgentIdentity(agent_id="DebugAgent")
    await _seed_user_turn(sid, identity)
    _patch_generator(monkeypatch, _fake_gen)

    ctx = PipelineContext(session_id=sid, identity=identity)
    ctx["resumable_stream"] = True

    resp = await dispatcher_mod.dispatch_request(
        "test-model", _FakeRequest(), identity, "req-1", 0.0, ctx
    )

    # 模擬 client 立即斷線：完全不消費 resp —— producer 必須自行跑完
    st = await _wait_status(fresh_buffer, sid, "done")
    assert st["last_seq"] == 2  # 三個 chunk（含 [DONE]）

    # 伺服器端持久化：assistant 節點接在 user 之後，內容為累積文字
    session = await session_service.get_session_by_id(sid)
    linear = TreeMessageManager.get_linear_path(session["messages"])
    assert linear[-1]["role"] == "assistant"
    assert linear[-1]["content"] == "Hello world"

    # 回應本身是緩衝訂閱者：消費可得完整重播
    chunks = [c async for c in resp.body_iterator]
    assert [c for c in chunks if c.startswith("data: ")] == [
        _sse("Hello "),
        _sse("world"),
        "data: [DONE]\n\n",
    ]


@pytest.mark.asyncio
async def test_subscriber_disconnect_does_not_stop_producer(sandbox_db, fresh_buffer, monkeypatch):
    """訂閱者消費到一半被取消（斷線）→ producer 續跑至完成並持久化。"""
    sid = "sess-resume-2"
    identity = AgentIdentity(agent_id="DebugAgent")
    await _seed_user_turn(sid, identity)

    async def slow_gen():
        yield _sse("a")
        await asyncio.sleep(0.1)
        yield _sse("b")
        yield "data: [DONE]\n\n"

    _patch_generator(monkeypatch, slow_gen)

    ctx = PipelineContext(session_id=sid, identity=identity)
    ctx["resumable_stream"] = True
    resp = await dispatcher_mod.dispatch_request(
        "test-model", _FakeRequest(), identity, "req-2", 0.0, ctx
    )

    async def consume_one():
        async for _c in resp.body_iterator:
            break  # 收到第一個 chunk 即斷線

    reader = asyncio.create_task(consume_one())
    await asyncio.wait_for(reader, timeout=2)
    reader.cancel()

    st = await _wait_status(fresh_buffer, sid, "done")
    assert st["last_seq"] == 2

    session = await session_service.get_session_by_id(sid)
    linear = TreeMessageManager.get_linear_path(session["messages"])
    assert linear[-1]["content"] == "ab"


@pytest.mark.asyncio
async def test_without_flag_legacy_passthrough_no_buffer_no_persist(
    sandbox_db, fresh_buffer, monkeypatch
):
    """無旗標（heartbeat／外部呼叫）：原路徑直通，零緩衝、零伺服器端寫入。"""
    sid = "sess-legacy-1"
    identity = AgentIdentity(agent_id="DebugAgent")
    await _seed_user_turn(sid, identity)
    before = await session_service.get_session_by_id(sid)
    before_len = len(TreeMessageManager.get_linear_path(before["messages"]))

    _patch_generator(monkeypatch, _fake_gen)
    ctx = PipelineContext(session_id=sid, identity=identity)  # 不設旗標

    resp = await dispatcher_mod.dispatch_request(
        "test-model", _FakeRequest(), identity, "req-3", 0.0, ctx
    )
    chunks = [c async for c in resp.body_iterator]

    assert len([c for c in chunks if c.startswith("data: ")]) == 3
    assert fresh_buffer.get_state(sid) is None, "無旗標不得建立緩衝"

    after = await session_service.get_session_by_id(sid)
    after_len = len(TreeMessageManager.get_linear_path(after["messages"]))
    assert after_len == before_len, "無旗標不得伺服器端持久化（防雙寫）"


@pytest.mark.asyncio
async def test_resumable_without_identity_skips_persistence(sandbox_db, fresh_buffer, monkeypatch):
    """旗標開啟但身分缺失（理論邊界）：緩衝照常、跳過持久化不崩潰。"""
    sid = "sess-resume-3"
    await _seed_user_turn(sid, AgentIdentity(agent_id="DebugAgent"))
    before = await session_service.get_session_by_id(sid)
    before_len = len(TreeMessageManager.get_linear_path(before["messages"]))

    _patch_generator(monkeypatch, _fake_gen)
    ctx = PipelineContext(session_id=sid, identity=None)
    ctx["resumable_stream"] = True

    await dispatcher_mod.dispatch_request("test-model", _FakeRequest(), None, "req-4", 0.0, ctx)
    st = await _wait_status(fresh_buffer, sid, "done")
    assert st["last_seq"] == 2

    after = await session_service.get_session_by_id(sid)
    assert len(TreeMessageManager.get_linear_path(after["messages"])) == before_len
