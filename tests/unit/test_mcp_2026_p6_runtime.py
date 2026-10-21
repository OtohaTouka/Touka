"""ADR-0204 / MCP 2026 P6：cache、subscriptions、headers、icons 與 telemetry。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types
from mcp.client import CacheConfig
from mcp.client.caching import (
    CacheEntry,
    CacheKey,
    ClientResponseCache,
)
from mcp.client.subscriptions import SubscriptionLost, ToolsListChanged
from mcp.shared.exceptions import MCPError
from src.models.identity import AgentIdentity
from src.protocols.manager import McpToolArgumentRefusal, ProtocolManager
from src.protocols.mcp.cache import PrincipalAwareResponseCacheStore
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.header_policy import (
    McpHeaderPolicyError,
    inspect_tool_header_policy,
    validate_header_arguments,
)
from src.protocols.mcp.http_client import redact_http_headers, redact_http_url
from src.protocols.mcp.icons import McpIconService
from src.protocols.mcp.sdk_adapter import adapt_call_exception
from src.protocols.mcp.subscriptions import McpSubscriptionSupervisor
from src.protocols.mcp.telemetry import sanitize_span_attributes
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig


def _registry(**overrides: Any) -> ProtocolRegistry:
    registry = ProtocolRegistry("__missing_p6_registry__.json")
    registry.servers = {
        "srv": ProtocolServerConfig(command="echo", **overrides),
    }
    return registry


def _context() -> McpRequestContext:
    return McpRequestContext(
        request_id="p6-request",
        session_id="p6-session",
        identity=AgentIdentity.for_role("P6Owner", "Owner"),
        human_principal_id="human-p6",
        installation_id="srv",
        auth_level="Manual",
        origin="foreground",
    )


@pytest.mark.asyncio
async def test_private_cache_is_partitioned_by_verified_principal_and_public_is_shared():
    store = PrincipalAwareResponseCacheStore(max_entries=8)
    private_key = CacheKey("tools/list", "", "private-arm")
    public_key = CacheKey("tools/list", "", "public-arm")
    alice_private = CacheEntry(
        value={"owner": "alice"},
        scope="private",
        expires_at=9999999999,
    )
    public = CacheEntry(
        value={"catalog": "shared"},
        scope="public",
        expires_at=9999999999,
    )

    with store.bind_principal("alice"):
        await store.set(private_key, alice_private)
        await store.set(public_key, public)
        assert await store.get(private_key) == alice_private

    with store.bind_principal("bob"):
        assert await store.get(private_key) is None
        assert await store.get(public_key) == public

    assert store.snapshot()["entries"] == 2
    assert store.snapshot()["private_entries"] == 1
    assert store.snapshot()["public_entries"] == 1


@pytest.mark.asyncio
async def test_cache_eviction_removes_private_key_for_every_principal():
    store = PrincipalAwareResponseCacheStore(max_entries=8)
    key = CacheKey("resources/read", "touka://same", "private-arm")
    for principal in ("alice", "bob"):
        with store.bind_principal(principal):
            await store.set(
                key,
                CacheEntry(
                    value={"owner": principal},
                    scope="private",
                    expires_at=9999999999,
                ),
            )

    await store.delete(key)

    for principal in ("alice", "bob"):
        with store.bind_principal(principal):
            assert await store.get(key) is None


@pytest.mark.asyncio
async def test_sdk_owner_enforces_ttl_and_scope_over_touka_principal_store():
    now = [100.0]
    store = PrincipalAwareResponseCacheStore(max_entries=8)
    cache = ClientResponseCache(
        store=store,
        partition="touka-request-principal",
        arm_id="installation-generation",
        default_ttl_ms=0,
        clock=lambda: now[0],
        share_public=True,
        negotiated_version=lambda: "2026-07-28",
    )
    private = types.ListToolsResult(
        tools=[],
        ttlMs=50,
        cacheScope="private",
    )
    generation = cache.capture("tools/list", "")
    with store.bind_principal("alice"):
        await cache.write("tools/list", "", private, generation, "use")
        assert await cache.read("tools/list", "") is not None
    with store.bind_principal("bob"):
        assert await cache.read("tools/list", "") is None

    now[0] += 0.051
    with store.bind_principal("alice"):
        assert await cache.read("tools/list", "") is None

    public = types.ListToolsResult(
        tools=[],
        ttlMs=50,
        cacheScope="public",
    )
    generation = cache.capture("tools/list", "")
    with store.bind_principal("alice"):
        await cache.write("tools/list", "", public, generation, "use")
    with store.bind_principal("bob"):
        assert await cache.read("tools/list", "") is not None


@pytest.mark.asyncio
async def test_manager_enables_exactly_one_sdk_cache_owner_and_can_disable_it(monkeypatch):
    captured: list[CacheConfig | None] = []

    async def _connect(
        _config: ProtocolServerConfig,
        *,
        cache_config: CacheConfig | None = None,
        **_kwargs: Any,
    ) -> tuple[Any, Any]:
        captured.append(cache_config)
        return SimpleNamespace(protocol_version="2026-07-28"), SimpleNamespace()

    monkeypatch.setattr("src.protocols.manager.connect_mcp_server", _connect)

    enabled = ProtocolManager(_registry(cache_enabled=True))
    config = enabled.registry.get_server("srv")
    await enabled.connect_server("srv", config, **await enabled._judge("srv", config))
    assert isinstance(captured[-1], CacheConfig)
    assert captured[-1].share_public is True
    assert isinstance(captured[-1].store, PrincipalAwareResponseCacheStore)

    disabled = ProtocolManager(_registry(cache_enabled=False))
    config = disabled.registry.get_server("srv")
    await disabled.connect_server("srv", config, **await disabled._judge("srv", config))
    assert captured[-1] is None


class _FakeSubscription:
    subscription_id = "listen-1"
    honored = SimpleNamespace(
        tools_list_changed=True,
        prompts_list_changed=None,
        resources_list_changed=None,
        resource_subscriptions=None,
    )

    def __init__(self, events: list[Any], *, lost: bool = False) -> None:
        self._events = events
        self._lost = lost

    def __aiter__(self) -> _FakeSubscription:
        return self

    async def __anext__(self) -> Any:
        if self._events:
            return self._events.pop(0)
        if self._lost:
            self._lost = False
            message = "stream lost"
            raise SubscriptionLost(message)
        raise StopAsyncIteration


class _FakeListenClient:
    protocol_version = "2026-07-28"

    def __init__(self) -> None:
        self.attempts = 0
        self.filters: list[dict[str, Any]] = []

    @asynccontextmanager
    async def listen(self, **filters: Any):
        self.attempts += 1
        self.filters.append(filters)
        if self.attempts == 1:
            yield _FakeSubscription([], lost=True)
            return
        yield _FakeSubscription([ToolsListChanged()])


@pytest.mark.asyncio
async def test_subscription_loss_marks_stale_and_relistens_with_bounded_backoff():
    client = _FakeListenClient()
    delays: list[float] = []
    events: list[str] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 2:
            raise asyncio.CancelledError

    supervisor = McpSubscriptionSupervisor(
        client,
        tools_list_changed=True,
        prompts_list_changed=False,
        resources_list_changed=False,
        max_backoff_seconds=2,
        sleep=_sleep,
        on_event=lambda kind: events.append(kind),
    )
    task = supervisor.start()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = supervisor.snapshot()
    assert client.attempts == 2
    assert delays == [1.0, 1.0]
    assert events == ["tools/list_changed"]
    assert status["stale"] is True
    assert status["reconnect_attempts"] == 2
    assert client.filters[0] == {
        "tools_list_changed": True,
        "prompts_list_changed": False,
        "resources_list_changed": False,
        "resource_subscriptions": (),
    }


@pytest.mark.asyncio
async def test_invalid_subscription_ack_filter_fails_closed_without_breaking_client():
    class _InvalidFilterClient(_FakeListenClient):
        @asynccontextmanager
        async def listen(self, **filters: Any):
            sub = _FakeSubscription([])
            sub.honored = SimpleNamespace(
                tools_list_changed=True,
                prompts_list_changed=True,
                resources_list_changed=None,
                resource_subscriptions=None,
            )
            yield sub

    supervisor = McpSubscriptionSupervisor(
        _InvalidFilterClient(),
        tools_list_changed=True,
        prompts_list_changed=False,
        resources_list_changed=False,
        max_backoff_seconds=1,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    await supervisor.run_once()
    assert supervisor.snapshot()["status"] == "invalid_filter"
    assert supervisor.snapshot()["stale"] is True


def test_x_mcp_header_accepts_only_static_properties_and_case_insensitive_unique_names():
    valid = {
        "type": "object",
        "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "nested": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "x-mcp-header": "Page"},
                },
            },
        },
    }
    policy = inspect_tool_header_policy(valid, approved_for_sensitive=False)
    assert policy.header_map == {
        ("region",): "Region",
        ("nested", "page"): "Page",
    }

    duplicate = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "x-mcp-header": "Trace"},
            "b": {"type": "string", "x-mcp-header": "trace"},
        },
    }
    with pytest.raises(McpHeaderPolicyError, match="duplicates"):
        inspect_tool_header_policy(duplicate, approved_for_sensitive=False)

    through_ref = {
        "type": "object",
        "$defs": {
            "secret": {"type": "string", "x-mcp-header": "Secret"},
        },
        "properties": {"value": {"$ref": "#/$defs/secret"}},
    }
    with pytest.raises(McpHeaderPolicyError, match="properties"):
        inspect_tool_header_policy(through_ref, approved_for_sensitive=True)


def test_sensitive_header_requires_per_tool_owner_approval_and_never_becomes_auth():
    schema = {
        "type": "object",
        "properties": {
            "api_token": {"type": "string", "x-mcp-header": "Api-Token"},
        },
    }
    with pytest.raises(McpHeaderPolicyError, match="owner approval"):
        inspect_tool_header_policy(schema, approved_for_sensitive=False)

    policy = inspect_tool_header_policy(schema, approved_for_sensitive=True)
    assert policy.sensitive is True
    assert policy.header_map == {("api_token",): "Api-Token"}


def test_header_value_and_aggregate_bounds_apply_before_sdk_call():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "x-mcp-header": "Query"},
        },
    }
    policy = inspect_tool_header_policy(schema, approved_for_sensitive=True)
    with pytest.raises(McpHeaderPolicyError, match="value bytes"):
        validate_header_arguments(policy, {"query": "x" * 4096})


class _HeaderToolSession:
    protocol_version = "2026-07-28"
    server_capabilities = SimpleNamespace(
        tools=SimpleNamespace(list_changed=False),
        resources=None,
        prompts=None,
    )

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema
        self.tool_calls = 0

    async def list_tools(self, *, cursor: str | None = None) -> Any:
        assert cursor is None
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="search",
                    title=None,
                    description="search",
                    input_schema=self._schema,
                    output_schema=None,
                    annotations=None,
                    icons=[],
                    meta=None,
                )
            ],
            next_cursor=None,
        )

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
        self.tool_calls += 1
        return SimpleNamespace(
            content=[],
            structured_content={"ok": True},
            is_error=False,
            meta=None,
        )


def _manager_with_header_session(
    session: _HeaderToolSession,
    *,
    approved: bool,
) -> ProtocolManager:
    registry = _registry(
        approved_header_tools=["search"] if approved else [],
    )
    manager = ProtocolManager(registry)
    manager.handle_pool.put(
        manager.handle_key_for("srv", _context()),
        LegacyMcpHandle(session=session),
    )
    return manager


@pytest.mark.asyncio
async def test_sensitive_header_tool_is_quarantined_at_catalog_ingest():
    session = _HeaderToolSession(
        {
            "type": "object",
            "properties": {
                "token": {"type": "string", "x-mcp-header": "Token"},
            },
        }
    )
    catalog = await _manager_with_header_session(
        session,
        approved=False,
    ).list_server_tool_descriptors("srv", request_context=_context())

    assert catalog.items == []
    assert catalog.issues[0].code == "McpHeaderPolicyError"
    assert session.tool_calls == 0


@pytest.mark.asyncio
async def test_approved_header_tool_still_applies_value_bound_before_sdk_call():
    session = _HeaderToolSession(
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "x-mcp-header": "Query"},
            },
        }
    )
    manager = _manager_with_header_session(session, approved=True)

    with pytest.raises(McpToolArgumentRefusal, match="value bytes"):
        await manager.call_tool_on_server(
            "srv",
            "search",
            {"query": "x" * 4096},
            request_context=_context(),
        )
    assert session.tool_calls == 0


@pytest.mark.asyncio
async def test_icon_data_uri_checks_magic_dimensions_and_rejects_svg():
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x10\x00\x00\x00\x10"
        b"\x08\x06\x00\x00\x00"
    )
    service = McpIconService()
    accepted, issues = await service.sanitize_icons(
        [
            {
                "src": "data:image/png;base64,"
                + base64.b64encode(png).decode("ascii"),
                "mimeType": "image/png",
            },
            {
                "src": "data:image/svg+xml;base64,"
                + base64.b64encode(b"<svg/>").decode("ascii"),
                "mimeType": "image/svg+xml",
            },
        ],
        server_url=None,
    )
    assert len(accepted) == 1
    assert accepted[0]["src"].startswith("data:image/png;base64,")
    assert issues[0].code == "unsupported_mime"


@pytest.mark.asyncio
async def test_icon_remote_fetch_is_same_origin_no_credentials_no_redirect_and_magic_checked():
    calls: list[tuple[str, dict[str, str]]] = []
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x02\x00\x00\x00\x02"
        b"\x08\x06\x00\x00\x00"
    )

    async def _fetch(url: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
        calls.append((url, headers))
        return 200, "image/png", png

    service = McpIconService(fetch=_fetch)
    accepted, issues = await service.sanitize_icons(
        [
            {"src": "https://mcp.example.test/icon.png"},
            {"src": "https://evil.example.test/icon.png"},
        ],
        server_url="https://mcp.example.test/mcp",
    )

    assert len(accepted) == 1
    assert issues[0].code == "cross_origin"
    assert calls == [
        (
            "https://mcp.example.test/icon.png",
            {"Accept": "image/png, image/jpeg"},
        )
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mcp.method": "tools/call",
            "mcp.server": "server-id",
            "tool.name": "danger",
            "arguments": {"token": "secret"},
            "Mcp-Param-Token": "secret",
            "resource.uri": "https://example.test/private?q=secret",
            "exception.message": "Bearer secret",
            "query": "secret",
        },
        {
            "mcp.method": "resources/read",
            "mcp.transport": "streamable_http",
            "baggage": "tenant=owner",
            "tracestate": "vendor=secret",
            "content": "private prompt",
        },
    ],
)
def test_otel_attributes_are_allowlisted_and_drop_peer_trace_and_payload(payload: dict[str, Any]):
    sanitized = sanitize_span_attributes(payload)
    assert set(sanitized) <= {
        "mcp.method",
        "mcp.transport",
        "mcp.protocol.version",
        "mcp.cache.status",
        "mcp.subscription.status",
        "error.type",
    }
    flattened = repr(sanitized)
    assert "secret" not in flattened
    assert "private" not in flattened
    assert "tenant" not in flattened


def test_error_mapper_preserves_legacy_resource_not_found_and_modern_invalid_params():
    legacy = adapt_call_exception(
        MCPError(-32002, "resource missing"),
        sanitize=lambda value: value,
        source_protocol_version="2025-11-25",
    )
    modern = adapt_call_exception(
        MCPError(-32602, "invalid resource uri"),
        sanitize=lambda value: value,
        source_protocol_version="2026-07-28",
    )

    assert legacy.protocol_code == -32002
    assert modern.protocol_code == -32602
    assert modern.protocol_code not in range(-32099, -32019)


def test_http_diagnostics_keep_method_but_redact_name_param_and_query():
    headers = redact_http_headers(
        {
            "Mcp-Method": "tools/call",
            "Mcp-Name": "private-tool",
            "Mcp-Param-Token": "secret",
            "Authorization": "Bearer secret",
        }
    )

    assert headers["Mcp-Method"] == "tools/call"
    assert headers["Mcp-Name"] == "<redacted>"
    assert headers["Mcp-Param-Token"] == "<redacted>"
    assert headers["Authorization"] == "<redacted>"
    assert (
        redact_http_url("https://mcp.example.test/mcp?token=secret#private")
        == "https://mcp.example.test/mcp"
    )
