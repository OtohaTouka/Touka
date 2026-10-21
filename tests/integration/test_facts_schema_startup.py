"""Proxy startup and the memory store schema: one upgrade, visible failures, honest reads.

Each test points the memory data root at tmp_path and resets the shared stores before and
after. Stores are synthetic; the vector store is a stub, so no embedding model is loaded.
"""

from __future__ import annotations

import asyncio
import errno
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import memory_server.tools as memory_tools_module
import pytest
from fastapi.testclient import TestClient
from memory_server import factual as fm
from memory_server.services.manager import MemoryManager
from memory_server.vector import VectorStore
from src import main as main_module
from src.main import app
from src.models.identity import AgentIdentity
from src.services.bot_service import bot_service
from src.services.identity_router import get_verified_identity
from src.tools.builtin import memory as proxy_memory_gateway

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

sqlite = fm.sqlite3

AGENT = "StartupProbe"
IDENTITY = AgentIdentity.for_role(agent_id=AGENT, role="Owner")
OWNER = AgentIdentity.for_role(agent_id="owner-test", role="Owner")
GARBAGE = b"this is not a sqlite database " * 64
STARTUP_MUST_NOT_BUILD_MANAGER = "startup must not build the memory manager"
SCHEMA_NOTICE_CODES = {
    "memory_upgrade_no_space",
    "memory_upgrade_locked",
    "memory_store_unreadable",
    "memory_upgrade_failed",
}
OLD_CREATE = (
    "CREATE TABLE facts (id TEXT PRIMARY KEY, agent_id TEXT, content TEXT, tags TEXT,"
    " importance TEXT DEFAULT NULL, emotions TEXT DEFAULT NULL, created_at INTEGER,"
    " updated_at INTEGER, archived INTEGER DEFAULT 0)"
)
OLD_INSERT = (
    "INSERT INTO facts (id, agent_id, content, tags, created_at, updated_at, archived)"
    " VALUES (?, ?, ?, '[]', 1700000000, 1700000000, 0)"
)


def _make_v0_db(path: Path, content: str = "remembered from before") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(path)
    try:
        conn.execute(OLD_CREATE)
        conn.execute(OLD_INSERT, ("fact_old", AGENT, content))
        conn.commit()
    finally:
        conn.close()
    return path


def _version(path: Path) -> int:
    conn = sqlite.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _corrupt_after_header(db: Path) -> bytes:
    """Keep page 1 (header and schema), overwrite every later page; return the healthy bytes."""
    healthy = db.read_bytes()
    conn = sqlite.connect(db)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()
    assert len(healthy) > page_size
    db.write_bytes(healthy[:page_size] + b"t" * (len(healthy) - page_size))
    return healthy


