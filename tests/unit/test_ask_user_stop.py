"""AI 問使用者問題沒等到回答時，這一輪直接結束、不再呼叫模型（ADR-0308）。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import AnthropicMessage, AnthropicRequest
from src.handlers import react_core
from src.handlers.claude_handler import handle_claude_request
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.ask_user_store import _reset_ask_user_store, get_ask_user_store
from src.services.rhythm_service import rhythm_service
from src.tools.builtin.interaction import ASK_USER_TIMEOUT, ask_user
from src.utils.ask_user_stop import ask_user_stop_text, is_blank_answer, mark_unanswered
from src.utils.tool_usage import record_tool_executed

from tests.helpers.claude_stream import delegate_stream_to_create

QUESTION = {"question": "要部署到哪裡？", "options": ["正式環境", "測試環境"]}


@pytest.fixture(autouse=True)
def _reset():
    _reset_ask_user_store()
    yield
    _reset_ask_user_store()


async def _emit(_event: dict) -> None:
    return None


# ── 工具本身 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_timeout_marks_the_turn_to_stop():
    ctx: dict[str, Any] = {}
    result = await ask_user(questions=[QUESTION], _emit_event=_emit, _ctx=ctx, _timeout=0.01)
    assert "未" in result
    assert "最合理的判斷" not in result
    assert ask_user_stop_text(ctx, set()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [[], [[]], [[], []], [[""]], [["  "], []]])
async def test_submitting_nothing_counts_as_no_answer(answers):
    captured: dict[str, Any] = {}

    async def emit(event: dict) -> None:
        captured["id"] = event["id"]
        get_ask_user_store().deliver_answer(event["id"], answers)

    ctx: dict[str, Any] = {}
    result = await ask_user(questions=[QUESTION], _emit_event=emit, _ctx=ctx, _timeout=5)
    assert captured["id"]
    assert "未" in result
    assert ask_user_stop_text(ctx, set()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [[["正式環境"]], [["正式環境"], []]])
async def test_a_partial_or_full_answer_does_not_stop_the_turn(answers):
    async def emit(event: dict) -> None:
        get_ask_user_store().deliver_answer(event["id"], answers)

    ctx: dict[str, Any] = {}
    questions = [QUESTION, {"question": "可不選", "options": ["A"], "multiSelect": True}]
    result = await ask_user(questions=questions, _emit_event=emit, _ctx=ctx, _timeout=5)
    assert json.loads(result)[0]["answer"] == ["正式環境"]
    assert ask_user_stop_text(ctx, set()) is None


@pytest.mark.asyncio
async def test_a_call_without_a_context_still_returns_quietly():
    assert "未" in await ask_user(questions=[QUESTION], _emit_event=_emit, _timeout=0.01)


@pytest.mark.asyncio
async def test_no_event_channel_does_not_tell_the_model_to_guess():
    result = await ask_user(questions=[QUESTION])
    assert "未在時限" in result
    assert "最合理的判斷" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "waited"),
    [
        ("不是數字", ASK_USER_TIMEOUT),
        (None, ASK_USER_TIMEOUT),
        ("nan", ASK_USER_TIMEOUT),
        (-1, ASK_USER_TIMEOUT),
        (ASK_USER_TIMEOUT * 10, ASK_USER_TIMEOUT),
        (10**400, ASK_USER_TIMEOUT),  # 大到轉不成小數
        (0.5, 0.5),
        ("2", 2.0),
    ],
)
async def test_the_model_can_shorten_the_wait_but_not_extend_it(monkeypatch, given, waited):
    seen: list[float] = []

    async def wait(_token: str, timeout: float) -> dict[str, Any]:
        seen.append(timeout)
        return {"answers": [["正式環境"]]}

    monkeypatch.setattr(get_ask_user_store(), "wait_for_answer", wait)
    await ask_user(questions=[QUESTION], _emit_event=_emit, _timeout=given)
    assert seen == [waited]


def test_is_blank_answer_only_when_every_answer_is_empty():
    assert is_blank_answer(None) is True
    assert is_blank_answer([]) is True
    assert is_blank_answer([[], ["  "]]) is True
    assert is_blank_answer([[], ["A"]]) is False


# ── 固定文字 ─────────────────────────────────────────────────────


def test_the_notice_has_the_question_options_and_tools():
    ctx: dict[str, Any] = {"ask_user_unanswered": [QUESTION]}
    text = ask_user_stop_text(ctx, {"read", "memory_tools", "memory_tools.save", "ask_user"})
    assert text is not None
    assert "要部署到哪裡？" in text
    assert "正式環境" in text
    assert "測試環境" in text
    assert "memory_tools、read" in text
    assert "ask_user" not in text
    assert "memory_tools.save" not in text
    assert "回答前不得代為決定" in text
    assert "直接回覆" in text


def test_the_notice_lists_questions_already_answered_in_this_turn():
    ctx: dict[str, Any] = {
        "ask_user_unanswered": [QUESTION],
        "ask_user_answered": [{"question": "用哪個分支？", "answer": ["dev"]}],
    }
    text = ask_user_stop_text(ctx, set())
    assert text is not None
    assert "用哪個分支？" in text
    assert "dev" in text


def test_marking_works_on_the_real_pipeline_context():
    ctx = PipelineContext()
    mark_unanswered(ctx, [QUESTION])
    mark_unanswered(ctx, [{"question": "第二題"}])
    text = ask_user_stop_text(ctx, set())
    assert text is not None
    assert "要部署到哪裡？" in text
    assert "第二題" in text


def test_no_notice_when_nothing_was_left_unanswered():
    answered = {"ask_user_answered": [{"question": "q", "answer": ["a"]}]}
    assert ask_user_stop_text(answered, set()) is None
    assert ask_user_stop_text({}, set()) is None


@pytest.mark.parametrize(
    "questions",
    [
        ["只是一段文字"],
        [{"question": "沒有選項"}],
        [{"options": ["A"]}],
        [{"question": "選項不是清單", "options": "A"}],
        [{"question": None, "options": [None, 3]}],
        [None],
    ],
)
def test_a_malformed_question_still_produces_a_notice(questions):
    assert ask_user_stop_text({"ask_user_unanswered": questions}, set()) is not None


# ── 整輪 ─────────────────────────────────────────────────────────


def _response(blocks: list[MagicMock], dumped: list[dict], stop_reason: str) -> MagicMock:
    resp = MagicMock()
    resp.content = blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 3, "output_tokens": 2}
    resp.model_dump.return_value = {
        "id": "m",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": dumped,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return resp


def _tool_block(call_id: str, name: str, args: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = call_id
    block.name = name
    block.input = args
    return block


def _tool_response(*calls: tuple[str, str, dict]) -> MagicMock:
    return _response(
        [_tool_block(*call) for call in calls],
        [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls],
        "tool_use",
    )


def _text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return _response([block], [{"type": "text", "text": text}], "end_turn")


async def _run(
    client: MagicMock,
    tools: dict[str, Any],
    request_id: str,
    monkeypatch: pytest.MonkeyPatch,
    native_request: AnthropicRequest | None = None,
) -> list[str]:
    async def run_tool(
        _self: Any,
        name: str,
        args: dict,
        _rid: str,
        ctx: Any,
        queue: Any,
        call_id: str | None = None,
        actually_used_tools: Any = None,
    ) -> str:
        record_tool_executed(actually_used_tools, name, args)
        result = await tools[name](**args, _emit_event=_emit, _ctx=ctx)
        await queue.put(None)
        return str(result)

    monkeypatch.setattr(react_core.ToolExecutor, "execute_to_queue", run_tool)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *_a, **_k: None)
    rhythm_service.clear_session(request_id)
    context = {
        "anthropic_client": delegate_stream_to_create(client),
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": tools,
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
    chunks = [c async for c in handle_claude_request(native_request or request, context)]
    rhythm_service.clear_session(request_id)
    return chunks


def _text_of(chunks: list[str]) -> str:
    texts = []
    for chunk in chunks:
        payload = chunk[len("data: ") :].strip() if chunk.startswith("data: ") else ""
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        texts.append((obj.get("choices") or [{}])[0].get("delta", {}).get("content", "") or "")
    return "".join(texts)


ASK_ARGS = {"questions": [QUESTION], "_timeout": 0.01}


@pytest.mark.asyncio
async def test_a_timeout_ends_the_turn_without_calling_the_model_again(monkeypatch):
    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=[_tool_response(("t1", "ask_user", ASK_ARGS)), _text_response("自己猜著做")]
    )
    chunks = await _run(client, {"ask_user": ask_user}, "ask-stop-1", monkeypatch)
    assert client.messages.create.call_count == 1
    text = _text_of(chunks)
    assert "要部署到哪裡？" in text
    assert "回答前不得代為決定" in text
    assert "自己猜著做" not in text
    assert chunks[-1].strip() == "data: [DONE]"


@pytest.mark.asyncio
async def test_other_tools_in_the_same_round_still_finish(monkeypatch):
    other = AsyncMock(return_value="done")
    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=[
            _tool_response(("t1", "ask_user", ASK_ARGS), ("t2", "vault_tools", {})),
            _text_response("自己猜著做"),
        ]
    )
    chunks = await _run(
        client, {"ask_user": ask_user, "vault_tools": other}, "ask-stop-2", monkeypatch
    )
    assert other.await_count == 1
    assert client.messages.create.call_count == 1
    assert "vault_tools" in _text_of(chunks)


@pytest.mark.asyncio
async def test_an_answer_lets_the_turn_continue(monkeypatch):
    async def answering(**kwargs: Any) -> str:
        real_emit = kwargs["_emit_event"]

        async def emit(event: dict) -> None:
            await real_emit(event)
            get_ask_user_store().deliver_answer(event["id"], [["測試環境"]])

        return await ask_user(**{**kwargs, "_emit_event": emit})

    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=[_tool_response(("t1", "ask_user", ASK_ARGS)), _text_response("照你選的做")]
    )
    chunks = await _run(client, {"ask_user": answering}, "ask-stop-3", monkeypatch)
    assert client.messages.create.call_count == 2
    assert "照你選的做" in _text_of(chunks)
    assert "回答前不得代為決定" not in _text_of(chunks)


@pytest.mark.asyncio
async def test_two_questions_in_one_turn_one_answered_one_timed_out(monkeypatch):
    answered_once: list[str] = []

    async def first_answered(**kwargs: Any) -> str:
        real_emit = kwargs["_emit_event"]

        async def emit(event: dict) -> None:
            await real_emit(event)
            if not answered_once:
                answered_once.append(event["id"])
                get_ask_user_store().deliver_answer(event["id"], [["dev"]])

        return await ask_user(**{**kwargs, "_emit_event": emit})

    first = {"questions": [{"question": "用哪個分支？", "options": ["dev"]}], "_timeout": 5}
    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=[
            _tool_response(("t1", "ask_user", first)),
            _tool_response(("t2", "ask_user", ASK_ARGS)),
            _text_response("自己猜著做"),
        ]
    )
    chunks = await _run(client, {"ask_user": first_answered}, "ask-stop-4", monkeypatch)
    assert client.messages.create.call_count == 2
    text = _text_of(chunks)
    assert "用哪個分支？" in text
    assert "dev" in text
    assert "要部署到哪裡？" in text


@pytest.mark.asyncio
async def test_the_native_claude_ending_keeps_its_own_shape(monkeypatch):
    """Claude 原生入口的結尾沒有 ``choices``；已經串流過的文字要清空，才不會顯示兩次。"""
    said = MagicMock()
    said.type = "text"
    said.text = "我先問一下"
    asked = _response(
        [said, _tool_block("t1", "ask_user", ASK_ARGS)],
        [
            {"type": "text", "text": "我先問一下"},
            {"type": "tool_use", "id": "t1", "name": "ask_user", "input": ASK_ARGS},
        ],
        "tool_use",
    )
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[asked, _text_response("自己猜著做")])
    native = AnthropicRequest(
        model="claude-3-5-sonnet", messages=[AnthropicMessage(role="user", content="hi")]
    )
    chunks = await _run(client, {"ask_user": ask_user}, "ask-stop-native", monkeypatch, native)
    assert client.messages.create.call_count == 1
    assert chunks[-1].strip() == "data: [DONE]"
    ending = json.loads(chunks[-2][len("data: ") :])
    assert "choices" not in ending
    assert ending["content"][0] == {"type": "text", "text": ""}
    assert "回答前不得代為決定" in _text_of(chunks)  # 文字片段不分入口都是同一種格式
