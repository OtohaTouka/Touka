"""Regression（管線順序）：<mission_awareness> 必須撐過真實管線順序.

生產管線相對順序為 ContextLayer → … → ToolLayer（見 proxy_service.pipeline）。
歷史缺陷：ToolLayer 於 step 12「任務感知數據準備」才把 mission 寫入
``ctx["mission_awareness"]`，並註解「傳遞物件，讓 ContextLayer 決定如何呈現」——
但 ContextLayer 早已執行完畢，導致 <mission_awareness> 區塊**永遠不會**注入
system prompt（靜默功能流失／dead code path）。

修復方向（比照 ToolLayer._inject_availability_note，2026-07-03 起可用的
module-level inject helpers）：由 ToolLayer 於備妥 mission 後**自行注入**區塊。

以下測試在**修復前為 RED**（區塊缺席）、**修復後為 GREEN**（區塊存在且僅一次）。
ADR-0092 context breakdown 以 prompt_blocks.ALL_BLOCK_TAGS 解析 <mission_awareness>
標籤，故標籤名稱不得更動——測試同時鎖定該字面標籤。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import (
    AnthropicMessage,
    AnthropicRequest,
    OpenAIChatRequest,
    OpenAIMessage,
)
from src.middleware.layers.context import ContextLayer, render_mission_block
from src.middleware.layers.tool import ToolLayer
from src.middleware.pipeline import ProxyPipeline
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.utils.permission_grants import NameGrant

# ToolLayer step 12 以區域匯入 MissionManager；於來源模組 patch 類別方法即可攔截。
_MISSION_MGR = "memory_server.services.mission_manager.MissionManager.get_current_mission"


def _perm_allow_all() -> MagicMock:
    """允許全部工具的 permission_service 替身：確保 ToolLayer 不注入 <tool_availability>."""
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
    return perm


def _fake_mission():
    """建立一個進行中的假 mission（純物件，不觸碰磁碟／data/）。

    1 個 DONE + 1 個 DOING → 進度 1/2；status=DOING 通過 ToolLayer 的 != "DONE" 閘門。
    """
    tasks = [
        SimpleNamespace(
            status="DONE", id="t1", description="設置", dependencies=[], artifact_ref=None
        ),
        SimpleNamespace(
            status="DOING", id="t2", description="驗證", dependencies=["t1"], artifact_ref=None
        ),
    ]
    return SimpleNamespace(status="DOING", goal="驗證任務感知注入", tasks=tasks)


def _ctx() -> PipelineContext:
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger"))
    ctx["agent_id"] = "DebugAgent"
    ctx["resumable_stream"] = True
    return ctx


def _openai_system_text(request: OpenAIChatRequest) -> str:
    return "\n".join(
        m.content
        for m in request.messages
        if getattr(m, "role", None) == "system" and isinstance(m.content, str)
    )


@pytest.mark.asyncio
async def test_toollayer_self_injects_mission_anthropic():
    """修復核心：ToolLayer 晚於 ContextLayer，須自行把 <mission_awareness> 注入 system。"""
    request = AnthropicRequest(
        model="claude-3-5-sonnet-latest",
        messages=[AnthropicMessage(role="user", content="下一步？")],
        system="Original System Prompt.",
    )
    ctx = _ctx()

    with patch("src.middleware.layers.tool.permission_service", _perm_allow_all()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=_fake_mission())
    ):
        await ToolLayer().process(request, ctx)

    assert isinstance(request.system, str)
    assert "<mission_awareness>" in request.system
    assert "</mission_awareness>" in request.system
    assert "驗證任務感知注入" in request.system
    assert "1/2" in request.system  # 1 DONE / 2 tasks
    assert request.system.count("<mission_awareness>") == 1  # 僅注入一次


@pytest.mark.asyncio
async def test_toollayer_self_injects_mission_openai():
    """OpenAI 形狀：區塊須注入 messages 的 system 訊息（inject_openai_system 路徑）。"""
    request = OpenAIChatRequest(
        model="gpt-4o",
        messages=[OpenAIMessage(role="user", content="下一步？")],
    )
    ctx = _ctx()

    with patch("src.middleware.layers.tool.permission_service", _perm_allow_all()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=_fake_mission())
    ):
        await ToolLayer().process(request, ctx)

    system_text = _openai_system_text(request)
    assert "<mission_awareness>" in system_text
    assert "驗證任務感知注入" in system_text
    assert system_text.count("<mission_awareness>") == 1


@pytest.mark.asyncio
async def test_mission_block_survives_real_pipeline_order():
    """以生產相對順序 ContextLayer → ToolLayer 跑一遍：區塊必須存在，且僅注入一次。

    這是缺陷的直接複現：ContextLayer 先讀（ctx 尚無 mission）→ ToolLayer 後備妥。
    修復前 system 內完全沒有區塊（RED）；修復後由 ToolLayer 自注入（GREEN，count==1）。
    """
    request = AnthropicRequest(
        model="claude-3-5-sonnet-latest",
        messages=[AnthropicMessage(role="user", content="下一步？")],
        system="Original System Prompt.",
    )
    ctx = _ctx()

    pipeline = ProxyPipeline([ContextLayer(), ToolLayer()])
    with patch("src.middleware.layers.tool.permission_service", _perm_allow_all()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=_fake_mission())
    ):
        await pipeline.execute(request, ctx)

    assert "<mission_awareness>" in request.system
    assert "驗證任務感知注入" in request.system
    assert request.system.count("<mission_awareness>") == 1


@pytest.mark.asyncio
async def test_done_mission_not_injected():
    """已完成的 mission（status==DONE）不應注入（ToolLayer 閘門）。"""
    done_mission = SimpleNamespace(
        status="DONE",
        goal="已完成目標",
        tasks=[
            SimpleNamespace(
                status="DONE", id="t1", description="x", dependencies=[], artifact_ref=None
            )
        ],
    )
    request = AnthropicRequest(
        model="claude-3-5-sonnet-latest",
        messages=[AnthropicMessage(role="user", content="hi")],
        system="Base.",
    )
    ctx = _ctx()

    with patch("src.middleware.layers.tool.permission_service", _perm_allow_all()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=done_mission)
    ):
        await ToolLayer().process(request, ctx)

    assert "<mission_awareness>" not in request.system


def test_render_mission_block_format():
    """render_mission_block 產出正確 tag、進度比例與狀態圖示（SSOT 渲染，供兩層共用）。"""
    block = render_mission_block(_fake_mission())

    assert block.count("<mission_awareness>") == 1
    assert "</mission_awareness>" in block
    assert "驗證任務感知注入" in block
    assert "1/2" in block  # 1 DONE / 2 tasks
    assert "✅ t1" in block  # DONE 圖示
    assert "⏳ t2" in block  # DOING 圖示
    assert "(依賴: t1)" in block  # 依賴渲染
