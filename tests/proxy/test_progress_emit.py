"""P1 結構化進度頻道 — 後端 emit 驗證。

驗證 handler 在 ReAct 自主續跑時，會 emit 結構化的 touka_progress / react_slots
事件（current/total），供前端「Agent 活動」strip 顯示進度。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.claude_handler import handle_claude_request
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_executor as executor_module
from src.services.autonomy import autonomy_service
from src.services.dispatchers.group import dispatch_group_stream
from src.services.rhythm_service import rhythm_service
from src.services.tool_executor import ToolExecutor
from src.tools.builtin.research import research

from tests.helpers.claude_stream import delegate_stream_to_create


def _text_response(text: str) -> MagicMock:
    """組裝一個「純文字、無工具」的 Claude 回應（用於觸發 ReAct 續跑）。"""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 3, "output_tokens": 2}
    resp.model_dump.return_value = {
        "id": "msg_x",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return resp


def _parse_progress(chunks: list[str]) -> list[dict]:
    """從 SSE 字串串流中萃取 touka_progress 事件。"""
    out: list[dict] = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: ") :].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "touka_progress":
            out.append(obj)
    return out


def _tool_response(call_id: str, name: str = "vault_tools") -> MagicMock:
    """組裝一個「工具調用」的 Claude 回應（消耗工具輪 → 觸發進度事件）。"""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = call_id
    block.input = {"action": "list"}
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 3, "output_tokens": 2}
    resp.model_dump.return_value = {
        "id": "msg_x",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return resp


@pytest.mark.asyncio
async def test_claude_handler_emits_react_slots_progress():
    """ADR-0146：進度事件改記「有限 run 的工具輪」current/total（wire 形狀沿用
    react_slots）；不限 run（互動預設）不發。以無人 run（enable_autonomy＋
    pulse_rounds=3）跑兩輪工具＋一輪文字收束驗證。"""
    request_id = "test-progress-req-1"
    rhythm_service.clear_session(request_id)
    context = {
        "anthropic_client": delegate_stream_to_create(MagicMock()),  # 3b-U1：走 stream()
        "protocol_manager": MagicMock(),
        "tool_registry": {"vault_tools": AsyncMock(return_value="ok")},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": request_id,
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "enable_autonomy": True,
        "pulse_rounds": 3,
        "session_id": "sess-progress-1",
        "model": "claude-3-5-sonnet",
    }
    context["anthropic_client"].messages.create = AsyncMock(
        side_effect=[
            _tool_response("c1"),
            _tool_response("c2"),
            _text_response("done"),
        ]
    )

    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [],
    }
    request.messages = [MagicMock(content="hi", role="user")]

    chunks: list[str] = []
    async for chunk in handle_claude_request(request, context):
        chunks.append(chunk)
        if len(chunks) > 100:
            break

    progress = _parse_progress(chunks)
    react = [p for p in progress if p.get("kind") == "react_slots"]
    assert react, f"expected react_slots progress events, got {progress}"

    assert [p["current"] for p in react] == [1, 2]
    assert all(p["total"] == 3 for p in react)

    rhythm_service.clear_session(request_id)


@pytest.mark.asyncio
async def test_tool_executor_emits_repair_progress(monkeypatch):
    """終端指令失敗（EXIT_CODE!=0）注入自我修復時，emit repair 進度事件。"""
    sid = "sess-repair-progress-1"
    autonomy_service.reset_repair_attempts(sid)

    async def fake_bash(**kwargs):
        return "command failed [EXIT_CODE: 1]"

    # Explicit synthetic registration preserves the real terminal classification.
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    bindings["bash"] = replace(bindings["bash"], handler=fake_bash)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)

    # 放行 Gate 2（本測試聚焦 repair emit，不測權限），並避免觸碰 DB
    monkeypatch.setattr(
        "src.services.tool_executor.permission_service.evaluate_policy",
        lambda ctx: type("D", (), {"effect": "ALLOW", "reason": "ok"})(),
    )
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id",
        AsyncMock(return_value=None),
    )

    te = ToolExecutor(
        protocol_manager=None,
        tool_registry={"bash": fake_bash},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    ctx = {
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "session_id": sid,
        "model": "m",
    }
    queue: asyncio.Queue = asyncio.Queue()
    await te.execute_to_queue(
        "bash", {"command": "false"}, "req-repair-1", ctx, queue, call_id="c1"
    )

    events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev is not None:
            events.append(ev)

    repair_evs = [
        e for e in events if e.get("type") == "touka_progress" and e.get("kind") == "repair"
    ]
    assert repair_evs, f"expected a repair progress event, got {events}"
    expected_max = autonomy_service.config.get("self_repair", {}).get("max_attempts", 3)
    assert repair_evs[0]["attempt"] == 1
    assert repair_evs[0]["max"] == expected_max
    autonomy_service.reset_repair_attempts(sid)


@pytest.mark.asyncio
async def test_group_dispatcher_emits_group_progress(monkeypatch):
    """群聊 roster 逐位處理時，emit group 進度事件（index/total/agent）。"""
    roster = [{"name": "Alice", "id": "Alice"}, {"name": "Bob", "id": "Bob"}]
    monkeypatch.setattr(
        "src.services.dispatchers.group.parse_group_roster",
        lambda messages, registry: roster,
    )
    monkeypatch.setattr(
        "src.services.dispatchers.group.resource_manager.read_json",
        lambda path: {},
    )
    monkeypatch.setattr(
        "src.services.dispatchers.group.record_working_brain",
        lambda *a, **k: None,
    )
    # ADR-0245：成員一律走靈魂路徑，brain 與靈魂注入改成假的
    monkeypatch.setattr("src.services.dispatchers.group.get_active_brain", lambda i: "m")
    monkeypatch.setattr(
        "src.services.dispatchers.group.soul_service",
        MagicMock(inject_home_messages=AsyncMock()),
    )

    async def fake_get_stream_generator(*a, **k):
        async def gen():
            yield "data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(
        "src.services.dispatchers.group.get_stream_generator", fake_get_stream_generator
    )

    request = OpenAIChatRequest(
        model="ToukaGroup",
        messages=[OpenAIMessage(role="user", content="@Alice @Bob 哈囉")],
    )

    chunks = []
    async for chunk in dispatch_group_stream(request, "req-group-1", 0.0, PipelineContext()):
        chunks.append(chunk)

    group_evs = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: ") :].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(obj, dict)
            and obj.get("type") == "touka_progress"
            and obj.get("kind") == "group"
        ):
            group_evs.append(obj)

    assert len(group_evs) == 2
    assert group_evs[0] == {
        "type": "touka_progress",
        "kind": "group",
        "index": 1,
        "total": 2,
        "agent": "Alice",
    }
    assert group_evs[1] == {
        "type": "touka_progress",
        "kind": "group",
        "index": 2,
        "total": 2,
        "agent": "Bob",
    }


@pytest.mark.asyncio
async def test_claude_handler_emits_token_usage():
    """P3：handler 在回合結束 emit 本回合 token 用量＋估算成本（token_usage 進度）。"""
    request_id = "test-tokusage-req-1"
    rhythm_service.clear_session(request_id)
    context = {
        "anthropic_client": delegate_stream_to_create(MagicMock()),  # 3b-U1：走 stream()
        "protocol_manager": MagicMock(),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": request_id,
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": "sess-tokusage-1",
        "model": "claude-3-5-sonnet",
    }
    context["anthropic_client"].messages.create = AsyncMock(return_value=_text_response("done"))
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [],
    }
    request.messages = [MagicMock(content="hi", role="user")]

    with patch("src.handlers.react_core.autonomy_service.consume_quota", return_value=1):
        chunks: list[str] = []
        async for chunk in handle_claude_request(request, context):
            chunks.append(chunk)
            if len(chunks) > 80:
                break

    tok = [p for p in _parse_progress(chunks) if p.get("kind") == "token_usage"]
    assert tok, "expected a token_usage progress event"
    # _text_response usage: input_tokens=3 + output_tokens=2 → total 5
    assert tok[-1]["total"] == 5
    assert tok[-1]["cost"] >= 0
    rhythm_service.clear_session(request_id)


@pytest.mark.asyncio
async def test_tool_executor_passes_emit_channel(monkeypatch):
    """P2b：execute_to_queue 會把 _emit_event 與 _call_id 注入工具 kwargs，且連到佇列。"""
    received: dict = {}

    async def fake_tool(**kwargs):
        received.update(kwargs)
        return "ok"

    monkeypatch.setattr(
        "src.services.tool_executor.permission_service.evaluate_policy",
        lambda ctx: type("D", (), {"effect": "ALLOW", "reason": "ok"})(),
    )
    monkeypatch.setattr(
        "src.services.tool_executor.session_service.get_session_by_id",
        AsyncMock(return_value=None),
    )
    te = ToolExecutor(
        protocol_manager=None,
        tool_registry={"mytool": fake_tool},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    ctx = {
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "session_id": "s-emit",
        "model": "m",
    }
    queue: asyncio.Queue = asyncio.Queue()
    await te.execute_to_queue("mytool", {"x": 1}, "req-emit", ctx, queue, call_id="cid-1")

    assert callable(received.get("_emit_event"))
    assert received.get("_call_id") == "cid-1"
    # 該通道確實連到佇列
    await received["_emit_event"]({"type": "tool_stream", "id": "cid-1", "chunk": "hi"})
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert any(isinstance(e, dict) and e.get("type") == "tool_stream" for e in drained)


@pytest.mark.asyncio
async def test_research_google_single_attempt_emits_no_retry(monkeypatch):
    """research 的 Google 搜尋只用呼叫者模型送一次：失敗也不換模型重試、不 emit tool_retry。"""
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "fake-key")
    emitted = []
    posts = []

    async def _emit(ev):
        emitted.append(ev)

    class _Resp:
        status_code = 429

    async def _fake_post(*a, **k):
        posts.append(a)
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    out = await research("查天氣", source="google", model="gemini-2.5-flash", _emit_event=_emit)

    retries = [
        e for e in emitted if e.get("type") == "touka_progress" and e.get("kind") == "tool_retry"
    ]
    assert len(posts) == 1
    assert retries == []
    assert "搜尋失敗" in out
