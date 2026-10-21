"""Payload-free MCP OpenTelemetry attribute boundary."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "mcp.method",
        "mcp.transport",
        "mcp.protocol.version",
        "mcp.cache.status",
        "mcp.subscription.status",
        "error.type",
    }
)
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")


def sanitize_span_attributes(attributes: dict[str, Any]) -> dict[str, str | bool | int]:
    """Drop all payload/peer-context fields and bound remaining scalar values."""
    sanitized: dict[str, str | bool | int] = {}
    for key, value in attributes.items():
        if key not in _ATTRIBUTE_ALLOWLIST:
            continue
        if isinstance(value, bool | int) or (
            isinstance(value, str) and _SAFE_VALUE_RE.fullmatch(value)
        ):
            sanitized[key] = value
    return sanitized


class McpTelemetry:
    """One payload-free OTel instrumentation layer; exporter ownership stays external."""

    def __init__(self) -> None:
        self._tracer = trace.get_tracer("touka.mcp")

    @contextmanager
    def span(
        self,
        method: str,
        *,
        enabled: bool,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        """Start a root span without trusting peer baggage/tracestate/parent."""
        if not enabled:
            yield
            return
        safe = sanitize_span_attributes(
            {
                "mcp.method": method,
                **(attributes or {}),
            }
        )
        with self._tracer.start_as_current_span(
            "mcp.request",
            context=Context(),
            attributes=safe,
            record_exception=False,
        ) as span:
            try:
                yield
            except Exception as exc:
                error_type = type(exc).__name__
                if _SAFE_VALUE_RE.fullmatch(error_type):
                    span.set_attribute("error.type", error_type)
                span.set_status(Status(StatusCode.ERROR))
                raise
