"""claude_handler 特徵化 golden（react-core-extraction E0）。

共通 8 情境＋claude 專屬：thinking_split（native thinking block → reasoning 頻道，
ADR-0152）、native_anthropic_egress（雙 egress：Anthropic 形狀請求 → Anthropic 形狀
終端回應）。keepalive（5s SSE 心跳）在即時 mock 下不觸發＝golden 無心跳 chunk，
其行為已升核心單一實作（ADR-0157 K1）、由 tests/proxy/test_core_keepalive.py 把關。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.services.rhythm_service import rhythm_service

from tests.helpers.claude_stream import delegate_stream_to_create

from .harness import (
    UPSTREAM_S,
    assert_golden,
    drain,
    install_clock,
    make_clock,
    make_ctx,
    make_mock_executor,
    msg,
)

# E3 起 claude_handler＝薄殼；時鐘與 ToolExecutor 靶點在 react_core（harness 已涵蓋）。
_MODULE = "src.handlers.react_core"


def _block(btype: str, **attrs) -> MagicMock:
    b = MagicMock()
    b.type = btype
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def _resp(blocks: list, dump_content: list) -> MagicMock:
    r = MagicMock()
    r.content = blocks
    r.usage = MagicMock()
    r.usage.model_dump.return_value = {"input_tokens": 10, "output_tokens": 5}
    r.model_dump.return_value = {
        "id": "msg_golden", "model": "claude-3-5-sonnet", "role": "assistant",
        "content": dump_content, "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return r


def _text_resp(text: str) -> MagicMock:
    return _resp([_block("text", text=text)], [{"type": "text", "text": text}])


def _tool_resp(*calls: tuple[str, dict]) -> MagicMock:
    blocks = [
        _block("tool_use", id=f"c{i+1}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    dump = [
        {"type": "tool_use", "id": f"c{i+1}", "name": name, "input": args}
        for i, (name, args) in enumerate(calls)
    ]
    return _resp(blocks, dump)


def _request():
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [msg("user", "hi")]
    return request


class FakeAnthropicRequest:
    """型別名含 anthropic → 觸發 is_native_anthropic 雙 egress 分支。"""

    def __init__(self):
        self.model = "claude-3-5-sonnet"
        self.pulse_slots = None
        self.messages = [msg("user", "hi")]

    def model_dump(self, **_k):
        return {
            "model": "claude-3-5-sonnet", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }


async def _drive(monkeypatch, scenario: str, responses: list, *,
                 request=None,
                 ctx_extra: dict | None = None,
                 tool_registry: dict | None = None,
                 interrupt_on_call: int | None = None,
                 interrupt_before: bool = False) -> list[str]:
    clock = make_clock()
    install_clock(monkeypatch, _MODULE, clock)
    sid = f"sess-gc-{scenario}"
    rid = f"req-gc-{scenario}"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)
    if interrupt_before:
        rhythm_service.interrupt_session(sid)

    queue = list(responses)
    call_no = {"n": 0}

    async def create_side_effect(**_k):
        call_no["n"] += 1
        clock["now"] += UPSTREAM_S
        if interrupt_on_call is not None and call_no["n"] == interrupt_on_call:
            rhythm_service.interrupt_session(sid)
        return queue.pop(0) if queue else _text_resp("Done!")

    anthropic_client = MagicMock()
    anthropic_client.messages.create = AsyncMock(side_effect=create_side_effect)
    delegate_stream_to_create(anthropic_client)  # 3b-U1：主迴圈走 messages.stream()
    ctx = make_ctx(rid, sid, anthropic_client=anthropic_client,
                   model="claude-3-5-sonnet", **(ctx_extra or {}))
    if tool_registry is not None:
        ctx["tool_registry"] = tool_registry

    try:
        with patch(f"{_MODULE}.ToolExecutor") as MockExecutor:
            MockExecutor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            return await drain(handle_claude_request(request or _request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)


@pytest.mark.asyncio
async def test_pure_text(monkeypatch):
    chunks = await _drive(monkeypatch, "pure_text", [_text_resp("你好，我是 Sumi。")])
    assert_golden("claude", "pure_text", chunks)


@pytest.mark.asyncio
async def test_single_tool_round(monkeypatch):
    chunks = await _drive(monkeypatch, "single_tool_round", [
        _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
        _text_resp("清單如上。"),
    ])
    assert_golden("claude", "single_tool_round", chunks)


@pytest.mark.asyncio
async def test_multi_tool_round(monkeypatch):
    chunks = await _drive(monkeypatch, "multi_tool_round", [
        _tool_resp(
            ("vault_tools", {"vault": "personal", "action": "list"}),
            ("memory_tools", {"action": "query", "query": "紅茶"}),
        ),
        _text_resp("兩件事都查好了。"),
    ])
    assert_golden("claude", "multi_tool_round", chunks)


@pytest.mark.asyncio
async def test_budget_exhaustion_wrap_up(monkeypatch):
    chunks = await _drive(
        monkeypatch, "budget_exhaustion",
        [
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _text_resp("總結：已完成一輪查詢。"),
        ],
        ctx_extra={"enable_autonomy": True, "pulse_rounds": 1},
    )
    assert_golden("claude", "budget_exhaustion", chunks)


@pytest.mark.asyncio
async def test_interrupt_pre_call(monkeypatch):
    chunks = await _drive(
        monkeypatch, "interrupt_pre_call", [_text_resp("不該到這")], interrupt_before=True
    )
    assert "已由使用者中斷" in "".join(chunks)
    assert_golden("claude", "interrupt_pre_call", chunks)


@pytest.mark.asyncio
async def test_interrupt_during_run(monkeypatch):
    chunks = await _drive(
        monkeypatch, "interrupt_during_run",
        [
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _text_resp("不該到這"),
        ],
        interrupt_on_call=2,
    )
    assert_golden("claude", "interrupt_during_run", chunks)


@pytest.mark.asyncio
async def test_hallucination_warning(monkeypatch):
    chunks = await _drive(
        monkeypatch, "hallucination_warning",
        [_text_resp("查詢結果：\n> research ✅\n以上。")],
        tool_registry={"research": object()},
    )
    assert_golden("claude", "hallucination_warning", chunks)


@pytest.mark.asyncio
async def test_memory_warning(monkeypatch):
    chunks = await _drive(
        monkeypatch, "memory_warning", [_text_resp("好的，我幫你記下來了！")]
    )
    assert_golden("claude", "memory_warning", chunks)


@pytest.mark.asyncio
async def test_thinking_split(monkeypatch):
    """claude 專屬：thinking block → reasoning 頻道（正文乾淨，ADR-0152）。"""
    resp = _resp(
        [
            _block("thinking", thinking="使用者上次提過偏好，讓我想想……"),
            _block("text", text="答案是紅茶。"),
        ],
        [
            {"type": "thinking", "thinking": "使用者上次提過偏好，讓我想想……"},
            {"type": "text", "text": "答案是紅茶。"},
        ],
    )
    chunks = await _drive(monkeypatch, "thinking_split", [resp])
    assert_golden("claude", "thinking_split", chunks)


@pytest.mark.asyncio
async def test_native_anthropic_egress(monkeypatch):
    """claude 專屬：Anthropic 形狀請求 → 雙 egress 的 Anthropic 終端回應。"""
    chunks = await _drive(
        monkeypatch, "native_anthropic_egress",
        [_text_resp("你好，我是 Sumi。")],
        request=FakeAnthropicRequest(),
    )
    assert_golden("claude", "native_anthropic_egress", chunks)


# ── model-p1a U4：上游錯誤／截斷／拒答（3b-U5a 起 finish_reason 照實回報）──


def _stop_resp(blocks: list, dump_content: list, stop_reason: str) -> MagicMock:
    """帶 stop_reason 的回應（SDK Message 的屬性與 model_dump 兩處一致）。"""
    r = _resp(blocks, dump_content)
    r.stop_reason = stop_reason
    r.model_dump.return_value["stop_reason"] = stop_reason
    return r


async def _drive_raising(monkeypatch, scenario: str, exc: Exception) -> list[str]:
    """SDK 呼叫直接拋例外（_drive 的假 client 只回傳、不拋，故另立最小驅動）。"""
    clock = make_clock()
    install_clock(monkeypatch, _MODULE, clock)
    sid = f"sess-gc-{scenario}"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)

    async def create_side_effect(**_k):
        clock["now"] += UPSTREAM_S
        raise exc

    anthropic_client = MagicMock()
    anthropic_client.messages.create = AsyncMock(side_effect=create_side_effect)
    delegate_stream_to_create(anthropic_client)  # 3b-U1：主迴圈走 messages.stream()
    ctx = make_ctx(f"req-gc-{scenario}", sid, anthropic_client=anthropic_client,
                   model="claude-3-5-sonnet")
    try:
        with patch(f"{_MODULE}.ToolExecutor"):
            return await drain(handle_claude_request(_request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)


@pytest.mark.asyncio
async def test_upstream_error(monkeypatch):
    """SDK 例外（訊息仿 anthropic.BadRequestError 的 str）→ 單一錯誤 delta＋[DONE]。

    adapter 以 ``except Exception`` 接住、只用 ``str(e)``——例外型別不影響輸出。
    """
    exc = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'prompt is too long: 210000 tokens > 200000 maximum'}}"
    )
    chunks = await _drive_raising(monkeypatch, "upstream_error", exc)
    assert "API 發生錯誤" in "".join(chunks)  # 確認走到 RoundError 路徑
    assert_golden("claude", "upstream_error", chunks)


@pytest.mark.asyncio
async def test_truncated(monkeypatch):
    """stop_reason=max_tokens：半句正文照送，終端 finish_reason 是 length（3b-U5a）。"""
    text = "紅茶的沖泡步驟：第一步，把水煮到"
    resp = _stop_resp([_block("text", text=text)], [{"type": "text", "text": text}],
                      "max_tokens")
    chunks = await _drive(monkeypatch, "truncated", [resp])
    assert_golden("claude", "truncated", chunks)


@pytest.mark.asyncio
async def test_refusal(monkeypatch):
    """stop_reason=refusal 且沒有內容：只有一則拒答提示 delta，終端是 content_filter。"""
    chunks = await _drive(monkeypatch, "refusal", [_stop_resp([], [], "refusal")])
    assert_golden("claude", "refusal", chunks)
