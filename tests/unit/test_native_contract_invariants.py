"""Native contract invariants pinned against the real runtime tables."""

from __future__ import annotations

from pathlib import Path

import guard_native_tool_contracts as guard
from memory_server.tools import VAULT_ACTIONS
from memory_server.tools.contracts import MEMORY_INTERNAL_ACTIONS
from src.services.guard_service import (
    MEMORY_QUERY_SIGNAL,
    MEMORY_SAVE_SIGNAL,
    VERIFICATION_TOOLS,
)
from src.tools.builtin import tool_registry
from src.tools.contracts import PUBLIC_TOOL_CONTRACTS, native_parameters
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESERVED_FIELDS = set(EXECUTION_CONTEXT_KWARG_NAMES) | {
    "resource_proxy",
    "manager",
    "source_context",
    "source_resolver",
    "safety_flags",
    "superseded_by",
}


def test_memory_internal_actions_exact() -> None:
    """Adding a model-hidden Memory action must change this test, so review sees it."""
    assert frozenset({"export_facts"}) == MEMORY_INTERNAL_ACTIONS
    source = (PROJECT_ROOT / guard.MEMORY_CONTRACTS).read_text(encoding="utf-8")
    assert guard.assigned_string_set(source, "MEMORY_INTERNAL_ACTIONS") == {"export_facts"}


def test_real_public_schemas_do_not_advertise_runtime_fields() -> None:
    for name in PUBLIC_TOOL_CONTRACTS:
        assert guard.reserved_field_errors(native_parameters(name), RESERVED_FIELDS) == [], name
    poisoned = native_parameters("memory_tools")
    poisoned["properties"]["session_id"] = {"type": "string"}
    assert guard.reserved_field_errors(poisoned, RESERVED_FIELDS)


def test_memory_signal_actions_exist_in_contracts() -> None:
    for qualified in set(VERIFICATION_TOOLS) | {MEMORY_SAVE_SIGNAL, MEMORY_QUERY_SIGNAL}:
        tool, _, action = qualified.partition(".")
        if not action:
            assert tool in tool_registry, qualified
        elif tool == "vault_tools":
            assert action in VAULT_ACTIONS, qualified
        else:
            assert action in PUBLIC_TOOL_CONTRACTS[tool], qualified
