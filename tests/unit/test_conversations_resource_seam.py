"""Memory reads UI conversations only through the resource protocol."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.config_utils import get_resource_proxy
from memory_server.protocols import IResourceProxy
from src.services.resource_manager import resource_manager
from src.utils import async_db

from tests.support.conversation_search import DAY, DAY_END, denied, search, success, tree
from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.session_scope import A
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


async def test_bound_resource_forwards_real_reader_parameters_and_identity(
    conversation_db: ConversationDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol_method = getattr(IResourceProxy, "query_ui_conversations", None)
    method = getattr(async_db.session_service, "query_ui_conversations", None)
    assert callable(protocol_method), "IResourceProxy lacks the UI conversation query contract"
    assert callable(method), "SessionService lacks the UI conversation reader"
    assert get_resource_proxy() is resource_manager
    calls = []

    async def observed(*args: Any, **kwargs: Any) -> Any:
        calls.append(inspect.signature(method).bind(*args, **kwargs).arguments)
        return await method(*args, **kwargs)

    monkeypatch.setattr(async_db.session_service, "query_ui_conversations", observed)
    messages = tree("irrelevant", "RAIN 100%_\\")
    await conversation_db.ui("one", "one", messages=messages)
    await conversation_db.ui("two", "two", messages=messages)
    result = await search(
        source="ui",
        query="100%_\\",
        role="assistant",
        date_start="2026-05-13",
        date_end="2026-05-13",
        page_size=1,
        offset=1,
    )
    success(result, 2)
    assert "RAIN 100%_\\" in result and len(calls) == 1
    assert calls[0] == {
        "identity": A,
        "query": "100%_\\",
        "role": "assistant",
        "date_start": DAY,
        "date_end": DAY_END,
        "oldest_first": True,
        "limit": 1,
        "offset": 1,
    }
    assert calls[0]["identity"] is A


async def test_unbound_reader_fails_loudly_without_legacy_fallback(
    conversation_db: ConversationDB,
) -> None:
    binder = getattr(resource_manager, "bind_ui_conversation_reader", None)
    assert callable(binder), "ResourceManager lacks explicit reader binding"
    await conversation_db.ui("one", "ui-visible")
    conversation_db.legacy("legacy-visible")
    bound = await search(source="all")
    binder(None)
    try:
        result = await search(source="all")
        denied(result, "internal_error")
        assert "visible" not in result
    finally:
        binder(async_db.session_service)
    success(bound, 2)


async def test_search_uses_selects_without_session_initialization(
    conversation_db: ConversationDB,
) -> None:
    await conversation_db.ui("one", "visible")
    conversation_db.statements.clear()
    result = await search(source="ui")
    success(result, 1)
    assert "visible" in result
    statements = [sql.lstrip().upper() for sql in conversation_db.statements]
    assert any(sql.startswith("SELECT") for sql in statements)
    assert all(sql.startswith(("SELECT", "BEGIN", "COMMIT", "ROLLBACK")) for sql in statements)
    assert not any(sql.startswith("BEGIN IMMEDIATE") for sql in statements)


@pytest.mark.parametrize("state", ["absent", "directory", "corrupt"])
async def test_store_absence_is_distinct_from_invalid_database(
    conversation_db: ConversationDB, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    path = conversation_db.path.parent / "missing-parent" / "sessions.db"
    if state == "directory":
        path.mkdir(parents=True)
    elif state == "corrupt":
        path.parent.mkdir()
        path.write_bytes(b"invalid sqlite database")
    monkeypatch.setattr(async_db, "get_db_path", lambda: path)
    result = await search(source="ui")
    if state == "absent":
        success(result, 0)
        assert not path.parent.exists()
        assert conversation_db.statements == []
    else:
        denied(result, "internal_error")
