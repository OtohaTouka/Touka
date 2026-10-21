"""SDK v2 ``connect_mcp_server``：stdio env／sentinel／Client 契約。"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr, ValidationError
from src.config import get_app_data_dir
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.protocols.registry import ProtocolServerConfig
from src.utils.mcp_sentinels import APPDATA_SENTINEL


class _FakeClient:
    """進入 transport 的最小 public Client 替身。"""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self._target = target
        self._transport: Any = None
        self.session = _FakeSession()
        _FakeClient.last_kwargs = kwargs

    async def __aenter__(self) -> _FakeClient:
        self._transport = await self._target.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return bool(await self._target.__aexit__(*exc))


class _FakeSession:
    async def send_discover(self, _version: str) -> dict[str, Any]:
        return {
            "cacheScope": "public",
            "capabilities": {},
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "ttlMs": 0,
        }

    def adopt(self, _result: Any) -> None:
        return None


async def test_q36_explicit_modern_performs_validated_discover_before_adopt(monkeypatch):
    events: list[Any] = []
    stack = AsyncExitStack()
    stack.callback(events.append, "closed")

    async def discover(version: str) -> dict[str, Any]:
        events.append(("discover", version))
        return await _FakeSession().send_discover(version)

    def adopt(result: Any) -> None:
        assert isinstance(result, types.DiscoverResult)
        events.append(("adopt", result.supported_versions))

    client = SimpleNamespace(session=SimpleNamespace(send_discover=discover, adopt=adopt))

    async def connect_once(_config: Any, **kwargs: Any):
        events.append(("connect", kwargs["mode"]))
        return client, stack

    monkeypatch.setattr(mcp_client, "_connect_once", connect_once)
    result, owned_stack = await mcp_client.connect_mcp_server(
        ProtocolServerConfig(command="test", protocol_mode="2026-07-28")
    )
    try:
        assert result is client
        assert owned_stack is stack
        assert events == [
            ("connect", "2026-07-28"),
            ("discover", "2026-07-28"),
            ("adopt", ["2026-07-28"]),
        ]
    finally:
        await stack.aclose()
    assert events[-1] == "closed"


@pytest.mark.parametrize("code", [-32601, -32602, -32020, -32021, -32022])
async def test_q36_modern_discover_error_closes_without_legacy_reconnect(monkeypatch, code):
    events: list[Any] = []
    stack = AsyncExitStack()
    stack.callback(events.append, "closed")
    failure = MCPError(code, "test negotiation error")

    async def discover(_version: str) -> Any:
        events.append("discover")
        raise failure

    async def connect_once(_config: Any, **kwargs: Any):
        events.append(("connect", kwargs["mode"]))
        return SimpleNamespace(session=SimpleNamespace(send_discover=discover)), stack

    monkeypatch.setattr(mcp_client, "_connect_once", connect_once)
    try:
        with pytest.raises(MCPError) as caught:
            await mcp_client.connect_mcp_server(
                ProtocolServerConfig(command="test", protocol_mode="2026-07-28")
            )
        assert caught.value is failure
        assert events == [("connect", "2026-07-28"), "discover", "closed"]
    finally:
        await stack.aclose()


@pytest.mark.parametrize("raw", [{}, {"resultType": "complete", "capabilities": "invalid"}])
async def test_q36_malformed_discover_never_adopts_and_closes(monkeypatch, raw):
    events: list[Any] = []
    stack = AsyncExitStack()
    stack.callback(events.append, "closed")

    async def discover(_version: str) -> Any:
        events.append("discover")
        return raw

    def adopt(_result: Any) -> None:
        events.append("adopt")

    async def connect_once(_config: Any, **kwargs: Any):
        events.append(("connect", kwargs["mode"]))
        return SimpleNamespace(session=SimpleNamespace(send_discover=discover, adopt=adopt)), stack

    monkeypatch.setattr(mcp_client, "_connect_once", connect_once)
    try:
        with pytest.raises(ValidationError):
            await mcp_client.connect_mcp_server(
                ProtocolServerConfig(command="test", protocol_mode="2026-07-28")
            )
        assert events == [("connect", "2026-07-28"), "discover", "closed"]
    finally:
        await stack.aclose()


async def test_connect_resolves_args_sentinels_and_keeps_env_behavior(monkeypatch):
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params: Any):
        captured["params"] = params
        yield ("read", "write")

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "Client", _FakeClient)

    config = ProtocolServerConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", APPDATA_SENTINEL],
        env={"MARKER_DIR": APPDATA_SENTINEL},
    )
    _client, stack = await mcp_client.connect_mcp_server(config)
    await stack.aclose()

    params = captured["params"]
    assert params.command == "npx"
    assert params.args == [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        str(get_app_data_dir()),
    ]
    assert params.env["MARKER_DIR"] == str(get_app_data_dir())
    assert params.env["TOOL_BASE_URL"].startswith("http://")
    assert _FakeClient.last_kwargs == {
        "mode": "2026-07-28",
        "cache": None,
        "read_timeout_seconds": mcp_client.MCP_INIT_TIMEOUT_SECONDS,
        "elicitation_callback": mcp_client.decline_unbound_elicitation,
    }


async def test_creation_can_supply_exact_clean_launch_environment(monkeypatch):
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params: Any):
        captured["params"] = params
        yield ("read", "write")

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "Client", _FakeClient)

    config = ProtocolServerConfig(
        command="python",
        args=["server.py"],
        env={"DECLARED": "value"},
    )
    _client, stack = await mcp_client.connect_mcp_server(
        config,
        launch_environment={"PATH": "safe", "DECLARED": "value"},
    )
    await stack.aclose()

    assert captured["params"].env == {"PATH": "safe", "DECLARED": "value"}


async def test_remote_oauth_provider_is_bound_as_request_time_http_auth(monkeypatch):
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_http_client(**kwargs: Any):
        captured.update(kwargs)
        yield object()

    @asynccontextmanager
    async def fake_http_transport(_url: str, *, http_client: Any):
        assert http_client is not None
        yield ("read", "write")

    @asynccontextmanager
    async def provider():
        yield OAuthBearerCredential(
            grant_id="grant-a",
            grant_version=1,
            authorization_header=SecretStr("Bearer access-canary"),
        )

    monkeypatch.setattr(mcp_client, "build_mcp_http_client", fake_http_client)
    monkeypatch.setattr(mcp_client, "streamable_http_client", fake_http_transport)
    monkeypatch.setattr(mcp_client, "Client", _FakeClient)

    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        protocol_mode="2026-07-28",
        auth_mode="oauth_user",
        auth_profile_id="profile-a",
    )
    _client, stack = await mcp_client.connect_mcp_server(
        config,
        oauth_credential_provider=provider,
    )
    await stack.aclose()

    assert captured["headers"] == {}
    assert captured["auth"].__class__.__name__ == "McpOAuthBearerAuth"
    assert captured["transport"].__class__.__name__ == "PinnedOAuthTransport"
