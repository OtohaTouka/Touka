# ruff: noqa: ANN201, D102, TC002, TC003
"""Request-time OAuth bearer injection for the MCP httpx2 transport。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

import httpx2
from src.protocols.mcp.oauth.service import OAuthBearerCredential


class OAuthCredentialProvider(Protocol):
    """Request-aware provider held across one complete HTTP attempt。"""

    def __call__(
        self,
        *,
        wire_request: httpx2.Request,
    ) -> AbstractAsyncContextManager[OAuthBearerCredential]: ...


class McpOAuthBearerAuth(httpx2.Auth):
    """Fetch and fence the exact active grant for every outbound HTTP request。"""

    def __init__(
        self,
        provider: OAuthCredentialProvider,
    ) -> None:
        self._provider = provider

    async def async_auth_flow(self, request: httpx2.Request):
        async with self._provider(wire_request=request) as credential:
            request.headers["Authorization"] = credential.authorization_header.get_secret_value()
            request.extensions["touka_oauth_private_cidrs"] = credential.allowed_private_cidrs
            yield request
