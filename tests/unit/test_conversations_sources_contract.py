"""Expanded sources preserve the existing public argument contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from memory_server.tools.contracts import MemoryConversationsInput
from pydantic import ValidationError
from src.tools.contracts import native_parameters, validate_native_arguments
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES

from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.conversation_search import denied, search, success
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


@pytest.mark.parametrize(
    "arguments", [{}, {"source": None}, {"source": "all"}, {"source": "ui"}, {"source": "legacy"}]
)
async def test_source_selection_reads_exactly_the_requested_stores(
    conversation_db: ConversationDB, arguments: dict[str, Any]
) -> None:
    await conversation_db.ui("ui", "ui-visible")
    conversation_db.legacy("legacy-visible")
    result = await search(**arguments)
    source = arguments.get("source")
    success(result, 2 if source in (None, "all") else 1)
    assert ("ui-visible" in result) == (source != "legacy")
    assert ("legacy-visible" in result) == (source != "ui")
    header = result.split("<historical_data", 1)[0]
    assert ("ui" in header) == (source != "legacy")
    assert ("legacy" in header) == (source != "ui")


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": "both"},
        {"source": "Both"},
        {"source": "UI"},
        {"page_size": 0},
        {"page_size": 21},
        {"page_size": "10"},
        {"limit": 5},
        {"offset": -1},
        {"role": "system"},
        {"date_start": "2026-02-30"},
        {"date_start": "2026-5-13"},
        {"date_end": "2026/05/13"},
        {"date_start": "2026-05-14", "date_end": "2026-05-13"},
        {"target": "ScopeB"},
        {"session_id": "other"},
        {"cursor": "opaque"},
        {"bypass": True},
    ],
)
def test_invalid_public_arguments_remain_validation_errors(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MemoryConversationsInput.model_validate({"action": "conversations", **arguments})


def test_schema_keeps_field_names_and_page_limits() -> None:
    fields = set(MemoryConversationsInput.model_fields)
    assert fields == {
        "action",
        "source",
        "query",
        "role",
        "date_start",
        "date_end",
        "page_size",
        "offset",
    }
    assert not fields & EXECUTION_CONTEXT_KWARG_NAMES
    schema = native_parameters("memory_tools")["properties"]["page_size"]
    assert (schema["minimum"], schema["maximum"], schema["default"]) == (1, 20, 10)
    parsed = MemoryConversationsInput.model_validate({"action": "conversations"})
    assert parsed.source is None and parsed.offset == 0 and parsed.query == ""


def test_public_validation_reports_field_without_reading_a_store() -> None:
    _, error = validate_native_arguments(
        "memory_tools", {"action": "conversations", "page_size": 21}
    )
    assert error is not None
    denied(error, "validation_error")
    assert "page_size" in error


def test_role_description_does_not_claim_verified_authorship() -> None:
    description = MemoryConversationsInput.model_fields["role"].description
    assert "助理（assistant）" in description
    assert "你自己" not in description
