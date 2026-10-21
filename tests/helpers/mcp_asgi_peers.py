"""MCP 連線隔離測試的共用對端（life1）。

- 每台是真的 ``MCPServer``（stateless streamable HTTP，只有 ``ping`` 工具），經
  ``httpx2.ASGITransport`` 服務；全離線、不開 socket。
- ``PeerRouter`` 依 port 分流；``failures[name]`` 有值時，該台符合 ``fail_methods`` 的 POST
  換成注入的失敗（``None``＝所有 POST），可在測試途中切換，模擬斷線或下線一段時間。
- ``install(monkeypatch, router)`` 讓正式的 ``build_mcp_http_client`` 走這個 router，連線仍經
  ``connect_mcp_server``／``_connect_once``。
"""

from __future__ import annotations

import contextlib
import gc
import json
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx2
from mcp.server import MCPServer
from src.models.identity import AgentIdentity
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp.canonical import McpRequestContext
from src.protocols.mcp.http_client import build_mcp_http_client
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

Failure = Callable[[httpx2.Request], httpx2.Response]
_BASE_PORT = 8201
_INJECTED = "injected mid-flight failure"


def connect_error(request: httpx2.Request) -> httpx2.Response:
    """對方不在（Dispatch 重啟中、行程掛掉）。"""
    raise httpx2.ConnectError(_INJECTED, request=request)


def remote_disconnect(request: httpx2.Request) -> httpx2.Response:
    """對方在回應前斷線。"""
    raise httpx2.RemoteProtocolError(_INJECTED, request=request)


def redirect_302(request: httpx2.Request) -> httpx2.Response:
    """對方把呼叫轉址到別台。"""
    return httpx2.Response(302, headers={"Location": "https://elsewhere.test/mcp"}, request=request)


def _peer_app(name: str) -> Any:
    server = MCPServer(f"peer-{name}", version="1.0.0")

    @server.tool()
    def ping() -> str:
        return f"pong-{name}"

    return server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")


class PeerRouter(httpx2.AsyncBaseTransport):
    """把每台的請求轉給它的 ASGI app；``failures`` 指定的台改回注入的失敗。"""

    def __init__(self, *names: str) -> None:
        self.ports = {name: _BASE_PORT + index for index, name in enumerate(names)}
        self._names = {port: name for name, port in self.ports.items()}
        self.apps = {name: _peer_app(name) for name in names}
        self._inner = {
            self.ports[name]: httpx2.ASGITransport(app=app) for name, app in self.apps.items()
        }
        self.failures: dict[str, Failure | None] = {}
        self.fail_methods: set[str] | None = {"tools/call"}
        self.injected: Counter[str] = Counter()
        self.methods: dict[str, list[str]] = {name: [] for name in names}

    def config(self, name: str) -> ProtocolServerConfig:
        """指向這台的 remote 設定。"""
        return ProtocolServerConfig(url=f"http://127.0.0.1:{self.ports[name]}/mcp")

    def registry(self) -> ProtocolRegistry:
        """只含這些對端的 registry（不讀任何設定檔）。"""
        registry = ProtocolRegistry("__missing_mcp_asgi_peers_registry__.json")
        registry.servers = {name: self.config(name) for name in self.ports}
        return registry

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        name = self._names[request.url.port]
        if request.method == "POST":
            body = json.loads(await request.aread() or b"{}")
            method = str(body.get("method"))
            self.methods[name].append(method)
            failure = self.failures.get(name)
            if failure is not None and (self.fail_methods is None or method in self.fail_methods):
                self.injected[name] += 1
                return failure(request)
        return await self._inner[request.url.port].handle_async_request(request)

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator[PeerRouter]:
        """啟動每台 app 的 lifespan（stateless session manager 需要）。"""
        async with contextlib.AsyncExitStack() as served:
            for app in self.apps.values():
                await served.enter_async_context(app.router.lifespan_context(app))
            yield self

    def handshakes(self, name: str) -> int:
        """這台被連線（discover）的次數。"""
        return self.methods[name].count("server/discover")


def install(monkeypatch: Any, router: PeerRouter) -> None:
    """正式的 http client factory 改走 router；其餘連線程式不動。"""
    monkeypatch.setattr(
        mcp_client,
        "build_mcp_http_client",
        lambda **kwargs: build_mcp_http_client(**{**kwargs, "transport": router}),
    )


def request_context(name: str) -> McpRequestContext:
    """呼叫 ``name`` 用的前景請求 context。"""
    return McpRequestContext(
        request_id=f"isolation-{name}",
        session_id="isolation-session",
        identity=AgentIdentity.for_role("IsolationOwner", "Owner"),
        human_principal_id="human-isolation",
        installation_id=name,
        auth_level="Manual",
        origin="foreground",
    )


def task_leak_messages(caplog: Any) -> list[str]:
    """GC 之後，這個測試有沒有 Task exception was never retrieved 之類的 asyncio 警告。"""
    gc.collect()
    records = caplog.get_records("call") + caplog.records
    leaks = ("never retrieved", "destroyed but it is pending")
    return [r.getMessage() for r in records if any(leak in r.getMessage() for leak in leaks)]


async def ping(manager: Any, name: str) -> str:
    """呼叫 ``name`` 的 ping，回傳結果的 JSON 字串。"""
    outcome = await manager.call_tool_on_server(
        name, "ping", {}, request_context=request_context(name)
    )
    assert outcome.is_error is False
    return json.dumps(outcome.model_dump(mode="json"))
