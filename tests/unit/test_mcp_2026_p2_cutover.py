"""MCP 2026 遷移 P2：Python SDK v2 原子 cutover 契約。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import threading
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError
from src.protocols.mcp import http_client as mcp_http
from src.protocols.registry import ProtocolServerConfig
from src.routers.mcp import MCPServerItem
from src.services.creation.models import McpScaffoldSpec, ToolSpec
from src.services.creation.scaffold import generate_fastmcp_source, mcp_scaffolder

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dependencies(package: str) -> set[str]:
    text = (REPO_ROOT / package / "pyproject.toml").read_text(encoding="utf-8")
    return {line.strip().strip('",') for line in text.splitlines() if line.strip().startswith('"')}


def test_workspace_pins_python_sdk_v2_and_direct_http_dependencies():
    assert importlib.metadata.version("mcp") == "2.2.0"
    proxy = _dependencies("ToukaProxy")
    dispatch = _dependencies("ToukaDispatch")
    memory = _dependencies("ToukaMemory")
    assert "mcp==2.2.0" in proxy
    assert "mcp==2.2.0" in dispatch
    assert "httpx2~=2.12.0" in proxy
    assert "pydantic-settings~=2.14.0" in proxy
    assert "mcp==2.2.0" not in memory


def test_sdk_v2_public_client_and_server_imports_are_available():
    mcp = importlib.import_module("mcp")
    server = importlib.import_module("mcp.server")
    assert hasattr(mcp, "Client")
    assert hasattr(server, "MCPServer")


@pytest.mark.parametrize("model", [ProtocolServerConfig, MCPServerItem])
def test_q36_runtime_and_wire_default_to_modern(model):
    assert model(command="echo").protocol_mode == "2026-07-28"


@pytest.mark.parametrize("model", [ProtocolServerConfig, MCPServerItem])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_q36_runtime_and_wire_reject_retired_protocol_modes(model, mode):
    with pytest.raises(ValidationError):
        model(command="echo", protocol_mode=mode)


@pytest.mark.parametrize("model", [ProtocolServerConfig, MCPServerItem])
def test_q36_runtime_and_wire_remove_legacy_sse_compatibility_field(model):
    assert "sse_compat_enabled" not in model.model_fields


def test_scaffold_emits_sdk_v2_server_and_marks_handoff_major():
    spec = McpScaffoldSpec(name="P2 Server", tools=[ToolSpec(name="ping")])
    source = generate_fastmcp_source(spec)
    assert "from mcp.server import MCPServer" in source
    assert "mcp = MCPServer('P2 Server', version='2.0.0')" in source

    result = mcp_scaffolder.scaffold(spec)
    assert result.ok is True
    assert result.spec is not None
    assert result.spec.sdk_major == 2


def test_http_client_factory_is_fail_closed_and_uses_explicit_ssl(monkeypatch):
    captured: dict[str, Any] = {}
    ssl_context = object()

    def _ssl_context(*, cafile: str | None = None) -> object:
        captured["cafile"] = cafile
        return ssl_context

    monkeypatch.setattr(
        mcp_http.ssl,
        "create_default_context",
        _ssl_context,
    )

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(mcp_http.httpx2, "AsyncClient", _FakeAsyncClient)
    ca_file = Path("private-ca.pem")

    client = mcp_http.build_mcp_http_client(
        headers={"Authorization": "Bearer secret"},
        ca_file=ca_file,
    )

    assert isinstance(client, _FakeAsyncClient)
    assert captured["cafile"] == str(ca_file)
    assert captured["verify"] is ssl_context
    assert captured["follow_redirects"] is False
    # mcp 2.2 起 SDK 不看 follow_redirects；真正擋轉址的是 response hook（dep1）。
    assert captured["event_hooks"]["response"] == [mcp_http._reject_redirect]
    assert captured["trust_env"] is False
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    timeout = captured["timeout"]
    assert timeout.connect == mcp_http.MCP_HTTP_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == mcp_http.MCP_HTTP_READ_TIMEOUT_SECONDS
    assert timeout.write == mcp_http.MCP_HTTP_WRITE_TIMEOUT_SECONDS
    assert timeout.pool == mcp_http.MCP_HTTP_POOL_TIMEOUT_SECONDS


def test_http_diagnostics_redact_every_non_allowlisted_header():
    redacted = mcp_http.redact_http_headers(
        {
            "Authorization": "Bearer secret",
            "X-Custom-Token": "secret",
            "Content-Type": "application/json",
        }
    )
    assert redacted == {
        "Authorization": "<redacted>",
        "X-Custom-Token": "<redacted>",
        "Content-Type": "application/json",
    }
    assert all(issubclass(error, httpx2.RequestError) for error in mcp_http.MCP_HTTP_EXCEPTIONS)


async def test_mcpserver_sync_handler_runs_in_worker_thread():
    caller_thread = threading.get_ident()
    server = MCPServer("worker-test", version="2.0.0")

    @server.tool()
    def handler_thread() -> int:
        return threading.get_ident()

    async with Client(server, cache=None) as client:
        result = await client.call_tool("handler_thread", {})

    assert result.structured_content is not None
    assert result.structured_content["result"] != caller_thread


async def test_mcpserver_handler_mcperror_stays_jsonrpc_error():
    server = MCPServer("error-test", version="2.0.0")

    @server.tool()
    async def invalid_call() -> str:
        raise MCPError(-32602, "invalid tool arguments", {"field": "query"})

    async with Client(server, cache=None) as client:
        with pytest.raises(MCPError) as caught:
            await asyncio.wait_for(client.call_tool("invalid_call", {}), timeout=5.0)

    assert caught.value.code == -32602
    assert caught.value.data == {"field": "query"}
