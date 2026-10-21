"""K1（ADR-0157）：keepalive 心跳升核心單一實作——pump_send_round。

原為 claude adapter 內建（wait_for+shield 輪詢）；推廣三家時升 react_core
而非複製三份（本抽取案的海拔原則）。事件驅動測試（不真睡 5s）：把
``_KEEPALIVE_S`` 縮到 10ms、慢上游等 Event——觀察到心跳才放行上游。
"""

from __future__ import annotations

import asyncio

import pytest
from src.handlers import react_core

_HEARTBEAT = ": heartbeat\n\n"


@pytest.mark.asyncio
async def test_pump_emits_heartbeat_while_upstream_slow(monkeypatch):
    """上游久候 → 先發 SSE 心跳 comment、上游回覆後原樣透傳。"""
    monkeypatch.setattr(react_core, "_KEEPALIVE_S", 0.01)
    release = asyncio.Event()

    async def slow_round():
        await release.wait()
        yield {"ok": 1}

    items = []
    async for item in react_core.pump_send_round(slow_round()):
        items.append(item)
        if item == _HEARTBEAT:
            release.set()  # 觀察到心跳才放行上游（決定性、不靠真實 5s）

    assert items[0] == _HEARTBEAT
    assert items[-1] == {"ok": 1}
    assert all(i == _HEARTBEAT for i in items[:-1])


@pytest.mark.asyncio
async def test_pump_passthrough_fast_multi_item_round():
    """快速回覆（gemini 503 事件＋raw 的多項 round）原樣透傳、零心跳。"""

    async def fast_round():
        yield "data: {\"type\": \"touka_status\"}\n\n"
        yield {"ok": 2}

    items = [item async for item in react_core.pump_send_round(fast_round())]
    assert items == ["data: {\"type\": \"touka_status\"}\n\n", {"ok": 2}]


@pytest.mark.asyncio
async def test_pump_heartbeat_between_items(monkeypatch):
    """多項 round 的「後續項」久候同樣有心跳（覆蓋 gemini 退避睡眠段）。"""
    monkeypatch.setattr(react_core, "_KEEPALIVE_S", 0.01)
    release = asyncio.Event()

    async def retry_round():
        yield "data: retrying\n\n"
        await release.wait()
        yield {"ok": 3}

    items = []
    async for item in react_core.pump_send_round(retry_round()):
        items.append(item)
        if item == _HEARTBEAT:
            release.set()

    assert items[0] == "data: retrying\n\n"
    assert _HEARTBEAT in items
    assert items[-1] == {"ok": 3}


@pytest.mark.asyncio
async def test_pump_cancels_pending_task_on_early_close(monkeypatch):
    """G23（守衛涵蓋缺口）：消費端提前收流（aclose→GeneratorExit）時，pump 的 finally 須
    cancel 未完成的 next_task——否則客戶端斷線後殘留懸掛的 asyncio 任務（含仍在飛的上游
    LLM 請求）。這裡以永不完成的上游驗證：收流後上游 round 的 finally（cancel 觸發）必執行。"""
    monkeypatch.setattr(react_core, "_KEEPALIVE_S", 0.01)
    started = asyncio.Event()
    cleaned = {"v": False}

    async def slow_round():
        started.set()
        try:
            await asyncio.sleep(3600)  # 永不完成的上游（模擬在飛的 LLM 請求）
            yield {"never": 1}
        finally:
            cleaned["v"] = True  # next_task 被 cancel → CancelledError 於此觸發 finally

    gen = react_core.pump_send_round(slow_round())
    first = await gen.__anext__()          # 前進到第一個心跳（next_task 掛在 sleep 上）
    assert first == _HEARTBEAT
    await started.wait()

    await gen.aclose()                     # 消費端提前收流 → pump finally 應 cancel next_task
    for _ in range(3):
        await asyncio.sleep(0)             # 讓 cancel 傳播、slow_round 的 finally 執行

    assert cleaned["v"], "pump 未 cancel 掛起的 next_task＝孤兒任務（含在飛的上游請求）"
