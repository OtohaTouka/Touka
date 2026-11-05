"""模型回合失敗的文字：建立時就遮罩與限長，SNS 對外只留固定文字。

都比對實際送出或存下的字串：`RoundError` 的比較會讓期望值也經過遮罩，比物件驗不出來。
會走到廠商錯誤的情境都走過真的模型回合（`react_core` ＋假上游），不是替換回應。
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import openai
import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters import responses_adapter, retry
from src.handlers.adapters.base import RoundError
from src.handlers.gemini_handler import handle_gemini_request
from src.handlers.openai_handler import handle_openai_request, handle_responses_request
from src.handlers.react_core import SNS_FAILURE_NOTICE
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult
from src.routers.heartbeat import _execute_single_pulse, _process_heartbeat_loop
from src.services.dispatchers.group import dispatch_group_stream
from src.services.models.routes import CustomRouteDisposition
from src.services.rhythm_service import rhythm_service
from src.services.sns_gateway import SNSGatewayService
from src.tools.subagent_tools import _result_from_error
from src.utils.async_db import SessionSnapshot
from src.utils.history_manager import TreeMessageManager
from src.utils.secret_text import sanitize_error_text

from tests.helpers.stream_transport import FakeUpstream, json_reply
from tests.proxy.characterization.harness import drain, install_clock, make_clock, make_ctx
from tests.proxy.characterization.test_golden_claude import _drive_raising
from tests.proxy.characterization.test_golden_gemini import _request as gemini_request
from tests.proxy.characterization.test_golden_openai import _request as openai_request
from tests.unit.test_sns_gateway import MockAdapter, _make_mocks, make_event

_MODULE = "src.handlers.react_core"
MAX_TEXT = 300
API_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
BEARER_VALUE = "abcdefghijklmnopqrstuvwxyz0123456789"
FIELD_VALUE = "hunter2hunter2hunter2"
URL_VALUE = "pw-in-url"
LOCAL_PATH = "/srv/someone/.config/provider/key.json"
FILLER = "長" * 5000
LEAKY = (
    f"upstream said: {API_KEY} Authorization: Bearer {BEARER_VALUE} api_key={FIELD_VALUE} "
    f"https://user:{URL_VALUE}@example.test/v1 {LOCAL_PATH} {FILLER}"
)
SOUL_IDENTITY = AgentIdentity.for_role("Alice", "Owner")


@pytest.fixture(autouse=True)
def soul_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.sns_gateway.agent_service.identify_agent",
        lambda value: {"agent_id": value, "is_toukasoul": value in {"Alice", "Bob"}},
    )
    monkeypatch.setattr(
        "src.services.sns_gateway.AuthService.get_all_users",
        lambda _: [SimpleNamespace(owned_agents=["Alice", "Bob"])],
    )


def assert_clean(text: str) -> None:
    for secret in (API_KEY, BEARER_VALUE, FIELD_VALUE, URL_VALUE, LOCAL_PATH):
        assert secret not in text
    assert FILLER not in text
    assert len(text) <= MAX_TEXT


def delta_text(chunks: list[str]) -> str:
    """把串流裡的回覆文字接起來（OpenAI 與 Anthropic 兩種片段）。"""
    text = ""
    for chunk in chunks:
        for line in chunk.split("\n\n"):
            stripped = line.strip()
            if not stripped.startswith("data: ") or stripped == "data: [DONE]":
                continue
            data = json.loads(stripped[6:])
            if data.get("choices"):
                text += data["choices"][0].get("delta", {}).get("content") or ""
            elif data.get("type") == "content_block_delta":
                text += data["delta"].get("text") or ""
    return text


async def _round_against_upstream(monkeypatch: pytest.MonkeyPatch, handler: Any, request: Any,
                                  ctx: Any, replies: list[Any] | None = None) -> list[str]:
    """假上游回 400，錯誤本文含憑證；其餘都是真的回合。"""
    install_clock(monkeypatch, _MODULE, make_clock())
    FakeUpstream(replies or [json_reply(LEAKY, 400)]).install(monkeypatch)
    sid = ctx["session_id"]
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)
    try:
        with patch("src.handlers.react_core.ToolExecutor"):
            return await drain(handler(request, ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)


# ── RoundError 本身 ─────────────────────────────────────────────────


def test_round_error_masks_and_limits_its_text() -> None:
    text = RoundError(f"OpenAI API Error (401)\n\n`{LEAKY}`").user_message
    assert_clean(text)
    assert text.startswith("OpenAI API Error (401)")
    assert "[REDACTED]" in text


def test_round_error_masks_the_retry_status_text() -> None:
    note = RoundError("x", retry_note=f"busy ({LEAKY})").retry_note
    assert note is not None
    assert_clean(note)
    assert RoundError("x").retry_note is None


def test_upstream_error_kind_in_the_retry_status_is_masked() -> None:
    body = {"type": "overloaded_error", "status": f"overloaded api_key={FIELD_VALUE}"}
    note = retry.on_error_body(RoundError("x"), body, "OpenAI").retry_note
    assert note is not None
    assert FIELD_VALUE not in note


@pytest.mark.parametrize(
    "text",
    [
        LEAKY,
        f"❌ **OpenAI 連線錯誤**\n\n`{API_KEY}`",
        f"see {LOCAL_PATH} and C:\\Users\\someone\\key.txt",
        f"https://user:{URL_VALUE}@example.test/a?token=abc",
        f"  token={FIELD_VALUE}  ",
        "plain text with `backticks` and [REDACTED] already",
        "長" * 299 + " 第 300 字是空白",
        "連線逾時",
        "",
    ],
)
def test_masking_twice_changes_nothing(text: str) -> None:
    """`dataclasses.replace` 會重跑建立時的遮罩，結果要一樣。"""
    once = sanitize_error_text(text)
    assert sanitize_error_text(once) == once
    error = RoundError(text)
    assert replace(error, error_type="overloaded").user_message == error.user_message


@pytest.mark.parametrize(
    "text",
    [
        "長" * 280 + " https://example.test/v1/abcdefghijklmnopqrstuvwxyz",  # 切在網址中間
        "長" * 297 + f" {LOCAL_PATH}",  # 切在路徑換成的遮罩記號中間
    ],
)
def test_masking_twice_changes_nothing_after_a_real_cut(text: str) -> None:
    """路徑先遮罩才限長，遮完放得下就不會切到，所以要先確認真的切了。"""
    once = sanitize_error_text(text)
    assert len(once) == MAX_TEXT
    assert sanitize_error_text(once) == once


def test_replacing_other_fields_keeps_the_masked_text() -> None:
    error = RoundError(f"OpenAI API Error (429)\n\n`{LEAKY}`")
    wrapped = retry.on_status(error, 429, {}, "OpenAI busy (429)")
    assert wrapped.user_message == error.user_message
    assert_clean(wrapped.user_message)


# ── 四家廠商的真回合 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_round_sends_masked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("req-mask-openai", "sess-mask-openai", custom_connection=False)
    text = delta_text(await _round_against_upstream(monkeypatch, handle_openai_request,
                                                    openai_request(), ctx))
    assert "API Error (400)" in text
    assert_clean(text)


@pytest.mark.asyncio
async def test_responses_round_sends_masked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses 走官方 SDK，換掉它的傳輸，錯誤本文同樣由假上游送回。"""

    def http_client(**kwargs: Any) -> Any:
        transport = httpx2.MockTransport(lambda _request: httpx2.Response(400, text=LEAKY))
        return openai.DefaultAsyncHttpxClient(transport=transport, **kwargs)

    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)
    install_clock(monkeypatch, _MODULE, make_clock())
    ctx = make_ctx("req-mask-resp", "sess-mask-resp", custom_connection=False,
                   base_url="http://fake-api.test/v1")
    with patch("src.handlers.react_core.ToolExecutor"):
        text = delta_text(await drain(handle_responses_request(openai_request(), ctx)))
    assert "API Error (400)" in text
    assert_clean(text)


