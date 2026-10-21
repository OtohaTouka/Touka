"""Gemini send_round 的送出次數上限（ADR-0244，1b-U3；3b-U5a 重試搬到主迴圈）。

主迴圈的 adapter（``stream=True``）每輪只送 1 次、不在 adapter 裡等；可重試的錯誤由 react_core
退避重送（test_main_loop_retry.py）。主迴圈以外只有 complete_once，``max_attempts``＝1（R3 不重試，
契約測試在 test_complete_once_contract.py）；原本「沒設上限就送 _MAX_RETRIES＝3 次」那條沒有呼叫者，
R-3b ③ 照禁令 46 移除。
退避等待以 gemini 模組視角的 asyncio 代理換掉（同 test_golden_gemini 的做法），不真睡。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.gemini_adapter import GeminiAdapter

from tests.helpers.stream_transport import FakeUpstream, Reply


@pytest.fixture
def sleep(monkeypatch) -> AsyncMock:
    waits = AsyncMock(return_value=None)
    monkeypatch.setattr("src.handlers.adapters.gemini_adapter.asyncio",
                        SimpleNamespace(sleep=waits, Queue=asyncio.Queue))
    return waits


@pytest.fixture
def posts(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def failing_post(_client, url, **_kwargs):
        sent.append(url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)
    monkeypatch.setattr(
        "src.handlers.adapters.gemini_adapter.asyncio",
        SimpleNamespace(sleep=AsyncMock(return_value=None), Queue=asyncio.Queue),
    )
    return sent


async def _drain(adapter: GeminiAdapter, state) -> list:
    items = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    return items


async def _state(adapter: GeminiAdapter):
    request = OpenAIChatRequest(model="gemini-2.5-flash",
                                messages=[OpenAIMessage(role="user", content="hi")])
    return await adapter.prepare_request(request, {"gemini_api_key": "fake-gemini"})


async def test_main_loop_sends_once_and_does_not_wait(monkeypatch, sleep):
    """3b-U5a 翻轉：主迴圈那條原本送 3 次；現在 adapter 只送 1 次、不等待。

    R-3b ② M1：還沒收到 200 就連不上，標成可重試，由 react_core 重送。"""
    upstream = FakeUpstream([Reply(error=httpx.ConnectError("down"))] * 3).install(monkeypatch)
    adapter = GeminiAdapter(stream=True)
    state = await _state(adapter)
    items = await _drain(adapter, state)
    assert len(upstream.requests) == 1
    assert items == [RoundError("API Error (Timeout)\n\n`No Response`")]
    assert items[0].retry_note == "Gemini connection error"
    sleep.assert_not_awaited()


async def test_single_attempt_when_capped(posts):
    adapter = GeminiAdapter()
    state = await _state(adapter)
    assert state.max_attempts == 1  # 主迴圈以外的預設就是 1（complete_once 也明寫 1）
    items = await _drain(adapter, state)
    assert len(posts) == 1
    assert isinstance(items[-1], RoundError)
