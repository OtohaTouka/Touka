"""Reliability R2：owned SQLite 的 strict snapshot、版本與條件式提交。"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from src.main import app
from src.models import session_persistence as contract
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.routers import ui_sessions
from src.services import sliding_window_service as sliding
from src.services.identity_router import get_verified_identity
from src.utils import async_db

OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
OTHER = AgentIdentity.for_role(agent_id="synthetic-other", role="normal", is_human=True)
SID = "synthetic-session"
TREE = {
    "messages": {
        "u": {
            "id": "u",
            "role": "user",
            "content": "hello",
            "parentId": None,
            "childrenIds": ["a"],
            "extension": {"present": None},
        },
        "a": {
            "id": "a",
            "role": "assistant",
            "content": "reply",
            "parentId": "u",
            "steps": [{"type": "thinking", "content": "synthetic"}],
            "attachments": [{"name": "synthetic.txt"}],
            "tool_calls": [],
        },
        "branch": {"id": "branch", "role": "assistant", "content": "other", "parentId": "u"},
    },
    "currentId": "a",
    "extension": {"present": None},
}


@pytest_asyncio.fixture
async def owned_db(tmp_path, monkeypatch):
    path = tmp_path / "sessions.db"
    monkeypatch.setattr(async_db, "get_db_path", lambda: path)
    await async_db.init_db_async()
    return path


async def snapshot(sid=SID, identity=OWNER):
    method = getattr(async_db.SessionService, "read_persistence_snapshot", None)
    assert callable(method), "strict persistence snapshot is missing"
    return await method(sid, identity)


async def save(revision, *, sid=SID, identity=OWNER, **values):
    model = getattr(contract, "SessionChanges", None)
    method = getattr(async_db.SessionService, "conditional_save_session", None)
    assert model is not None and callable(method), "typed conditional save is missing"
    return await method(sid, revision, model(**values), identity)


async def seed(**values):
    params = {
        "session_id": SID,
        "title": "original",
        "messages": copy.deepcopy(TREE),
        "owner_user_id": OWNER.agent_id,
        "updatedAt": 1000,
        "project_id": "project",
        "active_skills": ["skill"],
        "active_mcp_servers": ["server"],
        "context_summary": "summary",
    }
    params.update(values)
    assert await async_db.SessionService.upsert_session(**params)


@pytest.mark.asyncio
async def test_absence_create_full_tree_and_next_revision(owned_db):
    before = await snapshot()
    assert before.session is None and before.revision
    result = await save(before.revision, title="new", messages=TREE, updatedAt=1000)
    assert result.status == "saved" and result.representation == "full"
    after = await snapshot()
    assert after.revision == result.revision != before.revision
    assert after.session["messages"] == TREE
    assert after.session["owner_user_id"] == OWNER.agent_id
    stale = await save(before.revision, title="overwrite", messages=TREE, updatedAt=2000)
    assert stale.status == "failed"
    assert (await snapshot()).session["title"] == "new"


@pytest.mark.asyncio
async def test_same_revision_race_commits_only_one_full_snapshot(owned_db):
    await seed()
    revision = (await snapshot()).revision
    outcomes = await asyncio.gather(
        save(revision, title="one", messages=TREE),
        save(revision, title="two", messages=TREE),
    )
    assert sorted(result.status for result in outcomes) == ["failed", "saved"]
    assert (await snapshot()).session["title"] in {"one", "two"}


@pytest.mark.asyncio
async def test_omitted_metadata_preserved_and_explicit_null_or_empty_clears(owned_db):
    await seed()
    first = await snapshot()
    result = await save(first.revision, title="renamed")
    preserved = await snapshot()
    assert result.status == "saved"
    assert preserved.session["project_id"] == "project"
    assert preserved.session["active_mcp_servers"] == ["server"]
    assert preserved.session["active_skills"] == ["skill"]
    result = await save(
        preserved.revision,
        project_id=None,
        active_mcp_servers=[],
        active_skills=[],
        context_summary=None,
    )
    cleared = await snapshot()
    assert result.status == "saved" and cleared.session["project_id"] is None
    assert cleared.session["active_mcp_servers"] == cleared.session["active_skills"] == []
    assert cleared.session["context_summary"] is None
    assert cleared.session["messages"] == TREE


@pytest.mark.parametrize(
    "column,value",
    [
        ("title", "other"),
        ("messages", json.dumps({"messages": {}, "currentId": None})),
        ("updatedAt", 2000),
        ("is_home", 1),
        ("home_for_agent", "other"),
        ("agent_id", "other"),
        ("project_id", "other"),
        ("auth_level", "YOLO"),
        ("active_mcp_servers", '["other"]'),
        ("active_skills", '["other"]'),
        ("last_activity_at", 2000),
        ("context_summary", "other"),
        ("owner_user_id", "other"),
    ],
)
@pytest.mark.asyncio
async def test_every_mutable_row_column_invalidates_the_old_revision(owned_db, column, value):
    await seed()
    before = await snapshot()
    async with async_db.get_async_db() as db:
        # column is exclusively the finite parameterized fixture above, never external input.
        await db.execute(f"UPDATE user_sessions SET {column} = ? WHERE id = ?", (value, SID))  # noqa: S608
        await db.commit()
    changed = await snapshot()
    assert before.revision != changed.revision
    result = await save(before.revision, title="must-not-win", messages=TREE)
    assert result.status == "failed"
    assert (await snapshot()).revision == changed.revision


@pytest.mark.asyncio
async def test_identity_session_and_selected_delete_recreate_fence_tokens(owned_db):
    missing = await snapshot()
    assert missing.revision != (await snapshot(identity=OTHER)).revision
    assert missing.revision != (await snapshot(sid="other-session")).revision
    denied = await save(missing.revision, identity=OTHER, title="wrong", messages=TREE)
    assert denied.status == "failed" and (await snapshot()).session is None
    created = await save(missing.revision, title="new", messages=TREE, updatedAt=1000)
    delete = getattr(async_db.SessionService, "conditional_delete_session", None)
    assert callable(delete), "conditional delete generation fence is missing"
    deleted = await delete(SID, created.revision, OWNER)
    assert deleted.status == "saved"
    after_delete = await snapshot()
    assert after_delete.session is None and after_delete.revision != missing.revision
    stale = await save(missing.revision, title="revive", messages=TREE, updatedAt=1000)
    assert stale.status == "failed" and (await snapshot()).session is None
    recreated = await save(after_delete.revision, title="new", messages=TREE, updatedAt=1000)
    assert recreated.status == "saved" and recreated.revision != created.revision


@pytest.mark.parametrize(
    "column,value",
    [
        ("messages", "{broken"),
        ("messages", '{"messages":{},"currentId":"missing"}'),
        ("active_skills", "not-json"),
        ("active_mcp_servers", "[123]"),
        ("messages", '{"messages":{},"currentId":null,"bad":NaN}'),
    ],
)
@pytest.mark.asyncio
async def test_corrupt_row_never_issues_absence_token_or_allows_overwrite(owned_db, column, value):
    await seed()
    before = await snapshot()
    async with async_db.get_async_db() as db:
        await db.execute(f"UPDATE user_sessions SET {column} = ? WHERE id = ?", (value, SID))  # noqa: S608
        await db.commit()
    with pytest.raises(Exception, match="Session persistence could not be confirmed"):
        await snapshot()
    result = await save(before.revision, title="overwrite", messages=TREE)
    assert result.status == "unknown"
    async with async_db.get_async_db() as db:
        cursor = await db.execute(f"SELECT {column} FROM user_sessions WHERE id = ?", (SID,))  # noqa: S608
        assert (await cursor.fetchone())[0] == value


@pytest.mark.asyncio
async def test_read_failure_is_unknown_without_calling_the_legacy_upsert(owned_db, monkeypatch):
    await seed()
    before = await snapshot()
    old_upsert = AsyncMock()
    monkeypatch.setattr(async_db.SessionService, "_upsert_session_unlocked", old_upsert)

    @asynccontextmanager
    async def broken_db():
        raise aiosqlite.OperationalError("synthetic-sensitive-storage-fault")
        yield

    monkeypatch.setattr(async_db, "get_async_db", broken_db)
    with pytest.raises(Exception, match="Session persistence could not be confirmed") as caught:
        await snapshot()
    assert "synthetic-sensitive" not in str(caught.value)
    result = await save(before.revision, title="overwrite")
    assert result.status == "unknown"
    assert "synthetic-sensitive" not in result.model_dump_json()
    old_upsert.assert_not_awaited()


@pytest.mark.parametrize("commit_first", [False, True])
@pytest.mark.asyncio
async def test_commit_exception_is_unknown_even_if_commit_happened(
    owned_db, monkeypatch, commit_first
):
    await seed()
    before = await snapshot()
    original = aiosqlite.Connection.commit

    async def uncertain_commit(db):
        if commit_first:
            await original(db)
        raise aiosqlite.OperationalError("synthetic-sensitive-commit-fault")

    with monkeypatch.context() as patcher:
        patcher.setattr(aiosqlite.Connection, "commit", uncertain_commit)
        result = await save(before.revision, title="changed")
    assert result.status == "unknown"
    assert (await snapshot()).session["title"] == ("changed" if commit_first else "original")


@pytest.mark.asyncio
async def test_compare_and_write_share_one_sqlite_transaction(owned_db, monkeypatch):
    await seed()
    before = await snapshot()
    original = aiosqlite.Connection.execute
    other_attempted = False

    async def execute_with_interleaved_writer(db, sql, parameters=None):
        nonlocal other_attempted
        if sql.lstrip().upper().startswith("INSERT INTO USER_SESSIONS") and not other_attempted:
            other_attempted = True
            async with aiosqlite.connect(str(owned_db), timeout=0) as other:
                with pytest.raises(aiosqlite.OperationalError, match="locked"):
                    await other.execute(
                        "UPDATE user_sessions SET title = ? WHERE id = ?", ("racer", SID)
                    )
                    await other.commit()
        return await original(db, sql, parameters or ())

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute_with_interleaved_writer)
    result = await save(before.revision, title="winner")
    assert other_attempted and result.status == "saved"
    assert (await snapshot()).session["title"] == "winner"


@pytest_asyncio.fixture
async def client(owned_db, monkeypatch):
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    monkeypatch.setattr(ui_sessions, "_is_registered_agent", lambda _value: False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as value:
            yield value
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def payload(revision=None, **values):
    result = {"id": SID, "title": "changed", "messages": copy.deepcopy(TREE), "updatedAt": 2000}
    if revision is not None:
        result["revision"] = revision
    result.update(values)
    return result


@pytest.mark.asyncio
async def test_route_snapshot_distinguishes_absence_and_existing_without_owner(client):
    missing = await client.get(f"/v1/ui/sessions/{SID}?snapshot=true")
    assert missing.status_code == 200
    body = missing.json()
    assert body["exists"] is False and body["session"] is None and body["revision"]
    assert (await client.get(f"/v1/ui/sessions/{SID}")).status_code == 404
    await seed()
    found = await client.get(f"/v1/ui/sessions/{SID}?snapshot=true")
    body = found.json()
    assert body["exists"] is True and body["session"]["messages"] == TREE
    assert "owner_user_id" not in body["session"]
    ordinary = (await client.get(f"/v1/ui/sessions/{SID}")).json()
    assert ordinary["messages"] == TREE and ordinary["revision"] == body["revision"]
    assert "owner_user_id" not in ordinary and "exists" not in ordinary
    assert ordinary["round_count"] == 1


@pytest.mark.asyncio
async def test_route_missing_revision_cannot_create_or_overwrite(client):
    result = await client.post(f"/v1/ui/sessions/{SID}", json=payload())
    assert (await snapshot()).session is None
    assert result.json()["persistence"]["status"] == "failed"
    await seed()
    before = await snapshot()
    result = await client.post(f"/v1/ui/sessions/{SID}", json=payload())
    assert (await snapshot()).revision == before.revision
    assert result.json()["persistence"]["status"] == "failed"


@pytest.mark.asyncio
async def test_route_stale_revision_does_not_overwrite_newer_tree(client):
    await seed()
    old = await snapshot()
    await save(old.revision, title="newer")
    newer = await snapshot()
    response = await client.post(f"/v1/ui/sessions/{SID}", json=payload(old.revision))
    assert (await snapshot()).revision == newer.revision
    assert response.json()["persistence"]["status"] == "failed"


@pytest.mark.asyncio
async def test_route_full_save_preserves_extensions_and_clears_selected_metadata(client):
    await seed()
    before = await snapshot()
    response = await client.post(
        f"/v1/ui/sessions/{SID}",
        json=payload(
            before.revision,
            project_id=None,
            active_mcp_servers=[],
            active_skills=[],
            context_summary=None,
        ),
    )
    after = await snapshot()
    assert after.session["messages"] == TREE
    assert after.session["project_id"] is None
    assert after.session["active_mcp_servers"] == after.session["active_skills"] == []
    assert after.session["context_summary"] is None
    assert response.status_code == 200
    assert response.json()["persistence"] == {
        "status": "saved",
        "representation": "full",
        "revision": after.revision,
    }


@pytest.mark.asyncio
async def test_route_snapshot_foreign_owner_and_corrupt_row_never_issue_token(client, monkeypatch):
    await seed(owner_user_id=OTHER.agent_id)
    monkeypatch.setattr(ui_sessions.resource_manager, "is_owner", lambda *_args: False)
    denied = await client.get(f"/v1/ui/sessions/{SID}?snapshot=true")
    assert denied.status_code == 403 and "revision" not in denied.json()
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET messages = ? WHERE id = ?", ("{broken", SID))
        await db.commit()
    corrupt = await client.get(f"/v1/ui/sessions/{SID}?snapshot=true")
    assert corrupt.status_code == 500 and "revision" not in corrupt.json()
    assert "broken" not in corrupt.text


@pytest.mark.asyncio
async def test_route_owner_changed_during_project_gate_prevents_old_owner_write(
    client, monkeypatch
):
    await seed()
    before = await snapshot()

    async def change_owner(_identity, _project):
        async with async_db.get_async_db() as db:
            await db.execute(
                "UPDATE user_sessions SET owner_user_id = ? WHERE id = ?", (OTHER.agent_id, SID)
            )
            await db.commit()
        return True

    monkeypatch.setattr(ui_sessions, "_can_access_project", change_owner)
    response = await client.post(
        f"/v1/ui/sessions/{SID}", json=payload(before.revision, project_id="new")
    )
    after = await snapshot()
    assert after.session["owner_user_id"] == OTHER.agent_id
    assert after.session["title"] == "original" and after.session["project_id"] == "project"
    assert response.json()["persistence"]["status"] == "failed"


@pytest.mark.asyncio
async def test_route_delete_confirmed_missing_is_idempotent_and_stale_delete_keeps_row(client):
    missing = await client.delete(f"/v1/ui/sessions/{SID}")
    assert missing.status_code == 200 and missing.json() == {"status": "success", "session_id": SID}
    await seed()
    before = await snapshot()
    await save(before.revision, title="newer")
    denied = await client.delete(f"/v1/ui/sessions/{SID}", headers={"If-Match": before.revision})
    assert (await snapshot()).session["title"] == "newer"
    assert denied.status_code != 200


@pytest.mark.asyncio
async def test_route_home_delete_stays_protected(client):
    soul = AgentIdentity(agent_id="synthetic-soul")
    await seed(identity=soul)
    assert await async_db.SessionService.set_home(SID, soul, True)
    before = await snapshot()
    response = await client.delete(f"/v1/ui/sessions/{SID}", headers={"If-Match": before.revision})
    assert response.status_code == 403 and (await snapshot()).revision == before.revision


@pytest.mark.asyncio
async def test_process_epoch_invalidates_previous_tokens_without_changing_row(
    owned_db, monkeypatch
):
    await seed()
    before = await snapshot()
    monkeypatch.setattr(async_db, "_persistence_epoch", "synthetic-new-process", raising=True)
    after = await snapshot()
    assert after.session == before.session and after.revision != before.revision
    assert (await save(before.revision, title="stale-process")).status == "failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"messages": {"messages": {}, "currentId": "synthetic-private-invalid-node"}},
        {"active_skills": ["synthetic-private-input", 123]},
        {"updatedAt": "synthetic-private-input"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_public_save_has_fixed_safe_validation_without_input(client, changes):
    revision = (await snapshot()).revision
    response = await client.post(f"/v1/ui/sessions/{SID}", json=payload(revision, **changes))
    assert "synthetic-private" not in response.text
    assert response.status_code == 422
    assert (await snapshot()).session is None


@pytest.mark.asyncio
async def test_public_null_auth_level_and_home_keep_existing_or_use_new_defaults(client):
    missing = await snapshot()
    response = await client.post(
        f"/v1/ui/sessions/{SID}",
        json=payload(missing.revision, agent_id="synthetic-soul", auth_level=None, is_home=None),
    )
    assert response.json()["persistence"]["status"] == "saved"
    current = await snapshot()
    assert current.session["auth_level"] == "Manual" and current.session["is_home"] == 0  # noqa: PAC_MANUAL_LEVEL_CHECK — owned DB 合約讀回
    assert (await save(current.revision, auth_level="YOLO")).status == "saved"
    assert await async_db.SessionService.set_home(
        SID, AgentIdentity(agent_id="synthetic-soul"), True
    )
    changed = await snapshot()
    response = await client.post(
        f"/v1/ui/sessions/{SID}", json=payload(changed.revision, auth_level=None, is_home=None)
    )
    assert response.json()["persistence"]["status"] == "saved"
    after = await snapshot()
    # None 保留 is_home；auth_level 另受模式授權（ADR-0246）：一般使用者存檔不能讓對話留在
    # YOLO，降成 Manual（擁有者保留的情形見 test_auth_mode_authority_security）
    assert after.session["auth_level"] == "Manual" and after.session["is_home"] == 1  # noqa: PAC_MANUAL_LEVEL_CHECK — owned DB 合約讀回


@pytest.mark.asyncio
async def test_route_delete_existing_requires_the_client_revision(client):
    await seed()
    before = await snapshot()
    response = await client.delete(f"/v1/ui/sessions/{SID}")
    assert (await snapshot()).revision == before.revision
    assert response.status_code != 200


@pytest.mark.asyncio
async def test_route_old_delete_intent_cannot_delete_recreated_same_id(client):
    missing = await snapshot()
    created = await save(missing.revision, title="same", messages=TREE, updatedAt=1000)
    response = await client.delete(f"/v1/ui/sessions/{SID}", headers={"If-Match": created.revision})
    assert response.status_code == 200
    missing_again = await snapshot()
    recreated = await save(missing_again.revision, title="same", messages=TREE, updatedAt=1000)
    response = await client.delete(f"/v1/ui/sessions/{SID}", headers={"If-Match": created.revision})
    assert response.status_code != 200 and (await snapshot()).revision == recreated.revision


@pytest.fixture
def legacy_archive(monkeypatch):
    """剪下的對話直接歸檔進舊對話庫；換成假的並記錄呼叫。"""
    manager = Mock()
    archive = manager.get_legacy_sync_service.return_value.archive_payload
    archive.return_value = 1
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    return archive


@pytest.fixture
def trim_service(legacy_archive, monkeypatch):
    service = sliding.SlidingWindowService()
    monkeypatch.setattr(
        service, "load_config", lambda: SlidingWindowConfig(enabled=True, summary_budget_pct=5)
    )
    monkeypatch.setattr(service, "_resolve_model", lambda _linear: "synthetic-model")
    monkeypatch.setattr(service, "_check_model_cooldown", lambda *_args: False)
    monkeypatch.setattr(service, "_current_total_tokens", lambda *_args: 100)
    monkeypatch.setattr(sliding, "get_context_window", lambda _model: 100)
    monkeypatch.setattr(
        service,
        "compute_split_by_tokens",
        lambda *_args: (
            [TREE["messages"]["u"]],
            [TREE["messages"]["a"]],
        ),
    )
    return service


@pytest.mark.asyncio
async def test_rich_save_during_summary_wins_and_stale_trim_discards_without_retry(
    owned_db, trim_service, legacy_archive, monkeypatch
):
    soul = AgentIdentity(agent_id="synthetic-soul")
    await seed(identity=soul)
    before = await snapshot()
    entered, release = asyncio.Event(), asyncio.Event()

    async def summary(*_args):
        entered.set()
        await release.wait()
        return "new-summary"

    summarize = AsyncMock(side_effect=summary)
    monkeypatch.setattr(sliding, "summarize_dialogue", summarize)
    task = asyncio.create_task(trim_service.check_and_trim(soul, SID))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        richer = copy.deepcopy(TREE)
        richer["messages"]["new"] = {"id": "new", "role": "user", "content": "new", "parentId": "a"}
        richer["currentId"] = "new"
        result = await asyncio.wait_for(
            save(before.revision, title="newer", messages=richer), timeout=1
        )
        assert result.status == "saved", "summary must not hold the session writer lock"
        release.set()
        trimmed = await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
    after = await snapshot()
    assert after.session["messages"] == richer and after.session["title"] == "newer"
    assert trimmed is False
    summarize.assert_awaited_once()
    legacy_archive.assert_called_once()


@pytest.mark.asyncio
async def test_trim_first_causes_stale_rich_save_conflict_without_overwrite(
    owned_db, trim_service, monkeypatch
):
    soul = AgentIdentity(agent_id="synthetic-soul")
    await seed(identity=soul)
    before = await snapshot()
    monkeypatch.setattr(sliding, "summarize_dialogue", AsyncMock(return_value="new-summary"))
    assert await trim_service.check_and_trim(soul, SID)
    trimmed = await snapshot()
    assert (
        trimmed.session["messages"] != TREE and trimmed.session["context_summary"] == "new-summary"
    )
    result = await save(before.revision, messages=TREE)
    assert result.status == "failed" and (await snapshot()).revision == trimmed.revision


@pytest.mark.asyncio
async def test_trim_strict_read_failure_has_no_export_summary_or_write(
    owned_db, trim_service, legacy_archive, monkeypatch
):
    await seed()
    before = await snapshot()
    monkeypatch.setattr(
        sliding.session_service,
        "read_persistence_snapshot",
        AsyncMock(side_effect=async_db.SessionPersistenceError()),
    )
    summarize = AsyncMock(return_value="must-not-run")
    monkeypatch.setattr(sliding, "summarize_dialogue", summarize)
    assert await trim_service.check_and_trim(OWNER, SID) is False
    legacy_archive.assert_not_called()
    summarize.assert_not_awaited()
    raw = await async_db.SessionService.get_session_by_id(SID)
    assert raw["messages"] == before.session["messages"]
