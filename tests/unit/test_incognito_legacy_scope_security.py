"""Incognito chats cannot reach the legacy conversation folder (hist-bak b; ban 20).

The hole: the builtin file verbs ignore incognito. ``read/list/find/grep`` still offer
``scope="legacy"`` in an incognito chat, read-only calls pass straight through in Manual,
and the old ``vault_tools`` shim counts as listed once every file verb is listed (ADR-0269
successor rule), so an incognito chat can read the raw files under ``legacy/<agent>/``.

Expected fix: ToolLayer offers the file verbs without the legacy scope in incognito, and a
hard call is refused with ``permission_denied`` before the memory engine runs, both at the
verb entry and at the Proxy ``vault_tools`` bridge that the verbs and the shim share.
Normal chats keep the legacy scope.

Written red first (``xfail(strict=True, raises=AssertionError)``, commit 935a6279); the
marks went with the fix. Write-family cases (design decision D1) have their own functions.

Everything runs in the conftest sandbox: the memory engine is an AsyncMock, policies come
from the tracked ``.example`` files, nothing reads data/ or calls out.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers.tool import ToolLayer
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import tool_registry
from src.tools.vault_tools import vault_tools as real_vault_tools

_OWNER = AgentIdentity.for_role(agent_id="IncognitoLegacyOwner", role="Owner")

READ_VERBS = ("read", "list", "find", "grep")
WRITE_VERBS = ("write", "edit", "insert", "undo", "delete")
FILE_VERBS = READ_VERBS + WRITE_VERBS

_VERB_ARGS: dict[str, dict[str, Any]] = {
    "read": {"path": "chat.json"},
    "list": {},
    "find": {"pattern": "*.json"},
    "grep": {"query": "secret"},
    "write": {"path": "chat.json", "content": "c"},
    "edit": {"path": "chat.json", "old_str": "a", "new_str": "b"},
    "insert": {"path": "chat.json", "line": 1, "text": "t"},
    "undo": {"path": "chat.json"},
    "delete": {"path": "chat.json"},
}


@pytest.fixture(autouse=True)
def _real_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other suites may leave a mock under ``vault_tools`` in the shared registry."""
    monkeypatch.setitem(tool_registry, "vault_tools", real_vault_tools)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The Memory vault engine behind the Proxy bridge; reaching it means the read happened."""
    backend = AsyncMock(return_value="--- Read chat.json ---\nan old secret line")
    monkeypatch.setattr("src.tools.vault_tools.memory_vault_impl", backend)
    return backend


def _context(*, incognito: bool, **extra: Any) -> PipelineContext:
    ctx = PipelineContext(identity=_OWNER, agent_id=_OWNER.agent_id, **extra)
    ctx["headers"] = {}
    ctx["incognito"] = incognito
    return ctx


async def _offered(*, incognito: bool) -> dict[str, dict[str, Any]]:
    """File-verb scope properties as ToolLayer offers them this turn."""
    request = OpenAIChatRequest(model="m", messages=[OpenAIMessage(role="user", content="hi")])
    await ToolLayer().process(request, _context(incognito=incognito))
    offered = {
        tool["function"]["name"]: tool["function"]["parameters"]["properties"]["scope"]
        for tool in request.tools or []
        if tool.get("function", {}).get("name") in FILE_VERBS
    }
    assert set(offered) == set(FILE_VERBS)
    return offered


def _assert_refused(result: Any, engine: AsyncMock) -> None:
    engine.assert_not_awaited()
    payload = json.loads(str(result))
    assert payload["error_class"] == "permission_denied"
    assert payload["denied_scope"] == "legacy"


async def _execute(
    name: str, args: dict[str, Any], *, incognito: bool, listed: frozenset[str]
) -> str:
    """Run one call through the model-loop executor (real roles, real .example policy)."""
    executor = ToolExecutor(None, {name: tool_registry[name]}, {}, {}, enforce_advertised=True)
    ctx = _context(
        incognito=incognito,
        auth_level="Manual",
        advertised_tools=AdvertisedTools(native=listed),
    )
    queue: asyncio.Queue = asyncio.Queue()
    return await executor.execute_to_queue(name, dict(args), "req-incog", ctx, queue, call_id="c1")


# --- Gate 1: what the model is offered -----------------------------------------------


@pytest.mark.parametrize("verb", READ_VERBS)
async def test_incognito_read_verbs_do_not_offer_the_legacy_scope(verb: str) -> None:
    scope = (await _offered(incognito=True))[verb]
    assert "legacy" not in scope["enum"]
    assert "legacy" not in scope["description"]
    assert "personal" in scope["enum"]


@pytest.mark.parametrize("verb", WRITE_VERBS)
async def test_incognito_write_verbs_do_not_offer_the_legacy_scope(verb: str) -> None:
    """Decision D1: writing would persist incognito content and edit can probe old text."""
    scope = (await _offered(incognito=True))[verb]
    assert "legacy" not in scope["enum"]
    assert "legacy" not in scope["description"]


async def test_normal_chats_keep_the_legacy_scope_after_an_incognito_turn() -> None:
    """The incognito projection must never edit the shared schema objects."""
    await _offered(incognito=True)
    offered = await _offered(incognito=False)
    for verb in FILE_VERBS:
        assert "legacy" in offered[verb]["enum"], verb


# --- Gate 2: a hard call is refused before the engine --------------------------------


@pytest.mark.parametrize("verb", READ_VERBS)
async def test_incognito_read_verbs_refuse_legacy_before_the_engine(
    verb: str, engine: AsyncMock
) -> None:
    result = await tool_registry[verb](
        scope="legacy", **_VERB_ARGS[verb], identity=_OWNER, _ctx=_context(incognito=True)
    )
    _assert_refused(result, engine)


@pytest.mark.parametrize("verb", WRITE_VERBS)
async def test_incognito_write_verbs_refuse_legacy_before_the_engine(
    verb: str, engine: AsyncMock
) -> None:
    """Decision D1; insert and edit must say permission_denied, not file-not-found."""
    result = await tool_registry[verb](
        scope="legacy", **_VERB_ARGS[verb], identity=_OWNER, _ctx=_context(incognito=True)
    )
    _assert_refused(result, engine)


async def test_incognito_vault_tools_shim_refuses_legacy(engine: AsyncMock) -> None:
    result = await tool_registry["vault_tools"](
        vault="legacy",
        action="read",
        path="chat.json",
        identity=_OWNER,
        _ctx=_context(incognito=True),
    )
    _assert_refused(result, engine)


@pytest.mark.parametrize("verb", READ_VERBS)
async def test_normal_chats_still_reach_the_legacy_engine(verb: str, engine: AsyncMock) -> None:
    await tool_registry[verb](
        scope="legacy", **_VERB_ARGS[verb], identity=_OWNER, _ctx=_context(incognito=False)
    )
    engine.assert_awaited_once()
    assert engine.await_args.kwargs["vault"] == "legacy"


# --- end to end: read-only calls pass PaC in Manual, so only the new check stops them -----


async def test_executor_refuses_incognito_read_of_legacy(engine: AsyncMock) -> None:
    result = await _execute(
        "read",
        {"scope": "legacy", "path": "chat.json"},
        incognito=True,
        listed=frozenset({"read"}),
    )
    _assert_refused(result, engine)


async def test_executor_refuses_incognito_shim_listed_through_its_successors(
    engine: AsyncMock,
) -> None:
    result = await _execute(
        "vault_tools",
        {"vault": "legacy", "action": "read", "path": "chat.json"},
        incognito=True,
        listed=frozenset(FILE_VERBS),
    )
    _assert_refused(result, engine)


async def test_executor_still_reads_legacy_in_normal_chats(engine: AsyncMock) -> None:
    await _execute(
        "read",
        {"scope": "legacy", "path": "chat.json"},
        incognito=False,
        listed=frozenset({"read"}),
    )
    engine.assert_awaited_once()
