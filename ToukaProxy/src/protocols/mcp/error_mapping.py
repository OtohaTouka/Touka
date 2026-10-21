"""MCP failure → Touka ErrorCode／模型面 error class 的單一映射。"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import ErrorCode
from src.protocols.mcp.canonical import McpCanonicalError, McpErrorKind

_ERROR_CLASS_INTERNAL = "internal_error"
_ERROR_CLASS_UPSTREAM = "upstream_error"

_MAPPINGS: dict[McpErrorKind, tuple[ErrorCode, str]] = {
    McpErrorKind.PROTOCOL: (ErrorCode.SYSTEM_ERROR, _ERROR_CLASS_UPSTREAM),
    McpErrorKind.TRANSPORT: (
        ErrorCode.NETWORK_CONNECTION_ERROR,
        _ERROR_CLASS_UPSTREAM,
    ),
    McpErrorKind.TOOL_RESULT: (
        ErrorCode.VALIDATION_ERROR,
        _ERROR_CLASS_UPSTREAM,
    ),
}


def map_mcp_error(
    kind: McpErrorKind,
    message: str,
    *,
    source_protocol_version: str,
    protocol_code: int | None = None,
    protocol_data: Any | None = None,
) -> McpCanonicalError:
    """建立 canonical error；所有 MCP 失敗分類只允許經此入口。"""
    touka_code, tool_error_class = _MAPPINGS.get(
        kind,
        (ErrorCode.SYSTEM_ERROR, _ERROR_CLASS_INTERNAL),
    )
    return McpCanonicalError(
        kind=kind,
        message=message,
        source_protocol_version=source_protocol_version,
        touka_error_code=touka_code.value,
        tool_error_class=tool_error_class,
        protocol_code=protocol_code,
        protocol_data=protocol_data,
    )