@pytest.mark.asyncio
async def test_gemini_round_sends_masked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("req-mask-gemini", "sess-mask-gemini")
    text = delta_text(await _round_against_upstream(monkeypatch, handle_gemini_request,
                                                    gemini_request(), ctx))
    assert "API Error (400)" in text
    assert_clean(text)


@pytest.mark.asyncio
async def test_claude_round_sends_masked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    text = delta_text(await _drive_raising(monkeypatch, "mask", Exception(LEAKY)))
    assert "API 發生錯誤" in text
    assert_clean(text)


# ── 繞過 RoundError 的地方 ───────────────────────────────────────────


def test_subagent_crash_summary_is_masked() -> None:
    summary = _result_from_error(RuntimeError(LEAKY), "task")["result_summary"]
    assert summary.startswith("RuntimeError")
    assert_clean(summary)


@pytest.mark.asyncio
async def test_heartbeat_crash_text_saved_in_the_conversation_is_masked() -> None:
    sessions = MagicMock(append_message_to_session=AsyncMock(return_value=True))
    with (
        patch("src.routers.heartbeat._execute_single_pulse",
              new=AsyncMock(side_effect=RuntimeError(LEAKY))),
        patch("src.routers.heartbeat.model_manager.check_models_health", new=AsyncMock()),
        patch("src.routers.heartbeat.session_service", sessions),
    ):
        await _process_heartbeat_loop(
            final_session_id="sess-mask-beat",
            identity=SOUL_IDENTITY,
            model_override=None,
            ctx=PipelineContext(agent_id="Alice", session_id="sess-mask-beat", auth_level="Auto"),
            pulse_count=1,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
            is_home_mode=False,
        )
    saved = [call.args[2] for call in sessions.append_message_to_session.await_args_list]
    assert len(saved) == 1
    assert_clean(saved[0])
    assert "多脈衝任務執行失敗" in saved[0]


