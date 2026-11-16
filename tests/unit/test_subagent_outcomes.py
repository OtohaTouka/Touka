"""子代理的結果講清楚（可視性第二批 PR 1；ADR-0318）.

- 模型錯誤（額度、金鑰、限流）一律算失敗，只寫固定的錯誤種類，不放廠商原文；網頁、心跳、IDE
  三種主代理走同一條路。
- 超過時限時交回已經做到的部分並標「逾時」；時限包住整個子代理（含 SubagentStart hook 與組裝）。
- 結果太長時保留結尾（結論在最後），子代理的文字不夾工具日誌行。
- 一次派多個時，子代理自己回報失敗的不算成功；同步派工的說法也看回報。

全部在 sandbox：上游走假的傳輸層，不連網路、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters import retry
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.openai_handler import handle_openai_request
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.rhythm_service import rhythm_service
from src.tools import subagent_tools as st

from tests.helpers.stream_transport import FakeUpstream, json_reply, stream_reply
from tests.proxy.characterization.harness import (
    drain,
    install_clock,
    make_clock,
    make_ctx,
    make_mock_executor,
    msg,
)

_GUARD_S = 10.0  # 測試護欄：生產碼的時限都遠小於它
_VENDOR_TEXT = "VENDOR-SAYS-QUOTA-GONE"
_SOUL = AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"])
# 網頁、心跳、IDE 三種主代理的 ctx 標記
_PARENTS = {
    "web": {"ui_authorized": True},
    "heartbeat": {"heartbeat_run": True, "enable_autonomy": True},
    "ide": {},
}


class _FakeRequest:
    tools: list[dict] | None = None
    model = "grok-4"
    system = ""
    messages: list[dict] = []


def _loop_ctx(**extra: Any) -> dict[str, Any]:
    return {
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": "r-outcome", "t0": 0.0, "agent_id": _SOUL.agent_id, "identity": _SOUL,
        "provider": "Grok", "api_key": "fake-key",
        "url": "https://api.x.test/v1/chat/completions", "custom_connection": False,
        **extra,
    }  # fmt: skip


def _chat_request() -> OpenAIChatRequest:
    return OpenAIChatRequest(model="grok-4", messages=[OpenAIMessage(role="user", content="hi")])


@pytest.fixture
def no_wait(monkeypatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(react_core, "_retry_sleep", fake_sleep)
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *_a, **_k: None)


@pytest.fixture
def fake_assemble(monkeypatch) -> None:
    async def _assemble(**_kw: Any) -> _FakeRequest:
        return _FakeRequest()

    monkeypatch.setattr(st, "_assemble_subagent_request", _assemble)


@pytest.fixture
def real_loop(monkeypatch) -> None:
    """子代理的串流走真的主迴圈（上游是假的傳輸層），ctx 用 ``_run_assembled`` 衍生的那份。"""

    async def _factory(_model, _request, _identity, _request_id, _t0, context, **_kw):
        return react_core.run_react_loop(OpenAIAdapter(stream=True), _chat_request(), context)

    monkeypatch.setattr(st, "get_stream_generator", _factory)


def _text_chunk(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


async def _one_result(base_ctx: Any) -> dict[str, Any]:
    async with asyncio.timeout(_GUARD_S):
        (result,) = await st.run_subagent_group(
            subtasks=[{"goal": "查一下", "task_id": "t1"}],
            parent_identity=_SOUL,
            parent_request_id="req-outcome",
            base_ctx=base_ctx,
            model="grok-4",
        )
    return result


# --------------------------------------------------------------------------- #
# 子代理-2：模型錯誤
# --------------------------------------------------------------------------- #


async def test_a_failed_round_records_the_error_kind(monkeypatch, no_wait):
    """主迴圈失敗時把固定的錯誤種類記在 ctx（心跳與子代理都讀它）。"""
    FakeUpstream([json_reply("bad key", 400)]).install(monkeypatch)
    ctx = PipelineContext(**_loop_ctx(heartbeat_run=True, session_id="s-kind"))
    async with asyncio.timeout(_GUARD_S):
        _ = [
            c
            async for c in react_core.run_react_loop(
                OpenAIAdapter(stream=True), _chat_request(), ctx
            )
        ]
    assert ctx.get("model_failed") is True
    assert ctx.get("model_error_type") == "invalid_request"


@pytest.mark.parametrize("parent", sorted(_PARENTS))
async def test_a_model_error_fails_the_subagent_the_same_way_for_every_parent(
    monkeypatch, no_wait, fake_assemble, real_loop, parent
):
    """限流用完重試：網頁、心跳、IDE 派的子代理都是 FAILED、寫出種類，不帶廠商原文。"""
    body = f"{_VENDOR_TEXT} key=abcdefgh12345"
    FakeUpstream([json_reply(body, 429)] * react_core.MAX_SEND_ATTEMPTS).install(monkeypatch)
    result = await _one_result(PipelineContext(**_loop_ctx(**_PARENTS[parent])))
    assert result["status"] == "FAILED"
    summary = result["result_summary"]
    assert "模型錯誤（rate_limit）" in summary
    assert _VENDOR_TEXT not in summary
    assert "abcdefgh12345" not in summary
    assert "UiGenerationStopped" not in summary


def test_an_unknown_error_kind_is_reported_as_unknown():
    """種類只出自 retry.py 的固定表；表外的值不照抄（模型看到的文字只有固定說法）。"""
    assert "rate_limit" in retry.KNOWN_ERROR_TYPES
    assert "unknown" in retry.KNOWN_ERROR_TYPES
    err = st.SubagentModelError("sk-leaked-by-someone", "")
    assert err.error_type == "unknown"


async def test_a_subagent_model_error_keeps_what_it_wrote_before(monkeypatch, fake_assemble):
    """失敗前寫下的文字當成已做到的部分交回；種類照記號。"""

    async def _factory(_model, _request, _identity, _request_id, _t0, context, **_kw):
        async def _gen():
            yield _text_chunk("讀完 a.py 了。")
            context["model_failed"] = True
            context["model_error_type"] = "overloaded"
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(st, "get_stream_generator", _factory)
    result = await _one_result({})
    assert result["status"] == "FAILED"
    assert "模型錯誤（overloaded）" in result["result_summary"]
    assert "讀完 a.py 了。" in result["result_summary"]


def test_the_subagent_does_not_inherit_the_parents_failure_or_ui_flags():
    """子代理的 ctx 從零開始：父代理的失敗記號與網頁旗標都不帶過去。"""
    parent = PipelineContext(
        session_id="s", ui_authorized=True, model_failed=True, model_error_type="rate_limit"
    )
    child = st._derive_context(parent)
    st._mark_subagent_run(child, parent, "goal", st._NO_SCOPE)
    assert child.get("ui_authorized") is False
    assert child.get("model_failed") is False
    assert child.get("model_error_type") is None


# --------------------------------------------------------------------------- #
# 子代理-3：逾時交回已做到的部分
# --------------------------------------------------------------------------- #


def _hanging_after_text(text: str):
    async def _factory(*_a: Any, **_kw: Any):
        async def _gen():
            yield _text_chunk(text)
            await asyncio.sleep(3600)

        return _gen()

    return _factory


async def test_a_timeout_hands_back_the_partial_text(monkeypatch, fake_assemble):
    monkeypatch.setattr(st, "get_stream_generator", _hanging_after_text("已讀完 a.py，"))
    monkeypatch.setattr(st, "_SUB_TIMEOUT_SECONDS", 0.05)
    stop = MagicMock()
    monkeypatch.setattr(st, "notify_subagent_stop", stop)

    with pytest.raises(st.SubagentTimeoutError) as caught:
        async with asyncio.timeout(_GUARD_S):
            await st._run_subagent(
                goal="g", parent_identity=_SOUL, parent_request_id="r", base_ctx={}, model=None
            )
    assert isinstance(caught.value, TimeoutError)
    assert caught.value.partial == "已讀完 a.py，"
    assert stop.call_args.args[2] == "已讀完 a.py，"


async def test_a_timed_out_subagent_result_says_timeout_and_keeps_the_partial(
    monkeypatch, fake_assemble
):
    monkeypatch.setattr(st, "get_stream_generator", _hanging_after_text("已讀完 a.py，"))
    monkeypatch.setattr(st, "_SUB_TIMEOUT_SECONDS", 0.05)
    result = await _one_result({})
    assert result["status"] == "FAILED"
    assert "逾時" in result["result_summary"]
    assert "已讀完 a.py，" in result["result_summary"]


async def test_the_time_limit_covers_the_start_hook_and_assembly(monkeypatch):
    """組裝（含 SubagentStart hook）卡住也在時限內中止，不是只算模型那段。"""

    async def _stuck_assemble(**_kw: Any):
        await asyncio.sleep(3600)

    monkeypatch.setattr(st, "_assemble_subagent_request", _stuck_assemble)
    monkeypatch.setattr(st, "_SUB_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(st.SubagentTimeoutError):
        async with asyncio.timeout(_GUARD_S / 5):
            await st._run_subagent(
                goal="g", parent_identity=_SOUL, parent_request_id="r", base_ctx={}, model=None
            )


async def test_a_timeout_inside_a_tool_is_not_mistaken_for_the_subagent_limit(
    monkeypatch, fake_assemble
):
    """子代理裡別的 TimeoutError 照原樣往外，不說成「子代理逾時」。"""

    async def _factory(*_a: Any, **_kw: Any):
        async def _gen():
            raise TimeoutError
            yield ""  # pragma: no cover

        return _gen()

    monkeypatch.setattr(st, "get_stream_generator", _factory)
    with pytest.raises(TimeoutError) as caught:
        await st._run_subagent(
            goal="g", parent_identity=_SOUL, parent_request_id="r", base_ctx={}, model=None
        )
    assert not isinstance(caught.value, st.SubagentTimeoutError)


# --------------------------------------------------------------------------- #
# 子代理-5：保留結論
# --------------------------------------------------------------------------- #


def test_a_long_result_keeps_the_conclusion_at_the_end():
    body = "過程" * (st._RESULT_SUMMARY_MAX)
    text = f'{body}\n結論：a.py 第 3 行有錯。\n```json\n{{"status": "done"}}\n```'
    result = st._result_from_text(text, "t1")
    assert result["result_summary"].endswith("結論：a.py 第 3 行有錯。")
    assert result["result_summary"].startswith(st.OMITTED_HEAD_NOTE)
    assert len(result["result_summary"]) <= st._RESULT_SUMMARY_MAX + len(st.OMITTED_HEAD_NOTE)
    assert result["report_status"] == "done"


def test_a_short_result_is_left_alone():
    result = st._result_from_text("結論：沒問題。", "t1")
    assert result["result_summary"] == "結論：沒問題。"


@pytest.mark.parametrize(("subagent_run", "has_log"), [(True, False), (False, True)])
async def test_the_subagent_text_has_no_tool_log_lines(monkeypatch, subagent_run, has_log):
    """子代理沒人看畫面：工具日誌行不串進它的文字（模型照樣收到工具結果）。主代理照舊。"""
    clock = make_clock()
    install_clock(monkeypatch, "src.handlers.react_core", clock)
    sid, rid = "sess-sub-log", "req-sub-log"
    rhythm_service.clear_session(sid)
    tool_round = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "vault_tools", "arguments": '{"action": "list"}'},
                        }
                    ],
                }
            }
        ]
    }
    final = {"choices": [{"message": {"content": "結論：清單如上。"}}]}
    FakeUpstream([stream_reply(tool_round, "one"), stream_reply(final, "one")]).install(monkeypatch)
    ctx = make_ctx(rid, sid, custom_connection=False, subagent_run=subagent_run)
    request = MagicMock(model="gpt-4", pulse_slots=None, messages=[msg("user", "hi")])
    request.model_dump.return_value = {
        "model": "gpt-4",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        with patch("src.handlers.react_core.ToolExecutor") as executor:
            executor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            chunks = await drain(handle_openai_request(request, ctx))
    finally:
        rhythm_service.clear_session(sid)
    text = "".join(chunks)
    assert "結論：清單如上。" in text
    assert ("[vault_tools done]" in text) is has_log


# --------------------------------------------------------------------------- #
# 子代理-8：多個子代理的計數、同步派工的說法
# --------------------------------------------------------------------------- #


def _report(status: str) -> str:
    return f'做了。\n```json\n{{"status": "{status}"}}\n```'


async def test_a_self_reported_failure_does_not_count_as_success(monkeypatch):
    replies = {
        "A": _report("done"),
        "B": _report("failed"),
        "C": _report("partial"),
        "D": "沒有回報塊的文字",
    }

    async def _fake_run(*, goal: str, **_kw: Any) -> str:
        if goal == "E":
            raise RuntimeError("boom")
        return replies[goal]

    monkeypatch.setattr(st, "_run_subagent", _fake_run)
    res = await st.dispatch_subagent_group(
        subtasks=[{"goal": g} for g in "ABCDE"],
        identity=_SOUL,
        request_id="req-count",
        model="m",
        _ctx={},
    )
    payload = json.loads(res)
    assert (payload["completed"], payload["partial"], payload["failed"]) == (1, 2, 2)
    assert "1 完成" in payload["message"]
    assert "2 失敗" in payload["message"]


@pytest.mark.parametrize(
    ("reply", "word"),
    [(_report("done"), "完成"), (_report("failed"), "失敗"), ("沒有回報塊", "部分完成或未確認")],
)
async def test_the_sync_dispatch_says_what_the_report_says(monkeypatch, reply, word):
    async def _fake_run(**_kw: Any) -> str:
        return reply

    monkeypatch.setattr(st, "_run_subagent", _fake_run)
    res = await st._dispatch_and_wait(
        goal="g",
        identity=_SOUL,
        request_id="r",
        base_ctx={},
        model=None,
        task_id="t1",
        read_only=False,
    )
    assert tool_errors.is_tool_success(res)
    assert f"子代理（t1）已結束：{word}。" in json.loads(res)["message"]
