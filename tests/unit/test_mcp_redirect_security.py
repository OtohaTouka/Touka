"""dep1：remote MCP 一律不跟轉址（禁令 22；ADR-0199 dep1 補記）。

mcp 2.2.0 起 SDK 不看呼叫端 ``follow_redirects``，會自己跟同源轉址；同源目標會收到
Authorization。``build_mcp_http_client`` 掛 response hook：任何 3xx 在 SDK 看到之前就丟
``McpRedirectRefusedError``（繼承 ``httpx2.TransportError``、不繼承 ConnectError／NetworkError）。

全離線：MockTransport、真 SDK 注入 MockTransport、127.0.0.1 兩個 port 的真 socket。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp import http_client as mcp_http
from src.protocols.mcp.http_client import McpRedirectRefusedError, build_mcp_http_client
from src.protocols.mcp.oauth.service import OAuthBearerCredential
from src.protocols.registry import ProtocolServerConfig

_URL = "https://mcp.example.test/mcp?api_key=query-canary"
_SECRETS = ("query-canary", "location-canary")


class _TrapStream(httpx2.AsyncByteStream):
    """回應內容被讀就記下來：hook 不得讀 3xx 的內容。"""

    def __init__(self) -> None:
        self.read = False

    async def __aiter__(self) -> Any:
        self.read = True
        yield b'{"jsonrpc":"2.0","id":1,"result":{}}'


_REDIRECTS = [
    pytest.param(
        307, "https://mcp.example.test/other?t=location-canary", _URL, id="same-origin-307"
    ),
    pytest.param(
        308, "https://mcp.example.test/other?t=location-canary", _URL, id="same-origin-308"
    ),
    pytest.param(
        302, "https://evil.example.test/mcp?t=location-canary", _URL, id="cross-origin-302"
    ),
    pytest.param(
        301,
        "https://127.0.0.1:8443/mcp?t=location-canary",
        "http://127.0.0.1:8443/mcp?api_key=query-canary",
        id="http-to-https-301",
    ),
    pytest.param(300, None, _URL, id="no-location-300"),
]


@pytest.mark.parametrize(("status", "location", "url"), _REDIRECTS)
@pytest.mark.parametrize("call_follow", [None, True], ids=["default", "caller-follows"])
async def test_factory_refuses_every_redirect_before_anyone_follows(
    caplog, status, location, url, call_follow
):
    seen: list[str] = []
    streams: list[_TrapStream] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        streams.append(_TrapStream())
        headers = {"Location": location} if location else {}
        return httpx2.Response(status, headers=headers, stream=streams[-1])

    client = build_mcp_http_client(
        headers={"Authorization": "Bearer static-canary"},
        transport=httpx2.MockTransport(handler),
    )
    extra = {} if call_follow is None else {"follow_redirects": call_follow}
    caplog.set_level(logging.WARNING, logger=mcp_http.__name__)
    async with client:
        with pytest.raises(McpRedirectRefusedError) as caught:
            await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, **extra)

    assert seen == [url]
    assert not streams[0].read
    error = caught.value
    assert isinstance(error, httpx2.TransportError)
    assert not isinstance(error, (httpx2.ConnectError, httpx2.NetworkError))
    assert isinstance(error, mcp_http.MCP_HTTP_EXCEPTIONS)
    assert str(status) in str(error)
    assert mcp_http.redact_http_url(url) in str(error)
    for secret in _SECRETS:
        assert secret not in str(error)
        assert secret not in caplog.text
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert str(status) in caplog.text


def test_factory_keeps_follow_redirects_false_and_registers_hook():
    client = build_mcp_http_client()
    assert client.follow_redirects is False
    assert mcp_http._reject_redirect in client.event_hooks["response"]


def _remote_factory(handler: Any, *, hook: bool = True) -> Any:
    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        transport = httpx2.MockTransport(handler)
        if hook:
            return build_mcp_http_client(**{**kwargs, "transport": transport})
        # 對照組：同樣設定、但沒掛 hook，證明 hook 不是多餘的。
        return httpx2.AsyncClient(
            headers=kwargs.get("headers"),
            auth=kwargs.get("auth"),
            transport=transport,
            follow_redirects=False,
        )

    return factory


def _redirecting(status: int, location: str, seen: list[tuple[str, str | None]]) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/mcp":
            return httpx2.Response(status, headers={"Location": location})
        return httpx2.Response(500)

    return handler


_SDK_REDIRECTS = [
    pytest.param(307, "https://mcp.example.test/elsewhere", id="same-origin-307"),
    pytest.param(302, "https://evil.example.test/elsewhere", id="cross-origin-302"),
]
_STATIC = ProtocolServerConfig(
    url="https://mcp.example.test/mcp",
    headers={"Authorization": "Bearer ${MCP_TOKEN}"},
    env={"MCP_TOKEN": "static-canary"},
)


@pytest.mark.parametrize(("status", "location"), _SDK_REDIRECTS)
async def test_real_sdk_discover_never_requests_the_redirect_target(monkeypatch, status, location):
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        mcp_client, "build_mcp_http_client", _remote_factory(_redirecting(status, location, seen))
    )

    with pytest.raises(Exception):  # noqa: B017, PT011 - 型別由 SDK 決定；重點是沒人跟
        await mcp_client.connect_mcp_server(_STATIC, read_timeout_seconds=5.0)

    assert seen == [("https://mcp.example.test/mcp", "Bearer static-canary")]


async def test_without_hook_the_sdk_follows_same_origin_and_resends_credentials(monkeypatch):
    """對照組（SDK 行為釘子）：拿掉 hook 時 mcp 2.2 自己跟同源轉址並帶 Authorization。"""
    seen: list[tuple[str, str | None]] = []
    handler = _redirecting(307, "https://mcp.example.test/elsewhere", seen)
    monkeypatch.setattr(mcp_client, "build_mcp_http_client", _remote_factory(handler, hook=False))

    with pytest.raises(Exception):  # noqa: B017, PT011
        await mcp_client.connect_mcp_server(_STATIC, read_timeout_seconds=5.0)

    assert ("https://mcp.example.test/elsewhere", "Bearer static-canary") in seen


@pytest.mark.parametrize(("status", "location"), _SDK_REDIRECTS)
async def test_real_sdk_oauth_provider_is_asked_once(monkeypatch, status, location):
    seen: list[tuple[str, str | None]] = []
    provided: list[str] = []
    monkeypatch.setattr(
        mcp_client, "build_mcp_http_client", _remote_factory(_redirecting(status, location, seen))
    )

    @asynccontextmanager
    async def provider(*, wire_request: httpx2.Request):
        provided.append(str(wire_request.url))
        yield OAuthBearerCredential(
            grant_id="grant-a",
            grant_version=1,
            authorization_header=SecretStr("Bearer oauth-canary"),
        )

    config = ProtocolServerConfig(
        url="https://mcp.example.test/mcp",
        auth_mode="oauth_user",
        auth_profile_id="profile-a",
    )
    with pytest.raises(Exception):  # noqa: B017, PT011
        await mcp_client.connect_mcp_server(
            config, oauth_credential_provider=provider, read_timeout_seconds=5.0
        )

    assert provided == ["https://mcp.example.test/mcp"]
    assert seen == [("https://mcp.example.test/mcp", "Bearer oauth-canary")]


@pytest.mark.parametrize("same_origin", [False, True], ids=["other-port", "same-origin"])
async def test_real_socket_redirect_target_never_receives_a_connection(same_origin):
    hits: dict[str, list[str]] = {"first": [], "second": []}

    def serve(label: str) -> Any:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            hits[label].append(head[0].split(" ")[1])
            target = ports["first"] if same_origin else ports["second"]
            writer.write(
                f"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:{target}/elsewhere\r\n".encode()
                + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()

        return handle

    servers = {label: await asyncio.start_server(serve(label), "127.0.0.1", 0) for label in hits}
    ports = {label: server.sockets[0].getsockname()[1] for label, server in servers.items()}
    config = ProtocolServerConfig(url=f"http://127.0.0.1:{ports['first']}/mcp")
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011
            await mcp_client.connect_mcp_server(config, read_timeout_seconds=5.0)
    finally:
        for server in servers.values():
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

    assert hits == {"first": ["/mcp"], "second": []}
