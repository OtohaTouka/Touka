"""硬中斷（P2）— bash 子進程登記 ＋ handler 迴圈頂端中斷。

驗證：
- bash 執行期會把子進程登記到 process_registry、結束後註銷（供 /stop 硬殺）。
- handler 在迴圈頂端偵測中斷旗標 → 在呼叫 LLM 前就停（不浪費一次 LLM 呼叫）。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.models.identity import AgentIdentity
from src.services.process_registry import process_registry
from src.services.rhythm_service import rhythm_service
from src.tools import terminal
from src.tools.terminal import bash

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.mark.asyncio
async def test_bash_registers_and_unregisters_process(monkeypatch):
    sid = "sess-bash-reg-1"
    process_registry.reload_all()
    seen = {}

    class _FakeStdout:
        def __init__(self):
            self._sent = False

        async def readline(self):
            if not self._sent:
                # 執行期間應已登記（供 /stop 找到並終止）
                seen["during"] = process_registry.count(sid)
                self._sent = True
                return b"done\n"
            return b""

    class _FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = _FakeStdout()

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def _fake_exec(*_args, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(terminal, "_classify_command_strategy", lambda *a, **k: "ALLOW")

    identity = AgentIdentity.for_role(agent_id="agentB", role="Owner", capabilities=[])
    await bash("echo hi", identity, env="powershell", session_id=sid)

    assert seen.get("during") == 1  # 執行期間有登記
    assert process_registry.count(sid) == 0  # 結束後已註銷
    process_registry.reload_all()


@pytest.mark.asyncio
async def test_claude_handler_stops_before_llm_when_interrupted():
    sid = "sess-loop-top-1"
    rhythm_service.interrupt_session(sid)
    try:
        client = MagicMock()
        client.messages.create = AsyncMock()  # 不應被呼叫
        delegate_stream_to_create(client)  # 3b-U1：主迴圈走 messages.stream()
        context = {
            "anthropic_client": client,
            "protocol_manager": MagicMock(),
            "tool_registry": {},
            "memory_tool_registry": {},
            "plugin_registry": {},
            "request_id": "req-loop-top-1",
            "t0": 0.0,
            "agent_id": "Charlie",
            "identity": AgentIdentity.for_role(
                agent_id="Charlie", role="Owner", capabilities=["*"]
            ),
            "is_toukasoul": True,
            "session_id": sid,
            "model": "claude-3-5-sonnet",
        }
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

        chunks = []
        async for c in handle_claude_request(request, context):
            chunks.append(c)
            if len(chunks) > 20:
                break

        # LLM 在迴圈頂端就被中斷攔下，從未被呼叫
        assert client.messages.create.call_count == 0
        assert client.messages.stream.call_count == 0  # 串流入口也沒被呼叫（3b-U1）
        joined = "".join(chunks)
        assert "[DONE]" in joined
        # 解析 SSE content（JSON 會把中文轉為 \\u escape，需解碼後比對）
        texts = []
        for c in chunks:
            payload = c[len("data: "):].strip() if c.startswith("data: ") else ""
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
            if delta:
                texts.append(delta)
        assert "已由使用者中斷" in "".join(texts)
    finally:
        rhythm_service.clear_interrupt(sid)


@pytest.mark.asyncio
async def test_bash_emits_tool_stream(monkeypatch):
    """P2b：bash 逐行 emit tool_stream，且最終結果仍含完整輸出。"""
    process_registry.reload_all()
    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    class _FakeStdout:
        def __init__(self):
            self._lines = [b"line1\n", b"line2\n"]

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class _FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = _FakeStdout()

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def _fake_exec(*_a, **_k):
        return _FakeProc()

    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(terminal, "_classify_command_strategy", lambda *a, **k: "ALLOW")

    identity = AgentIdentity.for_role(agent_id="agentC", role="Owner", capabilities=[])
    result = await bash(
        "echo hi",
        identity,
        env="powershell",
        session_id="sx",
        _emit_event=_emit,
        _call_id="cx",
    )

    streams = [e for e in emitted if e.get("type") == "tool_stream"]
    assert [e["chunk"] for e in streams] == ["line1\n", "line2\n"]
    assert all(e["id"] == "cx" for e in streams)
    assert "line1" in result and "line2" in result
    process_registry.reload_all()
