"""Heartbeat admission, scheduled execution and pulse persistence boundaries."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, FastAPI
from httpx import ASGITransport, AsyncClient
from src.core.exceptions import ToukaException
from src.models.context import PipelineContext
from src.routers import heartbeat as hb
from src.routers.auth import get_auth_service
from src.utils import async_db
from starlette.requests import Request
from starlette.responses import StreamingResponse

from tests.support.session_scope import HUMAN, A, B, assert_scope_denied, attempt, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db", "heartbeat_gates")]


@pytest.fixture
def heartbeat_gates(monkeypatch, scope_db):
    monkeypatch.setattr(
        hb, "get_auth_service", lambda: SimpleNamespace(get_all_users=lambda: scope_db.users)
    )
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _: True)
    monkeypatch.setattr(hb, "get_active_brain", lambda _: "synthetic-model")
    monkeypatch.setattr(hb, "_pulse_model_name", lambda *_: "synthetic-model")
    monkeypatch.setattr(hb.autonomy_service, "clear_interrupted_run", Mock())


def request():
    return Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 1234),
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(context={})),
        }
    )


async def awaken(identity, **fields):
    tasks = BackgroundTasks()
    payload = hb.HeartbeatAwakenRequest(
        token="synthetic-service-token",
        agent_id=identity,
        session_mode="independent",
        init_prompt="wake",
        **fields,
    )
    result = await attempt(
        hb.awaken_agent(
            payload,
            request(),
            tasks,
            auth_service=hb.get_auth_service(),
            _svc_token="synthetic-service-token",
        )
    )
    return result, tasks


@pytest.mark.parametrize("kind", ["chat", "home", "contradictory-home"])
async def test_awaken_rejects_foreign_session_before_append_or_background(kind):
    await seed(is_home=kind != "chat", home_for_agent=A.agent_id if kind != "chat" else None)
    if kind == "contradictory-home":
        async with async_db.get_async_db() as db:
            await db.execute("UPDATE user_sessions SET agent_id = ?", (B.agent_id,))
            await db.commit()
    before = await raw_row()
    result, tasks = await awaken(B, session_id="chat")
    assert tasks.tasks == [], "foreign conversation was admitted to background execution"
    assert await raw_row() == before
    assert_scope_denied(result)


@pytest.mark.parametrize(
    "fields", [{}, {"session_id": ""}, {"session_id": "pulse_channel_default"}]
)
async def test_default_heartbeat_channel_is_stable_and_per_soul(fields):
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET id = ?", ("pulse_channel_default",))
        await db.commit()
    old = await raw_row("pulse_channel_default")
    first, _ = await awaken(A, **fields)
    second, _ = await awaken(B, **fields)
    again, _ = await awaken(A, **fields)
    assert first["session_id"] != second["session_id"], "two souls share the default channel"
    assert first["session_id"] == again["session_id"]
    assert first["session_id"].startswith("touka.hb.v1.")
    assert (await raw_row(first["session_id"]))["agent_id"] == A.agent_id
    assert (await raw_row(second["session_id"]))["agent_id"] == B.agent_id
    assert await raw_row("pulse_channel_default") == old


async def test_schedule_foreign_session_is_rejected_when_it_executes():
    await seed()
    schedule = hb.HeartbeatSchedule.model_validate(
        {
            "id": "scheduled",
            "agent_id": B.agent_id,
            "mode": "interval",
            "interval_minutes": 30,
            "session_mode": "independent",
            "session_id": "chat",
        }
    )
    before = await raw_row()
    result, tasks = await awaken(
        B, session_id=schedule.session_id, model_override=schedule.model_override
    )
    assert tasks.tasks == []
    assert await raw_row() == before
    assert_scope_denied(result)


async def test_awaken_cannot_reserve_arbitrary_internal_channel():
    sid = "touka.hb.v1.not-the-computed-channel"
    result, tasks = await awaken(B, session_id=sid)
    assert tasks.tasks == []
    assert await raw_row(sid) is None
    assert_scope_denied(result)


async def test_awaken_rejects_foreign_row_at_computed_default_channel():
    digest = hashlib.sha256(
        json.dumps(["heartbeat", A.agent_id], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    sid = f"touka.hb.v1.{digest}"
    await seed(identity=B)
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET id = ?", (sid,))
        await db.commit()
    before = await raw_row(sid)
    result, tasks = await awaken(A, session_id="")
    assert tasks.tasks == []
    assert await raw_row(sid) == before
    assert_scope_denied(result)


@pytest.mark.parametrize("kind", ["foreign", "retired", "contradictory-home"])
async def test_retrigger_does_not_admit_unsafe_session_or_clear_interrupt(kind):
    await seed(identity=B if kind == "foreign" else A)
    sid = "sns_test_channel" if kind == "retired" else "chat"
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET id = ?", (sid,))
        if kind == "contradictory-home":
            await db.execute(
                "UPDATE user_sessions SET is_home = 1, home_for_agent = ?", (B.agent_id,)
            )
        await db.commit()
    before = await raw_row(sid)
    tasks = BackgroundTasks()
    result = await attempt(
        hb.retrigger_heartbeat(
            hb.HeartbeatRetriggerRequest(agent_id=A, session_id=sid), request(), tasks, HUMAN
        )
    )
    assert tasks.tasks == []
    hb.autonomy_service.clear_interrupted_run.assert_not_called()
    assert await raw_row(sid) == before
    if kind == "foreign":
        assert isinstance(result, ToukaException)
    else:
        assert_scope_denied(result)


def pulse_dependencies(monkeypatch, body):
    monkeypatch.setattr(hb.soul_service, "apply_soul_to_request", Mock())
    monkeypatch.setattr(hb.soul_service, "get_soul_data", lambda _: ("", {}))
    monkeypatch.setattr(hb.soul_service, "is_toukasoul", lambda _: True)
    monkeypatch.setattr(hb.rhythm_service, "get_effective_rounds", lambda *_a, **_k: 1)
    monkeypatch.setattr(hb.autonomy_service, "consume_quota", Mock())
    monkeypatch.setattr(hb._HEARTBEAT_TOOL_LAYER, "process", AsyncMock())
    monkeypatch.setattr(hb, "inject_continuity", AsyncMock())
    dispatch = AsyncMock(side_effect=lambda **_: StreamingResponse(body()))
    monkeypatch.setattr(hb, "dispatch_request", dispatch)
    return dispatch


async def text_body():
    yield 'data: {"choices":[{"delta":{"content":"synthetic response"}}]}\n\n'


async def test_pulse_checks_scope_before_history_reaches_model(monkeypatch):
    await seed()
    before = await raw_row()
    dispatch = pulse_dependencies(monkeypatch, text_body)
    await attempt(hb._execute_single_pulse("chat", B, None, PipelineContext(), 1, 0.0, ""))
    dispatch.assert_not_awaited()
    hb._HEARTBEAT_TOOL_LAYER.process.assert_not_awaited()
    assert await raw_row() == before


@pytest.mark.parametrize("split", [False, True], ids=["final", "intermediate"])
async def test_pulse_revalidates_at_each_stream_write(monkeypatch, split):
    await seed()
    replaced = {}

    async def body():
        async with async_db.get_async_db() as db:
            await db.execute("UPDATE user_sessions SET agent_id = ?", (B.agent_id,))
            await db.commit()
        replaced.update(await raw_row())
        text = "foreign output --- stage 自主行動" if split else "foreign output"
        yield f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"

    pulse_dependencies(monkeypatch, body)
    result = await attempt(hb._execute_single_pulse("chat", A, None, PipelineContext(), 1, 0.0, ""))
    assert await raw_row() == replaced, "pulse wrote after the binding changed"
    assert_scope_denied(result)


async def test_background_scope_failure_does_not_append_error_or_trim(monkeypatch):
    await seed(identity=B)
    before = await raw_row()
    pulse_dependencies(monkeypatch, text_body)
    for name in ("mark_heartbeat_in_flight", "clear_heartbeat_in_flight"):
        monkeypatch.setattr(hb.autonomy_service, name, Mock())
    monkeypatch.setattr(hb.autonomy_service, "check_quota", lambda _: True)
    monkeypatch.setattr(hb.autonomy_service, "is_over_budget", lambda _: False)
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    monkeypatch.setattr(hb.rhythm_service, "clear_interrupt", Mock())
    monkeypatch.setattr(hb.rhythm_service, "is_interrupted", lambda _: False)
    monkeypatch.setattr(hb, "record_heartbeat_started", Mock())
    monkeypatch.setattr(hb, "record_heartbeat_finished", Mock())
    trim = AsyncMock()
    monkeypatch.setattr(hb.sliding_window_service, "check_and_trim", trim)
    await hb._process_heartbeat_loop("chat", A, None, PipelineContext(), 1, 1, 0.0, "", True)
    assert await raw_row() == before
    trim.assert_not_awaited()


async def test_new_home_id_collision_never_overwrites_existing_content(monkeypatch):
    monkeypatch.setattr(hb.uuid, "uuid4", lambda: "fixed-id")
    sid = f"home_{A.agent_id}_fixed-id"
    await seed(sid=sid)
    before = await raw_row(sid)
    tasks = BackgroundTasks()
    result = await attempt(hb.awaken_agent(
        hb.HeartbeatAwakenRequest(token="synthetic", agent_id=A, session_mode="home"),
        request(), tasks, auth_service=hb.get_auth_service(), _svc_token="synthetic",
    ))
    assert await raw_row(sid) == before
    assert tasks.tasks == []
    assert isinstance(result, ToukaException)


async def test_awaken_http_scope_failure_is_fixed_403():
    await seed()
    before = await raw_row()
    app = FastAPI()
    app.include_router(hb.router, prefix="/v1")
    app.state.context = {}
    app.dependency_overrides[get_auth_service] = hb.get_auth_service
    app.dependency_overrides[hb.verify_heartbeat_token] = lambda: "synthetic"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/heartbeat/awaken", json={
            "token": "synthetic", "agent_id": B.agent_id,
            "session_id": "chat", "session_mode": "independent",
        })
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SESSION_SCOPE_MISMATCH"
    assert "private" not in response.text and A.agent_id not in response.text
    assert await raw_row() == before
