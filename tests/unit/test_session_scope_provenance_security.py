"""Creation marker migration, monotonicity and public projection."""

from __future__ import annotations

import copy

import pytest
from src.routers import ui_sessions
from src.utils import async_db

from tests.support.session_scope import EMPTY, HUMAN, TREE, A, B, cas, raw_row, seed, ui_save
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]
MARKER = "session_scope_version"


@pytest.mark.parametrize("writer", ["ui", "upsert"])
async def test_new_conversation_gets_creation_marker(writer):
    if writer == "ui":
        result = await ui_save(agent_id=A)
        assert result.persistence.status == "saved"
    else:
        await seed()
    assert (await raw_row()).get(MARKER) == 1, "new row lacks the creation marker"


async def test_old_schema_migration_is_idempotent_and_never_backfills_provenance():
    async with async_db.get_async_db() as db:
        await db.execute("DROP TABLE user_sessions")
        await db.execute(
            "CREATE TABLE user_sessions (id TEXT PRIMARY KEY, title TEXT, messages TEXT, "
            "updatedAt INTEGER, is_home INTEGER DEFAULT 0, home_for_agent TEXT, agent_id TEXT)"
        )
        await db.execute(
            "INSERT INTO user_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old", "old title", '{"messages":{},"currentId":null}', 1000, 0, None, A.agent_id),
        )
        await db.commit()
    await async_db.init_db_async()
    first = await raw_row("old")
    await async_db.init_db_async()
    assert await raw_row("old") == first
    assert first.get(MARKER) == 0, "old rows need an explicit untrusted default"
    assert first["updatedAt"] == 1000 and first["title"] == "old title"
    async with async_db.get_async_db() as db:
        columns = await (await db.execute("PRAGMA table_info(user_sessions)")).fetchall()
    column = next(row for row in columns if row["name"] == MARKER)
    assert column["notnull"] == 1 and column["dflt_value"] == "0"


@pytest.mark.parametrize(
    "tree,summary,expected",
    [
        (EMPTY, None, 1),
        (TREE, None, 0),
        (EMPTY, "prior context", 0),
    ],
)
async def test_human_switch_retains_marker_only_for_strictly_empty_draft(tree, summary, expected):
    await seed(tree=tree, context_summary=summary)
    result = await ui_save(agent_id=B)
    row = await raw_row()
    assert result.persistence.status == "saved" and row["agent_id"] == B.agent_id
    assert row.get(MARKER) == expected
    if expected == 0:
        result = await ui_save(agent_id=A, messages=copy.deepcopy(EMPTY), context_summary=None)
        assert result.persistence.status == "saved"
        assert (await raw_row())[MARKER] == 0, "clearing history cannot restore provenance"


async def test_old_row_cannot_acquire_marker_from_extra_payload_or_future_timestamp():
    await seed()
    row = await raw_row()
    assert MARKER in row, "session scope column is missing"
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET session_scope_version = 0 WHERE id = ?", ("chat",)
        )
        await db.commit()
    result = await ui_save(agent_id=A, session_scope_version=1, updatedAt=9999999999999)
    assert result.persistence.status == "saved"
    assert (await raw_row())[MARKER] == 0


async def test_marker_extra_is_ignored_by_public_model():
    model = ui_sessions.SessionModel(
        id="chat",
        title="draft",
        messages=copy.deepcopy(EMPTY),
        updatedAt=1000,
        session_scope_version=1,
    )
    assert MARKER not in model.model_dump()
    assert MARKER not in model.to_changes().model_fields_set


async def test_marker_is_not_exposed_by_list_get_or_snapshot():
    await seed()
    listed = await ui_sessions.list_sessions(HUMAN)
    plain = await ui_sessions.get_session("chat", False, HUMAN)
    snapshot = await ui_sessions.get_session("chat", True, HUMAN)
    assert MARKER not in listed[0]
    assert MARKER not in plain
    assert MARKER not in snapshot.session.model_dump()


async def test_creation_revision_can_immediately_save_again():
    result = await ui_save(agent_id=A)
    assert result.persistence.status == "saved"
    snapshot = await async_db.session_service.read_persistence_snapshot("chat", HUMAN)
    assert snapshot.revision == result.persistence.revision
    assert (await ui_save(title="second")).persistence.status == "saved"


async def test_same_soul_writers_and_set_home_preserve_marker():
    await seed()
    assert await async_db.session_service.append_message_to_session(
        "chat", "user", "next", identity=A
    )
    assert (await cas(title="rename")).status == "saved"
    assert await async_db.session_service.set_home("chat", A, True)
    assert (await raw_row()).get(MARKER) == 1
