"""工具資訊與網頁以外的路（ADR-0323 PR 4）.

- 前景-9：降級時四家說同一句英文（記憶工具這一輪用不了，不影響其他工具）；
  Anthropic 入口轉換時不再說第二次。
- 前景-11：工具結果超過上限時，只截給模型的那一份（保留頭尾、中間一行說明），核准說明與 hook 輸出
  照樣接在後面；畫面的工具卡、成功判定、``/debug/execute`` 拿完整版。
- 非串流被停：OpenAI 形狀的請求最後一包沒有 ``choices`` 時，自己組最小的
  ``chat.completion``（保留用量）；
  Anthropic 原生入口的形狀不變。
- 舊串流存檔：模型失敗時節點標 ``generationFailed``、失敗句不存進文字，沒寫字也存；群聊回合不標。
- 誠實度檢查：這一輪實際送出的請求沒有工具時不插矯正輪（矯正輪會強制要用工具，
  沒有工具時廠商回 400）。

全部離線：模型、hook、工具與串流緩衝都是假的。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters.base import Turn
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity
from src.services import approval_flow, dispatcher
from src.services.dispatchers.utils import convert_to_openai
from src.services.prompt_blocks import build_degraded_note
from src.services.rhythm_service import rhythm_service
from src.tools import terminal
from src.utils.tool_result_cap import MAX_TOOL_RESULT_CHARS, cap_tool_result

from tests.helpers.stream_transport import FakeUpstream, status_reply, stream_reply
from tests.proxy.characterization.harness import drain, install_clock, make_clock, make_ctx, msg
from tests.support.hook_wiring import executor_with, group, install_hooks, run_tool

_SOUL = AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"])
_BRAG = "根據過去的記憶系統內容，你上次說喜歡紅茶。"


# ── 前景-9：降級說明 ─────────────────────────────────────────────────────────


def test_the_degraded_note_is_about_memory_tools_only() -> None:
    assert build_degraded_note("Memory module failed to load") == (
        "Memory tools are unavailable this turn (Memory module failed to load). "
        "This does not affect the other tools in your list; if one fails, its result says so."
    )
    assert "(" not in build_degraded_note("")  # 沒有原因就不寫空括號
    assert "(Memory offline). This" in build_degraded_note("Memory offline.")  # 不疊兩個句點


def test_the_anthropic_entry_conversion_does_not_add_a_second_warning() -> None:
    native = AnthropicRequest(
        model="m", max_tokens=10, system="妳是 Sumi。",
        messages=[{"role": "user", "content": "hi"}],
    )
    converted = convert_to_openai(native)
    assert converted.messages[0].content == "妳是 Sumi。"


# ── 前景-11：工具結果上限 ────────────────────────────────────────────────────


def test_a_short_result_is_left_alone() -> None:
    text = "x" * MAX_TOOL_RESULT_CHARS
    assert cap_tool_result(text) == text


def test_a_long_result_keeps_its_head_and_tail() -> None:
    text = "H" * 30_000 + "MIDDLE" + "T" * 30_000
    capped = cap_tool_result(text)
    assert capped.startswith("H" * 100)
    assert capped.endswith("T" * 100)
    assert "MIDDLE" not in capped
    omitted = len(text) - MAX_TOOL_RESULT_CHARS
    assert f"原本 {len(text)} 字，略去中間 {omitted} 字" in capped
    assert "view_range" in capped
    assert len(capped) < MAX_TOOL_RESULT_CHARS + 200


@pytest.fixture
def hooks(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_hooks(tmp_path, monkeypatch)


async def test_only_the_model_copy_of_a_long_result_is_capped(
    monkeypatch: pytest.MonkeyPatch, hooks: Any
) -> None:
    hooks.configure({"PostToolUse": [group()]})
    hooks.http.bodies["PostToolUse"] = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "from post"}}
    )
    output = "A" * 30_000 + "MIDDLE" + "Z" * 30_000
    result = terminal._wrap_output("ls", f"[EXIT_CODE: 0]\n{output}", exit_code=0)
    tool = SimpleNamespace(calls=[])

    async def handler(**kwargs: Any) -> str:
        tool.calls.append(kwargs)
        return result

    executor = executor_with(monkeypatch, {"execute_command": handler})
    for_model, events = await run_tool(
        executor, "execute_command", {"command": "ls"}, mode="Manual", approve=True,
        for_model=True,
    )
    (card,) = [e for e in events if e.get("type") == "tool_result"]
    assert card["status"] == "success"  # 成功判定不受影響
    assert "MIDDLE" in card["result"]  # 畫面的工具卡完整
    assert "MIDDLE" not in for_model
    assert "系統截斷" in for_model
    assert approval_flow.APPROVED_BY_PERSON_NOTE in for_model  # 核准說明照樣接在後面
    assert "<hook_output>" in for_model and "from post" in for_model
    full, _events = await run_tool(
        executor, "execute_command", {"command": "ls"}, mode="Manual", approve=True
    )
    assert "MIDDLE" in full  # 沒指定給模型（例如 /debug/execute）拿完整版


def _loop_request() -> MagicMock:
    request = MagicMock(model="gpt-4", pulse_slots=None, messages=[msg("user", "hi")])
    request.model_dump.return_value = {
        "model": "gpt-4", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]
    }
    return request


async def test_the_main_loop_asks_for_the_model_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """接線：ReAct 迴圈執行工具時要的是給模型的那一份（會截），不是完整版."""
    install_clock(monkeypatch, "src.handlers.react_core", make_clock())
    sid, rid = "s-cap", "req-cap"
    rhythm_service.clear_session(sid)
    call = {"id": "c1", "type": "function",
            "function": {"name": "vault_tools", "arguments": '{"action": "list"}'}}
    tool_round = {"choices": [{"message": {"content": None, "tool_calls": [call]}}]}
    text_round = {"choices": [{"message": {"content": "好"}}]}
    FakeUpstream([stream_reply(tool_round, "one"), stream_reply(text_round, "one")]).install(
        monkeypatch
    )
    asked: list[bool] = []

    async def _execute(*args: Any, for_model: bool = False, **_kw: Any) -> str:
        asked.append(for_model)
        await args[4].put(None)  # 事件佇列：這個工具做完了
        return "ok"

    try:
        with patch("src.handlers.react_core.ToolExecutor") as executor:
            executor.return_value.execute_to_queue = AsyncMock(side_effect=_execute)
            ctx = make_ctx(rid, sid, custom_connection=False)
            async with asyncio.timeout(30):
                await drain(handle_openai_request(_loop_request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
    assert asked == [True]


async def test_a_failed_round_always_records_its_failure_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """舊串流存檔靠 ``model_failure_text`` 拿掉失敗句：一般路徑（用戶端收得到那句）也要記下."""
    install_clock(monkeypatch, "src.handlers.react_core", make_clock())
    sid, rid = "s-fail", "req-fail"
    rhythm_service.clear_session(sid)
    FakeUpstream([status_reply(400, "bad request")]).install(monkeypatch)
    ctx = make_ctx(rid, sid, custom_connection=False)
    try:
        async with asyncio.timeout(30):
            await drain(handle_openai_request(_loop_request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
    assert ctx["model_failed"] is True
    assert not ctx.get("caller_sends_failure_text")  # 一般路徑：失敗句照樣送給用戶端
    assert ctx["model_failure_text"]


# ── 非串流被停 ──────────────────────────────────────────────────────────────


def _install_stream(monkeypatch: pytest.MonkeyPatch, *chunks: str) -> None:
    async def gen() -> Any:
        for chunk in chunks:
            yield chunk
        yield "data: [DONE]\n\n"

    async def factory(*_args: Any, **_kwargs: Any) -> Any:
        return gen()

    monkeypatch.setattr(dispatcher, "get_stream_generator", factory)


def _data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_STOPPED = _data({"type": "turn_notice", "kind": "interrupted", "params": {}, "text": "停"})


async def _non_stream(request: Any) -> dict[str, Any]:
    response = await dispatcher.dispatch_request("m-1", request, _SOUL, "req-ns", 0.0, {})
    return json.loads(response.body)


async def test_a_stopped_non_stream_openai_reply_is_still_a_chat_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stream(monkeypatch, _STOPPED)
    request = OpenAIChatRequest(model="m-1", messages=[OpenAIMessage(role="user", content="hi")])
    body = await _non_stream(request)
    assert body["object"] == "chat.completion"
    assert body["model"] == "m-1"
    assert body["id"] and isinstance(body["created"], int)
    assert body["choices"] == [{
        "index": 0,
        "message": {"role": "assistant", "content": ""},
        "finish_reason": "stop",
    }]


async def test_a_usage_only_last_chunk_keeps_its_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
    _install_stream(
        monkeypatch,
        _data({"id": "c1", "choices": [{"index": 0, "delta": {"content": "好"}}]}),
        _data({"id": "c1", "choices": [], "usage": usage}),
    )
    request = OpenAIChatRequest(model="m-1", messages=[OpenAIMessage(role="user", content="hi")])
    body = await _non_stream(request)
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "好"}
    assert body["usage"] == usage
    assert body["id"] == "c1"


async def test_an_error_last_chunk_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    error = {"error": {"message": "bad", "type": "invalid_request_error"}}
    _install_stream(monkeypatch, _data(error))
    request = OpenAIChatRequest(model="m-1", messages=[OpenAIMessage(role="user", content="hi")])
    assert await _non_stream(request) == error


async def test_the_native_anthropic_non_stream_shape_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stream(monkeypatch, _STOPPED)
    request = AnthropicRequest(
        model="m-1", max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    )
    assert await _non_stream(request) == {}


# ── 舊串流存檔的出錯記號 ────────────────────────────────────────────────────


async def _pump(
    monkeypatch: pytest.MonkeyPatch, chunks: list[str], ctx: dict[str, Any], model: str = "m-1"
) -> AsyncMock:
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(dispatcher, "_persist_assistant_turn", persist)
    monkeypatch.setattr(dispatcher, "stream_buffer_service", MagicMock(
        append=AsyncMock(), finish=AsyncMock()
    ))
    monkeypatch.setattr(dispatcher, "spawn_logged", MagicMock())
    monkeypatch.setattr(dispatcher, "_maybe_trim_after_chat", MagicMock())

    async def gen() -> Any:
        for chunk in chunks:
            yield chunk

    await dispatcher._pump_stream_to_buffer(gen(), "s-old", model, _SOUL, context=ctx)
    return persist


def _text(text: str) -> str:
    return _data({"choices": [{"index": 0, "delta": {"content": text}}]})


FAILURE = "OpenAI API Error (500)"


async def test_a_failed_legacy_stream_is_saved_with_the_failure_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = {"model_failed": True, "model_failure_text": FAILURE}
    persist = await _pump(monkeypatch, [_text("說到一半"), _text(FAILURE)], ctx)
    args, kwargs = persist.await_args
    assert args[1] == "說到一半"  # 失敗句不存進文字
    assert kwargs["node_fields"]["generationFailed"] is True


async def test_a_failure_with_no_text_is_still_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = {"model_failed": True, "model_failure_text": FAILURE}
    persist = await _pump(monkeypatch, [_text(FAILURE)], ctx)
    args, kwargs = persist.await_args
    assert args[1] == ""
    assert kwargs["node_fields"] == {"generationFailed": True}


async def test_a_group_round_is_not_flagged_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = {"model_failed": True, "model_failure_text": FAILURE}
    persist = await _pump(monkeypatch, [_text("〔@A 這一輪出錯：x〕")], ctx, model="ToukaGroup")
    _args, kwargs = persist.await_args
    assert "generationFailed" not in kwargs["node_fields"]


async def test_a_successful_legacy_stream_is_saved_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist = await _pump(monkeypatch, [_text("好")], {})
    args, kwargs = persist.await_args
    assert args[1] == "好"
    assert "generationFailed" not in kwargs["node_fields"]


# ── 誠實度檢查：沒有工具就不跑 ───────────────────────────────────────────────

_TOOL = {
    "type": "function",
    "function": {"name": "memory_tools", "description": "d",
                 "parameters": {"type": "object", "properties": {}}},
}


async def _openai(tools: bool) -> tuple[Any, Any]:
    dump: dict[str, Any] = {"model": "gpt-4", "max_tokens": 100,
                            "messages": [{"role": "user", "content": "hi"}]}
    if tools:
        dump["tools"] = [_TOOL]
    request = MagicMock()
    request.model = "gpt-4"
    request.model_dump.return_value = dump
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    adapter = OpenAIAdapter()
    ctx = {"api_key": "k", "url": "http://fake.test", "provider": "OpenAI", "request_id": "r"}
    return adapter, await adapter.prepare_request(request, ctx)


async def _claude(tools: bool) -> tuple[Any, Any]:
    request = OpenAIChatRequest(
        model="claude-3-5-sonnet", messages=[OpenAIMessage(role="user", content="hi")],
        tools=[_TOOL] if tools else None,
    )
    adapter = ClaudeAdapter()
    return adapter, await adapter.prepare_request(request, {"anthropic_client": MagicMock()})


async def _responses(tools: bool) -> tuple[Any, Any]:
    request = OpenAIChatRequest(
        model="gpt-6-astra", messages=[OpenAIMessage(role="user", content="hi")],
        tools=[_TOOL] if tools else None,
    )
    adapter = ResponsesAdapter()
    return adapter, await adapter.prepare_request(request, {})


async def _gemini(tools: bool) -> tuple[Any, Any]:
    converted: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    if tools:
        converted["tools"] = [{"functionDeclarations": [{"name": "memory_tools"}]}]
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    adapter = GeminiAdapter()
    with patch(
        "src.handlers.adapters.gemini_adapter.convert_openai_to_gemini", return_value=converted
    ):
        state = await adapter.prepare_request(request, {"gemini_api_key": "k", "agent_id": "S"})
    return adapter, state


@pytest.mark.parametrize("build", [_openai, _claude, _responses, _gemini])
async def test_the_honesty_guard_needs_tools_in_the_round(build: Any) -> None:
    turn = Turn(visible_text=_BRAG, raw_assistant=[])
    adapter, without = await build(False)
    assert adapter.mid_loop_hook(without, turn, set(), True) is None
    adapter, with_tools = await build(True)
    assert adapter.mid_loop_hook(with_tools, turn, set(), True) is not None  # 對照組
    for state in (without, with_tools):
        close = getattr(adapter, "close", None)
        if close is not None:
            await asyncio.wait_for(close(state), 5)
