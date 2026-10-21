"""ask_user 工具單元測試（HAX G10，ADR-0085）。

驗證工具會 emit ask_user_request 事件、暫停等待、收到答案後回傳格式化結果；逾時回哨兵。
"""

import asyncio
import json

import pytest
from src.services.ask_user_store import _reset_ask_user_store, get_ask_user_store
from src.tools.builtin.interaction import ask_user


@pytest.fixture(autouse=True)
def _reset():
    _reset_ask_user_store()
    yield
    _reset_ask_user_store()


@pytest.mark.asyncio
async def test_ask_user_emits_event_and_returns_formatted_answers():
    emitted: list[dict] = []

    async def emit(event: dict) -> None:
        emitted.append(event)

    questions = [{"question": "選一個", "options": ["A", "B"], "multiSelect": False}]
    store = get_ask_user_store()

    async def answer_when_asked():
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        token = emitted[0]["id"]
        store.deliver_answer(token, [["A"]])

    task = asyncio.create_task(answer_when_asked())
    result = await ask_user(questions=questions, _emit_event=emit, _call_id="tok1")
    await task

    assert emitted[0]["type"] == "ask_user_request"
    assert emitted[0]["id"] == "tok1"
    parsed = json.loads(result)
    assert parsed[0]["question"] == "選一個"
    assert parsed[0]["answer"] == ["A"]


@pytest.mark.asyncio
async def test_ask_user_timeout_returns_sentinel():
    async def emit(event: dict) -> None:
        return None

    questions = [{"question": "q", "options": ["A"], "multiSelect": False}]
    result = await ask_user(
        questions=questions, _emit_event=emit, _call_id="tok2", _timeout=0.1
    )
    assert "未" in result
