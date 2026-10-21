"""中斷機制（#1）— 背景心跳多脈衝迴圈尊重中斷旗標。

修復前：`_process_heartbeat_loop` 是 detached BackgroundTask，無取消機制，一旦開跑
只能等 pulse_count × slots 跑完。本測試驗證：run 起點清除殘留旗標，且使用者於某個
pulse 後按停 → 後續 pulse 不再執行。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import heartbeat as hb
from src.services.activity_registry import activity_registry
from src.services.rhythm_service import rhythm_service


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_remaining_pulses_on_interrupt(monkeypatch):
    sid = "test-hb-interrupt-1"
    rhythm_service.clear_interrupt(sid)
    calls: list[int] = []

    async def fake_pulse(
        final_session_id, identity, model_override, ctx, pulse_slots, t0, init_prompt=""
    ):
        calls.append(1)
        # 第一個 pulse 跑完後，模擬使用者按下停止
        rhythm_service.interrupt_session(final_session_id)
        return 1

    monkeypatch.setattr(hb, "_execute_single_pulse", fake_pulse)
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())

    await hb._process_heartbeat_loop(
        final_session_id=sid,
        identity=AgentIdentity.for_role(agent_id="Alice", role="Owner"),
        model_override=None,
        ctx=PipelineContext(),
        pulse_count=5,
        pulse_slots=3,
        t0=0.0,
        init_prompt="",
        is_home_mode=False,
    )

    # 5 個 pulse 只跑了第 1 個，其餘因中斷而停
    assert len(calls) == 1
    rhythm_service.clear_interrupt(sid)


@pytest.mark.asyncio
async def test_heartbeat_loop_clears_stale_interrupt_at_start(monkeypatch):
    """run 起點會清除上一輪殘留的旗標，不致於誤停這次的 pulse。"""
    sid = "test-hb-interrupt-2"
    rhythm_service.interrupt_session(sid)  # 殘留旗標
    calls: list[int] = []

    async def fake_pulse(final_session_id, identity, model_override, ctx, pulse_slots, t0, init_prompt=""):
        calls.append(1)
        return 1

    monkeypatch.setattr(hb, "_execute_single_pulse", fake_pulse)
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())

    await hb._process_heartbeat_loop(
        final_session_id=sid,
        identity=AgentIdentity.for_role(agent_id="Alice", role="Owner"),
        model_override=None,
        ctx=PipelineContext(),
        pulse_count=2,
        pulse_slots=1,
        t0=0.0,
        init_prompt="",
        is_home_mode=False,
    )

    # 殘留旗標於起點被清除 → 兩個 pulse 都正常跑
    assert len(calls) == 2
    rhythm_service.clear_interrupt(sid)


@pytest.mark.asyncio
async def test_heartbeat_registers_and_clears_activity(monkeypatch):
    """P4：背景多脈衝期間登記活動（current/total），結束後清除。"""
    sid = "test-hb-activity-1"
    activity_registry.reload_all()
    rhythm_service.clear_interrupt(sid)
    seen: list[int | None] = []

    async def fake_pulse(
        final_session_id, identity, model_override, ctx, pulse_slots, t0, init_prompt=""
    ):
        # 執行期間應已登記目前脈衝進度
        snap = activity_registry.snapshot()
        seen.append(snap.get(final_session_id, {}).get("current"))
        return 1

    monkeypatch.setattr(hb, "_execute_single_pulse", fake_pulse)
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())

    await hb._process_heartbeat_loop(
        final_session_id=sid,
        identity=AgentIdentity.for_role(agent_id="Alice", role="Owner"),
        model_override=None,
        ctx=PipelineContext(),
        pulse_count=3,
        pulse_slots=1,
        t0=0.0,
        init_prompt="",
        is_home_mode=False,
    )

    # 3 個脈衝期間 current 依序為 1,2,3
    assert seen == [1, 2, 3]
    # 結束後清除（不再顯示「背景跑中」）
    assert sid not in activity_registry.snapshot()
    activity_registry.reload_all()
