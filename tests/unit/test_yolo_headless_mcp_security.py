"""ADR-0272 決定 4：YOLO 無人在場時，dangerous 的 MCP 工具只有人能核准（iso-c2 U1d，禁令 20）.

- 政策層：``mcp_server``＋風險 dangerous＋``human_present`` 不是 ``True``（缺鍵、``False``、
  字串、數字都算不在場）→ HUMAN_ONLY；有人在場、safe／read_only、原生工具照舊放行；
  Auto、Manual 不變；黑名單照舊 DENY。
- 執行層：心跳這類無人在場的 run 呼叫 dangerous 的 MCP 工具會被拒絕、伺服器沒被呼叫；
  互動對話照常執行。``human_present`` 只來自 ctx，模型的 args 改不動。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig, SafetyRules, ToolRiskConfig
from src.protocols.mcp.canonical import McpCompleteOutcome
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor

_ACTOR = AgentIdentity.for_role(agent_id="McpYoloProbe", role="Owner", is_human=False)
_MISSING = object()


def _config() -> AuthPoliciesConfig:
    return AuthPoliciesConfig(
        policies=SafetyRules(critical_blacklist=[r"^evil_remote$"]),
        tool_risk=ToolRiskConfig(
            default="dangerous",
            tools={"safe_remote": "safe", "lookup_remote": "read_only", "evil_remote": "safe"},
        ),
    )


def _effect(mode: str, tool: str, *, present: Any = _MISSING, mcp: bool = True) -> str:
    environment: dict[str, Any] = {"auth_level": mode, "isolation": "landlock"}
    if mcp:
        environment["mcp_server"] = "srv"
    if present is not _MISSING:
        environment["human_present"] = present
    context = PolicyContext(
        actor=_ACTOR, resource=tool, action="tool_call", environment=environment
    )
    return _config().evaluate(context).effect


@pytest.mark.parametrize("present", [_MISSING, False, None, "true", 1])
def test_yolo_headless_dangerous_mcp_needs_a_human(present):
    assert _effect("YOLO", "remote_write", present=present) == "HUMAN_ONLY"


def test_yolo_with_a_human_still_allows_dangerous_mcp():
    assert _effect("YOLO", "remote_write", present=True) == "ALLOW"


@pytest.mark.parametrize("tool", ["safe_remote", "lookup_remote"])
@pytest.mark.parametrize("present", [True, False])
def test_safe_and_read_only_mcp_tools_are_unchanged(tool, present):
    assert _effect("YOLO", tool, present=present) == "ALLOW"


@pytest.mark.parametrize("present", [True, False])
def test_native_dangerous_tool_is_not_affected(present):
    assert _effect("YOLO", "native_write", present=present, mcp=False) == "ALLOW"


@pytest.mark.parametrize(("mode", "effect"), [("Auto", "REVIEW"), ("Manual", "CONFIRM")])
def test_other_modes_are_unchanged(mode, effect):
    assert _effect(mode, "remote_write", present=False) == effect


def test_blacklisted_mcp_tool_stays_denied():
    assert _effect("YOLO", "evil_remote", present=False) == "DENY"
    assert _effect("YOLO", "evil_remote", present=True) == "DENY"


# --- 執行層 ---------------------------------------------------------------------------


class _FakeProtocolManager:
    def __init__(self) -> None:
        self.registry = MagicMock()
        self.registry.get_server.return_value = MagicMock(vault_jail_arg=None)
        self.calls: list[str] = []

    def resolve_tool(self, name: str) -> tuple[str, str]:
        return "srv", name

    async def call_tool_on_server(self, _sid, tool, _args, *, request_context):
        self.calls.append(tool)
        return McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": "ok"}],
        )


async def _call(monkeypatch, ctx: dict[str, Any], args: dict[str, Any]) -> tuple[str, list[str]]:
    monkeypatch.setattr(permission_service, "evaluate_policy", _config().evaluate)
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: True)
    manager = _FakeProtocolManager()
    executor = ToolExecutor(manager, {}, {}, {})
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(5):
        result = await executor.execute_to_queue("remote_write", args, "req-mcp", ctx, queue)
    return result, manager.calls


async def test_heartbeat_yolo_dangerous_mcp_is_refused(monkeypatch):
    ctx = {"identity": _ACTOR, "auth_level": "YOLO", "session_id": None, "heartbeat_run": True}
    # 模型自己在 args 帶 human_present／_hitl_preapproved 也沒用
    args = {"q": 1, "human_present": True, "_hitl_preapproved": True}
    result, calls = await _call(monkeypatch, ctx, args)
    assert json.loads(result)["reason"] == "human_only"
    assert calls == []


async def test_interactive_yolo_dangerous_mcp_runs(monkeypatch):
    ctx = {"identity": _ACTOR, "auth_level": "YOLO", "session_id": None, "resumable_stream": True}
    result, calls = await _call(monkeypatch, ctx, {"q": 1})
    assert calls == ["remote_write"]
    assert "human_only" not in result
