"""harness_context 區塊（OS 環境事實宣告）——TDD。

soul.md 管「我是誰」；<harness_context> 管「我在哪、這個世界的物理法則」——由 OS
（code）擁有、ToolLayer 注入、與人格零重疊（只講事實與機制、零風格指令）。設計拍板：
英文內文、名稱 harness_context、穩定層（不入 VOLATILE_SYSTEM_TAGS→跨回合快取）、
內容按「實際廣告工具面」條件化（沒有 dispatch 權限的 agent 不會被告知可以派工）、
admin 檢查走既有 context snapshot 分頁（唯讀）。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import context_snapshot_service as css
from src.services.prompt_blocks import (
    ALL_BLOCK_TAGS,
    HARNESS_CONTEXT,
    HARNESS_CONTEXT_MAX_TOKENS,
    VOLATILE_SYSTEM_TAGS,
    build_harness_context,
)
from src.utils.permission_grants import NameGrant
from src.utils.token_estimator import estimate_tokens

_CJK_RE = re.compile(r"[一-鿿]")

_FULL_FACE = frozenset({
    "dispatch_subagent", "dispatch_subagent_group", "mission_tools",
    "execute_command", "memory_tools", "send_message", "read", "write",
})


# --------------------------------------------------------------------------- #
# tag SSOT：註冊＋快取分層
# --------------------------------------------------------------------------- #


def test_tag_registered_and_stable_tier():
    """tag 進 ALL_BLOCK_TAGS（snapshot/守衛自動繼承）且不入波動清單（跨回合快取）。"""
    assert HARNESS_CONTEXT == "harness_context"
    assert HARNESS_CONTEXT in ALL_BLOCK_TAGS
    assert HARNESS_CONTEXT not in VOLATILE_SYSTEM_TAGS


# --------------------------------------------------------------------------- #
# 純函式：build_harness_context
# --------------------------------------------------------------------------- #


def test_builder_core_facts_present():
    """完整工具面：harness 宣告、模型、角色、防注入句、soul 分界、摘要機制都在。"""
    block = build_harness_context(
        model="claude-test-brain", role="Owner", advertised=_FULL_FACE
    )
    assert f"<{HARNESS_CONTEXT}>" in block
    assert f"</{HARNESS_CONTEXT}>" in block
    assert "Touka OS" in block
    assert "claude-test-brain" in block  # 「現在是哪個模型」——擁有者的核心訴求
    assert "Owner" in block
    assert "not by any user" in block  # 防注入：本塊由 OS 注入
    assert "soul" in block.lower()  # 與人格的分界宣告（不覆蓋 persona）
    assert "<conversation_summary>" in block  # 長對話摘要機制
    assert "heartbeat" in block.lower()  # 心跳世界觀（Touka 特有）


def test_builder_sections_follow_advertised_face():
    """內容跟著實際工具面走：沒被廣告的機制不得聲稱存在（防誤導＝防漂移）。"""
    block = build_harness_context(model="m", role="Base", advertised=frozenset())
    assert "dispatch_subagent" not in block
    assert "execute_command" not in block
    assert "mission board" not in block
    # 核心事實不隨工具面消失
    assert "Touka OS" in block
    assert f"<{HARNESS_CONTEXT}>" in block


def test_builder_delegation_claims_match_recursion_gate():
    """派工段的行為承諾（Subagent 面、不可再派、耐久）——Guard #40 的文字端。"""
    block = build_harness_context(
        model="m", role="Owner", advertised=frozenset({"dispatch_subagent"})
    )
    assert "cannot dispatch further sub-agents" in block
    assert "restart" in block  # 耐久交付：跨重啟可續 await


def test_builder_english_only():
    """拍板：英文內文（語言一致性可被 eval 對照；guard 亦釘）。"""
    block = build_harness_context(
        model="claude-test-brain", role="Owner", advertised=_FULL_FACE
    )
    assert not _CJK_RE.search(block)


def test_builder_token_ceiling():
    """膨脹紀律：完整面 ≤ HARNESS_CONTEXT_MAX_TOKENS（常數同模組、import 防漂移）。"""
    block = build_harness_context(
        model="claude-test-brain", role="Owner", advertised=_FULL_FACE
    )
    assert estimate_tokens(block, "claude-opus-4-8") <= HARNESS_CONTEXT_MAX_TOKENS


def test_builder_no_style_directives():
    """邊界：只講事實與機制，不得出現風格／語氣指令（歸 soul 管）。"""
    block = build_harness_context(
        model="m", role="Owner", advertised=_FULL_FACE
    ).lower()
    for banned in ("be concise", "be friendly", "your tone", "personality is"):
        assert banned not in block


# --------------------------------------------------------------------------- #
# ToolLayer 注入（toukasoul 面才注入；子代理 is_toukasoul=False 自然排除）
# --------------------------------------------------------------------------- #


class _AnthropicStubRequest(SimpleNamespace):
    """type 名含 anthropic → 走 system 屬性注入路徑。"""


def _make_ctx(is_toukasoul: bool) -> PipelineContext:
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    ctx["resumable_stream"] = True
    if is_toukasoul:
        ctx["is_toukasoul"] = True
    return ctx


def _perm_mock() -> MagicMock:
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.side_effect = lambda _c: MagicMock(effect="ALLOW", reason=None)
    return perm


@pytest.mark.asyncio
async def test_injected_for_toukasoul_with_resolved_model():
    """toukasoul 面（SoulLayer 已解析 model）→ 注入且含實際模型名。"""
    request = _AnthropicStubRequest(
        tools=None, project_id=None, ide_context=None, system=None,
        messages=[], model="claude-resolved-brain",
    )
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()):
        await ToolLayer().process(request, _make_ctx(is_toukasoul=True))
    assert isinstance(request.system, str)
    assert f"<{HARNESS_CONTEXT}>" in request.system
    assert "claude-resolved-brain" in request.system


@pytest.mark.asyncio
async def test_not_injected_for_non_toukasoul():
    """非 toukasoul（子代理組裝、直通模型）→ 不注入（子代理精簡版屬日後範圍）。"""
    request = _AnthropicStubRequest(
        tools=None, project_id=None, ide_context=None, system=None,
        messages=[], model="claude-raw",
    )
    with patch("src.middleware.layers.tool.permission_service", _perm_mock()):
        await ToolLayer().process(request, _make_ctx(is_toukasoul=False))
    assert f"<{HARNESS_CONTEXT}>" not in str(request.system or "")


# --------------------------------------------------------------------------- #
# snapshot 整合：BLOCK_META 覆蓋＋穩定層歸類
# --------------------------------------------------------------------------- #


def test_snapshot_meta_and_stable_tier():
    """Guard #39 契約：新 tag 必有 BLOCK_META；快照歸 stable（跨回合快取可視化）。"""
    assert HARNESS_CONTEXT in css.BLOCK_META
    system = (
        "<persona>P</persona>\n"
        f"<{HARNESS_CONTEXT}>Touka OS facts</{HARNESS_CONTEXT}>\n"
        "<mission_awareness>1/3</mission_awareness>\n"
    )
    snap = css.build_snapshot(
        agent_id="DebugAgent", model="claude-opus-4-8",
        system_text=system, tools=[], home_messages=[],
    )
    tier = {b["tag"]: b["cache_tier"] for b in snap["blocks"] if b.get("present")}
    assert tier[HARNESS_CONTEXT] == "stable"
