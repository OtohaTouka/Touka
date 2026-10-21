"""E45（ADR-0086／0092，批⑤-A）：SoulLayer 無痕閘阻斷 context_summary 注入。

無痕閘（``soul.py:57-60`` 的 ``if not ctx.get("incognito"):``）同時包住
``inject_home_messages`` 與 ``inject_context_summary``。既有測試只在 SoulService 層
孤立測 ``inject_context_summary``（``test_prompt_block_delimiters``），且該處 ctx 無
session_id 使其 no-op——從不經 ``SoulLayer.process`` 的無痕閘。把 ``inject_context_summary``
移出 if 區塊（或閘塌成恆真）在所有既有測試下全綠，真人續聊帶舊 session_id 即洩漏先前摘要。
以下經 ``process()`` 驅動閘：無痕→不注入、非無痕→照常注入。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.middleware.layers import soul as soul_mod
from src.models.context import PipelineContext


def _patch_soul(monkeypatch) -> AsyncMock:
    """把 process() 走到無痕閘所需的 soul_service 相依全部 stub 掉，回傳 summary spy。"""
    inject_summary = AsyncMock()
    monkeypatch.setattr(soul_mod.soul_service, "inject_context_summary", inject_summary)
    monkeypatch.setattr(soul_mod.soul_service, "inject_home_messages", AsyncMock())
    monkeypatch.setattr(soul_mod.soul_service, "get_soul_data", lambda aid: ("SOUL", {}))
    monkeypatch.setattr(soul_mod.soul_service, "apply_soul_to_request", lambda req, aid: None)
    monkeypatch.setattr(soul_mod, "get_active_brain", lambda identity: "gemini-2.5-flash")
    # ADR-0245 修補：前情摘要只給綁在這個靈魂的 session；這裡讓 sess-1 綁在 Sumi，
    # 兩個測試才只差在無痕閘（無痕那例因此不會因 session 沒綁而空過）
    monkeypatch.setattr(
        soul_mod.session_service, "get_session_by_id", AsyncMock(return_value={"agent_id": "Sumi"})
    )
    monkeypatch.setattr(
        soul_mod.agent_service,
        "identify_agent",
        lambda aid: {"agent_id": aid, "is_toukasoul": True, "is_custom": False},
    )
    return inject_summary


def _req() -> SimpleNamespace:
    # 類名非 Anthropic → is_anthropic=False；model 非 ToukaGroup → 不早退。
    return SimpleNamespace(
        model="ToukaSoul", messages=[{"role": "user", "content": "hi"}], system=""
    )


@pytest.mark.asyncio
async def test_incognito_blocks_context_summary_injection(monkeypatch):
    """無痕 + 有 session_id → 摘要**不得**注入（零殘留對話）。

    移 inject_context_summary 出 if 區塊（或閘恆真）→ 無痕下仍注入 → assert_not_awaited 紅。
    """
    inject_summary = _patch_soul(monkeypatch)
    ctx = PipelineContext(
        is_toukasoul=True, agent_id="Sumi", incognito=True, session_id="sess-1"
    )
    await soul_mod.SoulLayer().process(_req(), ctx)
    inject_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_incognito_injects_context_summary(monkeypatch):
    """陽性對照：非無痕 + 有 session_id → 摘要注入照常（證明 spy 會動、閘確有把關）。"""
    inject_summary = _patch_soul(monkeypatch)
    req = _req()
    ctx = PipelineContext(
        is_toukasoul=True, agent_id="Sumi", incognito=False, session_id="sess-1"
    )
    await soul_mod.SoulLayer().process(req, ctx)
    inject_summary.assert_awaited_once_with(req, "sess-1")
