"""ToukaDispatch 真 socket modern 身分、SSE 與舊入口拒絕測試。"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx2
import pytest
import uvicorn
from mcp.types.version import LATEST_MODERN_VERSION
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from memory_server import config_utils
from session_manager import SessionManager
from src.services.dispatch_supervisor import _REQUIRED_DISPATCH_TOOLS, _probe_dispatch_mcp

from ToukaDispatch import server as dispatch_server
from ToukaDispatch.telemetry import DispatchTelemetry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def _running_dispatch(app: Any) -> AsyncIterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="error",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)
        listener.close()


@pytest.mark.asyncio
async def test_modern_socket_preserves_request_scoped_sse_and_rejects_legacy(monkeypatch):
    monkeypatch.setenv("DISPATCH_TOKEN", "p4a-live-master")
    monkeypatch.setattr(dispatch_server, "_session_mgr", SessionManager())
    monkeypatch.setattr(dispatch_server, "_telemetry", DispatchTelemetry())
    config_utils._reset_resource_proxy_cache()
    app = dispatch_server.create_dispatch_app()

    async def request_progress(ctx, call_next):
        if ctx.request_id == "modern-tools":
            await ctx.session.send_progress_notification(
                "socket-progress",
                1,
                total=1,
                related_request_id=ctx.request_id,
            )
        return await call_next(ctx)

    app.state.modern_mcp.middleware.append(request_progress)

    async with _running_dispatch(app) as base_url:
        port = urlsplit(base_url).port
        assert port is not None
        assert await _probe_dispatch_mcp(port)

        async with httpx2.AsyncClient(trust_env=False) as http_client:
            assert (await http_client.get(base_url + "/sse")).status_code == 404
            retired_post = await http_client.post(
                base_url + "/sse/messages/",
                params={"session_id": "retired-transport-session"},
                headers={"authorization": "Bearer p4a-live-master"},
                json={
                    "jsonrpc": "2.0",
                    "id": "retired-call",
                    "method": "tools/call",
                    "params": {
                        "name": "dispatch_handshake",
                        "arguments": {
                            "dispatch_token": "p4a-live-master",
                            "caller_name": "retired-client",
                        },
                    },
                },
            )
            assert retired_post.status_code == 404
            headers = {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": LATEST_MODERN_VERSION,
                "mcp-method": "tools/list",
            }
            tools = await http_client.post(
                base_url + "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "modern-tools",
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: LATEST_MODERN_VERSION,
                            CLIENT_CAPABILITIES_META_KEY: {},
                            "progressToken": "socket-progress",
                        },
                    },
                },
            )
            assert tools.status_code == 200
            assert tools.headers["content-type"].startswith("text/event-stream")
            payloads = [
                json.loads(line[6:])
                for line in tools.text.splitlines()
                if line.startswith("data: ")
            ]
            assert len(payloads) == 2
            assert payloads[0]["method"] == "notifications/progress"
            assert payloads[0]["params"]["progressToken"] == "socket-progress"
            assert payloads[1]["id"] == "modern-tools"
            assert {
                tool["name"] for tool in payloads[1]["result"]["tools"]
            } == _REQUIRED_DISPATCH_TOOLS
            old = await http_client.post(
                base_url + "/mcp",
                headers={
                    **headers,
                    "mcp-method": "initialize",
                    "mcp-protocol-version": "2025-11-25",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "old-init",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "retired-client", "version": "1"},
                    },
                },
            )
            if old.headers.get("content-type", "").startswith("text/event-stream"):
                rejected = [
                    json.loads(line[6:])
                    for line in old.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert len(rejected) == 1
                denial = rejected[0]
            else:
                denial = old.json()
            assert denial["error"]["code"] == -32020
            assert "result" not in denial
            health = await http_client.get(base_url + "/health")
        telemetry = health.json()["telemetry"]
        assert telemetry["anonymous_connections"]["modern"] > 0
        assert telemetry["anonymous_connections"]["legacy"] == 0