@pytest.mark.asyncio
async def test_heartbeat_round_text_saved_in_the_conversation_is_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "sess-mask-pulse"
    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "hello")

    async def real_round(**kwargs: Any) -> SimpleNamespace:
        ctx = kwargs["context"]
        for key, value in make_ctx("req-mask-pulse", sid, custom_connection=False).items():
            if key not in {"agent_id", "session_id", "identity"}:
                ctx[key] = value
        return SimpleNamespace(body_iterator=handle_openai_request(openai_request(), ctx))

    install_clock(monkeypatch, _MODULE, make_clock())
    FakeUpstream([json_reply(LEAKY, 400)]).install(monkeypatch)
    with (
        patch("src.utils.async_db.SessionService.read_execution_snapshot", new_callable=AsyncMock,
              return_value=SessionSnapshot({"id": sid, "title": "t", "messages": tree}, "rev")),
        patch("src.utils.async_db.SessionService.conditional_save_session", new_callable=AsyncMock,
              return_value=SavedResult(status="saved", representation="full", revision="next")
              ) as save,
        patch("src.routers.heartbeat.dispatch_request", new=real_round),
        patch("src.routers.heartbeat.autonomy_service"),
        patch("src.routers.heartbeat.rhythm_service"),
        patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"),
        patch("src.handlers.react_core.ToolExecutor"),
    ):
        await _execute_single_pulse(
            final_session_id=sid,
            identity=SOUL_IDENTITY,
            model_override=None,
            ctx=PipelineContext(request_id="req-mask-pulse", identity=SOUL_IDENTITY),
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
        )
    saved_tree = save.await_args_list[-1].args[2].messages
    replies = [node["content"] for node in saved_tree["messages"].values()
               if node["role"] == "assistant"]
    assert len(replies) == 1
    assert "API Error (400)" in replies[0]
    assert_clean(replies[0])


# ── SNS：頻道裡是外人，只留固定文字 ──────────────────────────────────


def _pipeline_fills_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """閘道之後的管線照真的跑，換掉 middleware（只在 ctx 上加欄位）、模型路由與寫紀錄的函式。"""

    async def execute(request: Any, ctx: Any) -> None:
        for key, value in make_ctx("req-sns", ctx["session_id"], custom_connection=False).items():
            if key not in {"agent_id", "session_id", "identity"}:  # 外人訊息不帶身分
                ctx[key] = value
        if request.model != "ToukaGroup":
            request.model = "gpt-test"

    monkeypatch.setattr("src.services.proxy_service.pipeline.execute", execute)
    monkeypatch.setattr("src.services.proxy_service.log_payload", lambda *_a, **_k: None)
    monkeypatch.setattr("src.services.proxy_service.record_working_brain", lambda *_a: None)
    monkeypatch.setattr("src.services.dispatcher.record_working_brain", lambda *_a: None)
    monkeypatch.setattr("src.services.dispatchers.factory.model_manager", SimpleNamespace(
        get_models=lambda: [],
        resolve_custom_route=lambda *_a, **_k: SimpleNamespace(
            disposition=CustomRouteDisposition.UNKNOWN),
    ))
    monkeypatch.setattr("src.services.dispatchers.factory.get_provider_info",
                        lambda *_a, **_k: ("http://fake-api.test", "fake-key", "OpenAI"))


