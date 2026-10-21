"""隔離 MCP 的環境值不得從執行期錯誤、健康欄位或審批預覽外洩。"""

from __future__ import annotations

import json
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.models.mcp_catalog import MCPPromptGetRequest, MCPResourceReadRequest
from src.protocols import manager as mod
from src.protocols.mcp.canonical import McpOperationException, McpRequestContext
from src.protocols.mcp.handles import LegacyMcpHandle
from src.protocols.mcp.owner import McpConnectionOwner
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers import mcp as mcp_router
from src.services.approval_flow import approval_args_preview, card_isolation
from src.services.isolation.consent import ConsentKey
from src.services.isolation.mcp_launch import McpLaunch, McpLaunchBook, McpLaunchInfo
from src.utils.mcp_secrets import redact_values

from tests.helpers.mcp_transport import _FakeTransport

CANARY = "unusualResolvedCredential123"
LINE = "secondCredentialLine456"
ESCAPED = "canary\\value'\"秘密"
TOOL_URL = "http://localhost:8000"
INFO = McpLaunchInfo("self", "creation", "ok", "launched", "landlock")


@pytest.fixture
async def connected(monkeypatch):
    registry = ProtocolRegistry("__no_secret_config__.json")
    registry.servers = {"s": ProtocolServerConfig(command="fake", env={
        "TOKEN": "__RESOLVED__", "TOOL_BASE_URL": TOOL_URL + "/configured",
    })}
    manager = mod.ProtocolManager(registry, launch_book=McpLaunchBook())
    session = SimpleNamespace(list_tools=AsyncMock(side_effect=RuntimeError(CANARY + " " + LINE)))
    transport = _FakeTransport(session=session)
    monkeypatch.setattr(mod, "connect_mcp_server", transport.connect)
    monkeypatch.setattr(manager, "_start_subscription", Mock())
    monkeypatch.setattr(manager, "_connection_lost_hook", Mock())
    launch = McpLaunch(INFO, acquired=object(), exec_env={
        "TOKEN": CANARY + "\n" + LINE, "TOOL_BASE_URL": TOOL_URL,
    })
    await manager.connect_server("s", registry.servers["s"], launch=launch)
    yield manager, session
    await manager.cleanup()


async def test_health_failure_masks_resolved_and_multiline_values(connected, caplog):
    manager, _session = connected
    result = await manager.get_active_server_info()
    assert result[0]["status"] == "error"
    assert "<redacted>" in result[0]["last_error"]
    assert CANARY not in json.dumps(result) + caplog.text
    assert LINE not in json.dumps(result) + caplog.text


async def test_catalog_failure_logs_no_traceback_secret(connected, caplog):
    manager, _session = connected
    assert await manager.get_all_tools() == []
    assert CANARY not in caplog.text and LINE not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_loss_and_later_health_keep_the_sanitized_cause(connected, caplog):
    manager, session = connected
    key = manager.handle_key_for("s", None)
    owner = manager.handle_pool.get(key).stack
    await manager._on_connection_lost(key, owner, ConnectionError(CANARY + " " + LINE))
    lost = manager._installations["s"].last_error
    dead = Mock(spec=McpConnectionOwner)
    dead.alive = False
    dead.close = AsyncMock()
    manager.handle_pool.put(key, LegacyMcpHandle(session, dead))
    result = await manager.get_active_server_info()
    session.list_tools.assert_not_awaited()
    assert result[0]["status"] == "error"
    assert result[0]["last_error"] == lost
    assert CANARY not in json.dumps(result) + caplog.text
    assert LINE not in json.dumps(result) + caplog.text


async def test_server_display_metadata_does_not_echo_environment(connected):
    manager, session = connected
    session.list_tools.side_effect = None
    session.list_tools.return_value = SimpleNamespace(tools=[])
    session.server_info = {"name": CANARY, "version": LINE}
    result = await manager.get_active_server_info()
    assert result[0]["server_info"] == {"name": "<redacted>", "version": "<redacted>"}
    assert CANARY not in json.dumps(result) and LINE not in json.dumps(result)


@pytest.mark.parametrize("tool_name", ["ping", CANARY])
async def test_tool_exception_chain_does_not_expose_raw_server_error(
    connected, monkeypatch, tool_name
):
    manager, session = connected
    ctx = McpRequestContext(
        request_id="secret-check", identity=AgentIdentity.for_role("Probe", "Debugger"),
        installation_id="s", auth_level="Manual", origin="foreground",
    )
    monkeypatch.setattr(manager, "_tool_catalog_from_session",
                        AsyncMock(return_value=SimpleNamespace(items=[])))
    monkeypatch.setattr(mod, "_validate_catalog_header_policy", Mock())
    session.call_tool = AsyncMock(side_effect=ConnectionError(CANARY))
    with pytest.raises(McpOperationException) as caught:
        await manager._call_tool_on_authorized_server("s", tool_name, {}, request_context=ctx)
    assert CANARY not in "".join(traceback.format_exception(caught.value))


