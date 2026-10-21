"""MCP MRTR → Touka 行程內 interaction broker。"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from mcp import types
from src.protocols.mcp.canonical import McpRequestOrigin
from src.protocols.mcp.mrtr import (
    McpInteractionTimeoutError,
    McpInteractionUnsupportedError,
)
from src.protocols.mcp.schema_validation import (
    McpSchemaError,
    normalize_mcp_elicitation_schema,
)
from src.services.approval_service import approval_service
from src.utils.tool_events import (
    mcp_interaction_request_event,
    mcp_interaction_resolved_event,
)

if TYPE_CHECKING:
    import asyncio

    from src.protocols.mcp.mrtr import McpMrtrBinding
    from src.services.approval_service import ApprovalService

MCP_INTERACTION_TIMEOUT_SECONDS = 120.0


async def decline_unbound_elicitation(
    _context: Any,
    _params: Any,
) -> types.ElicitResult:
    """SDK capability callback：缺精確 operation context 時立即 decline。"""
    return types.ElicitResult(action="decline")


@dataclass(frozen=True)
class McpInteractionSink:
    """當前聊天 SSE 與原工具步驟的行程內綁定。"""

    queue: asyncio.Queue[dict[str, Any] | None]
    call_id: str
    tool_name: str
    agent_id: str
    session_id: str | None


_current_sink: contextvars.ContextVar[McpInteractionSink | None] = (
    contextvars.ContextVar("mcp_interaction_sink", default=None)
)


@contextmanager
def bind_mcp_interaction(sink: McpInteractionSink) -> Iterator[None]:
    """只在當前 coroutine 綁聊天 interaction sink。"""
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)


class InProcessMcpInteractionBroker:
    """以既有 ApprovalService 提供 schema/payload 型 ephemeral HITL。"""

    def __init__(self, service: ApprovalService | None = None) -> None:
        self._service = service or approval_service

    async def respond(
        self,
        _key: str,
        request: types.InputRequest,
        binding: McpMrtrBinding,
    ) -> types.InputResponse:
        """投影可支援的 form；headless decline，其餘 unsupported fail-loud。"""
        if not isinstance(request, types.ElicitRequest):
            raise McpInteractionUnsupportedError()
        params = request.params
        if not isinstance(params, types.ElicitRequestFormParams):
            raise McpInteractionUnsupportedError()
        sink = _current_sink.get()
        if (
            binding.request_context.origin != McpRequestOrigin.FOREGROUND
            or sink is None
        ):
            return types.ElicitResult(action="decline")

        try:
            schema = normalize_mcp_elicitation_schema(params.requested_schema)
        except McpSchemaError as exc:
            raise McpInteractionUnsupportedError() from exc
        exec_id = f"mcpint_{uuid.uuid4().hex[:12]}"
        registered = await self._service.register(
            exec_id,
            tool_name=sink.tool_name,
            agent_id=sink.agent_id,
            session_id=sink.session_id,
            arguments_preview={},
            interaction_kind="mcp_elicitation",
            interaction_schema=schema,
            interaction_payload={
                "mode": "form",
                "message": params.message,
            },
        )
        if not registered:
            return types.ElicitResult(action="decline")

        await sink.queue.put(
            mcp_interaction_request_event(
                interaction_id=exec_id,
                call_id=sink.call_id,
                tool=sink.tool_name,
                message=params.message,
                schema=schema,
                timeout_s=MCP_INTERACTION_TIMEOUT_SECONDS,
            )
        )
        resolution = await self._service.wait_for_resolution(
            exec_id,
            timeout_s=MCP_INTERACTION_TIMEOUT_SECONDS,
        )
        await sink.queue.put(
            mcp_interaction_resolved_event(
                interaction_id=exec_id,
                call_id=sink.call_id,
                tool=sink.tool_name,
                decision=resolution.decision,
            )
        )
        if resolution.decision == "timeout":
            raise McpInteractionTimeoutError()
        if resolution.decision == "declined":
            return types.ElicitResult(action="decline")
        if resolution.decision == "cancelled":
            return types.ElicitResult(action="cancel")

        content = resolution.response_payload or {}
        validator = Draft202012Validator(schema)
        if not validator.is_valid(content):
            raise McpInteractionUnsupportedError()
        return types.ElicitResult(action="accept", content=content)
