"""memory_tools(action="conversations"): public contract and source handling.

The action is public, its arguments are validated (Taipei calendar dates with
start <= end, role, page_size 1-20, offset >= 0, source), its field names never collide
with trusted runtime names, and adding it leaves every existing advertised field as it was
(the page size is ``page_size``, not the query action's ``limit``). Omitted source searches
both managed UI conversations and the legacy database.
Runs in the pytest sandbox with synthetic identities and stores.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from memory_server import tools as memory_gateway
from memory_server.tools.contracts import (
    MEMORY_ACTION_CONTRACTS,
    MEMORY_INTERNAL_ACTIONS,
    action_parameters,
)
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA
from pydantic import ValidationError
from src.tools.contracts import native_parameters, validate_native_arguments
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES

from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.conversation_search import search, success
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


CONVERSATION_FIELDS = {
    "action",
    "query",
    "date_start",
    "date_end",
    "role",
    "page_size",
    "offset",
    "source",
}
# Stripped or injected by the Proxy memory gateway; never public argument fields.
GATEWAY_TRUSTED = {
    "identity",
    "manager",
    "resource_proxy",
    "source_context",
    "source_resolver",
    "conversation_reader",
}
def _model() -> Any:
    assert "conversations" in MEMORY_ACTION_CONTRACTS, "no conversations action yet"
    return MEMORY_ACTION_CONTRACTS["conversations"]


# --- the action exists and is public ---------------------------------------------------


def test_conversations_is_a_public_memory_action() -> None:
    _model()
    assert "conversations" in memory_gateway.MEMORY_ACTIONS
    assert "conversations" not in MEMORY_INTERNAL_ACTIONS
    actions = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
    assert "conversations" in actions
    assert "conversations" in native_parameters("memory_tools")["properties"]["action"]["enum"]


def test_conversations_defaults() -> None:
    parsed = _model().model_validate({"action": "conversations"})
    assert parsed.query == ""
    assert parsed.date_start is None and parsed.date_end is None
    assert parsed.role is None
    assert parsed.page_size == 10
    assert parsed.offset == 0
    assert parsed.source is None  # omitted = every source wired now


# --- argument validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"page_size": 1, "offset": 0},
        {"page_size": 20, "offset": 400},
        {"date_start": "2026-05-13", "date_end": "2026-05-13"},
        {"date_start": "2026-05-13"},
        {"date_end": "2026-05-13"},
        {"role": "user"},
        {"role": "assistant"},
        {"query": "rain"},
        {"source": "legacy"},
        {"source": "ui"},
        {"source": "all"},
    ],
)
def test_conversations_accepts_valid_arguments(arguments: dict[str, Any]) -> None:
    _model().model_validate({"action": "conversations", **arguments})


@pytest.mark.parametrize(
    "arguments",
    [
        {"page_size": 0},
        {"page_size": 21},
        {"page_size": "10"},
        {"limit": 5},  # query's field name; conversations uses page_size
        {"offset": -1},
        {"date_start": "2026-5-13"},
        {"date_end": "2026/05/13"},
        {"date_start": "2026-02-30"},
        {"date_start": "2026-05-14", "date_end": "2026-05-13"},
        {"role": "system"},
        {"source": "web"},
    ],
)
def test_conversations_rejects_bad_arguments(arguments: dict[str, Any]) -> None:
    model = _model()
    with pytest.raises(ValidationError):
        model.model_validate({"action": "conversations", **arguments})


def test_conversations_fields_never_collide_with_trusted_names() -> None:
    fields = set(_model().model_fields)
    assert fields == CONVERSATION_FIELDS
    assert not fields & EXECUTION_CONTEXT_KWARG_NAMES
    assert not fields & GATEWAY_TRUSTED


@pytest.mark.parametrize("field", ["agent_id", "identity", "conversation_reader", "session"])
def test_conversations_refuses_fields_it_does_not_declare(field: str) -> None:
    model = _model()
    with pytest.raises(ValidationError):
        model.model_validate({"action": "conversations", field: "SomeoneElse"})


@pytest.mark.parametrize(
    ("arguments", "field"), [({"page_size": 21}, "page_size"), ({"role": "x"}, "role")]
)
def test_proxy_validation_names_the_bad_field(arguments: dict[str, Any], field: str) -> None:
    _model()
    _args, error = validate_native_arguments(
        "memory_tools", {"action": "conversations", **arguments}
    )
    assert error is not None
    payload = json.loads(error)
    assert payload["error_class"] == "validation_error"
    assert field in {issue["field"] for issue in payload["issues"]}


def test_proxy_validation_refuses_a_reversed_date_range() -> None:
    _model()
    _args, error = validate_native_arguments(
        "memory_tools",
        {"action": "conversations", "date_start": "2026-05-14", "date_end": "2026-05-13"},
    )
    assert error is not None
    assert json.loads(error)["error_class"] == "validation_error"


# --- the shared schema: existing actions keep their shapes (setdefault trap avoided) ------


def test_existing_memory_actions_keep_their_advertised_schema() -> None:
    """conversations only adds fields; shared names keep the first action's shape."""
    _model()
    before = action_parameters(
        {name: model for name, model in MEMORY_ACTION_CONTRACTS.items() if name != "conversations"}
    )
    after = native_parameters("memory_tools")
    for key, old in before["properties"].items():
        if key == "action":
            continue
        new = after["properties"][key]
        assert {k: v for k, v in new.items() if k != "description"} == {
            k: v for k, v in old.items() if k != "description"
        }, key
        assert new["description"].startswith(old["description"]), key
    assert after["properties"]["limit"]["default"] == 5
    assert before["oneOf"] == after["oneOf"][: len(before["oneOf"])]


def test_page_size_range_is_advertised() -> None:
    _model()
    page_size = native_parameters("memory_tools")["properties"]["page_size"]
    assert (page_size["minimum"], page_size["maximum"], page_size["default"]) == (1, 20, 10)
    assert "conversations:" in page_size["description"]


# --- source selection includes the managed UI store -----------------------------------


@pytest.mark.parametrize("source", ["ui", "all"])
async def test_wired_sources_search_the_requested_stores(
    source: str, conversation_db: ConversationDB
) -> None:
    await conversation_db.ui("chat", "ui-rain")
    conversation_db.legacy("legacy-rain")
    result = await search(source=source)
    success(result, 1 if source == "ui" else 2)
    assert "ui-rain" in result
    assert ("legacy-rain" in result) == (source == "all")


async def test_omitted_source_searches_both_databases_and_says_so(
    conversation_db: ConversationDB,
) -> None:
    await conversation_db.ui("chat", "ui-rain")
    conversation_db.legacy("legacy-rain")
    omitted = await search()
    explicit = await search(source="all")
    assert omitted == explicit
    success(omitted, 2)
    header, boundary, body = omitted.partition('<historical_data source="conversation_search"')
    assert boundary, omitted
    assert "legacy" in header and "ui" in header
    assert "legacy-rain" in body and "ui-rain" in body
