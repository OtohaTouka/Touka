"""Memory sources: the system records where a memory came from; the model cannot.

Saving records a locator (which conversation or heartbeat, and the saved message that
triggered it) plus a SHA-256 of that saved message, never its text. A query that asks for
sources reports, per fact, whether the original is still there, changed or gone; a gone
original never deletes the fact. Everything runs in tmp sandboxes: a tmp FactualStore, an
autospec vector stub, a tmp session database and a tmp legacy database.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from memory_server import factual as fm
from memory_server import tools as memory_gateway
from memory_server.models.source_ref import SourceRef, SourceStatus
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.manager import MemoryManager
from memory_server.vector import VectorStore
from pydantic import ValidationError
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import (
    IncognitoStreamAdmission,
    PersistedStreamAdmission,
    SavedResult,
)
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.routers import proxy as proxy_router
from src.services import dispatcher as dispatcher_mod
from src.services import interjection_queue as queue_mod
from src.services import memory_source_resolver as resolver_mod
from src.services.interjection_queue import InMemoryInterjectionQueue
from src.services.soul_service import SoulService
from src.services.stream_buffer_service import StreamBufferService
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import memory as proxy_memory_gateway
from src.utils.async_db import SessionSnapshot, init_db_async, session_service

if TYPE_CHECKING:
    from pathlib import Path

sqlite = fm.sqlite3

FROZEN_EPOCH = 1_760_000_000
FACT_ID = re.compile(r"ID:\s*([^)]+)")
STILL_THERE = "原文仍在"
CHANGED = "原文已修改"
GONE = "原文已不在"
NOT_RECORDED = "來源未記錄"
UNREADABLE_NOW = "來源暫時無法讀取"
UNCONFIRMED = "無法確認"
UI_OWNER = AgentIdentity.for_role(agent_id="synthetic-owner", role="normal", is_human=True)
UI_TREE = {
    "messages": {"user": {"id": "user", "role": "user", "content": "hi", "parentId": None}},
    "currentId": "user",
}
TEXT_CHUNK = 'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
OLD_CREATE = (
    "CREATE TABLE facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT, tags TEXT,"
    " importance TEXT DEFAULT NULL, emotions TEXT DEFAULT NULL, created_at INTEGER,"
    " updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
OLD_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at, archived)"
    " VALUES (?, ?, ?, '[]', 1700000000, 1700000000, 0)"
)


def _soul(prefix: str = "SourceProbe") -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"{prefix}{uuid.uuid4().hex[:8]}", role="Owner")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fact_id(reply: str) -> str:
    match = FACT_ID.search(reply)
    assert match is not None, reply
    return match.group(1)


def _stored_sources(store: fm.FactualStore, fact_id: str) -> Any:
    conn = sqlite.connect(store.db_path)
    try:
        row = conn.execute("SELECT sources FROM facts WHERE id = ?", (fact_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, fact_id
    return None if row[0] is None else json.loads(row[0])


def _tree(node_id: str, content: str) -> dict[str, Any]:
    node = {"id": node_id, "role": "user", "content": content, "parentId": None}
    return {"messages": {node_id: node}, "currentId": node_id}


async def _seed_session(identity: AgentIdentity, content: str) -> tuple[str, str]:
    session_id, node_id = f"s-{uuid.uuid4().hex[:8]}", f"n-{uuid.uuid4().hex[:8]}"
    assert await session_service.upsert_session(
        session_id, "T", _tree(node_id, content), identity=identity
    )
    return session_id, node_id


def _ctx(node_id: str | None, **flags: Any) -> PipelineContext:
    ctx = PipelineContext()
    ctx["source_message_id"] = node_id
    ctx.update(flags)
    return ctx


async def _save(
    identity: AgentIdentity, session_id: str | None, ctx: Any, content: str
) -> str:
    return await proxy_memory_gateway.memory_tools(
        action="save",
        content=content,
        identity=identity,
        session_id=session_id,
        request_id="req-source-1",
        _ctx=ctx,
    )


async def _query(identity: AgentIdentity, **arguments: Any) -> str:
    return await proxy_memory_gateway.memory_tools(
        action="query", identity=identity, request_id="req-query", **arguments
    )


def _make_v0_vault(vault: Path, agent: str, contents: list[str]) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(vault / "core_memory.db")
    try:
        conn.execute(OLD_CREATE)
        for index, content in enumerate(contents):
            conn.execute(OLD_INSERT, (f"fact_old{index}", agent, content))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
async def sandbox_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr("src.utils.async_db.get_db_path", lambda: db_path)
    await init_db_async()
    return db_path


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(resolver_mod, "get_current_epoch", lambda: FROZEN_EPOCH + 0.75)
    return FROZEN_EPOCH


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> MemoryManager:
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


@pytest.fixture
def vector(monkeypatch: pytest.MonkeyPatch, manager: MemoryManager) -> Any:
    stub = create_autospec(VectorStore, instance=True)
    stub.query_vectors.return_value = []
    stub.save_vector.return_value = True
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", stub)
    return stub


@pytest.fixture
def store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager, vector: Any
) -> fm.FactualStore:
    factual = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


@pytest.fixture
def legacy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "legacy"
    directory.mkdir()
    monkeypatch.setattr(
        MemoryManager, "get_legacy_dir", lambda _self, _identity, resource_proxy=None: directory
    )
    return directory


async def _execute(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], ctx: dict[str, Any], handler: Any
) -> str:
    permission = MagicMock()
    permission.get_tool_action_param.return_value = "action"
    permission.evaluate_policy.return_value = SimpleNamespace(effect="ALLOW", reason=None)
    monkeypatch.setattr("src.services.tool_executor.permission_service", permission)
    monkeypatch.setattr("src.services.tool_executor.soul_service.is_toukasoul", lambda _: False)
    executor = ToolExecutor(None, {"memory_tools": handler}, {}, {})
    return await executor.execute_to_queue(
        "memory_tools", args, "req-source-1", ctx, asyncio.Queue()
    )


# --- saving: the system attaches the source -----------------------------------


async def test_memory_save_auto_attaches_conversation_source(
    sandbox_db: Path,
    store: fm.FactualStore,
    frozen_clock: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "my cat is called Miso")
    ctx = {
        "identity": soul,
        "session_id": session_id,
        "auth_level": "Auto",
        "source_message_id": node_id,
    }

    reply = await _execute(
        monkeypatch,
        {"action": "save", "content": "the user's cat is Miso"},
        ctx,
        proxy_memory_gateway.memory_tools,
    )

    assert _stored_sources(store, _fact_id(reply)) == [
        {
            "kind": "conversation",
            "session_id": session_id,
            "message_id": node_id,
            "request_id": "req-source-1",
            "captured_at": frozen_clock,
            "content_sha256": _sha("my cat is called Miso"),
        }
    ]


async def test_heartbeat_source(
    sandbox_db: Path, store: fm.FactualStore, frozen_clock: int
) -> None:
    soul = _soul("Pulse")
    session_id, node_id = await _seed_session(soul, "【目前系統時間：test】 check the garden")

    reply = await _save(
        soul, session_id, _ctx(node_id, heartbeat_run=True), "the tomatoes need water"
    )

    [source] = _stored_sources(store, _fact_id(reply))
    assert source["kind"] == "heartbeat"
    assert source["message_id"] == node_id
    assert source["content_sha256"] == _sha("【目前系統時間：test】 check the garden")


async def _no_chunks():
    for _ in ():
        yield ""


@pytest.mark.parametrize("last_role", ["user", "assistant"])
async def test_heartbeat_pulse_marks_its_trigger_message(
    monkeypatch: pytest.MonkeyPatch, last_role: str
) -> None:
    captured: dict[str, Any] = {}

    async def dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul_service = object.__new__(SoulService)  # 不污染 singleton
    soul_service.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    tree = {
        "messages": {
            "init": {"id": "init", "role": "user", "content": "wake up", "parentId": None},
            "reply": {"id": "reply", "role": "assistant", "content": "ok", "parentId": "init"},
        },
        "currentId": "init" if last_role == "user" else "reply",
    }
    sessions = MagicMock(
        read_execution_snapshot=AsyncMock(
            return_value=SessionSnapshot({"id": "s", "title": "t", "messages": tree}, "rev")
        ),
        conditional_save_session=AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="next")
        ),
    )
    monkeypatch.setattr(heartbeat, "session_service", sessions)
    monkeypatch.setattr(heartbeat, "dispatch_request", dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul_service)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")

    await heartbeat._execute_single_pulse(
        "s", identity, "claude-sonnet-4-6", PipelineContext(), 1, 0.0, ""
    )

    trigger = captured["context"].get("source_message_id")
    if last_role == "user":
        assert trigger == "init"
    else:  # the injected nudge is saved first and becomes the trigger
        saved_tree = sessions.conditional_save_session.await_args.args[2].messages
        assert trigger == saved_tree["currentId"]
        assert saved_tree["messages"][trigger]["role"] == "user"


@pytest.mark.parametrize(
    "flags",
    [
        {"incognito": True},
        {"subagent_run": True, "dispatch_run": True},
    ],
)
async def test_incognito_no_source_locator(
    sandbox_db: Path,
    store: fm.FactualStore,
    monkeypatch: pytest.MonkeyPatch,
    flags: dict[str, Any],
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "a private aside")
    reads = AsyncMock(wraps=session_service.read_persistence_snapshot)
    monkeypatch.setattr(resolver_mod.session_service, "read_persistence_snapshot", reads)

    reply = await _save(soul, session_id, _ctx(node_id, **flags), "something said in private")

    assert _stored_sources(store, _fact_id(reply)) is None
    reads.assert_not_awaited()  # zero residue: not even a lookup of where it came from


@pytest.mark.parametrize("missing", ["session", "message", "context"])
async def test_save_without_a_trusted_locator_records_no_source(
    sandbox_db: Path, store: fm.FactualStore, missing: str
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "where this came from")
    ctx = None if missing == "context" else _ctx(None if missing == "message" else node_id)

    reply = await _save(
        soul, None if missing == "session" else session_id, ctx, f"fact without {missing}"
    )

    assert _stored_sources(store, _fact_id(reply)) is None


async def test_ui_persistence_marks_only_the_persistent_user_node(sandbox_db: Path) -> None:
    await session_service.upsert_session(
        "ui-s", "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )
    snapshot = await session_service.read_persistence_snapshot("ui-s", UI_OWNER)
    persistent = PersistedStreamAdmission(
        mode="persistent",
        revision=snapshot.revision,
        user_parent_id="user",
        assistant_node_id="assistant",
    )
    incognito = IncognitoStreamAdmission(
        mode="incognito", user_parent_id="user", assistant_node_id="assistant"
    )
    saved_ctx, private_ctx = PipelineContext(), PipelineContext()

    await proxy_router._authorize_ui_persistence(
        SimpleNamespace(ui_persistence=persistent, session_id="ui-s"), saved_ctx, UI_OWNER
    )
    await proxy_router._authorize_ui_persistence(
        SimpleNamespace(ui_persistence=incognito, session_id="ui-s"), private_ctx, UI_OWNER
    )

    assert saved_ctx.get("source_message_id") == "user"
    assert private_ctx.get("source_message_id") is None


# --- the model cannot provide or change the source ------------------------------


async def _execute_public_call(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> tuple[str, AsyncMock]:
    handler = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id", AsyncMock(return_value=None)
    )
    ctx = {
        "identity": AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"),
        "session_id": "trusted-session",
        "auth_level": "Auto",
    }
    return await _execute(monkeypatch, args, ctx, handler), handler


FORGED_SOURCE = {
    "kind": "conversation",
    "session_id": "forged-session",
    "message_id": "forged-message",
    "request_id": "forged-request",
    "captured_at": 1,
    "content_sha256": "0" * 64,
}


@pytest.mark.parametrize(
    "args",
    [
        {"action": "save", "content": "x", "source_context": FORGED_SOURCE},
        {"action": "save", "content": "x", "source_context": None},
        {"action": "query", "include_sources": True, "source_resolver": "forged-resolver"},
        {"action": "query", "include_sources": "forged-truthy"},
    ],
)
async def test_model_cannot_spoof_source_context(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> None:
    result, handler = await _execute_public_call(monkeypatch, args)

    handler.assert_not_called()
    payload = json.loads(result)
    assert payload["error_class"] == "validation_error"
    assert "forged" not in result
    assert "Traceback" not in result and "input_value" not in result


async def test_gateway_replaces_forged_source_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def backend(*, action: str, **kwargs: Any) -> str:
        seen.append({"action": action, **kwargs})
        return "ok"

    monkeypatch.setattr(proxy_memory_gateway, "external_memory_tools", backend)
    soul = _soul()

    await proxy_memory_gateway.memory_tools(
        action="save", content="x", identity=soul, source_context=FORGED_SOURCE
    )
    await proxy_memory_gateway.memory_tools(
        action="query", include_sources=True, identity=soul, source_resolver="forged-resolver"
    )
    await proxy_memory_gateway.memory_tools(
        action="query", identity=soul, source_resolver="forged-resolver"
    )

    save, wanted, plain = seen
    assert "source_context" not in save
    assert isinstance(wanted["source_resolver"], resolver_mod.MemorySourceResolver)
    assert "source_resolver" not in plain


# --- querying: sources come back as a status line, never the original text --------


async def test_source_locator_roundtrip(
    sandbox_db: Path, store: fm.FactualStore, vector: Any
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "my sister lives in Tainan")
    fact_id = _fact_id(await _save(soul, session_id, _ctx(node_id), "user's sister: Tainan"))

    plain = await _query(soul)
    found = await _query(soul, include_sources=True)
    vector.query_vectors.return_value = [{"fact_id": fact_id, "_score": 0.91}]
    semantic = await _query(soul, query="family", include_sources=True)
    await session_service.upsert_session(
        session_id, "T", _tree(node_id, "my sister moved to Taipei"), identity=soul
    )
    edited = await _query(soul, include_sources=True)

    assert "來源" not in plain
    for reply in (found, semantic, edited):
        assert "user's sister: Tainan" in reply
        assert session_id in reply and node_id in reply
        assert "my sister" not in reply  # status only, never the original text
    assert STILL_THERE in found and "對話" in found
    assert STILL_THERE in semantic and "信心度" in semantic  # came back through the index
    assert CHANGED in edited and STILL_THERE not in edited


async def test_orphaned_source_does_not_delete_fact(
    sandbox_db: Path, store: fm.FactualStore
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "remember my birthday is in May")
    fact_id = _fact_id(await _save(soul, session_id, _ctx(node_id), "birthday: May"))
    before = _stored_sources(store, fact_id)
    assert await session_service.delete_session(session_id)

    reply = await _query(soul, include_sources=True)

    assert GONE in reply and "birthday: May" in reply
    assert [fact["id"] for fact in store.query_facts(soul)] == [fact_id]
    assert _stored_sources(store, fact_id) == before  # reading never rewrites the fact


async def test_memory_source_resolves_legacy_message(
    sandbox_db: Path, store: fm.FactualStore, legacy_dir: Path
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "we adopted a dog named Mochi")
    _fact_id(await _save(soul, session_id, _ctx(node_id), "dog: Mochi"))
    other_id, other_node = await _seed_session(soul, "the dog sleeps a lot")
    _fact_id(await _save(soul, other_id, _ctx(other_node), "dog sleeps a lot"))
    legacy = LegacyStore(str(legacy_dir / "legacy_history.db"))
    legacy.upsert_session(session_id, soul, "export.json", "T", 1, 1)
    legacy.upsert_messages(
        [
            {
                "id": node_id,
                "session_id": session_id,
                "role": "user",
                "content": "we adopted a dog named Mochi",
                "timestamp": 1,
            },
            {  # same message id under another session: not this source
                "id": other_node,
                "session_id": session_id,
                "role": "user",
                "content": "the dog sleeps a lot",
                "timestamp": 1,
            },
        ]
    )
    for trimmed in (session_id, other_id):
        assert await session_service.delete_session(trimmed)

    reply = await _query(soul, include_sources=True)

    lines = [line for line in reply.splitlines() if "來源" in line]
    [archived] = [line for line in lines if session_id in line]
    [mismatched] = [line for line in lines if other_id in line]
    assert STILL_THERE in archived
    assert GONE in mismatched


async def test_legacy_get_message_reads_one_message_without_creating_the_db(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy_history.db"
    legacy = LegacyStore(str(path))
    assert legacy.get_message("m1") is None
    assert not path.exists()
    legacy.upsert_session("s1", _soul(), "f.json", "T", 1, 1)
    legacy.upsert_messages(
        [{"id": "m1", "session_id": "s1", "role": "user", "content": "hello", "timestamp": 5}]
    )

    assert legacy.get_message("m1") == {
        "id": "m1",
        "session_id": "s1",
        "role": "user",
        "content": "hello",
    }
    assert legacy.get_message("m2") is None


async def test_legacy_fact_shows_unknown_source(
    sandbox_db: Path, store: fm.FactualStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul()
    await _save(soul, None, None, "saved by an older path")
    fresh = await _query(soul, include_sources=True)
    _make_v0_vault(tmp_path / "old_vault", soul.agent_id, ["remembered before the upgrade"])
    upgraded = fm.FactualStore(base_dir=tmp_path / "old_vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", upgraded)
    old = await _query(soul, include_sources=True)

    for reply in (fresh, old):
        assert NOT_RECORDED in reply
        assert "上線前" not in reply and UNREADABLE_NOW not in reply
    assert "remembered before the upgrade" in old


async def test_sources_unreadable_while_store_is_still_v0(
    store: fm.FactualStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul()
    _make_v0_vault(tmp_path / "old_vault", soul.agent_id, ["kept while the upgrade waits"])
    full_disk = SimpleNamespace(total=10**12, used=10**12, free=0)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: full_disk)
    stuck = fm.FactualStore(base_dir=tmp_path / "old_vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", stuck)
    assert stuck.current_schema_status() is fm.FactsSchemaStatus.NO_SPACE

    reply = await _query(soul, include_sources=True)
    semantic_hit = [{"fact_id": "fact_old0", "_score": 0.9}]
    MemoryManager._vector_store_singleton.query_vectors.return_value = semantic_hit
    semantic = await _query(soul, query="upgrade pause", include_sources=True)

    for text in (reply, semantic):
        assert "kept while the upgrade waits" in text
        assert UNREADABLE_NOW in text and NOT_RECORDED not in text


async def test_resolver_error_shows_unconfirmed_without_failing_query(
    sandbox_db: Path, store: fm.FactualStore, manager: MemoryManager
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "my favourite tea is oolong")
    await _save(soul, session_id, _ctx(node_id), "tea: oolong")

    class _BrokenResolver:
        async def resolve(self, _identity: Any, _source: SourceRef) -> SourceStatus:
            raise OSError

    broken = await manager.query_memory(
        soul, include_sources=True, source_resolver=_BrokenResolver()
    )
    without = await manager.query_memory(soul, include_sources=True, source_resolver=None)

    for reply in (broken, without):
        assert "tea: oolong" in reply and UNCONFIRMED in reply
        assert STILL_THERE not in reply and GONE not in reply


async def test_malformed_sources_do_not_break_queries(
    store: fm.FactualStore, manager: MemoryManager
) -> None:
    soul = _soul()
    fact_id = store.save_fact(soul, "a fact whose sources were damaged")["id"]
    conn = sqlite.connect(store.db_path)
    try:
        conn.execute("UPDATE facts SET sources = ? WHERE id = ?", ('{"kind": "x"', fact_id))
        conn.commit()
    finally:
        conn.close()

    reply = await manager.query_memory(soul, include_sources=True, source_resolver=None)
    exported = json.loads(await memory_gateway.export_facts(soul, manager=manager))

    assert "a fact whose sources were damaged" in reply
    assert f"來源：{UNCONFIRMED}" in reply and NOT_RECORDED not in reply
    assert exported["facts"][0]["sources"] == []


async def test_resolve_checks_session_ownership(
    sandbox_db: Path, manager: MemoryManager, frozen_clock: int
) -> None:
    owner, stranger = _soul("Owner"), _soul("Stranger")
    session_id, node_id = await _seed_session(owner, "only for the owner")
    resolver = resolver_mod.MemorySourceResolver()

    mine = await resolver.capture(
        owner, _ctx(node_id), session_id=session_id, request_id="r1"
    )
    theirs = await resolver.capture(
        stranger, _ctx(node_id), session_id=session_id, request_id="r1"
    )

    assert mine is not None and theirs is None
    assert await resolver.resolve(owner, mine) is SourceStatus.AVAILABLE
    assert await resolver.resolve(stranger, mine) is SourceStatus.MISSING


async def test_gdpr_export_carries_sources_as_plain_dicts(
    sandbox_db: Path, store: fm.FactualStore, manager: MemoryManager, frozen_clock: int
) -> None:
    soul = _soul()
    session_id, node_id = await _seed_session(soul, "export me")
    fact_id = _fact_id(await _save(soul, session_id, _ctx(node_id), "exported fact"))

    exported = json.loads(await memory_gateway.export_facts(soul, manager=manager))

    [fact] = exported["facts"]
    assert fact["id"] == fact_id
    assert fact["sources"] == _stored_sources(store, fact_id)
    assert fact["sources"][0]["content_sha256"] == _sha("export me")


def test_reads_follow_the_schema_version(
    store: fm.FactualStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul()
    fact_id = store.save_fact(soul, "current schema fact")["id"]
    _make_v0_vault(tmp_path / "old_vault", soul.agent_id, ["old schema fact"])
    full_disk = SimpleNamespace(total=10**12, used=10**12, free=0)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: full_disk)
    stuck = fm.FactualStore(base_dir=tmp_path / "old_vault")

    current = [
        *store.query_facts(soul),
        *store.query_all_facts(),
        store.find_active_fact_by_content(soul, "current schema fact"),
        *store.get_active_facts(soul, [fact_id]).values(),
    ]
    old = [
        *stuck.query_facts(soul),
        *stuck.query_all_facts(),
        stuck.find_active_fact_by_content(soul, "old schema fact"),
        *stuck.get_active_facts(soul, ["fact_old0"]).values(),
    ]

    assert len(current) == 4 and all(fact["sources"] is None for fact in current)
    assert len(old) == 4 and all("sources" not in fact for fact in old)


def test_store_newer_than_the_program_neither_reads_nor_writes_sources(tmp_path: Path) -> None:
    soul = _soul()
    vault = tmp_path / "newer_vault"
    fm.FactualStore(base_dir=vault)
    conn = sqlite.connect(vault / "core_memory.db")
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    newer = fm.FactualStore(base_dir=vault)
    assert newer.current_schema_status() is fm.FactsSchemaStatus.NEWER
    source = SourceRef.model_validate(FORGED_SOURCE)

    saved = newer.save_fact(soul, "saved while running an older program", source=source)

    assert saved["status"] == "success"
    assert _stored_sources(newer, saved["id"]) is None  # same as any downgrade-period save
    assert all("sources" not in fact for fact in newer.query_facts(soul))


def test_source_ref_is_closed_and_frozen() -> None:
    ref = SourceRef.model_validate(FORGED_SOURCE)

    assert ref.model_dump(mode="json") == FORGED_SOURCE
    for broken in (
        {**FORGED_SOURCE, "excerpt": "the original text"},
        {**FORGED_SOURCE, "content_sha256": "A" * 64},
        {**FORGED_SOURCE, "content_sha256": "0" * 63},
        {**FORGED_SOURCE, "kind": "tool_result"},
    ):
        with pytest.raises(ValidationError):
            SourceRef.model_validate(broken)
    with pytest.raises(ValidationError):
        ref.session_id = "another-session"


# --- premise: a persistent UI turn saves the user node before the model runs ------


@pytest.fixture
def ui_buffer(monkeypatch: pytest.MonkeyPatch) -> StreamBufferService:
    monkeypatch.setattr(queue_mod, "get_current_epoch", lambda: 1000.0)
    queue = InMemoryInterjectionQueue()
    monkeypatch.setattr(dispatcher_mod, "interjection_queue", queue)
    buffer = StreamBufferService(interjections=queue)
    monkeypatch.setattr(dispatcher_mod, "stream_buffer_service", buffer)
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    return buffer


@pytest.mark.parametrize("user_node", ["user", "never-saved"])
async def test_persistent_ui_turn_saves_the_user_node_before_generation(
    sandbox_db: Path, ui_buffer: StreamBufferService, user_node: str
) -> None:
    await session_service.upsert_session(
        "ui-s", "UI", copy.deepcopy(UI_TREE), owner_user_id=UI_OWNER.agent_id
    )
    snapshot = await session_service.read_persistence_snapshot("ui-s", UI_OWNER)
    admission = PersistedStreamAdmission(
        mode="persistent",
        revision=snapshot.revision,
        user_parent_id=user_node,
        assistant_node_id="assistant",
    )
    entry = await ui_buffer.create("ui-s", identity=UI_OWNER, admission=admission)
    seen: list[str] = []

    async def generation():
        saved = await session_service.get_session_by_id("ui-s")
        seen.append(saved["messages"]["messages"].get(user_node, {}).get("content"))
        yield TEXT_CHUNK

    await dispatcher_mod._pump_ui_stream(
        generation(), "ui-s", entry.stream_id, "m", UI_OWNER, admission
    )

    if user_node == "user":
        assert seen == ["hi"]  # the model ran only after the user node was saved
        assert entry.status == "done"
    else:
        assert seen == []  # an unsaved user node never reaches the model
        assert entry.status == "error"
