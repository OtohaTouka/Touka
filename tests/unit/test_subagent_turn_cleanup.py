"""子代理的收尾與等結果綁在這一輪（ADR-0323 PR 3）.

- 子代理-8 (1)：子代理的 ctx 拿掉主代理的 ``mission_awareness``（組裝時 ContextLayer 會把它注入）。
- 子代理-8 (4)：背景子代理用 ``spawn_logged`` 開，正常關機
  （``cancel_all_background_tasks``）時被取消，等的一方收到「已取消」。
- 子代理拒答：子代理這一輪的拒答、安全過濾、工具幻覺小提示記下來；沒有文字時結果寫明被拒答或被擋，
  有文字時另附系統附註，``await_dispatch_result`` 照樣列出。
- 等候綁在這一輪：``mission_tools`` 的 ``await_dispatch_result`` 和 ``local_files``、同步派的子代理
  一樣，人按停止時一起收掉；其他 ``mission_tools`` 動作不綁。被取消的等候不標讀過，
  結果之後照樣拿得到。

全部在 sandbox：模型、組裝與工具都是假的，不連網路、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.openai_handler import handle_openai_request
from src.middleware.layers import context as context_layer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.dispatch_callback_store import get_callback_store
from src.services.rhythm_service import rhythm_service
from src.services.subagent_registry import subagent_registry
from src.tools import subagent_tools as st
from src.tools.mission_tools import mission_tools
from src.utils import task_utils
from src.utils.sse import sse_line
from src.utils.turn_notices import (
    NOTICE_REFUSAL,
    NOTICE_SAFETY,
    NOTICE_TOOL_FINISHED,
    NOTICE_TOOL_HALLUCINATION,
    turn_notice_event,
)

from tests.helpers.stream_transport import FakeUpstream, stream_reply
from tests.proxy.characterization.harness import drain, install_clock, make_clock, make_ctx, msg

_GUARD_S = 10.0
_SOUL = AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"])
_BLOCK = "<mission_awareness>parent board</mission_awareness>"


# ── 子代理-8 (1)：不帶主代理的看板 ─────────────────────────────────────────────


def _request() -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="m",
        messages=[
            OpenAIMessage(role="system", content="s"),
            OpenAIMessage(role="user", content="u"),
        ],
    )


async def test_a_subagent_does_not_carry_the_parent_board(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_layer, "render_mission_block", lambda _m: _BLOCK)
    base = PipelineContext(mission_awareness=SimpleNamespace(goal="parent"))
    parent_request = _request()
    await context_layer.ContextLayer().process(parent_request, base)  # 對照組：主代理看得到
    assert _BLOCK in str(parent_request.messages[0].content)

    ctx = st._derive_context(base)
    st._mark_subagent_run(ctx, base, "goal", st.SubagentScope())
    assert ctx.get("mission_awareness") is None
    assert base.get("mission_awareness") is not None  # 主代理自己的不動
    sub_request = _request()
    await context_layer.ContextLayer().process(sub_request, ctx)
    assert _BLOCK not in str(sub_request.messages[0].content)


# ── 子代理-8 (4)：關機時交回已取消 ─────────────────────────────────────────────


def _subagent_tasks() -> set[asyncio.Task[Any]]:
    return {t for t in task_utils._background_tasks if t.get_name().startswith("subagent:")}


async def test_a_background_subagent_is_cancelled_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def forever(**_kwargs: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr(st, "_run_subagent", forever)
    res = await st.dispatch_subagent(goal="g", identity=None, request_id="r", _ctx={})
    token = json.loads(res)["callback_token"]
    assert _subagent_tasks()  # 經 spawn_logged，關機收得到
    async with asyncio.timeout(_GUARD_S):
        await started.wait()
        assert await task_utils.cancel_all_background_tasks() >= 1
        result = await get_callback_store().wait_for_result(token, timeout=_GUARD_S)
    assert result["status"] == "CANCELLED"


# ── 子代理拒答 ──────────────────────────────────────────────────────────────


def _notice(kind: str, **params: Any) -> dict[str, Any]:
    return turn_notice_event(kind, "shown", **params)


async def _stream(*items: Any) -> Any:
    for item in items:
        yield item if isinstance(item, str) else sse_line(item)
    yield "data: [DONE]\n\n"


def _text(text: str) -> str:
    return sse_line({"choices": [{"delta": {"content": text}}]})


async def test_draining_keeps_refusal_safety_and_hallucination_notices() -> None:
    notices: list[dict[str, Any]] = []
    text = await st._drain_text(
        _stream(
            _text("先"),
            _notice(NOTICE_TOOL_FINISHED, tool="x"),
            _notice(NOTICE_REFUSAL, detail="不行"),
            _notice(NOTICE_SAFETY),
            _notice(NOTICE_TOOL_HALLUCINATION, tools="web_search"),
        ),
        notices=notices,
    )
    assert text == "先"
    kinds = [n["kind"] for n in notices]
    assert kinds == [NOTICE_REFUSAL, NOTICE_SAFETY, NOTICE_TOOL_HALLUCINATION]


@pytest.mark.parametrize(
    ("notices", "summary"),
    [
        (
            [_notice(NOTICE_REFUSAL, detail="不行\n就是不行")],
            f"{st.SUBAGENT_REFUSED}：不行⏎就是不行",
        ),
        ([_notice(NOTICE_REFUSAL)], st.SUBAGENT_REFUSED),
        ([_notice(NOTICE_SAFETY)], st.SUBAGENT_SAFETY_BLOCKED),
        ([], "(子代理無文字產出)"),
    ],
)
def test_a_subagent_with_no_text_says_why(notices: list[Any], summary: str) -> None:
    payload = st._result_from_text("", "t1", notices)
    assert payload["result_summary"] == summary
    assert "system_notes" not in payload


def test_a_subagent_with_text_gets_a_system_note() -> None:
    payload = st._result_from_text(
        "做完了", "t1", [_notice(NOTICE_TOOL_HALLUCINATION, tools="web_search")]
    )
    assert payload["result_summary"] == "做完了"
    assert payload["system_notes"] == [st.SUBAGENT_TOOL_HALLUCINATION.format(tools="web_search")]
    assert "system_notes" not in st._result_from_text("做完了", "t1", [])


class _FakeRequest:
    tools: list[dict] | None = None
    model = "grok-4"
    system = ""
    messages: list[dict] = []


async def test_a_refused_background_subagent_hands_back_the_refusal(
    monkeypatch: pytest.MonkeyPatch, zone_data_root: Any
) -> None:
    monkeypatch.setattr(st, "_assemble_subagent_request", AsyncMock(return_value=_FakeRequest()))
    monkeypatch.setattr(st, "session_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(st, "stop_agent_commands", AsyncMock(return_value=0))

    async def refused(*_args: Any, **_kwargs: Any) -> Any:
        return _stream(_notice(NOTICE_REFUSAL, detail="不行"))

    monkeypatch.setattr(st, "get_stream_generator", refused)
    res = await st.dispatch_subagent(goal="g", identity=_SOUL, request_id="r", _ctx={})
    token = json.loads(res)["callback_token"]
    async with asyncio.timeout(_GUARD_S):
        result = await get_callback_store().wait_for_result(token, timeout=_GUARD_S)
    assert result["result_summary"] == f"{st.SUBAGENT_REFUSED}：不行"
    text = await mission_tools(
        action="await_dispatch_result", identity=_SOUL, callback_token=token, timeout=_GUARD_S
    )
    assert f"Result: {st.SUBAGENT_REFUSED}：不行" in text


async def test_await_lists_the_system_notes(zone_data_root: Any) -> None:
    store = get_callback_store()
    token = store.generate_token()
    store.register(token, in_process=True)
    store.deliver_result(
        token,
        {"task_id": "t1", "status": "DONE", "result_summary": "做完了", "system_notes": ["附註"]},
    )
    text = await mission_tools(
        action="await_dispatch_result", identity=_SOUL, callback_token=token, timeout=_GUARD_S
    )
    assert "System note: 附註" in text
    assert text.index("System note: 附註") < text.index("Result: 做完了")


# ── 等候綁在這一輪 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "args", "tied"),
    [
        ("mission_tools", {"action": "await_dispatch_result", "callback_token": "c"}, True),
        ("mission_tools", {"action": "get_board"}, False),
        ("mission_tools", "not a dict", False),
        ("local_files", {}, True),
        ("dispatch_subagent", {}, True),
        ("research", {"action": "await_dispatch_result"}, False),
    ],
)
def test_only_the_await_action_of_mission_tools_is_tied(name: str, args: Any, tied: bool) -> None:
    assert react_core._tied_to_the_turn(name, args) is tied


async def test_a_tied_await_is_cancelled_with_the_turn() -> None:
    waiting = asyncio.create_task(asyncio.sleep(3600))
    board = asyncio.create_task(asyncio.sleep(3600))
    react_core._cancel_turn_bound_tools(
        [("c1", "mission_tools", waiting, True), ("c2", "mission_tools", board, False)]
    )
    await asyncio.sleep(0)
    assert waiting.cancelled()
    assert not board.cancelled()
    board.cancel()


async def test_a_cancelled_await_does_not_mark_the_result_read(
    monkeypatch: pytest.MonkeyPatch, zone_data_root: Any
) -> None:
    mark_read = MagicMock()
    monkeypatch.setattr(subagent_registry, "mark_read", mark_read)
    store = get_callback_store()
    token = store.generate_token()
    store.register(token, in_process=True)
    waiting = asyncio.create_task(
        mission_tools(
            action="await_dispatch_result", identity=_SOUL, callback_token=token, timeout=_GUARD_S
        )
    )
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    mark_read.assert_not_called()
    store.deliver_result(token, {"task_id": "t1", "status": "DONE", "result_summary": "好"})
    text = await mission_tools(  # 結果之後照樣拿得到
        action="await_dispatch_result", identity=_SOUL, callback_token=token, timeout=_GUARD_S
    )
    assert "Result: 好" in text
    mark_read.assert_called_once_with(token)


@pytest.mark.parametrize(
    ("arguments", "cancelled"),
    [('{"action": "await_dispatch_result", "callback_token": "c"}', True),
     ('{"action": "get_board"}', False)],
)
async def test_the_main_loop_ties_the_await_to_the_turn(
    monkeypatch: pytest.MonkeyPatch, arguments: str, cancelled: bool
) -> None:
    """接線：主代理的迴圈被收掉時，等子代理結果的那一步一起取消；看板的其他動作照舊在背景跑完。"""
    install_clock(monkeypatch, "src.handlers.react_core", make_clock())
    sid, rid = f"s-await-{cancelled}", f"req-await-{cancelled}"
    rhythm_service.clear_session(sid)
    call = {"id": "c1", "type": "function",
            "function": {"name": "mission_tools", "arguments": arguments}}
    tool_round = {"choices": [{"message": {"content": None, "tool_calls": [call]}}]}
    FakeUpstream([stream_reply(tool_round, "one")]).install(monkeypatch)
    ctx = make_ctx(rid, sid, custom_connection=False)
    request = MagicMock(model="gpt-4", pulse_slots=None, messages=[msg("user", "hi")])
    request.model_dump.return_value = {
        "model": "gpt-4", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]
    }
    running = asyncio.Event()
    seen: dict[str, Any] = {}

    async def _execute(*_a: Any, **_kw: Any) -> str:
        seen["task"] = asyncio.current_task()
        running.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        return "unreachable"

    try:
        with patch("src.handlers.react_core.ToolExecutor") as executor:
            executor.return_value.execute_to_queue = AsyncMock(side_effect=_execute)
            loop = asyncio.create_task(drain(handle_openai_request(request, ctx)))
            async with asyncio.timeout(_GUARD_S):
                await running.wait()
                loop.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop
                await asyncio.sleep(0)
    finally:
        rhythm_service.clear_session(sid)
        seen["task"].cancel()  # 沒綁的那一步：測試自己收掉
    assert seen.get("cancelled", False) is cancelled
