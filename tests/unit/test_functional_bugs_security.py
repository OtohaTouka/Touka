"""Two functional bugs surfaced by the DA11–DA16 adversarial review (DA19/DA20).

Both are "broken, not exploitable" — they silently disable real functionality:

  DA19 — request_mcp_creation (evolution step 4 / deploy) called request_approval
         with `agent_id=<str>`, but request_approval takes `identity: AgentIdentity`
         (routers/terminal.py). The resulting TypeError was swallowed by a broad
         `except Exception`, so the tool ALWAYS returned "Error" and could never
         deploy an evolved MCP capability. Fix passes `identity=`.

  DA20 — /debug/execute P14 Debugger whitelist called
         get_capabilities_by_id(...).get("allowed_tools") — but that method returns a
         list[str], so `.get()` raised AttributeError → any Debugger executing ANY
         tool got a 500. Fix uses the SSOT Gate-1 API get_allowed_tools + is_tool_allowed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.evolution_security import EvolutionPathGuard
from src.services.identity_router import get_verified_identity
from src.tools.builtin.evolution import request_mcp_creation
from src.utils.permission_grants import NameGrant

DEBUGGER = AgentIdentity.for_role(
    agent_id="DebugAgent", role="Debugger", is_human=True, capabilities=[]
)


class TestDA19RequestMcpCreation:
    @pytest.mark.asyncio
    async def test_request_approval_receives_identity_not_agent_id(self, monkeypatch):
        """The HITL approval call must pass `identity` (AgentIdentity), not `agent_id`,
        so request_approval no longer TypeErrors into a swallowed 'Error'."""
        captured: dict = {}

        async def _stub(**kwargs):
            captured.update(kwargs)
            return (False, {})

        monkeypatch.setattr("src.tools.builtin.evolution.request_approval", _stub)
        monkeypatch.setattr(EvolutionPathGuard, "tool_file_exists", lambda skill_id, fn: True)
        monkeypatch.setattr(EvolutionPathGuard, "read_tool_file", lambda skill_id, fn: "{}")

        # ADR-0149：request_mcp_creation 現於 headless fail-closed（自帶審批）；
        # DA19 驗的是「request_approval 收 identity 非 agent_id」——該通道只在互動路徑
        # 觸發，故以 _interactive=True 跑（DA19 不變式與新 gate 正交）。
        result = await request_mcp_creation("myskill", agent_id="X", _interactive=True)

        assert "identity" in captured
        assert "agent_id" not in captured  # the wrong kwarg is gone
        assert isinstance(captured["identity"], AgentIdentity)
        assert captured["identity"].agent_id == "X"
        # stub denied → "Rejected", NOT a swallowed "Error: ...unexpected keyword argument"
        assert result.startswith("Rejected")
        assert "unexpected keyword argument" not in result


class TestDA20DebuggerWhitelist:
    def test_forbidden_tool_denied_without_attributeerror(self, monkeypatch):
        """A Debugger executing a non-whitelisted tool must get 403 — not a 500 from
        the old `.get()`-on-a-list AttributeError."""
        app.dependency_overrides[get_verified_identity] = lambda: DEBUGGER
        monkeypatch.setattr(
            "src.services.permission_service.permission_service.get_allowed_tools",
            lambda identity: NameGrant.from_lists(["safe_tool"]),
        )
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/debug/execute", json={"tool_name": "danger_tool", "args": {}})
        app.dependency_overrides.clear()
        assert r.status_code == 403

    def test_allowed_tool_passes_whitelist(self, monkeypatch):
        """A whitelisted tool must pass the P14 gate and reach the executor (no crash)."""
        app.dependency_overrides[get_verified_identity] = lambda: DEBUGGER
        monkeypatch.setattr(
            "src.services.permission_service.permission_service.get_allowed_tools",
            lambda identity: NameGrant.from_lists(["safe_tool"]),
        )

        async def _fake_exec(*args, **kwargs):
            return "ok"

        monkeypatch.setattr(
            "src.services.tool_executor.ToolExecutor.execute_to_queue", _fake_exec
        )
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/debug/execute", json={"tool_name": "safe_tool", "args": {}})
        app.dependency_overrides.clear()
        assert r.status_code == 200
        assert r.json()["result"] == "ok"