@pytest.mark.asyncio
async def test_sns_failure_sends_and_stores_only_the_fixed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SNSGatewayService()
    adapter = MockAdapter(agent_id="Alice")
    identity_service, sessions = _make_mocks()
    _pipeline_fills_the_context(monkeypatch)
    install_clock(monkeypatch, _MODULE, make_clock())
    FakeUpstream([json_reply(LEAKY, 400)]).install(monkeypatch)
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=None),
        patch("src.services.sns_gateway.identity_service", identity_service),
        patch("src.services.sns_gateway.session_service", sessions),
        patch("src.handlers.react_core.ToolExecutor"),
    ):
        returned = await gateway.on_event(adapter, make_event("hi"), session_mode="independent")

    assert returned == SNS_FAILURE_NOTICE
    assert [message.text for _channel, message in adapter.sent_messages] == [SNS_FAILURE_NOTICE]
    stored = [call.args[2] for call in sessions.append_message_to_session.await_args_list
              if call.args[1] == "assistant"]
    assert stored == [SNS_FAILURE_NOTICE]
    # 心跳共用這份設定：SNS 的標記不能留在上面
    assert not gateway.context.get("sns_source")


def test_a_context_is_not_from_sns_by_default() -> None:
    assert PipelineContext().sns_source is False


def _install_group(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """兩位成員的群聊：第一位模型失敗，第二位正常。回傳第二位收到的訊息（跑完才有內容）。"""
    monkeypatch.setattr("src.services.dispatchers.group.parse_group_roster", lambda *_a: [
        {"id": "Alice", "name": "Alice"}, {"id": "Bob", "name": "Bob"}])
    monkeypatch.setattr("src.services.dispatchers.group.resource_manager.read_json",
                        lambda *_a: {})
    monkeypatch.setattr("src.services.dispatchers.group.record_working_brain",
                        lambda *_a, **_k: None)
    monkeypatch.setattr("src.services.dispatchers.group.get_active_brain", lambda _i: "m")
    monkeypatch.setattr("src.services.dispatchers.group.model_manager",
                        MagicMock(request_model_name=lambda brain: brain))
    monkeypatch.setattr("src.services.dispatchers.group.soul_service",
                        MagicMock(inject_home_messages=AsyncMock()))
    second_member_messages: list[str] = []
    calls = {"count": 0}

    async def member_round(_model: str, request: Any, _identity: Any, _rid: str, _t0: float,
                           nested: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            second_member_messages.extend(str(message.content) for message in request.messages)

            async def done() -> Any:
                yield 'data: {"choices": [{"delta": {"content": "ok"}, "index": 0}]}\n\n'
                yield "data: [DONE]\n\n"

            return done()
        for key, value in make_ctx("req-group", "sess-mask-group", custom_connection=False).items():
            if key not in {"agent_id", "session_id"}:
                nested[key] = value
        return handle_openai_request(openai_request(), nested)

    monkeypatch.setattr("src.services.dispatchers.group.get_stream_generator", member_round)
    install_clock(monkeypatch, _MODULE, make_clock())
    FakeUpstream([json_reply(LEAKY, 400)]).install(monkeypatch)
    return second_member_messages


@pytest.mark.asyncio
async def test_sns_group_keeps_the_fixed_text_for_the_channel_and_the_next_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = SNSGatewayService()
    adapter = MockAdapter(agent_id="Alice")
    identity_service, sessions = _make_mocks()
    next_member = _install_group(monkeypatch)
    _pipeline_fills_the_context(monkeypatch)
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=None),
        patch("src.services.sns_gateway.identity_service", identity_service),
        patch("src.services.sns_gateway.session_service", sessions),
        patch("src.handlers.react_core.ToolExecutor"),
    ):
        returned = await gateway.on_event(
            adapter, make_event("@Alice @Bob 哈囉"), session_mode="independent"
        )

    assert [message.text for _channel, message in adapter.sent_messages] == [returned]
    assert SNS_FAILURE_NOTICE in returned
    assert "API Error" not in returned
    seen_by_next = "\n".join(next_member)
    assert SNS_FAILURE_NOTICE in seen_by_next
    assert "API Error" not in seen_by_next


@pytest.mark.asyncio
async def test_plain_group_next_member_gets_masked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    next_member = _install_group(monkeypatch)
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content="@Alice @Bob 哈囉")]
    )
    context = PipelineContext(session_id="sess-mask-group", agent_id="Alice")
    chunks: list[str] = []
    with patch("src.handlers.react_core.ToolExecutor"):
        async for chunk in dispatch_group_stream(request, "req-group", 0.0, context):
            chunks.append(chunk)
    text = delta_text(chunks)
    assert "API Error (400)" in text
    assert SNS_FAILURE_NOTICE not in text
    replies = [message for message in next_member if "API Error (400)" in message]
    assert len(replies) == 1
    assert_clean(replies[0])
