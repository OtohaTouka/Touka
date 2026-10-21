"""U4a-2：react_core 的插話取出點、送達狀態與回合結束（ADR-0249 第 4a 段；裁定 4、5）。

- 取出點只在 ``append_tool_results`` 之後、進下一輪之前，而且只在 ``ui_authorized``；
  心跳與子代理（沒有 session）拿不到佇列。
- 取出＝pending(step=N)；下一輪送出成功才 delivered 並送 ``interjection_delivered``；
  下一輪重試全失敗、或回合結束時還沒送達的，一律 undelivered，送一次
  ``interjection_undelivered``。
- urgent：下一輪 ``tool_choice=none``，再下一輪恢復原本的值。
- 佇列是空的時候什麼都不送（40 份 golden 另外守）。

上游是相容 Chat 串流的假傳輸，工具執行換成假的，佇列與串流查詢用注入的實例。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.interjections import InterjectionRound
from src.models.identity import AgentIdentity
from src.models.session_persistence import UiGenerationStopped
from src.services.interjection_queue import InMemoryInterjectionQueue

from tests.helpers.stream_transport import FakeUpstream, status_reply, stream_reply


def openai_text(text: str) -> dict:
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def openai_tools(name: str, args: dict) -> dict:
    call = {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}
    return {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


_GUARD_S = 5.0
_SID, _STREAM = "s-ij", "st-ij"
_IDENTITY = AgentIdentity.for_role("Sumi", "Owner")
_NOTE = "使用者在工具執行期間補充：{}"
_TOOLS = [{"type": "function", "function": {"name": "vault_tools", "description": "列出檔案",
           "parameters": {"type": "object", "properties": {}}}}]
_TOOL = openai_tools("vault_tools", {"action": "list"})


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """工具執行換成假的、不入帳、退避不真睡。"""
    async def run(self, name, args, request_id, ctx, queue, call_id=None,
                  actually_used_tools=None):
        await queue.put(None)
        return f"{name} ok"

    monkeypatch.setattr(react_core.ToolExecutor, "execute_to_queue", run)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())


def _ctx(rid: str, **extra: Any) -> dict:
    return {"protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
            "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
            "request_id": rid, "t0": 0.0, "agent_id": "Sumi", "identity": _IDENTITY,
            "provider": "Grok", "url": "https://api.x.test/v1/chat/completions",
            "api_key": "fake-key", "custom_connection": False, "session_id": _SID,
            "ui_authorized": True, **extra}


async def _run(queue: InMemoryInterjectionQueue, ctx: dict,
               tools: list | None = None) -> tuple[list[str], str | None]:
    """跑完一回合；UI 路徑的停止／錯誤以 UiGenerationStopped 結束，一併回傳。"""
    request = OpenAIChatRequest(model="grok-4", tools=tools,
                                messages=[OpenAIMessage(role="user", content="hi")])
    chunks: list[str] = []
    stopped = None
    async with asyncio.timeout(_GUARD_S):
        try:
            async for chunk in react_core.run_react_loop(
                    OpenAIAdapter(stream=True), request, ctx, interjections=queue,
                    stream_state=lambda sid: {"stream_id": _STREAM, "status": "active"}
                    if sid == _SID else None):
                chunks.append(chunk)
        except UiGenerationStopped as stop:
            stopped = stop.generation
    return chunks, stopped


def _events(chunks: list[str], kind: str) -> list[dict]:
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e for e in events if e.get("type") == kind]


def _enqueue_at(queue: InMemoryInterjectionQueue, request_no: int, text: str, *,
                urgent: bool = False) -> tuple[list[str], Any]:
    """第 ``request_no`` 個請求送出時（那一輪進行中）使用者加一則插話。"""
    ids: list[str] = []

    def hook(count: int) -> None:
        if count == request_no:
            ids.append(queue.enqueue(_SID, _STREAM, text, urgent=urgent).id)

    return ids, hook


async def test_drained_at_step_n_and_delivered_after_the_next_round_is_sent(monkeypatch):
    queue = InMemoryInterjectionQueue()
    ids, hook = _enqueue_at(queue, 2, "先看 b")
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), stream_reply(_TOOL, "one"),
                             stream_reply(openai_text("好"), "one")], on_request=hook)
    upstream.install(monkeypatch)
    chunks, stopped = await _run(queue, _ctx("r-ij-1"))
    assert stopped is None
    assert upstream.sent[1]["messages"][-1]["role"] == "tool"  # 第 2 輪還沒帶
    assert upstream.sent[2]["messages"][-1] == {"role": "user", "content": _NOTE.format("先看 b")}
    assert _events(chunks, "interjection_delivered") == [{
        "type": "interjection_delivered", "interjection_id": ids[0], "step": 2,
        "urgent": False, "text": "先看 b"}]
    delivered = next(i for i, c in enumerate(chunks) if "interjection_delivered" in c)
    answer = next(i for i, c in enumerate(chunks) if '"content": "好"' in c)
    assert delivered < answer  # 下一輪送出成功之後、這一輪的文字之前
    assert [(i.status, i.step) for i in queue.list_items(_SID, _STREAM)] == [("delivered", 2)]
    assert _events(chunks, "interjection_undelivered") == []


async def test_next_round_failing_every_retry_turns_it_undelivered(monkeypatch):
    queue = InMemoryInterjectionQueue()
    ids, hook = _enqueue_at(queue, 1, "改成 c")
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), *[status_reply(503)] * 3],
                            on_request=hook)
    upstream.install(monkeypatch)
    chunks, stopped = await _run(queue, _ctx("r-ij-2"))
    assert (stopped, len(upstream.requests)) == ("error", 4)
    assert _events(chunks, "interjection_delivered") == []
    assert _events(chunks, "interjection_undelivered") == [
        {"type": "interjection_undelivered", "interjection_ids": ids}]
    assert [i.status for i in queue.list_items(_SID, _STREAM)] == ["undelivered"]


@pytest.mark.parametrize("ending", ["text", "budget", "wrap-up-tools"])
async def test_turn_end_returns_what_was_never_drained(monkeypatch, ending):
    """純文字結束、預算閘結束、收尾輪仍吐工具：還在排隊的都回給前端（只送一次）。"""
    queue = InMemoryInterjectionQueue()
    ids, hook = _enqueue_at(queue, 1, "還有 d")
    if ending == "text":
        replies = [stream_reply(openai_text("好"), "one")]
    else:
        monkeypatch.setattr(react_core.rhythm_service, "consume_round", lambda _rid: False)
        last = openai_text("總結") if ending == "budget" else _TOOL
        replies = [stream_reply(_TOOL, "one"), stream_reply(last, "one")]
    FakeUpstream(replies, on_request=hook).install(monkeypatch)
    chunks, stopped = await _run(queue, _ctx(f"r-ij-3-{ending}"))
    assert stopped is None
    assert _events(chunks, "interjection_undelivered") == [
        {"type": "interjection_undelivered", "interjection_ids": ids}]
    assert chunks.index(next(c for c in chunks if "interjection_undelivered" in c)) < \
        chunks.index("data: [DONE]\n\n")
    assert [i.status for i in queue.list_items(_SID, _STREAM)] == ["undelivered"]


async def test_stop_pressed_after_drain_returns_it_as_undelivered(monkeypatch):
    queue = InMemoryInterjectionQueue()
    ids, hook = _enqueue_at(queue, 1, "等等")
    stop = {"pressed": False}
    real_run = react_core.ToolExecutor.execute_to_queue

    async def press_stop_while_tools_run(self, *args, **kwargs):
        stop["pressed"] = True
        return await real_run(self, *args, **kwargs)

    monkeypatch.setattr(react_core.ToolExecutor, "execute_to_queue", press_stop_while_tools_run)
    monkeypatch.setattr(react_core.rhythm_service, "is_interrupted",
                        lambda _sid: stop["pressed"])
    FakeUpstream([stream_reply(_TOOL, "one")], on_request=hook).install(monkeypatch)
    chunks, stopped = await _run(queue, _ctx("r-ij-stop"))
    assert stopped == "cancelled"
    assert _events(chunks, "interjection_undelivered") == [
        {"type": "interjection_undelivered", "interjection_ids": ids}]


async def test_urgent_sets_tool_choice_none_for_one_round_then_restores(monkeypatch):
    queue = InMemoryInterjectionQueue()
    ids, hook = _enqueue_at(queue, 1, "先停一下", urgent=True)
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), stream_reply(_TOOL, "one"),
                             stream_reply(_TOOL, "one"), stream_reply(openai_text("好"), "one")],
                            on_request=hook)
    upstream.install(monkeypatch)
    chunks, _ = await _run(queue, _ctx("r-ij-urgent"), tools=_TOOLS)  # 有工具才送 none（M9）
    assert "tool_choice" not in upstream.sent[0]
    assert upstream.sent[1]["tool_choice"] == "none"  # 取出後的下一輪
    assert "tool_choice" not in upstream.sent[2]  # 再下一輪恢復原本（沒設）
    assert _events(chunks, "interjection_delivered")[0]["urgent"] is True


async def test_plain_interjection_leaves_tool_choice_alone(monkeypatch):
    queue = InMemoryInterjectionQueue()
    _ids, hook = _enqueue_at(queue, 1, "順便看 e")
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), stream_reply(openai_text("好"), "one")],
                            on_request=hook)
    upstream.install(monkeypatch)
    await _run(queue, _ctx("r-ij-plain"))
    assert "tool_choice" not in upstream.sent[1]


@pytest.mark.parametrize("extra", [
    {"ui_authorized": None},  # 心跳、外部 API：不是 UI 對話
    {"enable_autonomy": True, "ui_authorized": None},
    {"subagent_run": True, "session_id": None},  # 子代理：繼承 ui_authorized，但沒有 session
], ids=["not-ui", "heartbeat", "subagent"])
async def test_runs_outside_ui_conversations_never_drain(monkeypatch, extra):
    queue = InMemoryInterjectionQueue()
    queue.enqueue(_SID, _STREAM, "不該被取出")
    FakeUpstream([stream_reply(_TOOL, "one"), stream_reply(openai_text("好"), "one")]
                 ).install(monkeypatch)
    chunks, _ = await _run(queue, _ctx("r-ij-hb", **extra))
    assert [i.status for i in queue.list_items(_SID, _STREAM)] == ["queued"]
    assert not any("interjection_" in c for c in chunks)


async def test_empty_queue_sends_nothing_extra(monkeypatch):
    queue = InMemoryInterjectionQueue()
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), stream_reply(openai_text("好"), "one")])
    upstream.install(monkeypatch)
    chunks, _ = await _run(queue, _ctx("r-ij-empty"))
    assert not any("interjection_" in c for c in chunks)
    assert upstream.sent[1]["messages"][-1]["role"] == "tool"
    assert "tool_choice" not in upstream.sent[1]


@pytest.mark.parametrize(("state", "opened"), [
    ({"stream_id": _STREAM, "status": "active"}, True),
    ({"stream_id": _STREAM, "status": "done"}, False),  # 串流已結束
    ({"stream_id": "", "status": "active"}, False),
    (None, False),  # 查不到串流
])
def test_queue_only_for_an_active_stream(state, opened):
    round_ = InterjectionRound.begin(_ctx("r-ij-open"), InMemoryInterjectionQueue(),
                                    lambda _sid: state)
    assert (round_ is not None) is opened
