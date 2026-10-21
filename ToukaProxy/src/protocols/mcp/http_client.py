"""MCP remote transport 的受控 ``httpx2`` client factory（ADR-0199）。"""

from __future__ import annotations

import logging
import ssl
from typing import TYPE_CHECKING

import httpx2

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from httpx2 import AsyncBaseTransport, Auth

logger = logging.getLogger(__name__)

MCP_HTTP_CONNECT_TIMEOUT_SECONDS = 30.0
MCP_HTTP_READ_TIMEOUT_SECONDS = 60.0
MCP_HTTP_WRITE_TIMEOUT_SECONDS = 30.0
MCP_HTTP_POOL_TIMEOUT_SECONDS = 30.0

MCP_HTTP_EXCEPTIONS = (
    httpx2.TimeoutException,
    httpx2.TransportError,
)

_SAFE_LOG_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-method",
        "user-agent",
    }
)


def redact_http_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """只保留診斷安全 header 值，其餘一律遮罩。"""
    return {
        name: value if name.lower() in _SAFE_LOG_HEADERS else "<redacted>"
        for name, value in headers.items()
    }


def redact_http_url(url: object) -> str:
    """Drop query/fragment so credentials and raw resource parameters never enter logs."""
    return str(url).partition("?")[0].partition("#")[0]


class McpRedirectRefusedError(httpx2.TransportError):
    """remote MCP 回 3xx：一律不跟（禁令 22）。

    直接繼承 ``TransportError``：不是 ConnectError／NetworkError，模型層 retry 不會重試。
    訊息只有狀態碼與去掉 query 的原網址，不帶 Location、不讀回應內容。
    """

    def __init__(self, response: httpx2.Response) -> None:
        url = redact_http_url(response.request.url)
        super().__init__(f"MCP HTTP {response.status_code} redirect refused: {url}")


async def _reject_redirect(response: httpx2.Response) -> None:
    """任何 3xx 都在 SDK 看到之前丟錯；先記一條已遮罩的 warning（上層會吞掉訊息）。"""
    if response.is_redirect:
        logger.warning(
            "MCP HTTP %s redirect refused: %s",
            response.status_code,
            redact_http_url(response.request.url),
        )
        raise McpRedirectRefusedError(response)


async def _log_request(request: httpx2.Request) -> None:
    """以已脫敏 header 記錄 remote MCP request。"""
    logger.debug(
        "MCP HTTP request %s %s headers=%s",
        request.method,
        redact_http_url(request.url),
        redact_http_headers(request.headers),
    )


def build_mcp_http_client(
    *,
    headers: Mapping[str, str] | None = None,
    ca_file: Path | None = None,
    auth: Auth | None = None,
    transport: AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """建立 Touka 唯一的 MCP remote HTTP client。

    禁止環境 proxy 與 redirect；TLS 永遠使用顯式 ``SSLContext``，需要私有 CA 時由
    ``ca_file`` 注入。timeout 四個維度皆明確設定，避免 SDK default 漂移。
    ``follow_redirects=False`` 保留，但 mcp 2.2 起 SDK 不看這個設定、會自己跟同源
    轉址；真正擋下轉址的是 response hook ``_reject_redirect``（ADR-0199 dep1）。
    """
    ssl_context = ssl.create_default_context(cafile=str(ca_file) if ca_file is not None else None)
    timeout = httpx2.Timeout(
        connect=MCP_HTTP_CONNECT_TIMEOUT_SECONDS,
        read=MCP_HTTP_READ_TIMEOUT_SECONDS,
        write=MCP_HTTP_WRITE_TIMEOUT_SECONDS,
        pool=MCP_HTTP_POOL_TIMEOUT_SECONDS,
    )
    return httpx2.AsyncClient(
        headers=dict(headers or {}),
        auth=auth,
        transport=transport,
        verify=ssl_context,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [_log_request], "response": [_reject_redirect]},
    )
