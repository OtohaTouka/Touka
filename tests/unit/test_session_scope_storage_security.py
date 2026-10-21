"""Storage transaction scope, home lookup and human binding security."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager

import pytest
from src.models.identity import AgentIdentity
from src.utils import async_db

from tests.support.session_scope import (
    ADMIN,
    DEBUG,
    EMPTY,
    HUMAN,
    TREE,
    A,
    B,
    C,
    assert_scope_denied,
    attempt,
    cas,
    raw_row,
    seed,
    ui_save,
)
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]


@pytest.mark.parametrize(
    "writer,actor",
    [
        pytest.param(writer, actor, id=f"{name}-{writer}")
        for writer in ("append", "upsert", "conditional")
        for name, actor in (("foreign", B), ("debugger", DEBUG), ("anonymous", None))
        if writer != "conditional" or actor is not None
    ],
)
async def test_nonhuman_writer_cannot_change_foreign_conversation(writer, actor):
    await seed()
    before = await raw_row()
    if writer == "append":
        operation = async_db.session_service.append_message_to_session(
            "chat", "assistant", "foreign output", identity=actor
        )
    elif writer == "upsert":
        operation = async_db.session_service.upsert_session(
            "chat", "changed", copy.deepcopy(EMPTY), identity=actor
        )
    else:
        operation = cas(identity=actor, messages=copy.deepcopy(EMPTY))
    result = await attempt(operation)
    assert await raw_row() == before, "foreign writer changed the persisted row"
    assert_scope_denied(result)


@pytest.mark.parametrize("target", [B, None], ids=["other", "null"])
async def test_agent_cannot_clear_or_replace_binding_through_conditional_save(target):
    await seed()
    before = await raw_row()
    result = await cas(agent_id=target)
    assert await raw_row() == before
    assert_scope_denied(result)


async def test_set_home_rejects_foreign_target_before_clearing_valid_home():
    await seed("valid", B, is_home=True, home_for_agent=B.agent_id)
    await seed("foreign", A)
    before = [await raw_row("valid"), await raw_row("foreign")]
    result = await attempt(async_db.session_service.set_home("foreign", B, True))
    assert [await raw_row("valid"), await raw_row("foreign")] == before
    assert_scope_denied(result)


@pytest.mark.parametrize("reader", ["lookup", "injection"])
@pytest.mark.parametrize("kind", ["foreign", "ambiguous", "valid-plus-foreign", "retired"])
async def test_home_read_never_selects_inconsistent_or_ambiguous_rows(reader, kind, caplog):
    await seed("bad-home", A)
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = ?",
            (B.agent_id, "bad-home"),
        )
        if kind == "ambiguous":
            await db.execute("UPDATE user_sessions SET agent_id = ?", (B.agent_id,))
        if kind == "retired":
            await db.execute(
                "UPDATE user_sessions SET id = ?, agent_id = ? WHERE id = ?",
                ("sns_platform_channel", B.agent_id, "bad-home"),
            )
        await db.commit()
    if kind in {"ambiguous", "valid-plus-foreign"}:
        await seed("other-home", B, is_home=True, home_for_agent=B.agent_id)
    if reader == "lookup":
        result = await async_db.session_service.get_home_session_id(B)
        assert result is None, f"unsafe home was selected: {result}"
    else:
        result = await async_db.get_home_messages(B)
        assert result == [], "unsafe home reached prompt injection"
    assert "SESSION_SCOPE_MISMATCH" in caplog.text
    assert "private A" not in caplog.text


async def test_single_consistent_home_lookup_and_injection_agree():
    await seed(is_home=True, home_for_agent=A.agent_id)
    assert await async_db.session_service.get_home_session_id(A) == "chat"
    assert [m["content"] for m in await async_db.get_home_messages(A)] == ["private A", "answer A"]
    assert await async_db.get_home_messages(A, current_session_id="chat") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"is_home": True, "home_for_agent": B},
        {"home_for_agent": B},
        {"is_home": False, "home_for_agent": None},
    ],
)
async def test_ui_save_ignores_home_fields_outside_set_home(fields):
    await seed(is_home=True, home_for_agent=A.agent_id)
    result = await ui_save(**fields)
    row = await raw_row()
    assert result.persistence.status == "saved"
    assert (row["is_home"], row["home_for_agent"]) == (1, A.agent_id)


@pytest.mark.parametrize("caller", [HUMAN, ADMIN], ids=["owner", "admin"])
async def test_human_can_rebind_strictly_empty_draft(caller):
    await seed(tree=EMPTY)
    result = await ui_save(caller=caller, agent_id=B)
    row = await raw_row()
    assert result.persistence.status == "saved"
    assert row["agent_id"] == B.agent_id and row["owner_user_id"] == HUMAN.agent_id


async def test_human_can_switch_nonempty_conversation_without_losing_history():
    await seed()
    before = await raw_row()
    result = await ui_save(agent_id=B)
    row = await raw_row()
    assert result.persistence.status == "saved"
    assert row["agent_id"] == B.agent_id
    assert row["messages"] == before["messages"]
    assert row["owner_user_id"] == before["owner_user_id"]


async def test_home_must_be_unset_before_human_can_switch_souls():
    await seed(is_home=True, home_for_agent=A.agent_id)
    before = await raw_row()
    result = await attempt(ui_save(agent_id=B, is_home=False, home_for_agent=None))
    assert await raw_row() == before
    assert_scope_denied(getattr(result, "persistence", result))
    assert await async_db.session_service.set_home("chat", A, False)
    result = await ui_save(agent_id=B)
    row = await raw_row()
    assert result.persistence.status == "saved"
    assert (row["agent_id"], row["is_home"], row["home_for_agent"]) == (B.agent_id, 0, None)
    assert row["messages"] == before["messages"]
    assert row["session_scope_version"] == 0


@pytest.mark.parametrize("sid", ["touka.hb.v1.unclaimed", "touka.sns.v1.unclaimed", "touka.any"])
async def test_ui_cannot_create_reserved_namespace(sid):
    result = await attempt(ui_save(sid, agent_id=A))
    assert await raw_row(sid) is None, "UI reserved an internal channel identifier"
    assert_scope_denied(getattr(result, "persistence", result))


@pytest.mark.parametrize("sid", ["pulse_channel_default", "sns_test_channel"])
async def test_retired_conversation_refuses_ui_save(sid):
    # Historical rows must be seeded without going through the new creation gate.
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET id = ? WHERE id = ?", (sid, "chat"))
        await db.commit()
    before = await raw_row(sid)
    result = await attempt(ui_save(sid, agent_id=B))
    assert await raw_row(sid) == before
    assert_scope_denied(getattr(result, "persistence", result))


async def test_human_needs_access_to_both_old_binding_and_new_soul():
    await seed(identity=C)
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET owner_user_id = ?", ("other-human",))
        await db.commit()
    before = await raw_row()
    result = await attempt(ui_save(agent_id=B))
    assert getattr(result, "code", None) is not None
    assert await raw_row() == before
    result = await attempt(ui_save(caller=ADMIN, agent_id=B))
    assert result.persistence.status == "saved"
    before = await raw_row()
    result = await attempt(ui_save(agent_id=C))
    assert getattr(result, "code", None) is not None
    assert await raw_row() == before


async def test_unbound_nonempty_conversation_cannot_be_claimed_by_agent():
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET agent_id = NULL WHERE id = ?", ("chat",))
        await db.commit()
    before = await raw_row()
    result = await attempt(
        async_db.session_service.append_message_to_session("chat", "assistant", "claim", identity=B)
    )
    assert await raw_row() == before
    assert_scope_denied(result)


async def test_racing_first_writers_cannot_mix_souls():
    start = asyncio.Event()

    async def write(identity):
        await start.wait()
        return await attempt(
            async_db.session_service.upsert_session(
                "race", identity.agent_id, copy.deepcopy(TREE), identity=identity
            )
        )

    tasks = [asyncio.create_task(write(identity)) for identity in (A, B)]
    start.set()
    results = await asyncio.gather(*tasks)
    assert sum(result is True for result in results) == 1, results
    assert (await raw_row("race"))["agent_id"] in {A.agent_id, B.agent_id}
    assert_scope_denied(next(result for result in results if result is not True))


async def test_late_append_cannot_write_after_delete_and_foreign_recreate():
    await seed()
    assert await async_db.session_service.delete_session("chat")
    await seed(identity=B)
    before = await raw_row()
    result = await attempt(
        async_db.session_service.append_message_to_session(
            "chat", "assistant", "late A", identity=A
        )
    )
    assert await raw_row() == before
    assert_scope_denied(result)


async def test_same_soul_append_and_legacy_alias_preserve_binding():
    alias = AgentIdentity.for_role("ToukaSoulScopeA", "Owner")
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET agent_id = ? WHERE id = ?", (alias.agent_id, "chat")
        )
        await db.commit()
    assert await async_db.session_service.append_message_to_session(
        "chat", "user", "same soul", identity=A
    )
    row = await raw_row()
    assert row["agent_id"] == alias.agent_id
    assert row["owner_user_id"] == HUMAN.agent_id


async def test_human_identity_alone_does_not_authorize_non_ui_rebind():
    await seed()
    before = await raw_row()
    result = await cas(identity=HUMAN, agent_id=B)
    assert await raw_row() == before
    assert_scope_denied(result)


async def test_debugger_ui_request_cannot_use_human_switch_exception():
    await seed()
    before = await raw_row()
    result = await attempt(ui_save(caller=DEBUG, agent_id=B))
    assert await raw_row() == before
    assert_scope_denied(getattr(result, "persistence", result))


@pytest.mark.parametrize("writer", ["append", "upsert", "set_home"])
async def test_scope_read_and_write_share_immediate_transaction(monkeypatch, writer):
    await seed()
    original = async_db.get_async_db
    traces = []

    @asynccontextmanager
    async def traced_connection():
        statements = []
        traces.append(statements)
        async with original() as db:
            await db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(async_db, "get_async_db", traced_connection)
    if writer == "append":
        result = await async_db.session_service.append_message_to_session(
            "chat", "user", "next", identity=A
        )
    elif writer == "upsert":
        result = await async_db.session_service.upsert_session(
            "chat", "title", copy.deepcopy(TREE), identity=A
        )
    else:
        result = await async_db.session_service.set_home("chat", A, True)
    assert result is True
    writes = [
        trace
        for trace in traces
        if any(
            "USER_SESSIONS" in sql.upper() and sql.lstrip().upper().startswith(("INSERT", "UPDATE"))
            for sql in trace
        )
    ]
    assert len(writes) == 1
    statements = [" ".join(sql.upper().split()) for sql in writes[0]]
    assert "BEGIN IMMEDIATE" in statements, statements
    begin = statements.index("BEGIN IMMEDIATE")
    read = next(
        (
            i
            for i, sql in enumerate(statements)
            if sql.startswith("SELECT") and "USER_SESSIONS" in sql
        ),
        -1,
    )
    write = next(i for i, sql in enumerate(statements) if sql.startswith(("INSERT", "UPDATE")))
    assert begin < read < write, statements


@pytest.mark.parametrize("writer", ["append", "upsert"])
async def test_corrupt_tree_is_not_treated_as_an_empty_draft(writer):
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET messages = ?", ("{broken",))
        await db.commit()
    before = await raw_row()
    if writer == "append":
        result = await attempt(
            async_db.session_service.append_message_to_session(
                "chat", "user", "overwrite", identity=A
            )
        )
    else:
        result = await attempt(
            async_db.session_service.upsert_session(
                "chat", "overwrite", copy.deepcopy(EMPTY), identity=A
            )
        )
    assert await raw_row() == before
    assert result is not True
