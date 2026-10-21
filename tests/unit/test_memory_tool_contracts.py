"""Public memory arguments and query-to-archive contract, with isolated stores."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from memory_server import tools as memory_gateway
from memory_server.services.manager import MemoryManager
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA
from src.models.identity import AgentIdentity


@pytest.mark.parametrize(
    "field",
    [
        "emotions",
        "date_start",
        "date_end",
        "is_recent",
        "fact_id_to_archive",
        "operation",
        "target_id",
        "include_history",
        "content",
    ],
)
def test_memory_public_schema_exposes_supported_field(field: str) -> None:
    assert field in MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]


def test_memory_public_actions_include_duplicates_but_not_export() -> None:
    actions = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
    assert "manage_dups" in actions
    assert "export_facts" not in actions


def test_shared_memory_field_descriptions_retain_action_specific_meanings() -> None:
    description = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["importance"][
        "description"
    ]
    assert "save:" in description and "query:" in description
    content = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["content"]
    assert "save:" in content["description"] and "manage_dups:" in content["description"]
    operations = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["operation"]
    assert operations["enum"] == ["scan", "resolve", "supersede"]


def test_action_description_retains_required_hints_without_combinators() -> None:
    description = MEMORY_TOOLS_SCHEMA["function"]["parameters"]["properties"]["action"][
        "description"
    ]
    assert "save" in description and "content" in description
    assert "forget" in description and "fact_id_to_archive" in description


@pytest.mark.asyncio
async def test_query_exposes_canonical_factual_ids_for_exact_and_semantic_hits() -> None:
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    manager = MemoryManager.__new__(MemoryManager)
    factual = MagicMock()
    vector = MagicMock()
    factual.query_facts.return_value = [
        {"id": "fact-exact", "content": "exact", "created_at": 1700000000}
    ]
    # Semantic hits are rechecked against the fact store; this one is still active there.
    factual.get_active_facts.return_value = {
        "fact-semantic": {"id": "fact-semantic", "content": "semantic", "created_at": 1700000000}
    }
    vector.query_vectors.return_value = [
        {
            "id": "vector-point-not-a-fact",
            "fact_id": "fact-semantic",
            "content": "semantic",
            "created_at": 1700000000,
        }
    ]
    manager.get_factual_store = lambda: factual
    manager.get_vector_store = lambda: vector

    result = await manager.query_memory(identity=identity, query="needle", limit=2)

    assert "ID: fact-exact" in result
    assert "ID: fact-semantic" in result
    assert "vector-point-not-a-fact" not in result
    assert factual.query_facts.call_args.kwargs["identity"] is identity
    assert vector.query_vectors.call_args.kwargs["identity"] is identity


@pytest.mark.asyncio
async def test_manage_dups_resolve_uses_distinct_public_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    manager = create_autospec(MemoryManager, instance=True)
    manager.normalize_agent_id.return_value = identity.agent_id
    manager.find_duplicates.return_value = []
    manager.resolve_duplicate.return_value = "archived selected fact"
    monkeypatch.setattr(memory_gateway, "_global_manager", manager)

    result = await memory_gateway.memory_tools(
        action="manage_dups", operation="resolve", target_id="selected-fact", identity=identity
    )

    assert result == "archived selected fact"
    manager.resolve_duplicate.assert_called_once_with(identity, "selected-fact")
    manager.find_duplicates.assert_not_called()


@pytest.mark.asyncio
async def test_memory_query_passes_nondefault_filters_to_both_stores() -> None:
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    manager = MemoryManager.__new__(MemoryManager)
    factual = MagicMock()
    vector = MagicMock()
    factual.query_facts.return_value = []
    vector.query_vectors.return_value = []
    manager.get_factual_store = lambda: factual
    manager.get_vector_store = lambda: vector
    manager.normalize_time_range = lambda start, end, recent: (1700000001, 1700000400)
    values: dict[str, Any] = {
        "query": "needle",
        "limit": 2,
        "tags": ["tag-a"],
        "importance": "4",
        "emotions": ["感謝", "custom-emotion"],
        "date_start": "2026-09-10",
        "date_end": "2026-09-11",
        "is_recent": True,
    }

    await manager.query_memory(identity=identity, **values)

    for store in (factual.query_facts, vector.query_vectors):
        actual = store.call_args.kwargs
        assert actual["identity"] is identity
        assert actual["tags_any"] == ["tag-a"]
        assert actual["emotions_any"] == ["感謝", "custom-emotion"]
        assert actual["importance"] == "4"
        assert (actual["date_start"], actual["date_end"]) == (1700000001, 1700000400)
