"""Touka security policy layered over SDK-owned ``x-mcp-header`` handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcp.shared.inbound import (
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)

MAX_MCP_PARAM_HEADERS = 16
MAX_MCP_PARAM_NAME_BYTES = 64
MAX_MCP_PARAM_VALUE_BYTES = 2048
MAX_MCP_PARAM_AGGREGATE_BYTES = 8192

_SENSITIVE_FIELD_RE = re.compile(
    r"(?:auth|bearer|cookie|credential|email|key|password|phone|prompt|query|"
    r"secret|session|ssn|token)",
    re.IGNORECASE,
)


class McpHeaderPolicyError(ValueError):
    """Invalid or unapproved ``x-mcp-header`` contract."""


@dataclass(frozen=True)
class McpToolHeaderPolicy:
    """Validated SDK header map plus Touka sensitivity classification."""

    header_map: dict[tuple[str, ...], str]
    sensitive: bool


def inspect_tool_header_policy(
    input_schema: dict[str, Any],
    *,
    approved_for_sensitive: bool,
) -> McpToolHeaderPolicy:
    """Validate annotations and enforce bounded, per-tool owner approval."""
    if reason := find_invalid_x_mcp_header(input_schema):
        raise McpHeaderPolicyError(reason)
    header_map = x_mcp_header_map(input_schema)
    if len(header_map) > MAX_MCP_PARAM_HEADERS:
        message = f"x-mcp-header count exceeds {MAX_MCP_PARAM_HEADERS}"
        raise McpHeaderPolicyError(message)
    for token in header_map.values():
        if len(token.encode("ascii")) > MAX_MCP_PARAM_NAME_BYTES:
            message = (
                f"x-mcp-header name bytes exceed {MAX_MCP_PARAM_NAME_BYTES}"
            )
            raise McpHeaderPolicyError(message)
    sensitive = any(
        _SENSITIVE_FIELD_RE.search(".".join((*path, token))) is not None
        for path, token in header_map.items()
    )
    if sensitive and not approved_for_sensitive:
        message = "sensitive x-mcp-header requires per-tool owner approval"
        raise McpHeaderPolicyError(message)
    return McpToolHeaderPolicy(header_map=header_map, sensitive=sensitive)


def validate_header_arguments(
    policy: McpToolHeaderPolicy,
    arguments: dict[str, Any],
) -> None:
    """Bound encoded header bytes before the SDK touches the transport."""
    headers = mcp_param_headers(policy.header_map, arguments)
    if len(headers) > MAX_MCP_PARAM_HEADERS:
        message = f"Mcp-Param header count exceeds {MAX_MCP_PARAM_HEADERS}"
        raise McpHeaderPolicyError(message)
    total = 0
    for name, value in headers.items():
        name_bytes = len(name.encode("ascii"))
        value_bytes = len(value.encode("ascii"))
        if value_bytes > MAX_MCP_PARAM_VALUE_BYTES:
            # 標頭名稱＝Mcp-Param-＋已驗證的 RFC 9110 token（≤ 64 bytes），可寫進訊息與 log。
            # 不放參數值。
            message = f"{name} value bytes exceed {MAX_MCP_PARAM_VALUE_BYTES}"
            raise McpHeaderPolicyError(message)
        total += name_bytes + value_bytes
    if total > MAX_MCP_PARAM_AGGREGATE_BYTES:
        message = (
            "Mcp-Param aggregate bytes exceed "
            f"{MAX_MCP_PARAM_AGGREGATE_BYTES}"
        )
        raise McpHeaderPolicyError(message)
