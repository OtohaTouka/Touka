"""回合、子代理、剪裁、任務、MCP 表單與心跳的 hook 事件（ADR-0292 決定 17）。

- 只通知的事件：結束碼 2 與 JSON 都沒有效果，呼叫端不等，hook 失敗不影響任何事。
- 事件內容是白名單（每個事件釘住鍵的集合），不放派工目標、工具參數以外的對話內容。
- StopFailure 只在這一輪真的失敗時發：重試中不發、子代理不發、按停止不發，錯誤內容先遮罩。
- SubagentStart 的補充說明是資料，只進子代理第一則訊息，不進審查員的輸入（禁令 64 ③）；hook 期間
  按停止就不送模型（禁令 67 ⑦）。
- 剪裁的 hook 卡住時，剪裁照常做完（禁令 28、57）；存檔沒成功不發 PostCompact。
- Elicitation 只能拒絕，回 accept 或 content 照樣問人；無人在場本來就拒絕，不觸發。

全部在 sandbox：hook 走假的 http runner，上游走假的傳輸層，不連網路、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp import types
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import retry
from src.handlers.adapters.base import RoundError
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter, _State
from src.models.hooks import MATCHER_EVENTS, NO_CONVERSATION_EVENTS, WIRED_EVENTS
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult, UiGenerationStopped
from src.models.sliding_window import SlidingWindowConfig
from src.protocols.mcp.canonical import McpRequestOrigin
from src.protocols.mcp.interaction import (
    InProcessMcpInteractionBroker,
    McpInteractionSink,
    bind_mcp_interaction,
)
from src.routers import heartbeat as hb
from src.services import dispatcher, sliding_window_service
from src.services.approval_service import ApprovalService
from src.services.hooks import lifecycle
from src.services.hooks.outcome import interpret
from src.services.hooks.runners import RunResult
from src.services.rhythm_service import rhythm_service
from src.services.sliding_window_service import SlidingWindowService
from src.tools import subagent_tools
from src.tools.mission_tools import mission_tools
from src.utils.async_db import SessionSnapshot

from tests.helpers.stream_transport import FakeUpstream, json_reply, status_reply
from tests.support.hook_wiring import group, install_hooks, until

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.support.hook_wiring import HookSetup

_COMMON = {"hook_event_name", "session_id", "agent_id", "incognito"}
_NOTIFY_ONLY = (
    "StopFailure",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "TaskCreated",
    "TaskCompleted",
    "ConfigChange",
    "HeartbeatStart",
    "HeartbeatStop",
)
_SOUL = AgentIdentity.for_role("LifecycleProbe", "Owner")
_GUARD_S = 5.0


@pytest.fixture
def hooks(tmp_path, monkeypatch) -> HookSetup:
    return install_hooks(tmp_path, monkeypatch)


def _specific(event: str, **fields: Any) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}})


def _ok(stdout: str = "") -> RunResult:
    return RunResult(status="ok", duration_ms=1, exit_code=0, stdout=stdout)


def _extra(call: dict[str, Any]) -> set[str]:
    return set(call) - _COMMON


# ── 事件清單 ───────────────────────────────────────────────────────────────────


def test_every_new_event_is_wired_and_tasks_and_heartbeats_have_no_matcher():
    assert set(_NOTIFY_ONLY) | {"SubagentStart", "Elicitation"} <= set(WIRED_EVENTS)
    assert {"TaskCreated", "TaskCompleted", "HeartbeatStart", "HeartbeatStop"}.isdisjoint(
        MATCHER_EVENTS
    )
    assert {
        "StopFailure", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact", "Elicitation",
        "ConfigChange",
    } <= MATCHER_EVENTS  # fmt: skip
    assert {"ConfigChange", "HeartbeatStart", "HeartbeatStop"} == NO_CONVERSATION_EVENTS


# ── hook 的輸出 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", _NOTIFY_ONLY)
@pytest.mark.parametrize(
    "body",
    [
        '{"decision": "block", "reason": "no"}',
        '{"continue": false, "stopReason": "no"}',
        "plain text",
    ],
)
def test_notify_only_events_ignore_what_the_hook_answers(event, body):
    verdict = interpret(event, _ok(body))
    assert not (verdict.blocked or verdict.ask or verdict.failed or verdict.context)


@pytest.mark.parametrize("event", _NOTIFY_ONLY)
def test_notify_only_events_ignore_exit_2_and_additional_context(event):
    answered = _ok(_specific(event, additionalContext="extra"))
    assert interpret(event, answered).context is None
    exit_2 = interpret(event, RunResult(status="ok", duration_ms=1, exit_code=2, stderr="no"))
    assert not (exit_2.blocked or exit_2.failed or exit_2.context)


@pytest.mark.parametrize("event", _NOTIFY_ONLY)
def test_a_failing_hook_is_still_recorded_as_failed(event):
    assert interpret(event, RunResult(status="ok", duration_ms=1, exit_code=1)).failed


def test_subagent_start_only_takes_additional_context():
    verdict = interpret("SubagentStart", _ok(_specific("SubagentStart", additionalContext="note")))
    assert verdict.context == "note"
    block = interpret("SubagentStart", _ok('{"decision": "block", "reason": "no"}'))
    assert not block.blocked
    exit_2 = interpret("SubagentStart", RunResult(status="ok", duration_ms=1, exit_code=2))
    assert not exit_2.blocked


@pytest.mark.parametrize(
    "result",
    [
        RunResult(status="ok", duration_ms=1, exit_code=2, stderr="no"),
        _ok(_specific("Elicitation", action="decline")),
        _ok(_specific("Elicitation", action="cancel")),
        _ok('{"decision": "block", "reason": "no"}'),
    ],
    ids=["exit-2", "decline", "cancel", "decision-block"],
)
def test_elicitation_hooks_can_refuse(result):
    assert interpret("Elicitation", result).blocked


@pytest.mark.parametrize(
    "body",
    [
        _specific("Elicitation", action="accept", content={"name": "filled by hook"}),
        _specific("Elicitation", content={"name": "filled by hook"}),
    ],
)
def test_elicitation_hooks_can_not_answer_for_the_user(body):
    verdict = interpret("Elicitation", _ok(body))
    assert not verdict.blocked
    assert verdict.notes


def test_elicitation_hooks_get_no_path_to_the_model():
    assert interpret("Elicitation", _ok(_specific("Elicitation", additionalContext="x"))).context \
        is None


# ── StopFailure ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, "invalid_request"),
        (422, "invalid_request"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (402, "billing_error"),
        (404, "model_not_found"),
        (429, "rate_limit"),
        (529, "overloaded"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "unknown"),
    ],
)
def test_http_status_maps_to_the_claude_code_error_names(status, error_type):
    assert retry.error_type_for_status(status) == error_type
    tagged = retry.on_status(RoundError("boom"), status, {}, "busy")
    assert tagged.error_type == error_type


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        ({"type": "overloaded_error"}, "overloaded"),
        ({"type": "rate_limit_error"}, "rate_limit"),
        ({"status": "RESOURCE_EXHAUSTED"}, "rate_limit"),
        ({"type": "api_error"}, "server_error"),
        ({"code": "503"}, "server_error"),
        ({"type": "invalid_request_error"}, "invalid_request"),
        ({"type": "authentication_error"}, "authentication_failed"),
        ({"type": "something_new"}, "unknown"),
    ],
)
def test_error_bodies_after_a_200_map_to_the_same_names(body, error_type):
    assert retry.on_error_body(RoundError("boom"), body, "X").error_type == error_type


def test_an_error_nobody_classified_is_unknown():
    assert RoundError("boom").error_type == "unknown"


def _request(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


def _loop_ctx(**extra: Any) -> dict[str, Any]:
    return {
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": "r-life", "t0": 0.0, "agent_id": _SOUL.agent_id, "identity": _SOUL,
        "session_id": "s-life", "provider": "Grok", "api_key": "fake-key",
        "url": "https://api.x.test/v1/chat/completions", "custom_connection": False,
        **extra,
    }  # fmt: skip


async def _run_loop(adapter: Any, model: str, ctx: dict[str, Any]) -> list[str]:
    async with asyncio.timeout(_GUARD_S):
        return [c async for c in react_core.run_react_loop(adapter, _request(model), ctx)]


@pytest.fixture
def no_wait(monkeypatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(react_core, "_retry_sleep", fake_sleep)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *_a, **_k: None)


async def test_a_failed_round_fires_stop_failure_and_no_stop(monkeypatch, hooks, no_wait):
    hooks.configure({"StopFailure": [group()], "Stop": [group()]})
    FakeUpstream([json_reply("bad key=abcdefgh12345", 400)]).install(monkeypatch)
    await _run_loop(OpenAIAdapter(stream=True), "grok-4", _loop_ctx())
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert call["hook_event_name"] == "StopFailure"
    assert _extra(call) == {"error", "error_details"}
    assert call["error"] == "invalid_request"
    assert "abcdefgh12345" not in call["error_details"]
    assert "400" in call["error_details"]


async def test_retries_do_not_fire_only_the_final_failure_does(monkeypatch, hooks, no_wait):
    hooks.configure({"StopFailure": [group()]})
    FakeUpstream([status_reply(503, "busy")] * 3).install(monkeypatch)
    await _run_loop(GeminiAdapter(stream=True), "gemini-2.5-flash",
                    _loop_ctx(gemini_api_key="fake-key"))
    await until(lambda: hooks.http.calls)
    await asyncio.sleep(0)
    assert [call["error"] for call in hooks.http.calls] == ["server_error"]


async def test_a_retry_that_succeeds_fires_nothing(monkeypatch, hooks, no_wait):
    hooks.configure({"StopFailure": [group()]})
    good = {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]}
    FakeUpstream([status_reply(503), json_reply(json.dumps(good), 200)]).install(monkeypatch)
    await _run_loop(OpenAIAdapter(stream=False), "grok-4", _loop_ctx())
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_stop_failure_matches_on_the_error_type(hooks):
    hooks.configure({"StopFailure": [group(matcher="rate_limit")]})
    lifecycle.notify_stop_failure({"session_id": "s"}, "server_error", "boom")
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []
    lifecycle.notify_stop_failure({"session_id": "s"}, "rate_limit", "slow down")
    await until(lambda: hooks.http.calls)


@pytest.mark.parametrize(
    "secret",
    ["Bearer abc123", "key=abcdefgh", "password=hunter2", "api_key: abc123", "sk-abcdef1234567890"],
)
async def test_stop_failure_details_hide_short_credentials(hooks, secret):
    hooks.configure({"StopFailure": [group()]})
    lifecycle.notify_stop_failure({"session_id": "s"}, "unknown", f"upstream said {secret} once")
    await until(lambda: hooks.http.calls)
    details = hooks.http.calls[0]["error_details"]
    assert secret.split(maxsplit=1)[-1].split("=")[-1].split(": ")[-1] not in details


async def test_subagent_rounds_do_not_fire_stop_failure(hooks):
    hooks.configure({"StopFailure": [group()]})
    lifecycle.notify_stop_failure({"subagent_run": True}, "unknown", "boom")
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


async def test_no_hook_means_the_details_are_not_even_read(hooks, monkeypatch):
    def boom(_text: str) -> str:
        raise AssertionError

    monkeypatch.setattr(lifecycle, "redact_credentials", boom)
    lifecycle.notify_stop_failure({"session_id": "s"}, "unknown", "details")
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


class _FailingStream:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        async def stream() -> Any:
            raise self.error
            yield ""  # 讓它是 async generator

        return stream()


async def _dispatch(monkeypatch, error: Exception) -> None:
    monkeypatch.setattr(dispatcher, "get_stream_generator", _FailingStream(error))
    request = SimpleNamespace(stream=False, ui_persistence=None)
    await dispatcher.dispatch_request("grok-4", request, _SOUL, "r-life", 0.0, {"session_id": "s"})


async def test_an_unexpected_error_in_the_turn_fires_stop_failure_as_unknown(monkeypatch, hooks):
    hooks.configure({"StopFailure": [group()]})
    await _dispatch(monkeypatch, RuntimeError("Bearer abc123 went wrong"))
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert call["error"] == "unknown"
    assert "abc123" not in call["error_details"]


@pytest.mark.parametrize("reason", ["error", "cancelled"])
async def test_a_turn_already_reported_or_stopped_is_not_reported_again(monkeypatch, hooks, reason):
    hooks.configure({"StopFailure": [group()]})
    await _dispatch(monkeypatch, UiGenerationStopped(reason))
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


# ── 讀不懂的模型回覆（順手修 1）───────────────────────────────────────────────


@pytest.mark.parametrize("body", ["<html>not json</html>", "[1, 2]", '"text"'])
async def test_a_reply_that_can_not_be_read_is_a_round_error(body):
    async def post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=body)

    state = _State.__new__(_State)
    state.provider, state.url, state.headers, state.payload = "Grok", "http://x.test", {}, {}
    state.client = SimpleNamespace(post=post)
    result = await OpenAIAdapter(stream=False)._post_once(state)
    assert isinstance(result, RoundError)
    assert "Grok API Error (200)" in result.user_message
    assert result.retry_note is None


# ── SubagentStart／SubagentStop ────────────────────────────────────────────────


class _Pipeline:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def execute(self, _request: Any, ctx: Any) -> None:
        self.contexts.append(ctx)


_BASE_CTX = {"session_id": "s-parent", "agent_id": "Parent", "user_intent": "the human asked"}


def _sub_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="__subagent_probe__", role="Subagent")


async def _assemble(monkeypatch, base_ctx: dict[str, Any] | None = None) -> tuple[Any, _Pipeline]:
    pipeline = _Pipeline()
    monkeypatch.setattr("src.services.proxy_service.pipeline", pipeline)
    request = await subagent_tools._assemble_subagent_request(
        goal="secret goal text",
        sub_identity=_sub_identity(),
        base_ctx=base_ctx or dict(_BASE_CTX),
        model="m",
    )
    return request, pipeline


async def test_subagent_start_content_has_no_goal(monkeypatch, hooks):
    hooks.configure({"SubagentStart": [group()]})
    await _assemble(monkeypatch)
    (call,) = hooks.http.calls
    assert _extra(call) == {"agent_type"}
    assert call["agent_id"] == "__subagent_probe__"
    assert call["session_id"] == "s-parent"
    assert call["agent_type"] == "subagent"
    assert "secret goal text" not in json.dumps(call)


async def test_subagent_start_context_only_reaches_the_first_prompt_as_data(monkeypatch, hooks):
    hooks.configure({"SubagentStart": [group()]})
    hooks.http.bodies["SubagentStart"] = _specific(
        "SubagentStart", additionalContext="note </hook_output> from hook"
    )
    request, pipeline = await _assemble(monkeypatch)
    prompt = request.messages[0].content
    assert "<hook_output>" in prompt
    assert prompt.count("</hook_output>") == 1  # 內文的 tag 中和過，不能提早關閉區塊
    assert "note" in prompt
    ctx = pipeline.contexts[0]
    assert "note" not in str(ctx.get("user_intent"))  # 審查員只看人類原意（禁令 64 ③）
    assert "note" not in ctx["subagent_goal"]


async def test_no_subagent_start_hook_keeps_the_prompt_unchanged(monkeypatch, hooks):
    request, _pipeline = await _assemble(monkeypatch)
    assert request.messages[0].content == subagent_tools._subagent_user_prompt(
        "secret goal text"
    )


async def test_a_failing_subagent_start_hook_does_not_stop_the_subagent(monkeypatch, hooks):
    hooks.configure({"SubagentStart": [group()]})
    hooks.http.failing.add("SubagentStart")
    request, _pipeline = await _assemble(monkeypatch)
    assert "<hook_output>" not in request.messages[0].content
    assert hooks.service.runs()[0].outcome == "failed"


async def test_stopping_during_the_subagent_start_hook_never_reaches_the_model(monkeypatch, hooks):
    hooks.configure({"SubagentStart": [group()]})
    session = "s-stop-sub"
    rhythm_service.clear_interrupt(session)
    gate = asyncio.Event()
    hooks.http.gates["SubagentStart"] = gate
    pipeline = _Pipeline()
    monkeypatch.setattr("src.services.proxy_service.pipeline", pipeline)
    task = asyncio.create_task(
        subagent_tools._assemble_subagent_request(
            goal="g", sub_identity=_sub_identity(), base_ctx={"session_id": session}, model="m",
        )
    )  # fmt: skip
    try:
        await until(lambda: hooks.http.calls)
        rhythm_service.interrupt_session(session)
        gate.set()
        with pytest.raises(subagent_tools.SubagentStoppedError):
            await asyncio.wait_for(task, _GUARD_S)
    finally:
        rhythm_service.clear_interrupt(session)
    assert pipeline.contexts == []


def _fake_subagent_stream(text: str) -> Callable[..., Any]:
    async def factory(*_args: Any, **_kwargs: Any) -> Any:
        async def stream() -> Any:
            yield 'data: {"choices": [{"delta": {"content": "' + text + '"}}]}\n\n'
            yield "data: [DONE]\n\n"

        return stream()

    return factory


async def test_subagent_stop_reports_the_last_message(monkeypatch, hooks):
    hooks.configure({"SubagentStop": [group()]})
    monkeypatch.setattr("src.services.proxy_service.pipeline", _Pipeline())
    monkeypatch.setattr(subagent_tools, "get_stream_generator", _fake_subagent_stream("all done"))
    text = await subagent_tools._run_subagent(
        goal="g", parent_identity=None, parent_request_id="r", base_ctx=dict(_BASE_CTX), model="m",
    )  # fmt: skip
    assert text == "all done"
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert _extra(call) == {"agent_type", "last_assistant_message"}
    assert call["agent_id"].startswith("__subagent_")
    assert call["last_assistant_message"] == "all done"


async def test_subagent_stop_fires_when_the_subagent_fails_with_an_empty_message(
    monkeypatch, hooks
):
    hooks.configure({"SubagentStop": [group()]})
    monkeypatch.setattr("src.services.proxy_service.pipeline", _Pipeline())

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError

    monkeypatch.setattr(subagent_tools, "get_stream_generator", boom)
    with pytest.raises(ValueError):  # noqa: PT011 — 假的例外，只看它照樣往外傳
        await subagent_tools._run_subagent(
            goal="g", parent_identity=None, parent_request_id="r", base_ctx=dict(_BASE_CTX),
            model="m",
        )  # fmt: skip
    await until(lambda: hooks.http.calls)
    assert hooks.http.calls[0]["last_assistant_message"] == ""


async def test_incognito_subagent_hooks_keep_only_the_result(monkeypatch, hooks):
    hooks.configure({"SubagentStart": [group([{"type": "http", "url": "http://127.0.0.1:9/h",
                                               "name": "secret-name"}])]})
    await _assemble(monkeypatch, {**_BASE_CTX, "incognito": True})
    (run,) = hooks.service.runs()
    assert (run.incognito, run.hook) == (True, "")


# ── PreCompact／PostCompact ────────────────────────────────────────────────────


def _linear() -> list[dict[str, Any]]:
    roles = ["user", "assistant"] * 4
    return [
        {"id": f"m{i}", "role": role, "content": f"content_{i}",
         "parentId": f"m{i - 1}" if i else None}
        for i, role in enumerate(roles)
    ]  # fmt: skip


def _session() -> dict[str, Any]:
    linear = _linear()
    return {
        "id": "s-trim", "title": "t",
        "messages": {"messages": {m["id"]: m for m in linear}, "currentId": linear[-1]["id"]},
    }  # fmt: skip


def _trim_service(monkeypatch, *, saved: Any, summary: str | None = None) -> SlidingWindowService:
    svc = SlidingWindowService()
    config = SlidingWindowConfig(
        enabled=True, keep_rounds=2, upper_limit_rounds=3, summary_budget_pct=10
    )
    monkeypatch.setattr(svc, "load_config", lambda: config)
    monkeypatch.setattr(svc, "should_trim", lambda *_a: True)
    manager = MagicMock()
    manager.get_legacy_sync_service.return_value.archive_payload.return_value = 2
    monkeypatch.setattr(sliding_window_service, "get_manager", lambda: manager)
    monkeypatch.setattr(sliding_window_service, "get_context_window", lambda _model: 10)
    monkeypatch.setattr(
        sliding_window_service, "summarize_dialogue", AsyncMock(return_value=summary)
    )
    sessions = MagicMock()
    sessions.read_persistence_snapshot = AsyncMock(
        return_value=SessionSnapshot(_session(), "before")
    )
    sessions.conditional_save_session = AsyncMock(return_value=saved)
    monkeypatch.setattr(sliding_window_service, "session_service", sessions)
    return svc


_SAVED = SavedResult(status="saved", representation="full", revision="after")


async def test_trimming_fires_pre_then_post_compact(monkeypatch, hooks):
    hooks.configure({"PreCompact": [group()], "PostCompact": [group()]})
    svc = _trim_service(monkeypatch, saved=_SAVED, summary="short summary")
    assert await svc.check_and_trim(_SOUL, "s-trim") is True
    await until(lambda: len(hooks.http.calls) == 2)
    pre, post = hooks.http.calls
    assert (pre["hook_event_name"], _extra(pre)) == ("PreCompact", {"trigger"})
    assert post["hook_event_name"] == "PostCompact"
    assert _extra(post) == {"trigger", "compact_summary"}
    assert (pre["trigger"], post["trigger"]) == ("auto", "auto")
    assert post["compact_summary"] == "short summary"
    assert (pre["session_id"], pre["agent_id"]) == ("s-trim", _SOUL.agent_id)


async def test_a_trim_that_was_not_saved_fires_no_post_compact(monkeypatch, hooks):
    hooks.configure({"PreCompact": [group()], "PostCompact": [group()]})
    svc = _trim_service(monkeypatch, saved=None)
    assert await svc.check_and_trim(_SOUL, "s-trim") is False
    await until(lambda: hooks.http.calls)
    await asyncio.sleep(0.05)
    assert hooks.http.events() == ["PreCompact"]


async def test_a_stuck_compact_hook_does_not_hold_the_trim(monkeypatch, hooks):
    hooks.configure({"PreCompact": [group()], "PostCompact": [group()]})
    gate = asyncio.Event()
    hooks.http.gates.update({"PreCompact": gate, "PostCompact": gate})
    svc = _trim_service(monkeypatch, saved=_SAVED)
    try:
        assert await asyncio.wait_for(svc.check_and_trim(_SOUL, "s-trim"), _GUARD_S) is True
    finally:
        gate.set()
    await until(lambda: len(hooks.http.calls) == 2)


async def test_the_compact_summary_has_a_limit(monkeypatch, hooks):
    hooks.configure({"PostCompact": [group()]})
    svc = _trim_service(monkeypatch, saved=_SAVED, summary="x" * 50_000)
    await svc.check_and_trim(_SOUL, "s-trim")
    await until(lambda: hooks.http.calls)
    assert len(hooks.http.calls[0]["compact_summary"]) == lifecycle.MAX_COMPACT_SUMMARY_CHARS


async def test_compact_hooks_match_on_the_trigger(monkeypatch, hooks):
    hooks.configure({"PreCompact": [group(matcher="manual")]})
    await _trim_service(monkeypatch, saved=_SAVED).check_and_trim(_SOUL, "s-trim")
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


# ── TaskCreated／TaskCompleted ─────────────────────────────────────────────────


_TASK_KEYS = {"mission_id", "task_id", "task_subject"}


def _soul_with_board(tmp_name: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"LifecycleTask{tmp_name}", role="Owner")


async def _mission(identity: AgentIdentity, action: str, **arguments: Any) -> Any:
    arguments.setdefault("_ctx", {"session_id": "s-task", "agent_id": identity.agent_id})
    return await mission_tools(action=action, identity=identity, **arguments)


async def _board(identity: AgentIdentity) -> None:
    reply = await _mission(
        identity,
        "initialize",
        goal="goal that is not sent",
        tasks=[{"task_id": "T1", "description": "first"}, {"task_id": "T2", "description": "next"}],
    )
    assert reply.startswith("✅ Mission Initialized"), reply


async def test_each_task_on_a_new_board_fires_task_created(hooks):
    hooks.configure({"TaskCreated": [group()]})
    await _board(_soul_with_board("Create"))
    await until(lambda: len(hooks.http.calls) == 2)
    assert all(_extra(call) == _TASK_KEYS for call in hooks.http.calls)
    assert sorted(call["task_subject"] for call in hooks.http.calls) == ["first", "next"]
    assert "goal that is not sent" not in json.dumps(hooks.http.calls)


async def test_only_a_task_that_became_done_fires_task_completed(hooks):
    hooks.configure({"TaskCompleted": [group()]})
    soul = _soul_with_board("Done")
    await _board(soul)
    await _mission(soul, "update_status", task_id="T1", status="DOING")
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []
    await _mission(soul, "update_status", task_id="T1", status="DONE")
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert _extra(call) == _TASK_KEYS
    assert (call["task_id"], call["task_subject"]) == ("T1", "first")


async def test_an_illegal_transition_fires_nothing(hooks):
    hooks.configure({"TaskCompleted": [group()]})
    soul = _soul_with_board("Illegal")
    await _board(soul)
    await _mission(soul, "update_status", task_id="T1", status="DONE")  # 沒經過 DOING
    await asyncio.sleep(0.05)
    assert hooks.http.calls == []


# ── Elicitation ────────────────────────────────────────────────────────────────


def _elicitation() -> types.ElicitRequest:
    return types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message="Please fill this in",
            requestedSchema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    )


def _binding(origin: McpRequestOrigin = McpRequestOrigin.FOREGROUND) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(origin=origin))


def _elicit_sink(queue: asyncio.Queue, ctx: dict[str, Any], server: str = "srv") -> Any:
    async def before_shown(message: str, schema: dict[str, Any]) -> bool:
        return await lifecycle.run_elicitation_hooks(ctx, server, message, schema)

    return McpInteractionSink(
        queue=queue, call_id="c1", tool_name="srv__demo", agent_id="Alice", session_id="s-e",
        before_shown=before_shown,
    )  # fmt: skip


async def _elicit_and_decline(hooks: HookSetup, ctx: dict[str, Any] | None = None) -> Any:
    service = ApprovalService()
    broker = InProcessMcpInteractionBroker(service)
    queue: asyncio.Queue = asyncio.Queue()
    with bind_mcp_interaction(_elicit_sink(queue, ctx or {"session_id": "s-e"})):
        task = asyncio.create_task(broker.respond("q", _elicitation(), _binding()))
        waiter = asyncio.create_task(queue.get())
        done, _pending = await asyncio.wait(
            {task, waiter}, timeout=_GUARD_S, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "表單應該跳出或被拒絕"
        shown = waiter in done
        if shown:
            await service.resolve(waiter.result()["id"], decision="declined", resolver="tester")
        else:
            waiter.cancel()
        result = await asyncio.wait_for(task, _GUARD_S)
    return shown, result


async def test_elicitation_content_and_matcher(hooks):
    hooks.configure({"Elicitation": [group(matcher="srv")]})
    shown, _result = await _elicit_and_decline(hooks)
    assert shown
    (call,) = hooks.http.calls
    assert _extra(call) == {"mcp_server_name", "message", "mode", "requested_schema"}
    assert (call["mcp_server_name"], call["mode"]) == ("srv", "form")
    assert call["message"] == "Please fill this in"


async def test_elicitation_hook_for_another_server_does_not_run(hooks):
    hooks.configure({"Elicitation": [group(matcher="other")]})
    shown, _result = await _elicit_and_decline(hooks)
    assert shown
    assert hooks.http.calls == []


async def test_a_refusing_hook_declines_without_showing_the_form(hooks):
    hooks.configure({"Elicitation": [group()]})
    hooks.http.bodies["Elicitation"] = _specific("Elicitation", action="decline")
    shown, result = await _elicit_and_decline(hooks)
    assert not shown
    assert result.action == "decline"


async def test_a_hook_that_fills_the_form_still_asks_the_human(hooks):
    hooks.configure({"Elicitation": [group()]})
    hooks.http.bodies["Elicitation"] = _specific(
        "Elicitation", action="accept", content={"name": "filled by hook"}
    )
    shown, result = await _elicit_and_decline(hooks)
    assert shown
    assert result.action == "decline"  # 人按了拒絕，hook 代填沒有效果
    assert "filled by hook" not in json.dumps(result.model_dump())


async def test_a_failing_elicitation_hook_still_asks_the_human(hooks):
    hooks.configure({"Elicitation": [group()]})
    hooks.http.failing.add("Elicitation")
    shown, _result = await _elicit_and_decline(hooks)
    assert shown


async def test_stopping_during_the_elicitation_hook_declines(hooks):
    hooks.configure({"Elicitation": [group()]})
    session = "s-stop-elicit"
    rhythm_service.clear_interrupt(session)
    rhythm_service.interrupt_session(session)
    try:
        shown, result = await _elicit_and_decline(hooks, {"session_id": session})
    finally:
        rhythm_service.clear_interrupt(session)
    assert not shown
    assert result.action == "decline"


async def test_nobody_present_declines_before_any_hook_runs(hooks):
    hooks.configure({"Elicitation": [group()]})
    service = ApprovalService()
    broker = InProcessMcpInteractionBroker(service)
    with bind_mcp_interaction(_elicit_sink(asyncio.Queue(), {"session_id": "s-e"})):
        result = await broker.respond("q", _elicitation(), _binding(McpRequestOrigin.HEARTBEAT))
    assert result.action == "decline"
    assert hooks.http.calls == []


# ── HeartbeatStart／HeartbeatStop ──────────────────────────────────────────────


async def _heartbeat(monkeypatch, pulse: Any, pulses: int = 2, home: bool = False) -> None:
    monkeypatch.setattr(hb, "_execute_single_pulse", pulse)
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    await hb._process_heartbeat_loop(
        final_session_id="s-beat", identity=_SOUL, model_override=None, ctx=hb.PipelineContext(),
        pulse_count=pulses, pulse_slots=1, t0=0.0, init_prompt="", is_home_mode=home,
    )  # fmt: skip


_real_sleep = asyncio.sleep


async def _settle(hooks: HookSetup, count: int) -> None:
    async with asyncio.timeout(_GUARD_S):
        while len(hooks.http.calls) < count:
            await _real_sleep(0.005)


async def test_heartbeat_start_and_stop_content(monkeypatch, hooks):
    hooks.configure({"HeartbeatStart": [group()], "HeartbeatStop": [group()]})
    await _heartbeat(monkeypatch, AsyncMock(return_value=1))
    await _settle(hooks, 2)
    start, stop = hooks.http.calls
    assert (start["hook_event_name"], _extra(start)) == (
        "HeartbeatStart", {"pulses_total", "home"},
    )  # fmt: skip
    assert (start["pulses_total"], start["home"]) == (2, False)
    assert (stop["hook_event_name"], _extra(stop)) == (
        "HeartbeatStop", {"pulses_total", "pulses_done", "end"},
    )  # fmt: skip
    assert (stop["pulses_done"], stop["end"]) == (2, "completed")
    assert (start["session_id"], start["agent_id"]) == ("s-beat", _SOUL.agent_id)


async def test_a_heartbeat_that_blew_up_still_reports_how_it_ended(monkeypatch, hooks):
    hooks.configure({"HeartbeatStop": [group()]})
    monkeypatch.setattr(
        hb.session_service, "append_message_to_session", AsyncMock(return_value=True)
    )
    await _heartbeat(monkeypatch, AsyncMock(side_effect=OSError("boom")))
    await _settle(hooks, 1)
    assert (hooks.http.calls[0]["end"], hooks.http.calls[0]["pulses_done"]) == ("error", 0)


async def test_a_stuck_heartbeat_hook_does_not_hold_the_pulses(monkeypatch, hooks):
    hooks.configure({"HeartbeatStart": [group()]})
    gate = asyncio.Event()
    hooks.http.gates["HeartbeatStart"] = gate
    pulse = AsyncMock(return_value=1)
    try:
        await asyncio.wait_for(_heartbeat(monkeypatch, pulse), _GUARD_S)
    finally:
        gate.set()
    assert pulse.await_count == 2


# ── ConfigChange 的內容（接線與防漏見 test_hook_config_change_security.py）───────


async def test_config_change_content_has_no_values(hooks):
    hooks.configure({"ConfigChange": [group(matcher="autonomy")]})
    lifecycle.notify_config_change("autonomy")
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert _extra(call) == {"source"}
    assert call["source"] == "autonomy"
    lifecycle.notify_config_change("env")
    await asyncio.sleep(0.05)
    assert len(hooks.http.calls) == 1
