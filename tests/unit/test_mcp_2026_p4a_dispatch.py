"""ADR-0201 / MCP 2026 P4-A：Dispatch transport 與應用憑證邊界。"""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from session_manager import SessionManager

from ToukaDispatch import server as dispatch_server
from ToukaDispatch.telemetry import DispatchTelemetry

MODERN_VERSION = "2026-07-28"


def test_q36_dispatch_exposes_only_modern_and_health_routes():
    app = dispatch_server.create_dispatch_app()
    assert {getattr(route, "path", "").rstrip("/") for route in app.routes} == {
        "/mcp",
        "/health",
    }
    assert not hasattr(app.state, "legacy_mcp")


@pytest.mark.parametrize(
    ("method", "version"),
    [("initialize", None), ("initialize", "2025-11-25"), ("tools/list", "2025-11-25")],
)
async def test_q36_dispatch_rejects_legacy_wire_on_modern_endpoint(method, version):
    app = dispatch_server.create_dispatch_app()
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if version is not None:
        headers["mcp-protocol-version"] = version
    params = (
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "q36-old-wire", "version": "1"},
        }
        if method == "initialize"
        else {}
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8083"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    raw = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        payloads = [
            json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")
        ]
        assert len(payloads) == 1
        payload = payloads[0]
    else:
        payload = response.json()
    assert "result" not in payload
    assert payload["error"]["code"] == -32020


def _session_token(result: Any) -> str:
    text = result.content[0].text
    assert "Session token:" in text
    return text.split("Session token:", 1)[1].strip()


def test_session_store_contains_hashes_not_bearer_secrets():
    manager = SessionManager()
    session_token = manager.create_session(
        {"name": "untrusted-caller-name"},
        transport="modern",
    )
    mission_token = manager.rotate_mission_token("mission-a")

    assert session_token not in repr(manager._sessions)
    assert mission_token not in repr(manager._mission_tokens)
    assert "untrusted-caller-name" not in repr(manager._sessions)
    session = manager.validate(session_token, transport="modern")
    assert session is not None
    assert not hasattr(session, "session_token")
    assert not hasattr(session, "caller_info")
    assert session.token_hash != session_token
    assert session.audience == "dispatch-internal"


def test_session_validation_uses_constant_time_digest_comparison(monkeypatch):
    manager = SessionManager()
    token = manager.create_session({"name": "internal"})
    calls: list[tuple[str, str]] = []
    original = secrets.compare_digest

    def capture(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("session_manager.secrets.compare_digest", capture)

    assert manager.validate(token) is not None
    assert calls
    assert all(token not in pair for pair in calls)


def test_session_is_bound_to_transport_audience_and_method():
    manager = SessionManager()
    internal = manager.create_session(
        {"name": "internal"},
        transport="modern",
    )
    mission_token = manager.rotate_mission_token("mission-a")
    credential = manager.validate_mission_credential(mission_token)
    assert credential is not None
    external = manager.create_external_session(
        credential.mission_id,
        {"name": "external"},
        generation=credential.generation,
        transport="legacy",
    )

    assert (
        manager.validate(
            internal,
            transport="modern",
            method="dispatch_initialize_mission",
        )
        is not None
    )
    assert manager.validate(internal, transport="legacy") is None
    assert (
        manager.validate(
            external,
            transport="legacy",
            method="dispatch_initialize_mission",
        )
        is None
    )
    assert (
        manager.validate(
            external,
            transport="legacy",
            method="dispatch_get_board",
        )
        is not None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("audience", "dispatch-mission"),
        ("caller_type", "external"),
    ),
)
def test_session_rejects_inconsistent_server_owned_audience(
    field: str,
    value: str,
):
    manager = SessionManager()
    token = manager.create_session(
        {"name": "internal"},
        transport="modern",
    )
    key, info = next(iter(manager._sessions.items()))
    manager._sessions[key] = replace(info, **{field: value})

    assert manager.validate(token, transport="modern") is None


def test_mission_rotation_atomically_revokes_old_credential_and_sessions():
    manager = SessionManager()
    old_mission_token = manager.rotate_mission_token("mission-a")
    old_credential = manager.validate_mission_credential(old_mission_token)
    assert old_credential is not None
    old_session = manager.create_external_session(
        old_credential.mission_id,
        {"name": "external"},
        generation=old_credential.generation,
        transport="modern",
    )

    new_mission_token = manager.rotate_mission_token("mission-b")

    assert manager.validate_mission_token(old_mission_token) is None
    assert manager.validate(old_session, transport="modern") is None
    assert manager.validate_mission_token(new_mission_token) == "mission-b"


def test_mission_credential_is_consumed_exactly_once():
    manager = SessionManager()
    mission_token = manager.rotate_mission_token("mission-a")

    credential = manager.consume_mission_credential(mission_token)

    assert credential is not None
    assert credential.mission_id == "mission-a"
    assert manager.consume_mission_credential(mission_token) is None
    assert manager.validate_mission_credential(mission_token) is None


def test_process_restart_invalidates_all_ephemeral_credentials():
    before_restart = SessionManager()
    session_token = before_restart.create_session({"name": "internal"})
    mission_token = before_restart.rotate_mission_token("mission-a")

    after_restart = SessionManager()

    assert after_restart.validate(session_token) is None
    assert after_restart.validate_mission_token(mission_token) is None


@pytest.mark.asyncio
async def test_dispatch_app_serves_modern_discover_tools_and_application_auth(
    monkeypatch,
):
    monkeypatch.setenv("DISPATCH_TOKEN", "p4a-internal-token")
    monkeypatch.setattr(dispatch_server, "_session_mgr", SessionManager())
    monkeypatch.setattr(dispatch_server, "_telemetry", DispatchTelemetry())
    app = dispatch_server.create_dispatch_app()

    paths = {getattr(route, "path", "") for route in app.routes}
    assert {"/mcp", "/health"} == paths
    assert not hasattr(app.state, "legacy_mcp")

    records: list[httpx2.Request] = []

    async def capture(request: httpx2.Request) -> None:
        records.append(request)

    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8083",
            event_hooks={"request": [capture]},
        ) as http_client,
    ):
        target = streamable_http_client(
            "http://127.0.0.1:8083/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(target, mode="auto", cache=None) as client:
            tools = await client.list_tools()
            assert client.protocol_version == MODERN_VERSION
            assert client.session.discover_result is not None
            assert {tool.name for tool in tools.tools} == {
                "dispatch_handshake",
                "dispatch_initialize_mission",
                "dispatch_update_task",
                "dispatch_get_board",
            }
            handshake = await client.call_tool(
                "dispatch_handshake",
                {
                    "dispatch_token": "p4a-internal-token",
                    "caller_name": "p4a-modern-client",
                },
            )
            token = _session_token(handshake)
            board = await client.call_tool(
                "dispatch_get_board",
                {"session_token": token},
            )
            assert "Unauthorized" not in board.content[0].text

        health = await http_client.get("/health")
        assert health.status_code == 200
        telemetry = health.json()["telemetry"]
        assert telemetry["handshakes"]["internal"] == 1
        assert telemetry["last_used"]["modern"]["protocol_version"] == MODERN_VERSION

    assert records
    assert all(request.url.path == "/mcp" for request in records if request.method == "POST")
    assert all("mcp-session-id" not in request.headers for request in records)


def test_dispatch_child_contract_uses_primary_modern_endpoint():
    env = dispatch_server._build_child_env("mission-token", "mission-a")

    assert env["DISPATCH_URL"] == (f"http://localhost:{dispatch_server._DISPATCH_PORT}/mcp")
