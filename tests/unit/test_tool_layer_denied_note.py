"""Tier 1（工具介面重設計提案 §4）：Gate 1 可讀拒絕——TDD 紅燈.

ToolLayer 對政策 DENY 的 builtin 工具不再無聲移除：
  1. ctx["denied_tools"] 記錄 {tool, reason}；ctx["tools_filtered"] 標示有過濾發生。
  2. 把一段 <tool_availability> 說明注入 system（模型不再幻覺被過濾的工具）。
     注意：pipeline 中 ContextLayer 在 ToolLayer 之前執行，故說明由 ToolLayer 自行注入。
  3. _inject_tools 撞名去重不再無聲（log warning）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.utils.permission_grants import NameGrant

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #


class _AnthropicStubRequest(SimpleNamespace):
    """type 名含 anthropic → 走 system 屬性注入路徑。"""


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    ctx["resumable_stream"] = True
    return ctx


def _make_request() -> SimpleNamespace:
    return SimpleNamespace(
        tools=None,
        project_id=None,
        ide_context=None,
        system=None,
        messages=[{"role": "user", "content": "hi"}],
    )


def _perm_mock(denied: set[str] | None = None, allowed: list[str] | None = None) -> MagicMock:
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(
        allowed if allowed is not None else ["*"]
    )
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True

    deny_set = denied or set()

    def _eval(policy_ctx: Any) -> MagicMock:
        if policy_ctx.resource in deny_set:
            return MagicMock(effect="DENY", reason="test-block")
        return MagicMock(effect="ALLOW", reason=None)

    perm.evaluate_policy.side_effect = _eval
    return perm


def _advertised_names(request: Any) -> set[str]:
    return {
        t.get("function", {}).get("name")
        for t in (request.tools or [])
        if isinstance(t, dict)
    }


def _system_text(request: Any) -> str:
    for msg in request.messages:
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role == "system":
            content = getattr(msg, "content", None) or (
                msg.get("content") if isinstance(msg, dict) else ""
            )
            return str(content)
    return ""


# --------------------------------------------------------------------------- #
# 測試
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_policy_denied_builtin_recorded_and_noted_openai():
    request = _make_request()
    ctx = _make_ctx()
    with patch(
        "src.middleware.layers.tool.permission_service",
        _perm_mock(denied={"execute_command"}),
    ):
        await ToolLayer().process(request, ctx)

    assert "execute_command" not in _advertised_names(request)  # 原行為：仍不廣告
    denied = ctx.get("denied_tools")
    assert denied == [{"tool": "execute_command", "reason": "test-block"}]
    assert ctx.get("tools_filtered") is True

    note = _system_text(request)
    assert "<tool_availability>" in note
    assert "execute_command" in note
    assert "test-block" in note


@pytest.mark.asyncio
async def test_no_filtering_no_note():
    request = _make_request()
    ctx = _make_ctx()
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()):
        await ToolLayer().process(request, ctx)

    assert not ctx.get("denied_tools")
    assert not ctx.get("tools_filtered")
    # 無過濾 → 不注入 Tier 1 專屬的 <tool_availability> 說明。
    # 註（P2a）：validation_protocol 依「廣告集含 mutating dev verb」另行注入、與過濾無關，
    # 故此處只斷言「無可用性說明」，不再斷言 system 完全為空。
    assert "<tool_availability>" not in _system_text(request)


@pytest.mark.asyncio
async def test_policy_denied_noted_in_anthropic_system():
    request = _AnthropicStubRequest(
        tools=None,
        project_id=None,
        ide_context=None,
        system=None,
        messages=[],
    )
    ctx = _make_ctx()
    with patch(
        "src.middleware.layers.tool.permission_service",
        _perm_mock(denied={"execute_command"}),
    ):
        await ToolLayer().process(request, ctx)

    assert isinstance(request.system, str)
    assert "<tool_availability>" in request.system
    assert "execute_command" in request.system


@pytest.mark.asyncio
async def test_rbac_narrowed_sets_filtered_flag_and_generic_note():
    """RBAC 收窄（非政策 DENY）：不逐一點名，但要有通用提示（防幻覺）。"""
    request = _make_request()
    ctx = _make_ctx()
    with patch(
        "src.middleware.layers.tool.permission_service",
        _perm_mock(allowed=["research"]),
    ):
        await ToolLayer().process(request, ctx)

    assert _advertised_names(request) == {"research"}
    assert ctx.get("tools_filtered") is True
    assert not ctx.get("denied_tools")  # 無政策 DENY
    assert "<tool_availability>" in _system_text(request)


def test_inject_tools_duplicate_name_warns(caplog: pytest.LogCaptureFixture):
    layer = ToolLayer()
    request = SimpleNamespace(tools=[])
    schema_a = {"type": "function", "function": {"name": "dup_tool", "description": "a"}}
    schema_b = {"type": "function", "function": {"name": "dup_tool", "description": "b"}}
    with caplog.at_level(logging.WARNING, logger="src.middleware.layers.tool"):
        layer._inject_tools(request, [schema_a, schema_b])

    assert len(request.tools) == 1
    assert "duplicate tool name" in caplog.text
    assert "dup_tool" in caplog.text
