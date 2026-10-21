"""SDK v2 negotiation 失敗時 teardown 不得遮蔽原因（ADR-0194／0199）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.protocols.mcp import client as mcp_client
from src.protocols.registry import ProtocolServerConfig


class _BoomOnExit:
    async def __aenter__(self) -> _BoomOnExit:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        msg = "Attempted to exit cancel scope in a different task than it was entered in"
        raise RuntimeError(msg)


class _FailingClient:
    def __init__(self, _target: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FailingClient:
        msg = "HTTP 401: Unauthorized (invalid token)"
        raise ValueError(msg)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def test_original_error_survives_http_client_teardown_failure(monkeypatch):
    monkeypatch.setattr(
        mcp_client,
        "build_mcp_http_client",
        lambda **_kwargs: _BoomOnExit(),
    )
    monkeypatch.setattr(
        mcp_client,
        "streamable_http_client",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(mcp_client, "Client", _FailingClient)

    config = ProtocolServerConfig(
        url="https://x.example/mcp",
        protocol_mode="2026-07-28",
    )
    with pytest.raises(Exception) as exc_info:
        await mcp_client.connect_mcp_server(config)

    message = str(exc_info.value)
    assert "401" in message
    assert "cancel scope" not in message.lower()


async def test_stdio_negotiation_failure_preserves_cause(monkeypatch):
    monkeypatch.setattr(mcp_client, "Client", _FailingClient)

    config = ProtocolServerConfig(command="npx", args=["-y", "pkg@1.0.0"])
    with pytest.raises(Exception, match="401"):
        await mcp_client.connect_mcp_server(config)


class _CancelLeakClient(_FailingClient):
    async def __aenter__(self) -> _CancelLeakClient:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        return self


async def test_transport_cancel_leak_becomes_ordinary_error(monkeypatch):
    monkeypatch.setattr(mcp_client, "Client", _CancelLeakClient)

    config = ProtocolServerConfig(command="python", args=["server.py"])
    with pytest.raises(Exception) as exc_info:
        await mcp_client.connect_mcp_server(config)

    assert not isinstance(exc_info.value, asyncio.CancelledError)
    task = asyncio.current_task()
    assert task is not None
    assert task.cancelling() == 0


async def test_external_cancellation_is_still_respected(monkeypatch):
    class _DoubleCancelClient(_FailingClient):
        async def __aenter__(self) -> _DoubleCancelClient:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            task.cancel()
            await asyncio.sleep(0)
            return self

    monkeypatch.setattr(mcp_client, "Client", _DoubleCancelClient)

    config = ProtocolServerConfig(command="python", args=["server.py"])
    with pytest.raises(asyncio.CancelledError):
        await mcp_client.connect_mcp_server(config)
    task = asyncio.current_task()
    assert task is not None
    task.uncancel()


async def test_successful_remote_connect_closes_client_before_http(monkeypatch):
    closed: list[str] = []

    class _HTTPClient:
        async def __aenter__(self) -> _HTTPClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            closed.append("http")
            return False

    class _OKClient(_FailingClient):
        async def __aenter__(self) -> _OKClient:
            self.session = self
            return self

        async def send_discover(self, version: str) -> dict[str, Any]:
            return {
                "supportedVersions": [version],
                "capabilities": {},
                "resultType": "complete",
                "ttlMs": 0,
                "cacheScope": "public",
            }

        def adopt(self, result: Any) -> None:
            assert result.supported_versions == ["2026-07-28"]

        async def __aexit__(self, *exc: Any) -> bool:
            closed.append("client")
            return False

    monkeypatch.setattr(
        mcp_client,
        "build_mcp_http_client",
        lambda **_kwargs: _HTTPClient(),
    )
    monkeypatch.setattr(
        mcp_client,
        "streamable_http_client",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(mcp_client, "Client", _OKClient)

    config = ProtocolServerConfig(
        url="https://x.example/mcp",
        protocol_mode="2026-07-28",
    )
    _client, stack = await mcp_client.connect_mcp_server(config)
    await stack.aclose()

    assert closed == ["client", "http"]
