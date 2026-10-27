"""MCP 2026 遷移 P1：SDK-neutral boundary、handle 隔離與 launch env 契約。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from src.config import settings
from src.core.exceptions import ErrorCode
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.protocols.manager import ProtocolManager
from src.protocols.mcp.canonical import (
    McpCompleteOutcome,
    McpErrorKind,
    McpRequestContext,
)
from src.protocols.mcp.environment import LAUNCH_ENV_ALLOWLIST, LaunchEnvironmentPolicy
from src.protocols.mcp.error_mapping import map_mcp_error
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.sdk_adapter import (
    UNKNOWN_PROTOCOL_VERSION,
    adapt_call_exception,
    adapt_call_tool_result,
    adapt_tool_descriptor,
)
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.services import tool_errors
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor


class _Content:
    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "type": "text",
            "text": self.text,
            "annotations": {"audience": ["assistant"]},
        }


class _CallResult:
    def __init__(
        self,
        text: str,
        *,
        structured: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = [_Content(text)]
        self.structured_content = structured
        self.is_error = is_error
        self.meta = {"trace": "legacy"}


class _Session:
    def __init__(self, label: str, *, is_error: bool = False) -> None:
        self.label = label
        self.is_error = is_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        # dep1：呼叫當下核對清單，被叫的工具必須在當次清單上。
        pages = [await _CatalogSession(name).list_tools() for name in ("increment", "whoami")]
        return SimpleNamespace(tools=[page.tools[0] for page in pages])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> _CallResult:
        self.calls.append((tool_name, dict(arguments)))
        return _CallResult(
            self.label,
            structured={"label": self.label},
            is_error=self.is_error,
        )


class _CatalogSession:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def list_tools(self) -> Any:
        tool = SimpleNamespace(
            name=self.tool_name,
            title=None,
            description=self.tool_name,
            input_schema={"type": "object"},
            output_schema=None,
            annotations=None,
            icons=[],
            meta=None,
        )
        return SimpleNamespace(tools=[tool])


def _identity(agent_id: str = "AgentA") -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner")


def _context(
    server: str,
    *,
    request_id: str = "req-1",
    human_principal_id: str | None = None,
    identity: AgentIdentity | None = None,
) -> McpRequestContext:
    return McpRequestContext(
        request_id=request_id,
        session_id="session-1",
        identity=identity or _identity(),
        human_principal_id=human_principal_id,
        installation_id=server,
        auth_level="SafeAuto",
        origin="foreground",
    )


def _manager(config: ProtocolServerConfig) -> ProtocolManager:
    registry = ProtocolRegistry("__missing_p1_registry__.json")
    registry.servers = {"srv": config}
    return ProtocolManager(registry)


def test_sdk_adapter_preserves_complete_result_without_text_flattening():
    outcome = adapt_call_tool_result(
        _CallResult("failed upstream", structured={"retryable": False}, is_error=True)
    )

    assert isinstance(outcome, McpCompleteOutcome)
    assert outcome.result_type == "complete"
    assert outcome.source_protocol_version == UNKNOWN_PROTOCOL_VERSION
    assert outcome.is_error is True
    assert outcome.structured_content == {"retryable": False}
    assert outcome.content[0]["annotations"] == {"audience": ["assistant"]}
    assert outcome.meta == {"trace": "legacy"}


def test_sdk_adapter_defaults_missing_result_type_to_complete():
    outcome = adapt_call_tool_result(_CallResult("ok"))
    assert outcome.result_type == "complete"


def test_sdk_tool_descriptor_preserves_sdk_neutral_fields():
    tool = SimpleNamespace(
        name="lookup",
        title="Lookup",
        description="Find a record",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
        icons=[SimpleNamespace(model_dump=lambda **_kwargs: {"src": "icon.svg"})],
        meta={"visibility": "private"},
    )

    descriptor = adapt_tool_descriptor(tool)

    assert descriptor.name == "lookup"
    assert descriptor.title == "Lookup"
    assert descriptor.input_schema == {"type": "object"}
    assert descriptor.output_schema == {"type": "object"}
    assert descriptor.annotations == {"readOnlyHint": True}
    assert descriptor.icons == [{"src": "icon.svg"}]
    assert descriptor.meta == {"visibility": "private"}
    assert descriptor.source_protocol_version == UNKNOWN_PROTOCOL_VERSION


async def test_duplicate_side_effect_calls_execute_twice_without_completed_cache():
    manager = _manager(ProtocolServerConfig(command="echo"))
    context = _context("srv")
    session = _Session("ok")
    manager.handle_pool.put(
        manager.handle_key_for("srv", context),
        LegacyMcpHandle(session=session),
    )

    first = await manager.call_tool_on_server(
        "srv", "increment", {"amount": 1}, request_context=context
    )
    second = await manager.call_tool_on_server(
        "srv", "increment", {"amount": 1}, request_context=context
    )

    assert first.structured_content == {"label": "ok"}
    assert second.structured_content == {"label": "ok"}
    assert session.calls == [
        ("increment", {"amount": 1}),
        ("increment", {"amount": 1}),
    ]


async def test_call_tool_requires_explicit_request_context():
    manager = _manager(ProtocolServerConfig(command="echo"))
    with pytest.raises(TypeError):
        await manager.call_tool_on_server("srv", "lookup", {})  # type: ignore[call-arg]


async def test_ide_bridge_result_also_crosses_canonical_boundary(monkeypatch):
    manager = _manager(ProtocolServerConfig(command="echo"))
    manager._ide_clients = {"ToukaUI": {"url": "loopback", "token": "secret"}}

    async def _call_ide(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "opened"}]}

    monkeypatch.setattr("src.protocols.manager.call_ide_tool", _call_ide)

    result = await manager.call_tool_on_server(
        "ToukaUI",
        "openDiff",
        {},
        request_context=_context("ToukaUI"),
    )

    assert isinstance(result, McpCompleteOutcome)
    assert result.content == [{"type": "text", "text": "opened"}]


async def test_principal_scoped_handles_never_cross_principals():
    manager = _manager(ProtocolServerConfig(command="echo", auth_mode="oauth_user"))
    context_a = _context("srv", request_id="req-a", human_principal_id="human-a")
    context_b = _context("srv", request_id="req-b", human_principal_id="human-b")
    session_a = _Session("a")
    session_b = _Session("b")
    key_a = manager.handle_key_for("srv", context_a)
    key_b = manager.handle_key_for("srv", context_b)
    manager.handle_pool.put(key_a, LegacyMcpHandle(session=session_a))
    manager.handle_pool.put(key_b, LegacyMcpHandle(session=session_b))

    result_a = await manager.call_tool_on_server("srv", "whoami", {}, request_context=context_a)
    result_b = await manager.call_tool_on_server("srv", "whoami", {}, request_context=context_b)

    assert key_a != key_b
    assert result_a.structured_content == {"label": "a"}
    assert result_b.structured_content == {"label": "b"}
    assert len(session_a.calls) == 1
    assert len(session_b.calls) == 1


async def test_principal_scoped_catalog_never_crosses_principals():
    manager = _manager(ProtocolServerConfig(command="echo", auth_mode="oauth_user"))
    context_a = _context("srv", request_id="req-a", human_principal_id="human-a")
    context_b = _context("srv", request_id="req-b", human_principal_id="human-b")
    manager.handle_pool.put(
        manager.handle_key_for("srv", context_a),
        LegacyMcpHandle(session=_CatalogSession("private_a")),
    )
    manager.handle_pool.put(
        manager.handle_key_for("srv", context_b),
        LegacyMcpHandle(session=_CatalogSession("private_b")),
    )

    tools_a = await manager.get_all_tools(context_a)
    tools_b = await manager.get_all_tools(context_b)

    assert [item["tool"] for item in tools_a] == ["private_a"]
    assert [item["tool"] for item in tools_b] == ["private_b"]


async def test_principal_failure_stays_out_of_installation_health():
    manager = _manager(ProtocolServerConfig(command="echo", auth_mode="oauth_user"))
    context = _context("srv", human_principal_id="human-a")
    key = manager.handle_key_for("srv", context)
    manager.handle_pool.put(
        key,
        LegacyMcpHandle(session=_CatalogSession("private_a")),
    )

    manager._record_error("srv", "principal token expired", key=key)
    health = (await manager.get_active_server_info())[0]

    assert health["scope"] == "installation"
    assert health["last_error"] is None
    assert health["handle_count"] == 1
    assert health["handles"][0]["scope"] == "principal"
    assert health["handles"][0]["status"] == "error"
    assert health["handles"][0]["last_error"] == "principal token expired"


async def test_principal_scoped_installation_is_lazy_at_startup(monkeypatch):
    manager = _manager(ProtocolServerConfig(command="echo", auth_mode="oauth_user"))
    connected: list[str] = []

    async def _connect(name: str, *_args: Any, **_kwargs: Any) -> None:
        connected.append(name)

    async def _no_ide() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(manager, "connect_server", _connect)
    monkeypatch.setattr("src.protocols.manager.discover_ide_servers", _no_ide)

    await manager.initialize_all()

    assert connected == []
    assert manager.handle_pool.keys_for_installation("srv") == []


def test_error_mapper_separates_protocol_transport_and_tool_result():
    protocol = map_mcp_error(
        McpErrorKind.PROTOCOL,
        "method failed",
        source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
        protocol_code=-32603,
        protocol_data={"reason": "bad"},
    )
    transport = map_mcp_error(
        McpErrorKind.TRANSPORT,
        "connection refused",
        source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
    )
    tool_result = map_mcp_error(
        McpErrorKind.TOOL_RESULT,
        "tool rejected input",
        source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
    )

    assert protocol.touka_error_code == ErrorCode.SYSTEM_ERROR.value
    assert protocol.protocol_code == -32603
    assert protocol.protocol_data == {"reason": "bad"}
    assert transport.touka_error_code == ErrorCode.NETWORK_CONNECTION_ERROR.value
    assert tool_result.touka_error_code == ErrorCode.VALIDATION_ERROR.value


def test_sdk_exception_adapter_preserves_jsonrpc_error_fields():
    error = adapt_call_exception(
        MCPError(
            -32602,
            "bad params token-secret",
            {"field": "query"},
        ),
        sanitize=lambda message: message.replace("token-secret", "<redacted>"),
    )

    assert error.kind == McpErrorKind.PROTOCOL
    assert error.message == "bad params <redacted>"
    assert error.protocol_code == -32602
    assert error.protocol_data == {"field": "query"}


async def test_executor_builds_request_context_and_is_error_is_not_success(
    monkeypatch,
):
    identity = AgentIdentity.for_role(agent_id="human-a", role="normal", is_human=True)

    class _Registry:
        @staticmethod
        def get_server(_name: str) -> Any:
            return SimpleNamespace(vault_jail_arg=None)

    class _ProtocolManager:
        registry = _Registry()

        def __init__(self) -> None:
            self.contexts: list[McpRequestContext] = []

        @staticmethod
        def resolve_tool(name: str) -> tuple[str, str]:
            return "srv", name

        async def call_tool_on_server(
            self,
            _server: str,
            _tool: str,
            _args: dict[str, Any],
            *,
            request_context: McpRequestContext,
        ) -> McpCompleteOutcome:
            self.contexts.append(request_context)
            return McpCompleteOutcome(
                source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
                content=[{"type": "text", "text": "invalid query"}],
                structured_content={"field": "query"},
                is_error=True,
            )

    manager = _ProtocolManager()
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _context: PolicyDecision(effect="ALLOW", reason="test"),
    )
    monkeypatch.setattr(
        permission_service,
        "is_mcp_tool_allowed",
        lambda *_args: True,
    )
    executor = ToolExecutor(
        protocol_manager=manager,
        tool_registry={},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    result = await executor.execute_to_queue(
        "srv__lookup",
        {"query": ""},
        "request-42",
        {
            "identity": identity,
            "human_principal_id": "human-a",
            "auth_level": None,
            "session_id": "session-42",
            "project_id": "default",
            "resumable_stream": True,
        },
        queue,
    )

    assert tool_errors.is_tool_error(result)
    assert tool_errors.extract_error_class(result) == tool_errors.UPSTREAM_ERROR
    assert len(manager.contexts) == 1
    request_context = manager.contexts[0]
    assert request_context.request_id == "request-42"
    assert request_context.session_id == "session-42"
    assert request_context.identity == identity
    assert request_context.human_principal_id == "human-a"
    assert request_context.installation_id == "srv"
    assert request_context.origin == "foreground"
    assert request_context.auth_level == "Manual"


def test_creation_launch_environment_excludes_parent_secrets_and_adds_spec_env(monkeypatch):
    # iso-c1 缺口 2：試跑環境改用白名單常數（不再注入 base factory）。
    parent = {
        "LANG": "safe",
        "TOUKA_AUTH_SECRET": "touka-secret",
        "DISPATCH_TOKEN": "dispatch-secret",
        "HEARTBEAT_TOKEN": "heartbeat-secret",
        "OPENAI_API_KEY": "provider-secret",
    }
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: dict(parent))

    clean = LaunchEnvironmentPolicy.clean({"CANDIDATE_SETTING": "declared"})

    assert "LANG" in LAUNCH_ENV_ALLOWLIST
    assert clean == {"LANG": "safe", "CANDIDATE_SETTING": "declared"}
    assert not {
        "TOUKA_AUTH_SECRET",
        "DISPATCH_TOKEN",
        "HEARTBEAT_TOKEN",
        "OPENAI_API_KEY",
    }.intersection(clean)


def test_p1_config_fields_have_runtime_and_wire_defaults():
    config = ProtocolServerConfig(command="echo")
    assert config.protocol_mode == "2026-07-28"
    assert config.auth_mode == "none"
    assert config.auth_profile_id is None
    assert config.capability_policy == {}
    assert "sse_compat_enabled" not in ProtocolServerConfig.model_fields
