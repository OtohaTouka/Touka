"""Real session and legacy stores shared by conversation search acceptance tests."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest_asyncio
from memory_server import tools as memory_gateway
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.manager import MemoryManager
from src.services.resource_manager import resource_manager
from src.tools.builtin import memory as gateway
from src.utils import async_db

from tests.support.session_scope import A
from tests.support.zone_roots import point_zone_roots

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import SimpleNamespace

    import pytest
    from src.models.identity import AgentIdentity

DAY = 1_778_601_600_000
DAY_END = DAY + 86_400_000 - 1


def tree(*texts: str, timestamp: int | None = DAY) -> dict[str, Any]:
    """A committed branch with deterministic IDs and message timestamps."""
    nodes = {}
    for index, text in enumerate(texts):
        key = f"m{index}"
        nodes[key] = {
            "id": key,
            "parentId": f"m{index - 1}" if index else None,
            "role": "user" if index % 2 == 0 else "assistant",
            "content": text,
            "timestamp": timestamp,
        }
    return {"messages": nodes, "currentId": f"m{len(texts) - 1}" if texts else None}


async def search(identity: AgentIdentity = A, **kwargs: Any) -> str:
    """Use the public Proxy gateway, including Memory validation and rendering."""
    return await gateway.memory_tools(action="conversations", identity=identity, **kwargs)


def success(result: str, total: int) -> None:
    assert not result.startswith('{"error_class"'), result
    assert re.search(rf"共 {total} 則(?:，|\b)", result), result


def denied(result: str, error_class: str = "permission_denied") -> None:
    assert result.startswith('{"error_class"'), result
    assert json.loads(result)["error_class"] == error_class, result
    assert "historical_data" not in result
    assert "offset=" not in result
    assert "共 " not in result


def unknown_count(result: str, count: int) -> None:
    assert re.search(rf"無法按日期判定[：: ]*{count}(?:\D|$)", result), result


@dataclass
class ConversationDB:
    path: Path
    manager: MemoryManager
    statements: list[str] = field(default_factory=list)
    legacy_reads: list[str] = field(default_factory=list)

    async def ui(
        self,
        sid: str,
        text: str,
        *,
        identity: AgentIdentity = A,
        marker: Any = 1,
        home: str | None = None,
        is_home: bool = False,
        owner: str | None = "scope-human",
        messages: Any = None,
        title: str | None = None,
        timestamp: int | None = DAY,
    ) -> None:
        """Seed corrupt and historical metadata without weakening production writers."""
        payload = tree(text, timestamp=timestamp) if messages is None else messages
        async with async_db.get_async_db() as db:
            await db.execute(
                "INSERT INTO user_sessions "
                "(id, title, messages, updatedAt, agent_id, owner_user_id, "
                "session_scope_version, is_home, home_for_agent, last_activity_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    title or f"title-{text}",
                    json.dumps(payload),
                    DAY,
                    identity.agent_id,
                    owner,
                    marker,
                    int(is_home),
                    home,
                    DAY,
                ),
            )
            await db.commit()

    def legacy(
        self,
        text: str,
        *,
        identity: AgentIdentity = A,
        mid: str = "legacy-m",
        sid: str = "legacy-s",
        timestamp: int = DAY,
        title: str = "legacy-title",
    ) -> Path:
        store = self.manager.get_legacy_sync_service().get_store(identity)
        path = Path(store.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store.upsert_session(sid, identity, "archive.json", title, DAY, DAY)
        store.upsert_messages(
            [
                {
                    "id": mid,
                    "session_id": sid,
                    "role": "user",
                    "content": text,
                    "timestamp": timestamp,
                },
            ]
        )
        return path

    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        async with async_db.get_async_db() as db:
            await db.execute(sql, parameters)
            await db.commit()


@pytest_asyncio.fixture
async def conversation_db(
    scope_db: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ConversationDB]:
    """Only relocate storage and bind real implementations; no reader substitute."""
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    manager = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", manager)
    fixture = ConversationDB(scope_db.path, manager)
    original = async_db.get_async_db

    @asynccontextmanager
    async def traced_db() -> AsyncIterator[Any]:
        async with original() as db:
            await db.set_trace_callback(fixture.statements.append)
            yield db

    monkeypatch.setattr(async_db, "get_async_db", traced_db)
    original_legacy_query = LegacyStore.query_messages

    def observed_legacy_query(store: LegacyStore, *args: Any, **kwargs: Any) -> Any:
        fixture.legacy_reads.append(store.db_path)
        return original_legacy_query(store, *args, **kwargs)

    monkeypatch.setattr(LegacyStore, "query_messages", observed_legacy_query)
    binder = getattr(resource_manager, "bind_ui_conversation_reader", None)
    if callable(binder):
        binder(async_db.session_service)
    try:
        yield fixture
    finally:
        if callable(binder):
            binder(None)
