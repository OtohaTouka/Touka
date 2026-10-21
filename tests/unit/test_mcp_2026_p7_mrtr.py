"""ADR-0205 / MCP 2026 P7：ephemeral MRTR、HITL 與每輪重新授權。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.interaction import (
    InProcessMcpInteractionBroker,
    McpInteractionSink,
    bind_mcp_interaction,
)
from src.protocols.mcp.mrtr import (
    McpInteractionTimeoutError,
    McpInteractionUnsupportedError,
    McpMrtrAuthorizationError,
    McpMrtrBinding,
    McpMrtrDriver,
    McpMrtrLimitError,
    McpMrtrLimits,
    McpMrtrRoundsExceededError,
)
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.services.approval_service import ApprovalService


def _context(*, origin: str = "foreground") -> McpRequestContext:
    return McpRequestContext(
        request_id="p7-request",
        session_id="p7-session",
        identity=AgentIdentity.for_role("P7Owner", "Owner"),
        human_principal_id="human-p7",
        installation_id="srv",
        auth_level="Manual",
        origin=origin,
    )


def _binding(method: str = "tools/call") -> McpMrtrBinding:
    return McpMrtrBinding(
        request_context=_context(),
        installation_generation=1,
        operation_method=method,
        operation_name="demo",
        arguments_digest="sha256:test-only",
    )


def _input_required(state: str = "opaque-secret-state") -> types.InputRequiredResult:
    return types.InputRequiredResult(
        inputRequests={
            "question": types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message="Choose a value",
                    requestedSchema={
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                        },
                        "required": ["answer"],
                    },
                )
            )
        },
        requestState=state,
    )


class _Broker:
    def __init__(self, result: types.InputResponse) -> None:
        self.result = result
        self.bindings: list[McpMrtrBinding] = []

    async def respond(
        self,
        _key: str,
        _request: types.InputRequest,
        binding: McpMrtrBinding,
    ) -> types.InputResponse:
        self.bindings.append(binding)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["tools/call", "prompts/get", "resources/read"],
)
async def test_three_operations_echo_opaque_state_and_accept_form_input(method: str):
    broker = _Broker(
        types.ElicitResult(action="accept", content={"answer": "yes"})
    )
    authorizations: list[str] = []
    seen: list[tuple[types.InputResponses | None, str | None]] = []

    async def _authorize(binding: McpMrtrBinding) -> None:
        authorizations.append(binding.operation_method)

    async def _retry(
        responses: types.InputResponses | None,
        state: str | None,
    ) -> str:
        seen.append((responses, state))
        return "complete"

    result = await McpMrtrDriver(
        broker=broker,
        authorize=_authorize,
    ).drive(_input_required(), retry=_retry, binding=_binding(method))

    assert result == "complete"
    assert seen[0][1] == "opaque-secret-state"
    assert seen[0][0] == {
        "question": types.ElicitResult(
            action="accept",
            content={"answer": "yes"},
        )
    }
    assert authorizations == [method]
    assert broker.bindings[0].request_context is _binding(method).request_context or (
        broker.bindings[0].request_context == _binding(method).request_context
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_decline_and_cancel_are_returned_to_server_without_invented_content(
    action: str,
):
    broker = _Broker(types.ElicitResult(action=action))
    seen: list[types.InputResponses | None] = []

    async def _retry(
        responses: types.InputResponses | None,
        _state: str | None,
    ) -> str:
        seen.append(responses)
        return "complete"

    await McpMrtrDriver(
        broker=broker,
        authorize=lambda _binding: asyncio.sleep(0),
    ).drive(_input_required(), retry=_retry, binding=_binding())

    response = seen[0]
    assert response is not None
    assert response["question"].action == action
    assert response["question"].content is None


@pytest.mark.asyncio
async def test_timeout_aborts_without_retrying_upstream():
    class _TimeoutBroker:
        async def respond(self, *_args: Any) -> types.InputResponse:
            raise McpInteractionTimeoutError()

    retried = False

    async def _retry(
        _responses: types.InputResponses | None,
        _state: str | None,
    ) -> str:
        nonlocal retried
        retried = True
        return "complete"

    with pytest.raises(McpInteractionTimeoutError):
        await McpMrtrDriver(
            broker=_TimeoutBroker(),
            authorize=lambda _binding: asyncio.sleep(0),
        ).drive(_input_required(), retry=_retry, binding=_binding())
    assert not retried


@pytest.mark.asyncio
async def test_round_cap_is_enforced_before_unbounded_retry():
    broker = _Broker(types.ElicitResult(action="decline"))

    async def _retry(
        _responses: types.InputResponses | None,
        _state: str | None,
    ) -> types.InputRequiredResult:
        return _input_required("next-state")

    with pytest.raises(McpMrtrRoundsExceededError):
        await McpMrtrDriver(
            broker=broker,
            authorize=lambda _binding: asyncio.sleep(0),
            limits=McpMrtrLimits(max_rounds=1),
        ).drive(_input_required(), retry=_retry, binding=_binding())


@pytest.mark.asyncio
async def test_permission_revocation_after_human_response_blocks_retry():
    broker = _Broker(
        types.ElicitResult(action="accept", content={"answer": "yes"})
    )

    async def _revoked(_binding: McpMrtrBinding) -> None:
        raise McpMrtrAuthorizationError()

    retried = False

    async def _retry(
        _responses: types.InputResponses | None,
        _state: str | None,
    ) -> str:
        nonlocal retried
        retried = True
        return "complete"

    with pytest.raises(McpMrtrAuthorizationError):
        await McpMrtrDriver(
            broker=broker,
            authorize=_revoked,
        ).drive(_input_required(), retry=_retry, binding=_binding())
    assert not retried


@pytest.mark.asyncio
async def test_headless_elicitation_declines_without_registering_interaction():
    service = ApprovalService()
    broker = InProcessMcpInteractionBroker(service)
    result = await broker.respond(
        "question",
        _input_required().input_requests["question"],
        McpMrtrBinding(
            request_context=_context(origin="heartbeat"),
            installation_generation=1,
            operation_method="tools/call",
            operation_name="demo",
            arguments_digest="sha256:test-only",
        ),
    )
    assert isinstance(result, types.ElicitResult)
    assert result.action == "decline"
    assert service.pending_count() == 0


@pytest.mark.asyncio
async def test_sampling_and_roots_surface_unsupported_instead_of_silent_decline():
    broker = InProcessMcpInteractionBroker(ApprovalService())
    requests: list[types.InputRequest] = [
        types.CreateMessageRequest(
            params=types.CreateMessageRequestParams(
                messages=[],
                maxTokens=1,
            )
        ),
        types.ListRootsRequest(),
    ]
    for request in requests:
        with pytest.raises(McpInteractionUnsupportedError):
            await broker.respond("unsupported", request, _binding())


@pytest.mark.asyncio
async def test_nested_elicitation_schema_is_rejected_before_ui_projection():
    broker = InProcessMcpInteractionBroker(ApprovalService())
    sink = McpInteractionSink(
        queue=asyncio.Queue(),
        call_id="call-p7",
        tool_name="srv__demo",
        agent_id="P7Owner",
        session_id="p7-session",
    )
    request = types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message="Nested forms are unsupported",
            requestedSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                },
            },
        )
    )

    with (
        bind_mcp_interaction(sink),
        pytest.raises(McpInteractionUnsupportedError),
    ):
        await broker.respond("nested", request, _binding())


@pytest.mark.asyncio
async def test_interaction_event_uses_only_local_id_and_never_exposes_request_state():
    service = ApprovalService()
    broker = InProcessMcpInteractionBroker(service)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    sink = McpInteractionSink(
        queue=queue,
        call_id="call-p7",
        tool_name="srv__demo",
        agent_id="P7Owner",
        session_id="p7-session",
    )
    with bind_mcp_interaction(sink):
        task = asyncio.create_task(
            broker.respond(
                "question",
                _input_required().input_requests["question"],
                _binding(),
            )
        )
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event is not None
        assert event["type"] == "mcp_interaction_request"
        assert event["id"].startswith("mcpint_")
        assert "opaque-secret-state" not in str(event)
        assert "requestState" not in str(event)
        assert "request_state" not in str(event)
        assert await service.resolve(
            event["id"],
            decision="accepted",
            resolver="human-p7",
            response_payload={"answer": "yes"},
        )
        result = await asyncio.wait_for(task, timeout=1)
    assert isinstance(result, types.ElicitResult)
    assert result.action == "accept"
    assert result.content == {"answer": "yes"}


@pytest.mark.asyncio
async def test_state_and_response_bytes_have_hard_limits():
    broker = _Broker(
        types.ElicitResult(action="accept", content={"answer": "too large"})
    )
    driver = McpMrtrDriver(
        broker=broker,
        authorize=lambda _binding: asyncio.sleep(0),
        limits=McpMrtrLimits(max_state_bytes=4, max_response_bytes=4),
    )

    with pytest.raises(McpMrtrLimitError):
        await driver.drive(
            _input_required("state-too-large"),
            retry=lambda _responses, _state: asyncio.sleep(0, result="complete"),
            binding=_binding(),
        )


class _LowLevelOperationSession:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.calls: list[tuple[types.InputResponses | None, str | None]] = []

    async def call_tool(
        self,
        _name: str,
        _arguments: dict[str, Any],
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
        allow_input_required: bool = False,
        allow_claimed: bool = False,
        **_kwargs: Any,
    ) -> types.CallToolResult | types.InputRequiredResult:
        assert allow_input_required
        assert allow_claimed
        self.calls.append((input_responses, request_state))
        if input_responses is None:
            return _input_required()
        return types.CallToolResult(
            content=[types.TextContent(text="tool-complete")],
        )

    async def read_resource(
        self,
        _uri: str,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
        allow_input_required: bool = False,
        **_kwargs: Any,
    ) -> types.ReadResourceResult | types.InputRequiredResult:
        assert allow_input_required
        self.calls.append((input_responses, request_state))
        if input_responses is None:
            return _input_required()
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri="touka://resource",
                    text="resource-complete",
                    mimeType="text/plain",
                )
            ]
        )

    async def get_prompt(
        self,
        _name: str,
        _arguments: dict[str, str] | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
        allow_input_required: bool = False,
        **_kwargs: Any,
    ) -> types.GetPromptResult | types.InputRequiredResult:
        assert allow_input_required
        self.calls.append((input_responses, request_state))
        if input_responses is None:
            return _input_required()
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(text="prompt-complete"),
                )
            ]
        )


class _HighLevelClient:
    protocol_version = "2026-07-28"
    server_capabilities = SimpleNamespace(
        tools=SimpleNamespace(list_changed=False),
        resources=SimpleNamespace(list_changed=False, subscribe=False),
        prompts=SimpleNamespace(list_changed=False),
    )

    def __init__(self, operation: str) -> None:
        self.session = _LowLevelOperationSession(operation)

    async def list_tools(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="demo",
                    title=None,
                    description="",
                    input_schema={"type": "object", "properties": {}},
                    output_schema=None,
                    annotations=None,
                    icons=[],
                    meta=None,
                )
            ],
            next_cursor=None,
        )

    async def call_tool(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("P7 must not use SDK high-level automatic retry")

    async def read_resource(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("P7 must not use SDK high-level automatic retry")

    async def get_prompt(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("P7 must not use SDK high-level automatic retry")


def _manager(
    operation: str,
    *,
    mrtr_authorizer: Any | None = None,
) -> tuple[ProtocolManager, _HighLevelClient]:
    client = _HighLevelClient(operation)
    registry = ProtocolRegistry("__missing_p7_registry__.json")
    registry.servers = {"srv": ProtocolServerConfig(command="echo")}
    manager = ProtocolManager(
        registry,
        mrtr_broker=_Broker(
            types.ElicitResult(action="accept", content={"answer": "yes"})
        ),
        mrtr_authorizer=mrtr_authorizer or (
            lambda _binding: asyncio.sleep(0)
        ),
    )
    manager.handle_pool.put(
        manager.handle_key_for("srv", _context()),
        LegacyMcpHandle(session=client),
    )
    return manager, client


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tool", "resource", "prompt"])
async def test_manager_uses_low_level_driver_for_every_interactive_operation(
    operation: str,
):
    manager, client = _manager(operation)
    if operation == "tool":
        result = await manager.call_tool_on_server(
            "srv",
            "demo",
            {},
            request_context=_context(),
        )
        assert result.content[0]["text"] == "tool-complete"
    elif operation == "resource":
        result = await manager.read_server_resource(
            "srv",
            "touka://resource",
            request_context=_context(),
        )
        assert result.contents[0].text == "resource-complete"
    else:
        result = await manager.get_server_prompt(
            "srv",
            "demo",
            {},
            request_context=_context(),
        )
        assert result.messages[0].content.text == "prompt-complete"

    assert len(client.session.calls) == 2
    assert client.session.calls[1][1] == "opaque-secret-state"


@pytest.mark.asyncio
async def test_tool_retry_authorization_error_stays_typed_and_does_not_open_breaker():
    async def _revoked(_binding: McpMrtrBinding) -> None:
        raise McpMrtrAuthorizationError()

    manager, _client = _manager("tool", mrtr_authorizer=_revoked)

    # 本機授權拒絕不是上游故障；即使連續超過 breaker threshold，
    # 每次仍應回精確 authorization error，而不是被包成 operation/circuit error。
    for _attempt in range(4):
        with pytest.raises(McpMrtrAuthorizationError):
            await manager.call_tool_on_server(
                "srv",
                "demo",
                {},
                request_context=_context(),
            )
