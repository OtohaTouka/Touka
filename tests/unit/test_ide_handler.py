"""ToukaProxy → ToukaIDE SDK v2 client contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.protocols.ide import handler as ide_handler
from src.protocols.ide.handler import _parse_mcp_result, call_ide_tool


def test_parse_mcp_result_returns_text_from_content_list():
    res_json = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "Hello from tool"}]},
    }
    assert _parse_mcp_result(res_json) == "Hello from tool"


def test_parse_mcp_result_empty_content_list_returns_empty_string():
    assert _parse_mcp_result({"result": {"content": []}}) == ""


def test_parse_mcp_result_missing_result_key_returns_empty_string():
    assert _parse_mcp_result({"jsonrpc": "2.0", "id": 1}) == ""


def test_parse_mcp_result_missing_text_key_returns_empty_string():
    assert _parse_mcp_result({"result": {"content": [{"type": "image"}]}}) == ""


def test_parse_mcp_result_multi_content_returns_first():
    payload = {
        "result": {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]
        }
    }
    assert _parse_mcp_result(payload) == "first"


@pytest.mark.asyncio
async def test_call_ide_tool_uses_shared_v2_client_and_closes_stack(monkeypatch):
    result = SimpleNamespace(
        content=[
            SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "type": "text",
                    "text": "workspace tree output",
                }
            )
        ],
        is_error=False,
    )
    client = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    stack = SimpleNamespace(aclose=AsyncMock())
    captured = {}

    async def _connect(config, **_kwargs):
        captured["config"] = config
        return client, stack

    monkeypatch.setattr(ide_handler, "connect_mcp_server", _connect)
    ide_clients = {
        "ToukaUI": {
            "url": "http://localhost:3005/mcp",
            "token": "test-token",
            "protocol_mode": "2026-07-28",
        }
    }

    actual = await call_ide_tool(
        ide_clients,
        "get_workspace_structure",
        {"depth": 3},
    )

    assert actual == {
        "content": [{"type": "text", "text": "workspace tree output"}],
        "isError": False,
    }
    client.call_tool.assert_awaited_once_with(
        "get_workspace_structure",
        {"depth": 3},
        meta=None,  # 沒有人類卡片核准：不帶「由人核准」標記（ADR-0246 U2c-7）
    )
    stack.aclose.assert_awaited_once()
    config = captured["config"]
    assert config.url == "http://localhost:3005/mcp"
    assert config.protocol_mode == "2026-07-28"
    assert config.headers == {"Authorization": "Bearer ${TOUKA_IDE_SESSION_TOKEN}"}
    assert config.env == {"TOUKA_IDE_SESSION_TOKEN": "test-token"}


@pytest.mark.asyncio
async def test_call_ide_tool_closes_stack_when_tool_call_fails(monkeypatch):
    client = SimpleNamespace(call_tool=AsyncMock(side_effect=RuntimeError("tool failed")))
    stack = SimpleNamespace(aclose=AsyncMock())

    async def _connect(_config, **_kwargs):
        return client, stack

    monkeypatch.setattr(ide_handler, "connect_mcp_server", _connect)

    with pytest.raises(RuntimeError, match="tool failed"):
        await call_ide_tool(
            {
                "ToukaUI": {
                    "url": "http://localhost:3005/mcp",
                    "token": "test-token",
                }
            },
            "get_open_file",
            {},
        )

    stack.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_ide_tool_raises_when_no_touka_ui_client():
    with pytest.raises(ConnectionError, match="ToukaUI IDE Server"):
        await call_ide_tool({}, "get_open_file", {})


@pytest.mark.asyncio
async def test_call_ide_tool_rejects_missing_discovery_token():
    with pytest.raises(ConnectionError, match="Bearer token"):
        await call_ide_tool(
            {"ToukaUI": {"url": "http://localhost:3005/mcp", "token": ""}},
            "get_open_file",
            {},
        )
