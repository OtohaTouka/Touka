"""人在聊天按停止時，小幫手上的工作要跟著取消（ADR-0312）.

工具是獨立的背景工作，停止只會取消聊天串流，所以這裡走真的 ReAct 迴圈，不直接取消工具。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.handlers import react_core
from src.handlers.claude_handler import handle_claude_request
from src.models.identity import AgentIdentity
from src.services.local_bridge import LocalBridge
from src.services.rhythm_service import rhythm_service

from tests.helpers.claude_stream import delegate_stream_to_create

GENERATION = 1


def _tool_response(name: str) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = "t1"
    block.name = name
    block.input = {}
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage = MagicMock(input_tokens=3, output_tokens=2)
    response.model_dump.return_value = {
        "content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return response


def _text_response() -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "好了"
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=3, output_tokens=2)
    response.model_dump.return_value = {
        "content": [{"type": "text", "text": "好了"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return response


def _start_turn(
    monkeypatch: pytest.MonkeyPatch, name: str, tool: Any, request_id: str
) -> asyncio.Task[list[str]]:
    async def run_tool(
        _self: Any,
        _name: str,
        _args: dict,
        _rid: str,
        _ctx: Any,
        queue: Any,
        call_id: str | None = None,
        actually_used_tools: Any = None,
    ) -> str:
        result = await tool()
        await queue.put(None)
        return str(result)

    monkeypatch.setattr(react_core.ToolExecutor, "execute_to_queue", run_tool)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *_a, **_k: None)
    rhythm_service.clear_session(request_id)
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[_tool_response(name), _text_response()])
    context = {
        "anthropic_client": delegate_stream_to_create(client),
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {name: tool},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": request_id,
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": f"sess-{request_id}",
        "model": "claude-3-5-sonnet",
        "resumable_stream": True,
    }
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [],
    }
    request.messages = [MagicMock(content="hi", role="user")]

    async def consume() -> list[str]:
        return [chunk async for chunk in handle_claude_request(request, context)]

    return asyncio.create_task(consume())


async def _stop(turn: asyncio.Task[list[str]]) -> None:
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)


async def _next_job(bridge: LocalBridge) -> Any:
    for _ in range(400):
        response = await bridge.poll(GENERATION, "1.0", hold=0)
        if response is not None and response.job is not None:
            return response.job
        await asyncio.sleep(0.005)
    raise AssertionError("工作沒有送到小幫手")


@pytest.mark.asyncio
async def test_stopping_the_turn_withdraws_a_list_waiting_for_approval(monkeypatch):
    bridge = LocalBridge(check_interval=0.001)
    await bridge.poll(GENERATION, "1.0", hold=0)

    async def local_files() -> Any:
        return await bridge.submit("propose", {"items": [], "reason": ""}, "Charlie")

    turn = _start_turn(monkeypatch, "local_files", local_files, "stop-1")
    job = await _next_job(bridge)

    await _stop(turn)
    await asyncio.sleep(0.01)

    response = await bridge.poll(GENERATION, "1.0", hold=0)
    assert response is not None and job.id in response.cancelled
    assert bridge.confirm(job.id, GENERATION) is False


@pytest.mark.asyncio
async def test_stopping_the_turn_tells_the_helper_to_stop_a_running_list(monkeypatch):
    bridge = LocalBridge(check_interval=0.001)
    await bridge.poll(GENERATION, "1.0", hold=0)

    async def local_files() -> Any:
        return await bridge.submit("propose", {"items": [], "reason": ""}, "Charlie")

    turn = _start_turn(monkeypatch, "local_files", local_files, "stop-2")
    job = await _next_job(bridge)
    assert bridge.confirm(job.id, GENERATION) is True

    await _stop(turn)
    await asyncio.sleep(0.01)

    response = await bridge.poll(GENERATION, "1.0", hold=0)
    assert response is not None and job.id in response.cancelled


@pytest.mark.asyncio
async def test_other_tools_keep_running_when_the_turn_stops(monkeypatch):
    """其他工具維持原本的行為，這次只收掉小幫手的工作。"""
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled: list[bool] = []

    async def vault_tools() -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return "done"

    turn = _start_turn(monkeypatch, "vault_tools", vault_tools, "stop-3")
    await asyncio.wait_for(started.wait(), 5)

    await _stop(turn)
    await asyncio.sleep(0.01)

    assert cancelled == []
    release.set()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_finished_tool_is_not_cancelled(monkeypatch):
    bridge = LocalBridge(check_interval=0.001)
    await bridge.poll(GENERATION, "1.0", hold=0)

    async def local_files() -> str:
        return "ok"

    turn = _start_turn(monkeypatch, "local_files", local_files, "stop-4")
    chunks = await asyncio.wait_for(turn, 5)
    assert chunks[-1].strip() == "data: [DONE]"
    response = await bridge.poll(GENERATION, "1.0", hold=0)
    assert response is not None and response.cancelled == []