def _backups(vault: Path) -> list[str]:
    directory = vault / "core_memory_backups"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def _schema_notices(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [notice for notice in body["memory_notices"] if notice["code"] in SCHEMA_NOTICE_CODES]


def _call_gateway(action: str, **kwargs: Any) -> str:
    call = proxy_memory_gateway.memory_tools(action=action, identity=IDENTITY, **kwargs)
    return asyncio.run(call)


@pytest.fixture
def startup_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
    MemoryManager.reset_singletons()
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    yield tmp_path / "vault"
    MemoryManager.reset_singletons()


@pytest.fixture
def vector_stub(startup_vault: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    vector = create_autospec(VectorStore, instance=True)
    vector.save_vector.return_value = True
    vector.query_vectors.return_value = []
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)
    return vector


def test_s1_startup_upgrades_once_before_any_background_work(
    startup_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _make_v0_db(startup_vault / "core_memory.db")
    order: list[str] = []
    original_shared = fm.FactualStore.shared
    original_spawn = main_module.spawn_logged
    original_start_bots = bot_service.start_bots

    def shared(_cls: type) -> Any:
        order.append("factual_store")
        return original_shared()

    def spawn_logged(coro: Any, name: str) -> Any:
        order.append(f"spawn:{name}")
        return original_spawn(coro, name=name)

    async def start_bots() -> None:
        order.append("start_bots")
        await original_start_bots()

    monkeypatch.setattr(fm.FactualStore, "shared", classmethod(shared))
    monkeypatch.setattr(main_module, "spawn_logged", spawn_logged)
    monkeypatch.setattr(bot_service, "start_bots", start_bots)

    with TestClient(app):
        pass

    first = _backups(startup_vault)
    assert _version(db) == fm._target_version()
    assert len(first) == 1
    opened = order.index("factual_store")
    first_spawn = next(i for i, step in enumerate(order) if step.startswith("spawn:"))
    assert opened < first_spawn
    assert opened < order.index("start_bots")

    MemoryManager.reset_singletons()
    with TestClient(app):
        pass

    assert _backups(startup_vault) == first


def test_s2_failed_startup_upgrade_keeps_queries_and_refuses_saves(
    startup_vault: Path, monkeypatch: pytest.MonkeyPatch, vector_stub: Any
) -> None:
    db = _make_v0_db(startup_vault / "core_memory.db")
    full_disk = SimpleNamespace(total=10**12, used=10**12, free=0)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: full_disk)

    with TestClient(app):
        store = app.state.context["memory_factual_store"]
        assert store.current_schema_status() is fm.FactsSchemaStatus.NO_SPACE

    found = _call_gateway("query", query="")
    saved = _call_gateway("save", content="a new fact")

    assert "remembered from before" in found
    payload = json.loads(saved)
    assert next(iter(payload)) == "error_class"
    assert payload["error_class"] == "internal_error"
    assert "sqlite" not in saved.lower()
    assert "database" not in saved.lower()
    assert _version(db) == 0


def _no_space(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    _make_v0_db(vault / "core_memory.db")
    space = {"free": 0}
    monkeypatch.setattr(
        fm.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10**12, used=0, free=space["free"]),
    )
    return lambda: space.update(free=10**12)


def _locked(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    db = _make_v0_db(vault / "core_memory.db")
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.05)
    holder = sqlite.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder.close


def _unreadable(vault: Path, _monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    vault.mkdir(parents=True)
    db = vault / "core_memory.db"
    db.write_bytes(GARBAGE)

    def repair() -> None:
        db.unlink()
        _make_v0_db(db)

    return repair


def _failed_step(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    _make_v0_db(vault / "core_memory.db")
    steps = fm.FACTS_SCHEMA_STEPS
    version = fm._target_version() + 1  # always one past the real steps
    broken = fm._SchemaStep(
        version=version,
        set_version_sql=f"PRAGMA user_version = {version}",
        add_columns=(("broken", "ALTER TABLE no_such_table ADD COLUMN broken TEXT"),),
    )
    monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", (*steps, broken))
    return lambda: monkeypatch.setattr(fm, "FACTS_SCHEMA_STEPS", steps)


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (_no_space, "memory_upgrade_no_space"),
        (_locked, "memory_upgrade_locked"),
        (_unreadable, "memory_store_unreadable"),
        (_failed_step, "memory_upgrade_failed"),
    ],
    ids=["no_space", "locked", "unreadable", "failed"],
)
def test_s3_each_failure_shows_its_warning_until_the_store_is_upgraded(
    startup_vault: Path, monkeypatch: pytest.MonkeyPatch, setup: Any, code: str
) -> None:
    repair = setup(startup_vault, monkeypatch)
    repaired_once = False
    try:
        with TestClient(app) as client:
            failing = client.get("/v1/memory").json()
            repair()
            repaired_once = True
            fm.FactualStore(base_dir=startup_vault)  # another store finishes the upgrade
            repaired = client.get("/v1/memory").json()
    finally:
        if not repaired_once:
            repair()

    assert _schema_notices(failing) == [{"code": code, "level": "warning"}]
    assert failing["system_degraded"] is False
    assert _schema_notices(repaired) == []


def test_s3b_current_store_shows_no_schema_notice(startup_vault: Path) -> None:
    with TestClient(app) as client:
        body = client.get("/v1/memory").json()

    assert _schema_notices(body) == []


def test_s3c_store_that_cannot_be_opened_shows_unreadable(
    startup_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_cls: type) -> Any:
        raise PermissionError(errno.EACCES, "cannot create the vault")

    original_shared = fm.FactualStore.shared
    monkeypatch.setattr(fm.FactualStore, "shared", classmethod(refuse))

    with TestClient(app) as client:
        body = client.get("/v1/memory").json()
        context = app.state.context
        monkeypatch.setattr(fm.FactualStore, "shared", original_shared)
        fm.FactualStore.shared()  # the first later use builds it after all
        later = client.get("/v1/memory").json()

    assert _schema_notices(body) == [{"code": "memory_store_unreadable", "level": "warning"}]
    assert "memory_factual_store" not in context
    assert _schema_notices(later) == []


def test_s4_startup_opens_the_store_without_the_memory_manager(
    startup_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(STARTUP_MUST_NOT_BUILD_MANAGER)

    monkeypatch.setattr(memory_tools_module, "get_manager", forbidden)
    monkeypatch.setattr(MemoryManager, "__init__", forbidden)

    with TestClient(app):
        store = app.state.context.get("memory_factual_store")

    assert store is not None
    assert store.current_schema_status() is fm.FactsSchemaStatus.CURRENT


def test_s5_unreadable_store_query_is_a_structured_error(
    startup_vault: Path, vector_stub: Any
) -> None:
    startup_vault.mkdir(parents=True)
    (startup_vault / "core_memory.db").write_bytes(GARBAGE)
    assert fm.FactualStore.shared().current_schema_status() is fm.FactsSchemaStatus.UNREADABLE

    result = _call_gateway("query", query="")

    assert "No relevant memories found." not in result
    payload = json.loads(result)
    assert next(iter(payload)) == "error_class"
    assert payload["error_class"] == "internal_error"


def test_s6_store_broken_after_startup_is_shown_and_never_reads_as_empty(
    startup_vault: Path, vector_stub: Any
) -> None:
    with TestClient(app) as client:
        store = app.state.context["memory_factual_store"]
        store.save_fact(IDENTITY, "saved before the file broke")
        healthy = client.get("/v1/memory").json()
        (startup_vault / "core_memory.db").write_bytes(GARBAGE)

        result = _call_gateway("query", query="")
        broken = client.get("/v1/memory").json()
        listing = client.get("/v1/memory/list")

    assert _schema_notices(healthy) == []
    assert "No relevant memories found." not in result
    assert next(iter(json.loads(result))) == "error_class"
    assert store.current_schema_status() is fm.FactsSchemaStatus.UNREADABLE
    assert _schema_notices(broken) == [{"code": "memory_store_unreadable", "level": "warning"}]
    assert listing.status_code == 500
    assert "memories" not in listing.json()


def test_s6b_after_an_embedding_switch_the_live_store_drives_the_notice(
    startup_vault: Path, vector_stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        startup_store = app.state.context["memory_factual_store"]
        MemoryManager.reset_singletons()  # what switching the embedding model does
        monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector_stub)
        _call_gateway("save", content="saved after the switch")
        assert fm.FactualStore.shared() is not startup_store
        (startup_vault / "core_memory.db").write_bytes(GARBAGE)

        result = _call_gateway("query", query="")
        broken = client.get("/v1/memory").json()

    assert next(iter(json.loads(result))) == "error_class"
    assert _schema_notices(broken) == [{"code": "memory_store_unreadable", "level": "warning"}]


def test_s7_broken_pages_behind_a_good_header_keep_the_notice_until_a_read_works(
    startup_vault: Path, vector_stub: Any
) -> None:
    unreadable = [{"code": "memory_store_unreadable", "level": "warning"}]
    with TestClient(app) as client:
        store = app.state.context["memory_factual_store"]
        for index in range(30):
            store.save_fact(IDENTITY, f"fact {index} " + "x" * 200)
        db = startup_vault / "core_memory.db"
        healthy = _corrupt_after_header(db)

        failed_query = _call_gateway("query", query="")
        polls = [client.get("/v1/memory").json() for _poll in range(3)]
        blocked = _call_gateway("save", content="must not be written while reads fail")
        db.write_bytes(healthy)
        before_read = client.get("/v1/memory").json()
        found = _call_gateway("query", query="")
        after_read = client.get("/v1/memory").json()

    assert next(iter(json.loads(failed_query))) == "error_class"
    assert [_schema_notices(poll) for poll in polls] == [unreadable] * 3
    assert next(iter(json.loads(blocked))) == "error_class"
    assert _schema_notices(before_read) == unreadable
    assert "fact " in found
    assert _schema_notices(after_read) == []
