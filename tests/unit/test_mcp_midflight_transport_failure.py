"""dep1 重現測試：remote MCP 已連線後途中傳輸失敗，不得取消承載連線的 task，也不影響其他連線。

shared server 的 SDK ``Client`` 與 transport task group 在 lifespan task 裡進入、之後不退出
（``main.py`` 的 ``initialize_all``）。途中一個 POST 失敗，anyio 就把取消送到這個 host task：
lifespan 會在 yield 時被取消，清理時把其他連線與背景任務一起收掉。dep1 證實此缺陷
（mcp 2.0.0 與 2.2.0 相同；轉址 hook 讓 3xx 也成為觸發條件）；life1 改由每條連線專屬的
owner task 持有連線後，三個案例轉綠。

全離線：兩台真 ``MCPServer``（stateless streamable HTTP）經 ``httpx2.ASGITransport`` 服務，
只把 ``alpha`` 的 ``tools/call`` 換成注入的失敗；連線用正式的 ``build_mcp_http_client``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx2
import pytest
from mcp.server import MCPServer
from src.models.identity import AgentIdentity
from src.protocols.manager import ProtocolManager
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp.canonical import McpOperationException, McpRequestContext
from src.protocols.mcp.http_client import build_mcp_http_client
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig

_PORTS = {"alpha": 8101, "beta": 8102}
_WAIT_SECONDS = 10.0


def _app(name: str) -> Any:
    server = MCPServer(f"midflight-{name}", version="1.0.0")

    @server.tool()
    def ping() -> str:
        return f"pong-{name}"

    return server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")


def _connect_error(request: httpx2.Request) -> httpx2.Response:
    msg = "injected mid-flight failure"
    raise httpx2.ConnectError(msg, request=request)


def _read_timeout(request: httpx2.Request) -> httpx2.Response:
    msg = "injected mid-flight timeout"
    raise httpx2.ReadTimeout(msg, request=request)


def _redirect(request: httpx2.Request) -> httpx2.Response:
    location = f"http://127.0.0.1:{_PORTS['alpha']}/elsewhere"
    return httpx2.Response(307, headers={"Location": location}, request=request)


class _Router(httpx2.AsyncBaseTransport):
    """依 port 轉給兩台 ASGI app；只有 alpha 的 tools/call 換成注入的失敗。"""

    def __init__(self, apps: dict[int, Any], failure: Any) -> None:
        self._inner = {port: httpx2.ASGITransport(app=app) for port, app in apps.items()}
        self._failure = failure
        self.injected = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        alpha = request.url.port == _PORTS["alpha"] and request.url.path == "/mcp"
        if alpha and request.method == "POST":
            body = json.loads(await request.aread() or b"{}")
            if body.get("method") == "tools/call":
                self.injected += 1
                return self._failure(request)
        return await self._inner[request.url.port].handle_async_request(request)


def _context(name: str) -> McpRequestContext:
    return McpRequestContext(
        request_id=f"midflight-{name}",
        session_id="midflight-session",
        identity=AgentIdentity.for_role("MidflightOwner", "Owner"),
        human_principal_id="human-midflight",
        installation_id=name,
        auth_level="Manual",
        origin="foreground",
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_connect_error, id="connect-error"),
        pytest.param(_read_timeout, id="read-timeout"),
        # 轉址 hook 讓 3xx 變成整條連線失效（沒有 hook 時 SDK 只讓這次請求失敗）。
        pytest.param(_redirect, id="redirect-307"),
    ],
)
async def test_midflight_failure_keeps_lifespan_host_and_other_connection(monkeypatch, failure):
    apps = {port: _app(name) for name, port in _PORTS.items()}
    router = _Router(apps, failure)
    monkeypatch.setattr(
        mcp_client,
        "build_mcp_http_client",
        lambda **kwargs: build_mcp_http_client(**{**kwargs, "transport": router}),
    )
    registry = ProtocolRegistry("__missing_midflight_registry__.json")
    registry.servers = {
        name: ProtocolServerConfig(url=f"http://127.0.0.1:{port}/mcp")
        for name, port in _PORTS.items()
    }
    manager = ProtocolManager(registry)
    ready = asyncio.Event()
    stop = asyncio.Event()
    cancelled_by: list[str] = []

    async def lifespan_host() -> str:
        """模擬 lifespan：在這個 task 建立 shared 連線，之後停在 yield。"""
        try:
            for name in _PORTS:
                await manager.connect_server(name, registry.servers[name])
            ready.set()
            await stop.wait()
        except asyncio.CancelledError as exc:
            cancelled_by.append(str(exc.args))
            return "cancelled"
        finally:
            with contextlib.suppress(Exception):
                await manager.cleanup()
        return "completed"

    async with contextlib.AsyncExitStack() as served:
        for app in apps.values():
            await served.enter_async_context(app.router.lifespan_context(app))
        host = asyncio.create_task(lifespan_host())
        await asyncio.wait_for(ready.wait(), timeout=_WAIT_SECONDS)

        with pytest.raises(McpOperationException):
            await asyncio.wait_for(
                manager.call_tool_on_server("alpha", "ping", {}, request_context=_context("alpha")),
                timeout=_WAIT_SECONDS,
            )
        for _ in range(20):  # 讓 anyio 有機會送出取消
            await asyncio.sleep(0.01)
        host_survived = not host.done()
        beta_open = manager.handle_pool.get(manager.handle_key_for("beta", None)) is not None
        # host 已被取消時不再呼叫 beta：那會在測試 task 重新連線，掩蓋連線已被關掉。
        other = (
            await asyncio.wait_for(
                manager.call_tool_on_server("beta", "ping", {}, request_context=_context("beta")),
                timeout=_WAIT_SECONDS,
            )
            if host_survived and beta_open
            else None
        )
        stop.set()
        host_result = await asyncio.wait_for(host, timeout=_WAIT_SECONDS)

    assert router.injected == 1
    assert host_survived, f"mid-flight failure cancelled the host task: {cancelled_by}"
    assert host_result == "completed"
    assert beta_open, "the other connection was closed by the host's cleanup"
    assert other is not None
    assert other.is_error is False
    assert "pong-beta" in json.dumps(other.model_dump(mode="json"))
