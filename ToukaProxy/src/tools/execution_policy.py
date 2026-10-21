"""Pure handler-bound classification for execution approval exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class ToolExecutionClass(StrEnum):
    """Classification never comes from a model-supplied argument."""

    ORDINARY = "ordinary"
    TERMINAL = "terminal"
    SELF_APPROVING = "self_approving"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class ExecutionBinding:
    """Bind an exceptional canonical operation to its registered callable."""

    canonical_name: str
    handler: Callable[..., Any]
    kind: ToolExecutionClass


def classify_registered_tool(
    name: str,
    handler: Callable[..., Any] | None,
    bindings: Mapping[str, ExecutionBinding],
) -> ToolExecutionClass:
    """Unknown handlers remain ordinary; drift of an exceptional binding denies."""
    binding = bindings.get(name)
    if binding is None:
        return ToolExecutionClass.ORDINARY
    if handler is not binding.handler:
        return ToolExecutionClass.MISMATCH
    return binding.kind
