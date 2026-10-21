"""AskUserStore 單元測試（HAX G10，ADR-0085）。

驗證「暫停-等待-續跑」原語：deliver→wait 取回答案、逾時回哨兵、未知 token 靜默失敗、
並發 deliver 解除等待。仿 dispatch_callback_store 範式。
"""

import asyncio

import pytest
from src.services.ask_user_store import _reset_ask_user_store, get_ask_user_store


@pytest.fixture(autouse=True)
def _reset():
    _reset_ask_user_store()
    yield
    _reset_ask_user_store()


@pytest.mark.asyncio
async def test_deliver_then_wait_returns_answers():
    store = get_ask_user_store()
    token = store.generate_token()
    store.register(token)
    assert store.deliver_answer(token, [["甲"]]) is True
    result = await store.wait_for_answer(token, timeout=2)
    assert result["answers"] == [["甲"]]


@pytest.mark.asyncio
async def test_wait_timeout_returns_status():
    store = get_ask_user_store()
    token = store.generate_token()
    store.register(token)
    result = await store.wait_for_answer(token, timeout=0.1)
    assert result["status"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_deliver_unknown_token_is_false():
    store = get_ask_user_store()
    assert store.deliver_answer("nope", [["甲"]]) is False


@pytest.mark.asyncio
async def test_wait_unknown_token_returns_status():
    store = get_ask_user_store()
    result = await store.wait_for_answer("nope", timeout=0.1)
    assert result["status"] == "UNKNOWN_TOKEN"


@pytest.mark.asyncio
async def test_concurrent_deliver_unblocks_wait():
    store = get_ask_user_store()
    token = store.generate_token()
    store.register(token)

    async def deliver_later():
        await asyncio.sleep(0.05)
        store.deliver_answer(token, [["X", "其他：自訂"]])

    task = asyncio.create_task(deliver_later())
    result = await store.wait_for_answer(token, timeout=2)
    await task
    assert result["answers"] == [["X", "其他：自訂"]]
