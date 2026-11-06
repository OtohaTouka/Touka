"""New soul lifetimes must not inherit residual or preclaimed UI history."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from src import main
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig, SoulCreateRequest, SoulDeleteRequest, SoulUpdateRequest
from src.models.system_restore import RestoreStatus
from src.routers import souls, system
from src.services.agent_service import agent_service
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils import async_db

from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.conversation_search import denied, search, success, tree
from tests.support.session_scope import ADMIN, HUMAN, A, B, passport, raw_row, ui_save
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


class DatabaseWriteFailure(aiosqlite.OperationalError):
    def __init__(self) -> None:
        super().__init__("private database detail")


class ProcessStopped(BaseException):
    """Model a process exit before the restore cleanup can touch the store."""


@pytest.fixture
def soul_lifecycle(conversation_db: ConversationDB, tmp_path: Path, monkeypatch):
    """Use real soul files and session storage; isolate unrelated registration stores."""

    def identify(value):
        result = passport(value)
        if result["agent_id"] == A.agent_id:
            config = tmp_path / "soul" / A.agent_id / "config.json"
            result["is_toukasoul"] = config.exists() and json.loads(config.read_text()).get(
                "is_toukasoul", False
            )
        return result

    monkeypatch.setattr(agent_service, "identify_agent", identify)
    monkeypatch.setattr(
        agent_service,
        "get_all_agents",
        lambda: [{"id": i.agent_id} for i in (A, B) if identify(i.agent_id)["is_toukasoul"]],
    )
    monkeypatch.setattr(souls.memory_collection_guard, "assert_id_free", Mock())
    monkeypatch.setattr(souls, "_update_models_config", Mock())
    monkeypatch.setattr(souls, "_update_agents_registry", Mock())
    monkeypatch.setattr(souls, "_verify_admin_stepup", Mock())
    monkeypatch.setattr(souls, "assert_memory_cleared", AsyncMock())
    monkeypatch.setattr(souls, "assert_timeline_cleared", AsyncMock())
    monkeypatch.setattr(souls, "_remove_from_all_owned_agents", Mock())
    monkeypatch.setattr(souls, "_remove_identity_override", Mock())
    monkeypatch.setattr(UserService, "add_owned_agent", Mock())
    monkeypatch.setattr(UserService, "claim_owned_agent", Mock())
    monkeypatch.setattr(UserService, "remove_owned_agent", Mock())
    return tmp_path / "soul" / A.agent_id


async def create() -> None:
    result = await souls.create_soul(
        SoulCreateRequest(agent_id=A.agent_id, config=SoulConfig()), ADMIN
    )
    assert result["status"] == "success"


async def visible_control() -> None:
    result = await search(B, source="ui")
    success(result, 1)
    assert "allowed-control" in result


@pytest.mark.parametrize("alias", [False, True])
async def test_deleted_soul_recreated_under_new_owner_hides_old_history(
    conversation_db: ConversationDB, soul_lifecycle: Path, alias: bool
) -> None:
    await create()
    old_identity = AgentIdentity.for_role(f"ToukaSoul{A.agent_id}", "Owner") if alias else A
    await conversation_db.ui("old", "forbidden-old-owner", identity=old_identity)
    await conversation_db.ui("control", "allowed-control", identity=B)
    before = await raw_row("old")
    success(await search(source="ui"), 1)
    await souls.delete_soul(A.agent_id, SoulDeleteRequest(admin_password="unused"), ADMIN)
    assert not soul_lifecycle.exists()
    assert await raw_row("old") == before
    await create()
    await conversation_db.ui("new", "allowed-new-owner", owner=ADMIN.agent_id)
    result = await search(source="ui")
    success(result, 1)
    assert "allowed-new-owner" in result and "forbidden-old-owner" not in result
    assert await raw_row("old") == {**before, "session_scope_version": 0}
    await visible_control()


@pytest.mark.parametrize("entry", ["create", "update"])
async def test_save_between_invalidation_and_config_is_not_searchable(
    conversation_db: ConversationDB, soul_lifecycle: Path, monkeypatch, entry: str
) -> None:
    original = async_db.session_service.invalidate_agent_sessions
    calls = 0

    async def invalidate(identity):
        nonlocal calls
        await original(identity)
        calls += 1
        if calls == 1:
            assert not soul_lifecycle.exists()
            saved = await ui_save(
                "racing", caller=HUMAN.model_copy(update={"owned_agents": []}),
                agent_id=A, messages=tree("forbidden-racing"),
            )
            assert saved.persistence.status == "saved"
            assert (await raw_row("racing"))["session_scope_version"] == 1

    monkeypatch.setattr(async_db.session_service, "invalidate_agent_sessions", invalidate)
    await conversation_db.ui("control", "allowed-control", identity=B)
    if entry == "create":
        await create()
    else:
        await souls.update_soul(A.agent_id, SoulUpdateRequest(config=SoulConfig()), ADMIN)
    success(await search(source="ui"), 0)
    assert (await raw_row("racing"))["session_scope_version"] == 0
    assert calls == 2
    with pytest.raises(ToukaException) as caught:
        await ui_save(
            "too-late", caller=HUMAN.model_copy(update={"owned_agents": []}), agent_id=A,
        )
    assert caught.value.code == ErrorCode.AUTH_PERMISSION_DENIED
    await conversation_db.ui("new", "allowed-new")
    success(await search(source="ui"), 1)
    await visible_control()


@pytest.mark.parametrize("entry", ["create", "update", "convert", "existing-directory"])
async def test_second_invalidation_failure_rolls_back_new_soul(
    conversation_db: ConversationDB, soul_lifecycle: Path, monkeypatch, entry: str
) -> None:
    if entry in {"convert", "existing-directory"}:
        soul_lifecycle.mkdir(parents=True)
        (soul_lifecycle / "keep.txt").write_text("keep")
    if entry == "convert":
        (soul_lifecycle / "config.json").write_text('{"is_toukasoul": false, "keep": 42}')
    original = async_db.session_service.invalidate_agent_sessions
    calls = 0

    async def invalidate(identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert agent_service.identify_agent(A.agent_id)["is_toukasoul"]
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)
        await original(identity)

    monkeypatch.setattr(async_db.session_service, "invalidate_agent_sessions", invalidate)
    await conversation_db.ui("control", "allowed-control", identity=B)
    with pytest.raises(ToukaException) as caught:
        if entry == "create":
            await create()
        else:
            await souls.update_soul(A.agent_id, SoulUpdateRequest(config=SoulConfig()), ADMIN)
    assert caught.value.code == ErrorCode.SESSION_SAVE_FAILED
    assert calls == 2
    assert not agent_service.identify_agent(A.agent_id)["is_toukasoul"]
    if entry == "convert":
        assert json.loads((soul_lifecycle / "config.json").read_text()) == {
            "is_toukasoul": False, "keep": 42,
        }
    elif entry == "existing-directory":
        assert not (soul_lifecycle / "config.json").exists()
    else:
        assert not soul_lifecycle.exists()
    if entry in {"convert", "existing-directory"}:
        assert (soul_lifecycle / "keep.txt").read_text() == "keep"
    assert not any(
        call.kwargs.get("action") == "add" for call in souls._update_models_config.call_args_list
    )
    UserService.add_owned_agent.assert_not_called()
    UserService.claim_owned_agent.assert_not_called()
    denied(await search(source="ui"))
    await visible_control()


@pytest.mark.parametrize("entry", ["create", "update"])
@pytest.mark.parametrize("prefix", ["ToukaSoul", "toukasoul", "TOUKASOUL"])
async def test_prefixed_new_soul_is_refused_before_invalidation(
    conversation_db: ConversationDB, soul_lifecycle: Path, monkeypatch, entry: str, prefix: str
) -> None:
    await create()
    await conversation_db.ui("canonical", "allowed-canonical")
    await conversation_db.ui(
        "alias", "allowed-alias", identity=AgentIdentity(agent_id=f"ToukaSoul{A.agent_id}")
    )
    before = [await raw_row(sid) for sid in ("canonical", "alias")]
    invalidate = AsyncMock(wraps=async_db.session_service.invalidate_agent_sessions)
    monkeypatch.setattr(async_db.session_service, "invalidate_agent_sessions", invalidate)
    monkeypatch.setattr(souls, "_verify_admin_stepup", Mock())
    target = f"{prefix}{A.agent_id}"
    with pytest.raises(ToukaException) as caught:
        if entry == "create":
            await souls.create_soul(SoulCreateRequest(agent_id=target, config=SoulConfig()), ADMIN)
        else:
            await souls.update_soul(target, SoulUpdateRequest(config=SoulConfig()), ADMIN)
    assert caught.value.code == ErrorCode.VALIDATION_ERROR
    invalidate.assert_not_awaited()
    assert [await raw_row(sid) for sid in ("canonical", "alias")] == before
    success(await search(source="ui"), 2)


@pytest.mark.parametrize("actor", [A, AgentIdentity.for_role(f"ToukaSoul{A.agent_id}", "Owner")])
async def test_invalidation_uses_search_canonical_id(
    conversation_db: ConversationDB, actor: AgentIdentity
) -> None:
    await conversation_db.ui("canonical", "old")
    await conversation_db.ui(
        "alias", "old", identity=AgentIdentity(agent_id=f"ToukaSoul{A.agent_id}")
    )
    await conversation_db.ui("control", "allowed-control", identity=B)
    await async_db.session_service.invalidate_agent_sessions(actor)
    success(await search(source="ui"), 0)
    assert (await raw_row("canonical"))["session_scope_version"] == 0
    assert (await raw_row("alias"))["session_scope_version"] == 0
    await visible_control()


@pytest.mark.parametrize("alias", [False, True])
async def test_human_preclaim_is_not_new_souls_history(
    conversation_db: ConversationDB, soul_lifecycle: Path, alias: bool
) -> None:
    assert not agent_service.identify_agent(A.agent_id)["is_toukasoul"]
    target = AgentIdentity(agent_id=f"ToukaSoul{A.agent_id}" if alias else A.agent_id)
    caller = HUMAN.model_copy(update={"owned_agents": []})
    saved = await ui_save(
        "preclaim", caller=caller, agent_id=target, messages=tree("forbidden-planted")
    )
    assert saved.persistence.status == "saved"
    before = await raw_row("preclaim")
    assert before["session_scope_version"] == 1
    assert before["owner_user_id"] == HUMAN.agent_id
    denied(await search(source="ui"))
    await conversation_db.ui("control", "allowed-control", identity=B)
    await create()
    await conversation_db.ui("new", "allowed-new")
    result = await search(source="ui")
    success(result, 1)
    assert "allowed-new" in result and "forbidden-planted" not in result
    assert await raw_row("preclaim") == {**before, "session_scope_version": 0}
    await visible_control()


@pytest.mark.parametrize("actor", [A, AgentIdentity.for_role(f"ToukaSoul{A.agent_id}", "Owner")])
async def test_reset_count_and_purge_include_alias_but_not_foreign_home_reference(
    conversation_db: ConversationDB, actor: AgentIdentity
) -> None:
    await conversation_db.ui("canonical", "forbidden-canonical")
    await conversation_db.ui(
        "alias", "forbidden-alias", identity=AgentIdentity(agent_id=f"ToukaSoul{A.agent_id}")
    )
    await conversation_db.ui("control", "allowed-control", identity=B)
    await conversation_db.ui("foreign-home", "foreign", identity=B, home=A.agent_id)
    foreign_before = await raw_row("foreign-home")
    count = await async_db.session_service.count_agent_sessions(actor)
    purged = await async_db.session_service.purge_agent_sessions(actor)
    remaining = await async_db.session_service.count_agent_sessions(actor)
    result = await search(source="ui")
    success(result, 0)
    assert (count, purged, remaining) == (2, 2, 0)
    assert await raw_row("foreign-home") == foreign_before
    assert (await raw_row("control"))["session_scope_version"] == 1
    # The contradictory row is retained and still causes the existing scope refusal for B.
    denied(await search(B, source="ui"))
    await conversation_db.execute("DELETE FROM user_sessions WHERE id = ?", ("foreign-home",))
    await visible_control()


@pytest.mark.parametrize("entry", ["create", "update"])
async def test_every_new_soul_entry_lowers_all_candidate_metadata_atomically(
    conversation_db: ConversationDB, soul_lifecycle: Path, entry: str
) -> None:
    rows = {}
    for field in ("agent_id", "home_for_agent"):
        for alias in (False, True):
            value = f"ToukaSoul{A.agent_id}" if alias else A.agent_id
            sid = f"{field}-{alias}"
            await conversation_db.ui(
                sid,
                "forbidden",
                identity=AgentIdentity(agent_id=value) if field == "agent_id" else B,
                home=value if field == "home_for_agent" else None,
            )
            rows[sid] = await raw_row(sid)
    await conversation_db.ui("already-zero", "old", marker=0)
    await conversation_db.ui("control", "allowed-control", identity=B)
    control = await raw_row("control")
    conversation_db.statements.clear()
    if entry == "create":
        await create()
    else:
        await souls.update_soul(A.agent_id, SoulUpdateRequest(config=SoulConfig()), ADMIN)
    statements = list(conversation_db.statements)
    for sid, row in rows.items():
        assert await raw_row(sid) == {**row, "session_scope_version": 0}
    assert (await raw_row("already-zero"))["session_scope_version"] == 0
    assert await raw_row("control") == control
    assert sum(s.startswith("BEGIN IMMEDIATE") for s in statements) == 2
    assert sum(s == "COMMIT" for s in statements) == 2
    await visible_control()


@pytest.mark.parametrize("entry", ["create", "update"])
async def test_lowering_failure_blocks_soul_visibility_and_rolls_back(
    conversation_db: ConversationDB, soul_lifecycle: Path, monkeypatch, entry: str
) -> None:
    await conversation_db.ui("old", "forbidden")
    await conversation_db.ui("control", "allowed-control", identity=B)
    before = await raw_row("old")
    original_execute = aiosqlite.Connection.execute

    async def execute(db, sql, parameters=None):
        cursor = await original_execute(db, sql, parameters)
        if sql.lstrip().upper().startswith("UPDATE USER_SESSIONS"):
            raise DatabaseWriteFailure
        return cursor

    with monkeypatch.context() as failure:
        failure.setattr(aiosqlite.Connection, "execute", execute)
        with pytest.raises(ToukaException) as caught:
            if entry == "create":
                await create()
            else:
                await souls.update_soul(A.agent_id, SoulUpdateRequest(config=SoulConfig()), ADMIN)
    assert caught.value.code == ErrorCode.SESSION_SAVE_FAILED
    assert "private database detail" not in str(caught.value)
    assert not soul_lifecycle.exists()
    assert not agent_service.identify_agent(A.agent_id)["is_toukasoul"]
    souls._update_models_config.assert_not_called()
    souls._update_agents_registry.assert_not_called()
    assert await raw_row("old") == before
    denied(await search(source="ui"))
    await visible_control()


@pytest.fixture
def restore_runtime(tmp_path: Path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
    spec = importlib.util.spec_from_file_location("session_restore_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolver = module.PathResolver(
        tmp_path, env_path=tmp_path / "sandbox-environment.txt",
        data_root=tmp_path, config_dir=tmp_path / "config",
    )
    exits = []
    monkeypatch.setattr(system, "_last_restore", RestoreStatus(state="running"))
    monkeypatch.setattr(system.time, "sleep", Mock())
    monkeypatch.setattr(system.os, "_exit", exits.append)
    monkeypatch.setattr(system.model_manager, "reload_connections", Mock())
    monkeypatch.setattr(system.system_backup, "script_path_args", lambda: [])
    monkeypatch.setattr(system, "child_python_env", lambda: {})
    for key in module.OAUTH_SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return module.RestoreEngine(resolver), exits


@pytest.mark.parametrize("outcome", ["success", "partial-failure", "no-db-in-archive"])
async def test_restore_cannot_revive_preclaimed_history(
    conversation_db: ConversationDB, soul_lifecycle: Path, restore_runtime, monkeypatch,
    outcome: str, pending_marker: Path,
) -> None:
    caller = HUMAN.model_copy(update={"owned_agents": []})
    saved = await ui_save("plant", caller=caller, agent_id=A, messages=tree("forbidden-restore"))
    assert saved.persistence.status == "saved"
    await conversation_db.ui("control", "allowed-control", identity=B)
    await visible_control()
    before = {sid: await raw_row(sid) for sid in ("plant", "control")}
    archive = conversation_db.path.parent / "snapshot.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr(".env", "NORMAL_SETTING=synthetic\n")
        if outcome != "no-db-in-archive":
            backup.write(conversation_db.path, f"data/{conversation_db.path.name}")
    await create()
    success(await search(source="ui"), 0)
    engine, exits = restore_runtime

    def restore(*args, **kwargs):
        assert pending_marker.is_file()
        engine.run_restore(str(archive))
        if outcome == "partial-failure":
            raise DatabaseWriteFailure

    def exit_process(code):
        assert not pending_marker.exists()
        for sid in before:
            assert asyncio.run(raw_row(sid))["session_scope_version"] == 0
        exits.append(code)

    monkeypatch.setattr(system, "run_sync", restore)
    monkeypatch.setattr(system.os, "_exit", exit_process)
    await asyncio.to_thread(system._execute_restore, Path("manage_backup.py"), archive)
    assert soul_lifecycle.exists()
    assert not pending_marker.exists()
    success(await search(source="ui"), 0)
    success(await search(B, source="ui"), 0)
    for sid, row in before.items():
        assert await raw_row(sid) == {**row, "session_scope_version": 0}
    assert exits == ([] if outcome == "partial-failure" else [0])
    if outcome == "partial-failure":
        assert system._last_restore.model_dump() == {
            "state": "failed", "error_code": "SYSTEM_BACKUP_FAILED",
        }
    await conversation_db.ui("new", "allowed-after-restore")
    result = await search(source="ui")
    success(result, 1)
    assert "allowed-after-restore" in result and "forbidden-restore" not in result
    await conversation_db.ui("new-control", "allowed-control", identity=B)
    await visible_control()


@pytest.mark.parametrize("script_fails", [False, True])
async def test_restore_invalidation_failure_never_reports_done(
    conversation_db: ConversationDB, restore_runtime, monkeypatch, script_fails: bool,
    pending_marker: Path,
) -> None:
    await conversation_db.ui("old", "old")
    before = await raw_row("old")
    original_execute = aiosqlite.Connection.execute
    attempted = []

    async def execute(db, sql, parameters=None):
        cursor = await original_execute(db, sql, parameters)
        if sql.lstrip().upper().startswith("UPDATE USER_SESSIONS"):
            attempted.append(sql)
            raise DatabaseWriteFailure
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    monkeypatch.setattr(
        system, "run_sync", Mock(side_effect=DatabaseWriteFailure if script_fails else None)
    )
    await asyncio.to_thread(system._execute_restore, Path("manage_backup.py"), Path("snapshot.zip"))
    assert attempted
    assert restore_runtime[1] == []
    assert system._last_restore.model_dump() == {
        "state": "failed", "error_code": "SYSTEM_BACKUP_FAILED",
    }
    assert await raw_row("old") == before
    assert pending_marker.is_file()
    denied(await search(source="ui"), "internal_error")


@pytest.mark.parametrize("store", ["absent", "old-schema", "missing-table"])
async def test_restore_missing_store_or_column_uses_normal_startup(
    conversation_db: ConversationDB, restore_runtime, monkeypatch, store: str,
    pending_marker: Path,
) -> None:
    if store == "absent":
        conversation_db.path.unlink()
    elif store == "missing-table":
        await conversation_db.execute("DROP TABLE user_sessions")
    else:
        await conversation_db.ui("old", "old")
        await conversation_db.execute("ALTER TABLE user_sessions DROP COLUMN session_scope_version")
    monkeypatch.setattr(system, "run_sync", Mock())
    await asyncio.to_thread(system._execute_restore, Path("manage_backup.py"), Path("snapshot.zip"))
    assert restore_runtime[1] == [0]
    assert not pending_marker.exists()
    if store == "absent":
        assert not conversation_db.path.exists()
    else:
        async with async_db.get_async_db() as db:
            cursor = await db.execute("PRAGMA table_info(user_sessions)")
            assert "session_scope_version" not in {row["name"] for row in await cursor.fetchall()}
        await async_db.init_db_async()
        if store == "old-schema":
            assert (await raw_row("old"))["session_scope_version"] == 0
        success(await search(source="ui"), 0)


@pytest.fixture
def pending_marker(conversation_db: ConversationDB) -> Path:
    return Path(resource_manager.get_base_path()) / ".session-restore-pending"


@pytest.mark.parametrize("source", ["ui", "all"])
async def test_pending_restore_refuses_search_before_reading_rows(
    conversation_db: ConversationDB, pending_marker: Path, source: str,
) -> None:
    await conversation_db.ui("control", "allowed-control")
    conversation_db.legacy("allowed-legacy")
    resource_manager.write_file_text(pending_marker, "pending\n")
    conversation_db.statements.clear()
    conversation_db.legacy_reads.clear()

    result = await search(source=source)
    denied(result, "internal_error")
    assert "allowed-control" not in result and "allowed-legacy" not in result
    assert str(pending_marker) not in result
    assert conversation_db.statements == []
    assert conversation_db.legacy_reads == []

    resource_manager.remove_file(pending_marker)
    result = await search(source=source)
    success(result, 1 if source == "ui" else 2)
    assert "allowed-control" in result


async def test_restore_crash_recovered_after_migration_before_bots(
    conversation_db: ConversationDB, restore_runtime, pending_marker: Path, monkeypatch,
) -> None:
    await conversation_db.ui("plant", "forbidden-restore")
    archive = conversation_db.path.parent / "snapshot.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr(".env", "NORMAL_SETTING=synthetic\n")
        backup.write(conversation_db.path, f"data/{conversation_db.path.name}")
    await async_db.session_service.invalidate_restored_sessions()
    engine, exits = restore_runtime

    def restore(*args, **kwargs):
        assert pending_marker.is_file()
        engine.run_restore(str(archive))
        assert asyncio.run(raw_row("plant"))["session_scope_version"] == 1

    monkeypatch.setattr(system, "run_sync", restore)
    with monkeypatch.context() as stopped:
        stopped.setattr(
            async_db.session_service, "invalidate_restored_sessions",
            AsyncMock(side_effect=ProcessStopped),
        )
        with pytest.raises(ProcessStopped):
            await asyncio.to_thread(system._execute_restore, Path("manage_backup.py"), archive)
    assert exits == []
    assert pending_marker.is_file()
    assert (await raw_row("plant"))["session_scope_version"] == 1
    denied(await search(source="ui"), "internal_error")
    events = []

    async def migrate():
        assert pending_marker.is_file()
        assert (await raw_row("plant"))["session_scope_version"] == 1
        await async_db.init_db_async()
        events.append("migration")

    async def start_bots():
        assert events == ["migration"]
        assert not pending_marker.exists()
        assert (await raw_row("plant"))["session_scope_version"] == 0
        events.append("bots")

    monkeypatch.setattr(main, "init_db_async", migrate)
    monkeypatch.setattr(main.bot_service, "start_bots", start_bots)
    async with main.lifespan(main.app):
        assert events == ["migration", "bots"]
        success(await search(source="ui"), 0)
        await conversation_db.ui("new", "allowed-after-recovery")
        result = await search(source="ui")
        success(result, 1)
        assert "allowed-after-recovery" in result and "forbidden-restore" not in result


@pytest.mark.parametrize("failure", ["lowering", "marker-removal"])
async def test_startup_restore_failure_keeps_marker_and_search_closed(
    conversation_db: ConversationDB, pending_marker: Path, monkeypatch, caplog, failure: str,
) -> None:
    await conversation_db.ui("plant", "forbidden-restore")
    resource_manager.write_file_text(pending_marker, "pending\n")
    if failure == "lowering":
        monkeypatch.setattr(
            async_db.SessionService, "invalidate_restored_sessions",
            AsyncMock(side_effect=DatabaseWriteFailure),
        )
    else:
        original_remove = resource_manager.remove_file

        def remove(path, **kwargs):
            if Path(path) == pending_marker:
                raise DatabaseWriteFailure
            return original_remove(path, **kwargs)

        monkeypatch.setattr(resource_manager, "remove_file", remove)
    bots = AsyncMock()
    monkeypatch.setattr(main.bot_service, "start_bots", bots)
    async with main.lifespan(main.app):
        bots.assert_awaited_once()
        assert pending_marker.is_file()
        conversation_db.statements.clear()
        denied(await search(source="ui"), "internal_error")
        assert conversation_db.statements == []
    records = [r for r in caplog.records if r.getMessage() == "SESSION_RESTORE_RECOVERY_FAILED"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "private database detail" not in caplog.text


async def test_pending_marker_write_failure_prevents_restore_subprocess(
    conversation_db: ConversationDB, restore_runtime, monkeypatch,
) -> None:
    run = Mock()
    monkeypatch.setattr(system, "run_sync", run)
    monkeypatch.setattr(resource_manager, "write_file_text", Mock(side_effect=DatabaseWriteFailure))
    await asyncio.to_thread(system._execute_restore, Path("manage_backup.py"), Path("snapshot.zip"))
    run.assert_not_called()
    assert restore_runtime[1] == []
    assert system._last_restore.state == "failed"