async def test_mcp_approval_preview_never_contains_launch_environment(connected):
    preview = approval_args_preview({"query": "example"})
    card = await card_isolation(ConsentKey("Probe", "session", "call"), "s__ping")
    assert CANARY not in json.dumps(preview) + repr(card)
    assert LINE not in json.dumps(preview) + repr(card)


async def test_cleanup_releases_resolved_environment_after_owners_close(connected):
    manager, _session = connected
    await manager.cleanup()
    assert not manager._connection_owners
    assert manager._sanitize_for("s", CANARY) == CANARY


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("method", [
    "list_server_tools", "list_server_resources", "list_server_resource_templates",
    "list_server_prompts", "read_server_resource", "get_server_prompt",
])
async def test_catalog_api_errors_do_not_expose_environment(
    connected, monkeypatch, caplog, method, error_type
):
    manager, _session = connected
    host_method = "list_server_tool_descriptors" if method == "list_server_tools" else method
    monkeypatch.setattr(manager, host_method, AsyncMock(side_effect=error_type(CANARY)))
    monkeypatch.setattr(mcp_router, "verify_origin", Mock())
    monkeypatch.setattr(
        mcp_router.permission_service, "is_mcp_server_allowed", Mock(return_value=True)
    )
    kwargs = {
        "server_name": "s", "protocol_manager": manager,
        "http_request": Request({"type": "http", "headers": [], "state": {}}),
        "identity": AgentIdentity.for_role("Probe", "Debugger"),
    }
    if method == "read_server_resource":
        kwargs["payload"] = MCPResourceReadRequest(uri="mcp-test://example/readme")
    elif method == "get_server_prompt":
        kwargs["payload"] = MCPPromptGetRequest(name="example", arguments={})
    if method == "list_server_tools":
        result = await getattr(mcp_router, method)(**kwargs)
        assert result["tools"] == []
    else:
        with pytest.raises(ToukaException) as caught:
            await getattr(mcp_router, method)(**kwargs)
        assert CANARY not in "".join(traceback.format_exception(caught.value))
    assert CANARY not in caplog.text


async def test_catalog_issue_masks_echoed_item_name(connected):
    manager, _session = connected
    issue = manager._catalog_issue("s", CANARY, ValueError(LINE))
    assert CANARY not in issue.model_dump_json() and LINE not in issue.model_dump_json()


@pytest.mark.parametrize("message", [CANARY, "cancel scope " + CANARY])
async def test_close_failure_log_has_no_raw_transport_error(connected, caplog, message):
    caplog.set_level("DEBUG")
    manager, _session = connected
    stack = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError(message)))
    await manager._close_handles([LegacyMcpHandle(None, stack)])
    assert CANARY not in caplog.text


@pytest.mark.parametrize("encode", [str, repr, json.dumps])
@pytest.mark.parametrize("part", [ESCAPED, LINE, ESCAPED + "\n" + LINE])
def test_masking_covers_escaped_whole_values_and_lines(encode, part):
    assert redact_values(encode(part), [ESCAPED + "\n" + LINE]) == encode("<redacted>")


@pytest.mark.parametrize("values,text", [
    (["abcdXYZ1", "XYZ1efgh"], "abcdXYZ1efgh"),
    (["ababa"], "abababa"),
])
@pytest.mark.parametrize("binary", [False, True])
def test_masking_merges_overlaps_in_original_text(values, text, binary):
    message, expected = "before " + text + " after", "before <redacted> after"
    if binary:
        message, expected = message.encode(), expected.encode()
    assert redact_values(message, values) == expected


async def test_lost_connection_logs_type_and_string_with_escaped_secret(connected, caplog):
    manager, _ = connected
    manager._launch_secrets["s"].add(ESCAPED)
    key = manager.handle_key_for("s", None)
    owner = manager.handle_pool.get(key).stack
    await manager._on_connection_lost(key, owner, ConnectionError(ESCAPED))
    message = next(r.getMessage() for r in caplog.records if "MCP 連線中斷" in r.getMessage())
    assert message.endswith("ConnectionError: <redacted>")


async def test_tool_base_url_remains_visible_and_metadata_keys_do_not_collide(connected):
    manager, _ = connected
    for url in [TOOL_URL, TOOL_URL + "/configured"]:
        # Not a secret; URL path segments are shown only through the ban-23 whitelist.
        assert "<redacted>" not in manager._sanitize_for("s", url)
    assert manager._sanitize_metadata("s", {CANARY: [CANARY], LINE: {LINE: LINE}}) == {
        CANARY: ["<redacted>"], LINE: {LINE: "<redacted>"},
    }


def test_short_values_keep_the_existing_minimum_length():
    assert redact_values("info true yes", ["info", "true", "yes"]) == "<redacted> <redacted> yes"
