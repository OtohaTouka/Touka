"""P2a-1：驗證慣例 prompt block（提案 P2a；Codex「Validating your work」）——TDD.

「正確」不是工具層能判定的——一次無錯誤的工具調用仍可能語意錯誤。故把「改動後
自我驗證」定為**慣例**（prompt）。積極度依審批模式（Manual＝提醒、自動＝強制）。
ToolLayer 僅在廣告工具含 mutating dev verb（開發流程）時注入，不擾一般 persona 對話。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.prompt_blocks import (
    ALL_BLOCK_TAGS,
    VALIDATION_PROTOCOL,
    build_validation_protocol,
)
from src.utils.permission_grants import NameGrant

# --------------------------------------------------------------------------- #
# 純函式：build_validation_protocol
# --------------------------------------------------------------------------- #


def test_validation_protocol_tag_registered():
    assert VALIDATION_PROTOCOL == "validation_protocol"
    assert VALIDATION_PROTOCOL in ALL_BLOCK_TAGS


def test_build_validation_protocol_wraps_and_has_core_message():
    block = build_validation_protocol("Manual")
    assert f"<{VALIDATION_PROTOCOL}>" in block
    assert f"</{VALIDATION_PROTOCOL}>" in block
    # 核心：無錯誤 ≠ 正確、要驗證
    assert "VALIDATE" in block
    assert "correct" in block.lower()


def test_build_validation_protocol_mode_variants_differ():
    manual = build_validation_protocol("Manual")
    auto = build_validation_protocol("Auto")
    yolo = build_validation_protocol("YOLO")
    assert "Manual mode" in manual
    assert "Auto mode" in auto
    assert "YOLO mode" in yolo
    assert len({manual, auto, yolo}) == 3
    # 看不懂的值（含退役舊名稱）照 Manual 寫，不宣稱自動執行
    assert build_validation_protocol("SafeAuto") == manual


def test_manual_variant_reflects_approval_channel_reality():
    """H4：Manual 句與真實行為對齊——互動＝審批卡暫停（H2/H3 broker）、
    headless＝fail-closed 拒絕；不再是 fail-open 年代的
    「each tool call is human-approved」謊言。"""
    interactive = build_validation_protocol("Manual", interactive=True)
    assert "pauses for a human approval" in interactive
    assert "human-approved" not in interactive

    headless = build_validation_protocol("Manual", interactive=False)
    assert "denied fail-closed" in headless
    assert "Manual mode" in headless
    assert interactive != headless


# --------------------------------------------------------------------------- #
# ToolLayer 注入（僅開發流程＝廣告含 mutating dev verb 時）
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
        tools=None, project_id=None, ide_context=None, system=None,
        messages=[{"role": "user", "content": "hi"}],
    )


def _perm_mock(allowed: list[str] | None = None) -> MagicMock:
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(
        allowed if allowed is not None else ["*"]
    )
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.side_effect = lambda _c: MagicMock(effect="ALLOW", reason=None)
    return perm


def _system_text(request: Any) -> str:
    for msg in request.messages:
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role == "system":
            return str(
                getattr(msg, "content", None)
                or (msg.get("content") if isinstance(msg, dict) else "")
            )
    return ""


@pytest.mark.asyncio
async def test_injected_when_mutating_verb_advertised_manual():
    """預設 allowed=* → write/edit/execute_command 廣告 → 注入（Manual 變體）。"""
    request = _make_request()
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()):
        await ToolLayer().process(request, _make_ctx())
    sys = _system_text(request)
    assert f"<{VALIDATION_PROTOCOL}>" in sys
    assert "Manual mode" in sys


@pytest.mark.asyncio
async def test_not_injected_when_only_readonly_tool():
    """RBAC 收窄到唯讀工具 → 無 mutating verb → 不注入（不擾一般對話）。"""
    request = _make_request()
    with patch("src.middleware.layers.tool.permission_service", _perm_mock(allowed=["research"])):
        await ToolLayer().process(request, _make_ctx())
    assert f"<{VALIDATION_PROTOCOL}>" not in _system_text(request)


@pytest.mark.asyncio
async def test_autonomous_variant_when_auth_level_not_manual():
    """會話為自動審批模式 → 注入強制自我驗證變體。"""
    request = _make_request()
    ctx = _make_ctx()
    ctx["session_id"] = "sess-auto"
    session = {"auth_level": "YOLO", "agent_id": "DebugAgent", "active_mcp_servers": []}
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()), patch(
        "src.utils.async_db.session_service.get_session_by_id",
        new=AsyncMock(return_value=session),
    ):
        await ToolLayer().process(request, ctx)
    sys = _system_text(request)
    assert f"<{VALIDATION_PROTOCOL}>" in sys
    assert "YOLO mode" in sys


@pytest.mark.asyncio
async def test_injected_into_anthropic_system():
    request = _AnthropicStubRequest(
        tools=None, project_id=None, ide_context=None, system=None, messages=[]
    )
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()):
        await ToolLayer().process(request, _make_ctx())
    assert isinstance(request.system, str)
    assert f"<{VALIDATION_PROTOCOL}>" in request.system
