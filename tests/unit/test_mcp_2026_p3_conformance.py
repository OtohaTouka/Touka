"""ADR-0200 / MCP 2026 P3：dual-era、wire 與 TLS。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import sys
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer, mcpserver
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp.http_client import build_mcp_http_client
from src.protocols.mcp.sdk_adapter import adapt_call_tool_result
from src.protocols.registry import ProtocolServerConfig
from src.services.tool_executor import _render_mcp_complete

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "mcp_v2"
MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


def _meta(
    *,
    version: str = MODERN_VERSION,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        PROTOCOL_VERSION_KEY: version,
        CLIENT_INFO_KEY: {"name": "touka-p3-tests", "version": "1.0.0"},
        CLIENT_CAPABILITIES_KEY: capabilities or {},
    }


def _modern_headers(
    method: str,
    *,
    version: str = MODERN_VERSION,
    name: str | None = None,
) -> dict[str, str]:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": version,
        "mcp-method": method,
    }
    if name is not None:
        headers["mcp-name"] = name
    return headers


async def _send_json(
    send: Any,
    *,
    status: int,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_asgi_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


class _LegacyHTTPPeer:
    """不 import MCP SDK 的 legacy-only Streamable HTTP peer。"""

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["method"] != "POST":
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        message = json.loads(await _read_asgi_body(receive))
        method = str(message.get("method"))
        self.methods.append(method)
        self.requests.append(message)
        request_id = message.get("id")
        if request_id is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if method == "server/discover":
            await _send_json(
                send,
                status=404,
                payload={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
            return
        if method == "initialize":
            result = {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "p3-legacy-http", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "legacy_ping",
                        "description": "Legacy HTTP matrix marker.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        else:
            await _send_json(
                send,
                status=404,
                payload={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
            return
        await _send_json(
            send,
            status=200,
            payload={"jsonrpc": "2.0", "id": request_id, "result": result},
        )


class _StructuredModernPeer:
    """回傳任意 JSON structuredContent 的 modern raw-wire peer。"""

    def __init__(self, *, omit_result_type: bool = False) -> None:
        self.omit_result_type = omit_result_type

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        message = json.loads(await _read_asgi_body(receive))
        method = message["method"]
        if method == "tools/list":
            await _send_json(
                send,
                status=200,
                payload={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "cacheScope": "public",
                        "resultType": "complete",
                        "tools": [
                            {
                                "name": "value",
                                "description": "Return an arbitrary JSON value.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                },
                            }
                        ],
                        "ttlMs": 0,
                    },
                },
            )
            return
        arguments = message["params"].get("arguments", {})
        value = arguments.get("value")
        result: dict[str, Any] = {
            "content": [],
            "structuredContent": value,
            "isError": False,
        }
        if not self.omit_result_type:
            result["resultType"] = "complete"
        await _send_json(
            send,
            status=200,
            payload={"jsonrpc": "2.0", "id": message["id"], "result": result},
        )


@contextlib.asynccontextmanager
async def _http_client_for_app(
    app: Any,
    *,
    mode: str = "auto",
    request_hook: Any | None = None,
) -> AsyncIterator[Client]:
    transport = httpx2.ASGITransport(app=app)
    hooks = {"request": [request_hook]} if request_hook is not None else None
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
            event_hooks=hooks,
        ) as http_client,
    ):
        target = streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(target, mode=mode, cache=None) as client:
            yield client


async def test_dual_era_stdio_matrix_uses_discover_and_isolated_initialize():
    modern_params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURE_ROOT / "stdio_modern_server.py")],
    )
    async with Client(
        stdio_client(modern_params),
        mode="auto",
        cache=None,
        read_timeout_seconds=5.0,
    ) as modern:
        modern_tools = await modern.list_tools()
        assert modern.protocol_version == MODERN_VERSION
        assert modern.session.discover_result is not None
        assert modern.session.initialize_result is None
        assert [tool.name for tool in modern_tools.tools] == ["p3_ping"]

    legacy_params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURE_ROOT / "stdio_legacy_peer.py")],
    )
    async with Client(
        stdio_client(legacy_params),
        mode="auto",
        cache=None,
        read_timeout_seconds=5.0,
    ) as legacy:
        legacy_tools = await legacy.list_tools()
        assert legacy.protocol_version == LEGACY_VERSION
        assert legacy.session.discover_result is None
        assert legacy.session.initialize_result is not None
        assert [tool.name for tool in legacy_tools.tools] == ["legacy_ping"]


async def test_touka_modern_uses_real_discover_and_rejects_legacy_peer():
    modern, modern_stack = await mcp_client.connect_mcp_server(
        ProtocolServerConfig(
            command=sys.executable,
            args=[str(FIXTURE_ROOT / "stdio_modern_server.py")],
        ),
        launch_environment={},
        read_timeout_seconds=5.0,
    )
    try:
        modern_tools = await modern.list_tools()
        assert modern.session.discover_result is not None
        assert [tool.name for tool in modern_tools.tools] == ["p3_ping"]
    finally:
        await modern_stack.aclose()

    with pytest.raises(MCPError) as caught:
        await mcp_client.connect_mcp_server(
            ProtocolServerConfig(
                command=sys.executable,
                args=[str(FIXTURE_ROOT / "stdio_legacy_peer.py")],
            ),
            launch_environment={},
            read_timeout_seconds=5.0,
        )
    assert caught.value.code == -32601


async def test_dual_era_streamable_http_matrix_has_stateless_modern_wire():
    records: list[dict[str, Any]] = []

    async def capture(request: httpx2.Request) -> None:
        records.append(
            {
                "method": request.method,
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )

    server = MCPServer("p3-modern-http", version="2.0.0")

    @server.tool()
    def p3_ping() -> str:
        return "pong"

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    async with _http_client_for_app(app, request_hook=capture) as modern:
        tools = await modern.list_tools()
        assert modern.protocol_version == MODERN_VERSION
        assert modern.session.discover_result is not None
        assert [tool.name for tool in tools.tools] == ["p3_ping"]

    assert records
    assert {record["method"] for record in records} == {"POST"}
    for record in records:
        headers = record["headers"]
        assert headers["accept"] == "application/json, text/event-stream"
        assert headers["mcp-protocol-version"] == MODERN_VERSION
        assert "mcp-session-id" not in headers
        assert "last-event-id" not in headers
        meta = record["body"]["params"]["_meta"]
        assert meta[PROTOCOL_VERSION_KEY] == MODERN_VERSION
        assert CLIENT_CAPABILITIES_KEY in meta

    peer = _LegacyHTTPPeer()
    transport = httpx2.ASGITransport(app=peer)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as http_client:
        target = streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(target, mode="auto", cache=None) as legacy:
            tools = await legacy.list_tools()
            assert legacy.protocol_version == LEGACY_VERSION
            assert legacy.session.initialize_result is not None
            assert [tool.name for tool in tools.tools] == ["legacy_ping"]
    assert peer.methods[:2] == ["server/discover", "initialize"]
    assert "tools/list" in peer.methods


async def test_modern_http_negative_ladder_origin_and_method_allocation():
    server = MCPServer("p3-wire-negative", version="2.0.0")

    @server.tool()
    def requires_extension(ctx: mcpserver.Context) -> str:
        mcpserver.require_client_extension(
            ctx.request_context,
            "io.touka.p3-required",
        )
        return "ok"

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:8000"],
            allowed_origins=["http://127.0.0.1:8000"],
        ),
    )
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        missing_meta = await client.post(
            "/mcp",
            headers=_modern_headers("tools/list"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert missing_meta.status_code == 400
        assert missing_meta.json()["error"]["code"] == -32602

        mismatch = await client.post(
            "/mcp",
            headers=_modern_headers("tools/call"),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": _meta()},
            },
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == -32020

        unsupported = await client.post(
            "/mcp",
            headers=_modern_headers("tools/list", version="2099-01-01"),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/list",
                "params": {"_meta": _meta(version="2099-01-01")},
            },
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["error"]["code"] == -32022

        capability = await client.post(
            "/mcp",
            headers=_modern_headers("tools/call", name="requires_extension"),
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "_meta": _meta(),
                    "name": "requires_extension",
                    "arguments": {},
                },
            },
        )
        assert capability.status_code == 400
        assert capability.json()["error"]["code"] == -32021

        unknown = await client.post(
            "/mcp",
            headers=_modern_headers("touka/unknown"),
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "touka/unknown",
                "params": {"_meta": _meta()},
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == -32601

        rejected_origin = await client.post(
            "/mcp",
            headers={
                **_modern_headers("tools/list"),
                "origin": "https://evil.example",
            },
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/list",
                "params": {"_meta": _meta()},
            },
        )
        assert rejected_origin.status_code == 403

        no_get = await client.get(
            "/mcp",
            headers=_modern_headers("tools/list"),
        )
        assert no_get.status_code == 405
        assert no_get.headers["allow"] == "POST"

    for response in (
        missing_meta,
        mismatch,
        unsupported,
        capability,
        unknown,
    ):
        assert "mcp-session-id" not in response.headers


async def test_modern_http_request_scoped_sse_and_cleanup():
    server = MCPServer("p3-request-sse", version="2.0.0")

    @server.tool()
    async def progress_once(ctx: mcpserver.Context) -> str:
        await ctx.report_progress(1, 1, "done")
        return "ok"

    app = server.streamable_http_app(
        json_response=False,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:8000"],
            allowed_origins=["http://127.0.0.1:8000"],
        ),
    )
    transport = httpx2.ASGITransport(app=app)
    baseline = set(asyncio.all_tasks())
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers=_modern_headers("tools/call", name="progress_once"),
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "_meta": {**_meta(), "progressToken": "p3-progress"},
                    "name": "progress_once",
                    "arguments": {},
                },
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "notifications/progress" in response.text
        assert '"resultType":"complete"' in response.text
        assert "mcp-session-id" not in response.headers

    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks() - baseline
        if not task.done() and task is not asyncio.current_task()
    ]
    assert leaked == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("text", id="string"),
        pytest.param(42, id="number"),
        pytest.param(True, id="boolean"),
        pytest.param(["a", 2], id="array"),
        pytest.param({"ok": True}, id="object"),
        pytest.param(None, id="null"),
    ],
)
async def test_modern_structured_content_preserves_every_json_value(value: Any):
    peer = _StructuredModernPeer()
    transport = httpx2.ASGITransport(app=peer)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as http_client:
        target = streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(
            target,
            mode=MODERN_VERSION,
            cache=None,
        ) as client:
            result = await client.call_tool("value", {"value": value})

    outcome = adapt_call_tool_result(
        result,
        source_protocol_version=MODERN_VERSION,
    )
    assert outcome.structured_content_present is True
    assert outcome.structured_content == value
    assert _render_mcp_complete(outcome) == json.dumps(
        value,
        ensure_ascii=False,
    )


async def test_modern_wire_rejects_missing_result_type():
    peer = _StructuredModernPeer(omit_result_type=True)
    transport = httpx2.ASGITransport(app=peer)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as http_client:
        target = streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(
            target,
            mode=MODERN_VERSION,
            cache=None,
        ) as client:
            with pytest.raises(ValidationError):
                await client.call_tool("value", {"value": "x"})


class _ProbeSession:
    def __init__(self, error_code: int | None) -> None:
        self.error_code = error_code
        self.adopted: dict[str, Any] | None = None

    async def send_discover(self, _version: str) -> dict[str, Any]:
        if self.error_code is not None:
            raise MCPError(self.error_code, "probe rejected")
        return {
            "supportedVersions": [MODERN_VERSION],
            "capabilities": {"tools": {}},
            "resultType": "complete",
            "ttlMs": 0,
            "cacheScope": "public",
        }

    def adopt(self, result: Any) -> None:
        self.adopted = result.model_dump(by_alias=True)


class _ProbeClient:
    modes: list[str] = []
    error_code: int | None = None

    def __init__(self, _target: Any, **kwargs: Any) -> None:
        self.mode = kwargs["mode"]
        self.session = _ProbeSession(self.error_code)
        self.modes.append(self.mode)

    async def __aenter__(self) -> _ProbeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.mark.parametrize("code", [-32602, -32020, -32021, -32022])
async def test_touka_modern_never_falls_back_on_protocol_errors(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
):
    _ProbeClient.modes = []
    _ProbeClient.error_code = code
    monkeypatch.setattr(mcp_client, "Client", _ProbeClient)
    monkeypatch.setattr(mcp_client, "_build_stdio_transport", lambda *_a, **_kw: object())

    with pytest.raises(MCPError) as caught:
        await mcp_client.connect_mcp_server(
            ProtocolServerConfig(command="p3-probe", protocol_mode=MODERN_VERSION)
        )

    assert caught.value.code == code
    assert _ProbeClient.modes == [MODERN_VERSION]


async def test_touka_modern_rejects_method_not_found_without_reopening_transport(
    monkeypatch: pytest.MonkeyPatch,
):
    _ProbeClient.modes = []
    _ProbeClient.error_code = -32601
    monkeypatch.setattr(mcp_client, "Client", _ProbeClient)
    monkeypatch.setattr(mcp_client, "_build_stdio_transport", lambda *_a, **_kw: object())

    with pytest.raises(MCPError) as caught:
        await mcp_client.connect_mcp_server(
            ProtocolServerConfig(command="p3-probe", protocol_mode=MODERN_VERSION)
        )
    assert caught.value.code == -32601
    assert _ProbeClient.modes == [MODERN_VERSION]


def _write_local_ca(tmp_path: Path) -> tuple[Path, Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Touka P3 CA")])
    valid_from = datetime(2025, 1, 1, tzinfo=UTC)
    valid_until = datetime(2035, 1, 1, tzinfo=UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


async def test_httpx2_factory_trusts_only_the_injected_local_ca(
    global_test_sandbox: str,
):
    tls_root = Path(global_test_sandbox) / "p3_tls"
    tls_root.mkdir(exist_ok=True)
    ca_path, cert_path, key_path = _write_local_ca(tls_root)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=cert_path, keyfile=key_path)

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 11\r\nConnection: close\r\n\r\n"
                b'{"ok":true}'
            )
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(
        respond,
        host="127.0.0.1",
        port=0,
        ssl=server_context,
    )
    port = server.sockets[0].getsockname()[1]
    url = f"https://127.0.0.1:{port}/mcp"
    try:
        async with build_mcp_http_client(ca_file=ca_path) as trusted:
            response = await trusted.get(url)
            assert response.status_code == 200
            assert response.json() == {"ok": True}
        async with build_mcp_http_client() as untrusted:
            with pytest.raises(httpx2.TransportError):
                await untrusted.get(url)
    finally:
        server.close()
        await server.wait_closed()
