"""U4b-1：Ctrl+Enter 打斷正在接收的這一輪（ADR-0249 第 4b 段；master R2、裁定 1～5）。

收到本輪結果之前 urgent 先到：取消這一輪、關上游，半截的文字、思考、工具呼叫全部丟掉；不重試、
不拋 UiGenerationStopped、不算一步，送 ``round_interrupted_by_user``。取出全部排隊中的（urgent
優先）接在最後一則使用者側訊息後面，下一輪 tool_choice=none、送出成功才 delivered。本輪結果到了
之後才來的照第 4a 段等工具邊界。費用照廠商回報記、沒回報就估，「估計」只進畫面事件與日誌。
確定性時序：上游用 Event 停住，停住的那一刻做使用者的動作；外層包 ``asyncio.timeout``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ToukaException
from src.handlers import react_core
from src.handlers.adapters import responses_adapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks
from src.models.identity import AgentIdentity
from src.models.session_persistence import UiGenerationStopped
from src.services.interjection_queue import InMemoryInterjectionQueue
from src.services.rhythm_service import rhythm_service
from src.utils.async_db import session_service
from src.utils.token_estimator import estimate_tokens

from tests.helpers.stream_transport import (
    FakeUpstream,
    Reply,
    gemini_stream_reply,
    sse,
    status_reply,
    stream_reply,
)
from tests.unit.test_responses_parse import CTX as RESPONSES_CTX
from tests.unit.test_responses_parse import MSG, USAGE, final

_GUARD_S = 5.0
_SID, _STREAM = "s-int", "st-int"
INTERRUPTED, DELIVERED = "round_interrupted_by_user", "interjection_delivered"
_STEP0 = {"type": INTERRUPTED, "step": 0}
_TOOLS = [{"type": "function", "function": {"name": "vault_tools", "description": "列出檔案",
           "parameters": {"type": "object", "properties": {}}}}]  # 請求預設帶一個工具（M9）
_TOOL = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
        "function": {"name": "vault_tools", "arguments": '{"action": "list"}'}}]}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _note(*texts: str, cut: bool = True) -> str:
    """打斷的範本（cut）或工具邊界的範本。"""
    lead = "使用者打斷了正在寫的回答並補充" if cut else "使用者在工具執行期間補充"
    return "\n\n".join(f"{lead}：{text}" for text in texts)


def _chunk(**delta: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _text(text: str) -> Reply:
    return stream_reply({"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": text}}], "usage": {"prompt_tokens": 10,
                                                           "completion_tokens": 5}}, "one")


class _Hold(asyncio.Event):
    """上游停在這裡，停住的那一刻先做 ``on_reach``；``release=True``＝不停，只在這時做動作。"""

    def __init__(self, on_reach: Any = None, *, release: bool = False) -> None:
        super().__init__()
        self.reached, self._on_reach = asyncio.Event(), on_reach
        if release:
            self.set()

    async def wait(self) -> bool:
        self.reached.set()
        if self._on_reach is not None:
            self._on_reach()
        return await super().wait()


@pytest.fixture(autouse=True)
def tools(monkeypatch) -> dict[str, Any]:
    """工具換成假的（執行期間可做使用者動作）、退避不真睡、入帳與稽核只記下來。"""
    fake: dict[str, Any] = {"calls": [], "on_run": None, "usage": [], "audit": []}

    async def run(_self, name, args, request_id, ctx, queue, call_id=None,
                  actually_used_tools=None):
        fake["calls"].append(name)
        if fake["on_run"] is not None:
            fake["on_run"]()
        await queue.put(None)
        return f"{name} ok"

    monkeypatch.setattr(react_core.ToolExecutor, "execute_to_queue", run)
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())
    monkeypatch.setattr(react_core, "accumulate_token_usage",
                        lambda _identity, _provider, usage, *a, **k: fake["usage"].append(usage))
    monkeypatch.setattr(react_core, "log_usage_audit", lambda *a, **k: fake["audit"].append(a[4]))
    return fake


def _ctx(rid: str, **extra: Any) -> dict:
    return {"protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
            "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
            "request_id": rid, "t0": 0.0, "agent_id": "Sumi",
            "identity": AgentIdentity.for_role("Sumi", "Owner"), "provider": "Grok",
            "url": "https://api.x.test/v1/chat/completions", "api_key": "fake-key",
            "custom_connection": False, "session_id": _SID, "ui_authorized": True, **extra}


async def _run(queue: InMemoryInterjectionQueue, ctx: dict, adapter: Any = None,
               model: str = "grok-4", tools: list | None = _TOOLS) -> list[str]:
    request = OpenAIChatRequest(model=model, tools=tools,
                                messages=[OpenAIMessage(role="user", content="hi")])
    chunks: list[str] = []
    async with asyncio.timeout(_GUARD_S):
        try:
            async for chunk in react_core.run_react_loop(
                    adapter or OpenAIAdapter(stream=True), request, ctx, interjections=queue,
                    stream_state=lambda sid: {"stream_id": _STREAM, "status": "active"}):
                chunks.append(chunk)
        except UiGenerationStopped as stop:  # 打斷不是停止：出現就是錯
            chunks.append(f"stopped:{stop.generation}")
    assert chunks[-1] == "data: [DONE]\n\n"
    return chunks


def _events(chunks: list[str], kind: str | None = None) -> list[dict]:
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return [e for e in events if kind is None or kind in (e.get("type"), e.get("kind"))]


def _deltas(chunks: list[str], key: str = "content") -> list[str]:
    return [choice["delta"][key] for event in _events(chunks)
            for choice in event.get("choices") or []
            if isinstance(choice.get("delta"), dict) and choice["delta"].get(key)]


def _urgent(queue: InMemoryInterjectionQueue, *texts: str, ids: list | None = None) -> Any:
    """使用者動作：依序加入插話（``u`` 開頭＝Ctrl+Enter）。"""
    def act() -> None:
        for text in texts:
            item = queue.enqueue(_SID, _STREAM, text, urgent=text.startswith("u"))
            if ids is not None:
                ids.append(item.id)

    return act


@pytest.mark.parametrize("incognito", [False, True])
async def test_urgent_before_the_first_chunk_cancels_the_round(monkeypatch, incognito):
    if incognito:  # 無痕：一樣打斷，不碰資料庫
        forbid = AsyncMock(side_effect=AssertionError("incognito touched the database"))
        monkeypatch.setattr(session_service, "conditional_save_session", forbid)
    queue, ids, late = InMemoryInterjectionQueue(), [], []

    def retract_late(count: int) -> None:
        if count == 2:  # 已經取出、下一輪送出中：不能再收回
            with pytest.raises(ToukaException) as exc:
                queue.retract(_SID, _STREAM, ids[0])
            late.append((exc.value.status_code, exc.value.detail["code"]))

    upstream = FakeUpstream([Reply(pieces=[_Hold(_urgent(queue, "u改看 b", ids=ids))]),
                             _text("完整答案")], on_request=retract_late).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-first", incognito=incognito))
    assert len(upstream.requests) == 2  # 被打斷的一輪＋帶插話的下一輪，沒有重試
    upstream.assert_all_closed()
    assert upstream.sent[1]["messages"][-2:] == [{"role": "user", "content": "hi"},
                                                 {"role": "user", "content": _note("u改看 b")}]
    assert upstream.sent[1]["tool_choice"] == "none"
    kinds = [e["type"] for e in _events(chunks) if e.get("type") in (INTERRUPTED, DELIVERED)]
    assert kinds == [INTERRUPTED, DELIVERED] and _events(chunks, INTERRUPTED) == [_STEP0]
    assert [(e["interjection_id"], e["step"]) for e in _events(chunks, DELIVERED)] == [(ids[0], 0)]
    assert late == [(409, "INTERJECTION_NOT_RETRACTABLE")]
    assert _deltas(chunks) == ["完整答案"]


async def test_urgent_mid_stream_drops_the_half_round_and_it_is_not_a_step(monkeypatch, tools):
    queue = InMemoryInterjectionQueue()
    half = sse(_chunk(role="assistant", reasoning_content="想一半"), _chunk(content="半截"),
               _chunk(tool_calls=[{"index": 0, "id": "call_h", "type": "function",
                                   "function": {"name": "vault_tools", "arguments": '{"a'}}]),
               done=False)
    upstream = FakeUpstream([Reply(pieces=[half, _Hold(_urgent(queue, "u改看 b"))]),
                             stream_reply(_TOOL, "one"), _text("好了")]).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-mid", pulse_rounds=2))
    assert len(upstream.requests) == 3
    assert _deltas(chunks) == ["好了"] and _deltas(chunks, "reasoning_content") == []
    assert not any("半截" in c or "想一半" in c for c in chunks)
    assert "半截" not in json.dumps(upstream.sent[1], ensure_ascii=False)
    assert [m["role"] for m in upstream.sent[1]["messages"]][-2:] == ["user", "user"]
    assert tools["calls"] == ["vault_tools"]  # 只有第 2 輪真的工具呼叫
    assert rhythm_service.get_used_rounds("r-int-mid") == 1  # 被打斷的那一輪不扣額度
    assert [(e["current"], e["total"]) for e in _events(chunks, "react_slots")] == [(1, 2)]
    assert _events(chunks, INTERRUPTED) == [_STEP0]
    assert [e["step"] for e in _events(chunks, DELIVERED)] == [0]
    upstream.assert_all_closed()


@pytest.mark.parametrize("when", ["tools", "same-step"])  # 工具執行中／和完整結果同一步到
async def test_urgent_after_the_round_arrived_waits_for_the_tool_boundary(monkeypatch, tools, when):
    queue = InMemoryInterjectionQueue()
    act = _urgent(queue, "u先停")
    tools["on_run"] = act if when == "tools" else None
    upstream = FakeUpstream([stream_reply(_TOOL, "one"), Reply(pieces=[0.02, *_text("好").pieces])],
                            on_request=lambda n: act() if when != "tools" and n == 1 else None
                            ).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-after"))
    assert len(upstream.requests) == 2 and tools["calls"] == ["vault_tools"]  # 結果沒被丟掉
    assert [m["role"] for m in upstream.sent[1]["messages"]][-2:] == ["tool", "user"]
    assert upstream.sent[1]["messages"][-1]["content"] == _note("u先停", cut=False)
    assert upstream.sent[1]["tool_choice"] == "none"
    assert _events(chunks, INTERRUPTED) == []
    assert [e["step"] for e in _events(chunks, DELIVERED)] == [1]


async def test_urgent_already_queued_preempts_the_round_without_sending_it(monkeypatch):
    queue = InMemoryInterjectionQueue()
    item = queue.enqueue(_SID, _STREAM, "先看 b", urgent=True)  # 第一輪送出前就到了
    upstream = FakeUpstream([_text("好")]).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-pre"))
    assert len(upstream.requests) == 1
    assert upstream.sent[0]["messages"][-1] == {"role": "user", "content": _note("先看 b")}
    assert upstream.sent[0]["tool_choice"] == "none"
    assert _events(chunks, INTERRUPTED) == []  # 沒有正在接收的一輪
    assert [(e["interjection_id"], e["step"]) for e in _events(chunks, DELIVERED)] \
        == [(item.id, 0)]


@pytest.mark.parametrize(("when", "extra", "notes"), [
    ("before", {}, [1, 1]),  # 帶插話的那一輪 503 → 重試：插話只接一次
    ("backoff", {}, [0, 1]),  # 重試等待時才到：下一次不送，先接上插話
    ("before", {"ui_authorized": None}, [0, 0]),  # 不是 UI 對話：不跳過、不接
], ids=["before-ui", "backoff-ui", "before-not-ui"])
async def test_preempted_round_is_ui_only_and_never_doubled_by_retries(monkeypatch, when, extra,
                                                                      notes):
    queue = InMemoryInterjectionQueue()
    if when == "before":
        queue.enqueue(_SID, _STREAM, "先看 b", urgent=True)
    hook = _urgent(queue, "u先看 b") if when == "backoff" else None
    upstream = FakeUpstream([status_reply(503), _text("好")],
                            on_request=lambda n: hook() if hook and n == 1 else None
                            ).install(monkeypatch)
    chunks = await _run(queue, _ctx(f"r-int-retry-{when}", **extra))
    assert [json.dumps(s, ensure_ascii=False).count("打斷了正在寫的回答") for s in upstream.sent] \
        == notes
    assert _events(chunks, INTERRUPTED) == []
    assert len(_events(chunks, DELIVERED)) == max(notes)


class _StaleQueue(InMemoryInterjectionQueue):
    def urgent_signal(self, session_id: str, stream_id: str) -> asyncio.Event:
        event = asyncio.Event()
        event.set()  # set 了卻沒有任何插話可取
        return event


async def test_signal_without_items_does_not_spin(monkeypatch):
    """訊號和佇列對不上：這一回合不再理會訊號、照常送出，不會沒有 await 地空轉。"""
    upstream = FakeUpstream([_text("好")]).install(monkeypatch)
    chunks = await _run(_StaleQueue(), _ctx("r-int-stale"))
    assert len(upstream.requests) == 1 and _deltas(chunks) == ["好"]


async def test_tool_boundary_flow_is_unchanged_with_accumulated_pending(monkeypatch):
    """只有第 4a 段：兩個工具邊界各取出一則，各在下一輪送出後送達一次（pending 累加不影響）。"""
    queue = InMemoryInterjectionQueue()
    upstream = FakeUpstream(
        [stream_reply(_TOOL, "one"), stream_reply(_TOOL, "one"), _text("好")],
        on_request=lambda n: queue.enqueue(_SID, _STREAM, f"p{n}") if n < 3 else None,
    ).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-4a"))
    assert [(e["text"], e["step"]) for e in _events(chunks, DELIVERED)] == [("p1", 1), ("p2", 2)]
    assert [json.dumps(s, ensure_ascii=False).count("工具執行期間補充") for s in upstream.sent] \
        == [0, 1, 2]
    assert _events(chunks, INTERRUPTED) == [] and all("tool_choice" not in s for s in upstream.sent)


async def test_retract_racing_the_interrupt_keeps_the_round(monkeypatch):
    """set 之後同一步就收回：pump 醒來時訊號已 clear，這一輪照常收完。"""
    queue, hold = InMemoryInterjectionQueue(), _Hold()
    upstream = FakeUpstream([Reply(pieces=[hold, 0.02, *_text("完整答案").pieces])]
                            ).install(monkeypatch)

    async def user() -> str:
        await hold.reached.wait()
        item = queue.enqueue(_SID, _STREAM, "算了", urgent=True)
        status = queue.retract(_SID, _STREAM, item.id).status
        hold.set()
        return status

    async with asyncio.timeout(_GUARD_S):
        user_task = asyncio.create_task(user())
        chunks = await _run(queue, _ctx("r-int-retract"))
        assert await user_task == "retracted"
    assert len(upstream.requests) == 1 and _deltas(chunks) == ["完整答案"]
    assert _events(chunks, INTERRUPTED) == [] and _events(chunks, DELIVERED) == []


async def test_two_urgents_in_a_row_urgent_first_and_one_restore(monkeypatch):
    queue, ids = InMemoryInterjectionQueue(), []
    upstream = FakeUpstream([
        Reply(pieces=[_Hold(_urgent(queue, "p1", "u1", "p2", "u2", ids=ids))]),
        Reply(pieces=[_Hold(_urgent(queue, "u3", ids=ids))]),
        stream_reply(_TOOL, "one"), _text("好"),
    ]).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-twice"))
    sent = upstream.sent
    assert len(sent) == 4 and [s.get("tool_choice") for s in sent] == [None, "none", "none", None]
    assert [m["content"] for m in sent[2]["messages"][-2:]] == [
        _note("u1", "u2", "p1", "p2"), _note("u3")]  # urgent 優先，其餘照順序
    assert _events(chunks, INTERRUPTED) == [_STEP0, _STEP0]
    # 第一批等到第 3 輪送出成功才送達（第 2 輪被打斷、沒送成）
    assert [e["text"] for e in _events(chunks, DELIVERED)] == ["u1", "u2", "p1", "p2", "u3"]
    assert _events(chunks, "interjection_undelivered") == []


@pytest.mark.parametrize("extra", [
    {"ui_authorized": None}, {"enable_autonomy": True, "ui_authorized": None},
    {"subagent_run": True},  # 子代理就算帶著 session 也不給
], ids=["not-ui", "heartbeat", "subagent"])
async def test_runs_outside_ui_conversations_are_never_interrupted(monkeypatch, extra):
    queue = InMemoryInterjectionQueue()
    act = _Hold(_urgent(queue, "u停"), release=True)
    upstream = FakeUpstream([Reply(pieces=[act, 0.05, *_text("完整答案").pieces])]
                            ).install(monkeypatch)
    chunks = await _run(queue, _ctx("r-int-outside", **extra))
    assert len(upstream.requests) == 1 and _deltas(chunks) == ["完整答案"]
    assert _events(chunks, INTERRUPTED) == []
    assert [i.status for i in queue.list_items(_SID, _STREAM)] == ["queued"]


async def test_interrupt_on_a_loopback_server_closes_the_connection():
    queue, bodies = InMemoryInterjectionQueue(), []
    first_open, disconnected = asyncio.Event(), asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        length = next(int(line.split(":")[1]) for line in head.split("\r\n")
                      if line.lower().startswith("content-length:"))
        bodies.append(json.loads(await reader.readexactly(length)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Connection: close\r\n\r\n")
        if len(bodies) == 1:
            writer.write(sse(_chunk(role="assistant", content="半截"), done=False))
            await writer.drain()
            first_open.set()
            if await reader.read(1) == b"":  # 客戶端關連線＝EOF
                disconnected.set()
        else:
            writer.write(b"".join(_text("完整答案").pieces))
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions"

    async def user() -> None:
        await first_open.wait()
        queue.enqueue(_SID, _STREAM, "改看 b", urgent=True)
        await asyncio.wait_for(disconnected.wait(), 1.0)  # 打斷後 1 秒內伺服器看到斷線

    try:
        async with asyncio.timeout(_GUARD_S):
            user_task = asyncio.create_task(user())
            chunks = await _run(queue, _ctx("r-int-loop", url=url))
            await user_task
    finally:
        server.close()
        await server.wait_closed()
    assert len(bodies) == 2 and _deltas(chunks) == ["完整答案"]
    assert bodies[1]["messages"][-1] == {"role": "user", "content": _note("改看 b")}


# ── 計費與 Claude 格式（Claude 串流：廠商有回報、部分回報、沒回報） ────────────


def _cev(kind: str, **data: Any) -> bytes:
    payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
    return f"event: {kind}\ndata: {payload}\n\n".encode()


_C_START = _cev("message_start", message={
    "id": "msg_t", "type": "message", "role": "assistant", "model": "claude", "content": [],
    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}})
_C_TEXT = [_cev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
           _cev("content_block_delta", index=0, delta={"type": "text_delta", "text": "半截"})]
_C_FULL = [_C_START, *_C_TEXT, _cev("content_block_stop", index=0),
           _cev("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None},
                usage={"output_tokens": 3}), _cev("message_stop")]
_C_OUTPUT = _cev("message_delta", delta={"stop_reason": None, "stop_sequence": None},
                 usage={"output_tokens": 2})


class _ClaudeBytes(httpx2.AsyncByteStream):
    def __init__(self, script: list) -> None:
        self._script = script

    async def __aiter__(self) -> Any:
        for step in self._script:
            if isinstance(step, asyncio.Event):
                await step.wait()
            else:
                yield step


def _claude(scripts: list[list]) -> tuple[AsyncAnthropic, list[dict]]:
    """AsyncAnthropic＋httpx2.MockTransport：每個請求照順序回一份腳本，記下送出的 JSON。"""
    sent: list[dict] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.read()))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=_ClaudeBytes(scripts.pop(0)))

    return AsyncAnthropic(api_key="fake-key", base_url="https://api.anthropic.test",
                          max_retries=0, http_client=DefaultAsyncHttpxClient(
                              transport=httpx2.MockTransport(handle), follow_redirects=False,
                              event_hooks=liveness_event_hooks())), sent


@pytest.mark.parametrize(("first", "output", "estimated"), [
    ([_C_START, *_C_TEXT, _C_OUTPUT], 2, False),  # 廠商有回報輸入與輸出
    ([_C_START, *_C_TEXT], estimate_tokens("半截"), True),  # 部分回報：輸出用收到的文字估
    ([], 0, True),  # 沒回報：整份請求估
], ids=["reported", "partial", "unreported"])
async def test_interrupted_claude_round_is_billed(tools, caplog, first, output, estimated):
    caplog.set_level(logging.WARNING, logger=react_core.__name__)
    queue = InMemoryInterjectionQueue()
    client, sent = _claude([[*first, _Hold(_urgent(queue, "u改看 b"))], _C_FULL])
    model = "claude-sonnet-5"  # 規則表 False：插話併進同一則 user
    try:
        chunks = await _run(queue, {**_ctx(f"r-int-bill-{output}"), "anthropic_client": client,
                                    "model": model}, ClaudeAdapter(stream=True), model)
    finally:
        await client.close()
    partial = tools["usage"][0]
    assert set(partial) == {"prompt_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens", "completion_tokens"}  # 沒有「估計」
    # 有回報照記，沒有就用請求估
    assert partial["prompt_tokens"] == 10 if first else partial["prompt_tokens"] > 0
    assert partial["completion_tokens"] == output
    assert tools["usage"][1]["completion_tokens"] == 3
    assert all("estimated" not in totals for totals in tools["audit"])
    assert [e.get("estimated", False) for e in _events(chunks, "token_usage")] == [estimated]
    logged = [r.getMessage() for r in caplog.records if "Unfinished" in r.getMessage()]
    assert len(logged) == 1 and f"estimated={estimated}" in logged[0]
    messages = sent[1]["messages"]
    assert [m["role"] for m in messages] == ["user"]  # 不產生連續兩則 user
    assert messages[0]["content"][-1] == {"type": "text", "text": _note("u改看 b")}
    assert "半截" not in json.dumps(sent[1], ensure_ascii=False)


# ── M9-R1a：沒有工具不送 tool_choice（四家）；Claude 中途 system 只有一則 ─────────

_GEMINI_OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "好"}]},
                              "finishReason": "STOP", "index": 0}],
              "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3,
                                "totalTokenCount": 13}}
_NONE = {"openai": ("tool_choice", "none"), "claude": ("tool_choice", {"type": "none"}),
         "gemini": ("tool_config", {"function_calling_config": {"mode": "NONE"}}),
         "responses": ("tool_choice", "none")}


def _provider(monkeypatch, provider: str) -> tuple[Any, Any, str, dict, Any]:
    """一家的假上游：回 (取送出 JSON 的函式, adapter, model, ctx 額外欄位, 關閉)。"""
    if provider in ("openai", "gemini"):
        gemini = provider == "gemini"
        upstream = FakeUpstream([gemini_stream_reply(_GEMINI_OK, "one") if gemini
                                 else _text("好")]).install(monkeypatch)
        adapter, model = ((GeminiAdapter(stream=True), "gemini-2.5-flash") if gemini
                          else (OpenAIAdapter(stream=True), "grok-4"))
        return lambda: upstream.sent, adapter, model, {"gemini_api_key": "k"}, AsyncMock()
    if provider == "claude":
        client, sent = _claude([_C_FULL])
        return (lambda: sent, ClaudeAdapter(stream=True), "claude-opus-5-5",
                {"anthropic_client": client, "model": "claude-opus-5-5"}, client.close)
    sent: list[dict] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.read()))
        return httpx2.Response(200, json=final(output=[MSG], usage=USAGE))

    def client(**kw: Any) -> Any:
        return openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle), **kw)

    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", client)
    return lambda: sent, ResponsesAdapter(), "gpt-6-astra", dict(RESPONSES_CTX), AsyncMock()


@pytest.mark.parametrize("with_tools", [False, True], ids=["no-tools", "tools"])
@pytest.mark.parametrize("provider", list(_NONE))
async def test_interrupt_sends_tool_choice_none_only_with_tools(monkeypatch, provider, with_tools):
    """沒有 tools 時，打斷後的下一輪不送 tool_choice（四家）；有 tools 時是 none。"""
    queue = InMemoryInterjectionQueue()
    queue.enqueue(_SID, _STREAM, "先看 b", urgent=True)  # 一輪開始前就到：直接接上插話
    sent, adapter, model, extra, close = _provider(monkeypatch, provider)
    try:
        await _run(queue, {**_ctx(f"r-int-tc-{provider}"), **extra}, adapter, model,
                   tools=_TOOLS if with_tools else None)
    finally:
        await close()
    key, none = _NONE[provider]
    assert _note("先看 b") in json.dumps(sent()[0], ensure_ascii=False)
    if with_tools:
        assert sent()[0][key] == none
    else:
        assert key not in sent()[0]


_C_TOOL = [_C_START,
           _cev("content_block_start", index=0, content_block={
               "type": "tool_use", "id": "toolu_1", "name": "vault_tools", "input": {}}),
           _cev("content_block_delta", index=0, delta={
               "type": "input_json_delta", "partial_json": '{"action": "list"}'}),
           _cev("content_block_stop", index=0),
           _cev("message_delta", delta={"stop_reason": "tool_use", "stop_sequence": None},
                usage={"output_tokens": 5}), _cev("message_stop")]


@pytest.mark.parametrize("first", ["tool-boundary", "interrupt"])
async def test_claude_keeps_one_mid_conversation_system_message(tools, first):
    """中途 system 要接在 user 後、是最後一則或後接 assistant：第二段併進同一則。"""
    queue = InMemoryInterjectionQueue()
    if first == "tool-boundary":  # 工具邊界接了一則 system，下一輪又被打斷
        tools["on_run"] = _urgent(queue, "p1")
        scripts, notes = [_C_TOOL, [_Hold(_urgent(queue, "u2"))], _C_FULL], [
            _note("p1", cut=False), _note("u2")]
    else:  # 連續兩次打斷
        scripts = [[_Hold(_urgent(queue, "u1"))], [_Hold(_urgent(queue, "u2"))], _C_FULL]
        notes = [_note("u1"), _note("u2")]
    client, sent = _claude(scripts)
    try:
        await _run(queue, {**_ctx(f"r-int-sys-{first}"), "anthropic_client": client,
                           "model": "claude-opus-5-5"}, ClaudeAdapter(stream=True),
                   "claude-opus-5-5")
    finally:
        await client.close()
    roles = [m["role"] for m in sent[2]["messages"]]
    assert roles.count("system") == 1 and roles[-2:] == ["user", "system"]
    assert sent[2]["messages"][-1]["content"] == "\n\n".join(notes)
