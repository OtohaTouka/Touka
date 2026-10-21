"""UI admission, autonomous tools and trimming use the execution soul."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.core.exceptions import ToukaException
from src.models.context import PipelineContext
from src.models.sliding_window import SlidingWindowConfig
from src.services import dispatcher
from src.services import sliding_window_service as sliding
from src.services.stream_buffer_service import StreamBufferService
from src.tools.builtin import system
from src.utils import async_db

from tests.support.session_scope import HUMAN, TREE, A, B, C, attempt, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]


@pytest.fixture
def ui_runtime(monkeypatch):
    buffer = StreamBufferService()
    monkeypatch.setattr(dispatcher, "stream_buffer_service", buffer)
    monkeypatch.setattr(dispatcher, "record_working_brain", Mock())
    monkeypatch.setattr(dispatcher, "_maybe_trim_after_chat", AsyncMock())

    async def body():
        yield 'data: {"choices":[{"delta":{"content":"reply B"}}]}\n\n'

    model = AsyncMock(side_effect=lambda *_a, **_k: body())
    monkeypatch.setattr(dispatcher, "get_stream_generator", model)
    return SimpleNamespace(buffer=buffer, model=model)


async def ui_dispatch(caller=HUMAN, execution=B, target=B, revision=None):
    snapshot = await async_db.session_service.read_persistence_snapshot("chat", caller)
    request = OpenAIChatRequest(
        model=target.agent_id,
        messages=[{"role": "user", "content": "continue"}],
        stream=True,
        session_id="chat",
        ui_persistence={
            "mode": "persistent",
            "revision": revision or snapshot.revision,
            "user_parent_id": "u",
            "assistant_node_id": "new-assistant",
        },
    )
    context = PipelineContext(session_id="chat", identity=execution, agent_id=target.agent_id)
    context.update({"ui_requester": caller, "ui_authorized": True, "is_toukasoul": True})
    result = await attempt(
        dispatcher.dispatch_request(
            target.agent_id, request, execution, "scope-request", 0.0, context
        )
    )
    events = []
    if hasattr(result, "body_iterator"):
        async for chunk in result.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            if chunk.startswith("data: {"):
                events.append(json.loads(chunk.removeprefix("data: ")))
    return result, events


async def seed_user_tip(identity=A):
    tree = copy.deepcopy(TREE)
    tree["currentId"] = "u"
    await seed(identity=identity, tree=tree)


async def test_human_ui_dispatch_admits_switch_and_clears_provenance(ui_runtime):
    await seed_user_tip()
    result, events = await ui_dispatch()
    assert result.status_code == 200
    row = await raw_row()
    assert row["agent_id"] == B.agent_id, "dispatcher did not bind the admitted execution soul"
    assert row.get("session_scope_version") == 0
    assert row["owner_user_id"] == HUMAN.agent_id
    tree = json.loads(row["messages"])
    assert tree["messages"]["a"]["content"] == "answer A"
    assert tree["messages"]["new-assistant"]["content"] == "reply B"
    terminal = next(item for item in events if item.get("type") == "touka_stream_terminal")
    assert terminal["persistence"]["status"] == "saved"


@pytest.mark.parametrize(
    "caller,execution,target",
    [
        (B, B, B),
        (HUMAN, A, B),
        (HUMAN, C, C),
    ],
    ids=["nonhuman", "execution-mismatch", "unowned-soul"],
)
async def test_ui_admission_refuses_nonhuman_or_mismatched_soul(
    ui_runtime, caller, execution, target
):
    await seed_user_tip()
    before = await raw_row()
    result, _ = await ui_dispatch(caller, execution, target)
    ui_runtime.model.assert_not_awaited()
    assert ui_runtime.buffer.get_state("chat") is None
    assert await raw_row() == before
    assert isinstance(result, ToukaException) or result.status_code in {403, 409}


async def test_ui_switch_cannot_replay_revision(ui_runtime):
    await seed_user_tip()
    stale = await async_db.session_service.read_persistence_snapshot("chat", HUMAN)
    assert await async_db.session_service.append_message_to_session(
        "chat", "user", "newer", identity=A
    )
    before = await raw_row()
    result, _ = await ui_dispatch(revision=stale.revision)
    assert result.status_code == 409
    ui_runtime.model.assert_not_awaited()
    assert await raw_row() == before


async def test_same_soul_human_dispatch_persists_without_rebinding(ui_runtime):
    await seed_user_tip(B)
    result, events = await ui_dispatch()
    assert result.status_code == 200
    assert (await raw_row())["agent_id"] == B.agent_id
    terminal = next(item for item in events if item.get("type") == "touka_stream_terminal")
    assert terminal["persistence"]["status"] == "saved"


async def test_legacy_dispatcher_cannot_report_success_on_foreign_write():
    await seed()
    before = await raw_row()
    result = await attempt(
        dispatcher._persist_assistant_turn("chat", "foreign", B.agent_id, B, check_access=False)
    )
    assert await raw_row() == before
    assert result is not True


@pytest.mark.parametrize("tool", ["skill", "mcp"])
async def test_tool_write_cannot_use_anonymous_storage_to_cross_scope(monkeypatch, tool):
    await seed()
    monkeypatch.setattr(system.skill_service, "get_skill", lambda _: {})
    monkeypatch.setattr(system.permission_service, "can_use_skill", lambda *_: True)
    monkeypatch.setattr(system.config_utils, "config_exists", lambda _: True)
    monkeypatch.setattr(
        system.config_utils, "read_config_json", lambda _: {"mcpServers": {"x": {}}}
    )
    original = async_db.session_service.read_persistence_snapshot
    replaced = {}

    async def change_after_read(sid, identity):
        snapshot = await original(sid, identity)
        async with async_db.get_async_db() as db:
            await db.execute(
                "UPDATE user_sessions SET agent_id = ? WHERE id = ?", (B.agent_id, sid)
            )
            await db.commit()
        replaced.update(await raw_row(sid))
        return snapshot

    monkeypatch.setattr(async_db.session_service, "read_persistence_snapshot", change_after_read)
    function = system.activate_skill if tool == "skill" else system.connect_mcp_server
    result = await function("x", session_id="chat", identity=A, agent_id=A.agent_id)
    assert await raw_row() == replaced, "tool's anonymous upsert crossed the new binding"
    assert json.loads(result)["error_class"] == "permission_denied"


async def test_trim_checks_scope_before_archive_and_summary(monkeypatch):
    await seed(identity=A, is_home=True, home_for_agent=A.agent_id)
    before = await raw_row()
    service = sliding.SlidingWindowService()
    config = SlidingWindowConfig(enabled=True, summary_budget_pct=5)
    monkeypatch.setattr(service, "load_config", lambda: config)
    monkeypatch.setattr(service, "_resolve_model", lambda _: "synthetic")
    monkeypatch.setattr(sliding, "get_context_window", lambda _: 100)
    monkeypatch.setattr(service, "_check_model_cooldown", lambda *_: False)
    monkeypatch.setattr(service, "_current_total_tokens", lambda *_: 100)
    monkeypatch.setattr(
        service,
        "compute_split_by_tokens",
        lambda *_: (
            list(TREE["messages"].values()),
            [],
        ),
    )
    archive = Mock(return_value=2)
    manager = SimpleNamespace(
        get_legacy_sync_service=lambda: SimpleNamespace(archive_payload=archive)
    )
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    summary = AsyncMock(return_value="foreign summary")
    monkeypatch.setattr(sliding, "summarize_dialogue", summary)
    await attempt(service.check_and_trim(B, "chat"))
    archive.assert_not_called()
    summary.assert_not_awaited()
    assert await raw_row() == before


@pytest.mark.parametrize("tool", ["skill", "mcp"])
async def test_tool_does_not_recreate_deleted_session(monkeypatch, tool):
    await seed()
    monkeypatch.setattr(system.skill_service, "get_skill", lambda _: {})
    monkeypatch.setattr(system.permission_service, "can_use_skill", lambda *_: True)
    monkeypatch.setattr(system.config_utils, "config_exists", lambda _: True)
    monkeypatch.setattr(
        system.config_utils, "read_config_json", lambda _: {"mcpServers": {"x": {}}}
    )
    original = async_db.session_service.read_persistence_snapshot

    async def delete_after_read(sid, identity):
        snapshot = await original(sid, identity)
        async with async_db.get_async_db() as db:
            await db.execute("DELETE FROM user_sessions WHERE id = ?", (sid,))
            await db.commit()
        return snapshot

    monkeypatch.setattr(async_db.session_service, "read_persistence_snapshot", delete_after_read)
    function = system.activate_skill if tool == "skill" else system.connect_mcp_server
    result = await function("x", session_id="chat", identity=A, agent_id=A.agent_id)
    assert await raw_row() is None
    assert json.loads(result)["error_class"] == "resource_not_found"


@pytest.mark.parametrize("outcome", ["no_text", "error", "switch-again"])
async def test_admitted_switch_keeps_marker_revoked_after_generation(ui_runtime, outcome):
    await seed_user_tip()
    replacement = {}

    async def body():
        if outcome == "error":
            raise ConnectionError
        if outcome == "switch-again":
            snapshot = await async_db.session_service.read_persistence_snapshot("chat", HUMAN)
            result = await async_db.session_service.save_ui_session(
                "chat", snapshot.revision, async_db.SessionChanges(), HUMAN, A
            )
            assert result.status == "saved"
            replacement.update(await raw_row())
            yield 'data: {"choices":[{"delta":{"content":"late B"}}]}\n\n'

    ui_runtime.model.side_effect = lambda *_a, **_k: body()
    _, events = await ui_dispatch()
    row = await raw_row()
    assert row["session_scope_version"] == 0
    assert "new-assistant" not in json.loads(row["messages"])["messages"]
    terminal = next(item for item in events if item.get("type") == "touka_stream_terminal")
    if outcome == "switch-again":
        assert row == replacement
        assert terminal["persistence"]["status"] == "failed"
    else:
        assert row["agent_id"] == B.agent_id
