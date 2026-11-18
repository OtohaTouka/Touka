"""心跳與 SNS 的失敗、送不出去：存旗標，不存失敗句（可視性第四批 PR 2，ADR-0322）.

- SNS 先送再存：外送的是模型寫的文字，模型失敗時再接固定失敗文字；存下的只有模型寫的文字，
  節點加 ``generationFailed``。送不出去（adapter 回 False 或丟例外）時節點加 ``deliveryFailed``
  與平台；送出之後存檔失敗只記 log，不往 adapter 丟例外（平台才不會再回外人一句內部錯誤）。
- 心跳脈衝的模型失敗：已經寫出的文字照存，這個脈衝最後一個節點加 ``generationFailed``；
  一個字都沒寫也留一個節點。
- 兩邊都設 ``caller_sends_failure_text``，``react_core`` 不把失敗句當成回覆文字送出。
全部離線：模型回合換成假的串流。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.handlers.react_core import SNS_FAILURE_NOTICE
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult
from src.routers.heartbeat import _execute_single_pulse
from src.services.sns_gateway import SNSGatewayService
from src.utils.async_db import SessionPersistenceError, SessionSnapshot
from src.utils.history_manager import TreeMessageManager

from tests.unit.test_sns_gateway import MockAdapter, _make_mocks, make_event

SOUL = AgentIdentity.for_role("Alice", "Owner")


@pytest.fixture(autouse=True)
def soul_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.sns_gateway.agent_service.identify_agent",
        lambda value: {"agent_id": value, "is_toukasoul": value in {"Alice", "Bob"}},
    )


def _chunk(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()


def _round(texts: list[str], *, failed: bool = False, seen: dict[str, Any] | None = None) -> Any:
    """假的模型回合：送出 ``texts``；``failed`` 時像 ``react_core`` 一樣在 ctx 記下失敗."""

    async def run(_request: Any, ctx: Any, **_kwargs: Any) -> Any:
        if seen is not None:
            seen["caller_sends_failure_text"] = ctx.get("caller_sends_failure_text")

        async def body() -> Any:
            for text in texts:
                yield _chunk(text)
            if failed:
                ctx["model_failed"] = True
            yield b"data: [DONE]\n\n"

        return SimpleNamespace(body_iterator=body())

    return run


async def _sns(
    adapter: MockAdapter,
    texts: list[str],
    *,
    failed: bool = False,
    saved: bool | Exception = True,
    order: list[str] | None = None,
    seen: dict[str, Any] | None = None,
) -> tuple[str, list[Any]]:
    """跑一則 SNS 訊息；回傳外送的文字（on_event 的回傳值）與存下的助理節點呼叫."""
    identity_service, sessions = _make_mocks()

    async def append(_sid: str, role: str, *_args: Any, **_kwargs: Any) -> bool:
        if order is not None:
            order.append(f"save {role}")
        if role != "assistant":
            return True
        if isinstance(saved, Exception):
            raise saved
        return saved

    sessions.append_message_to_session = AsyncMock(side_effect=append)
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=None),
        patch("src.services.sns_gateway.identity_service", identity_service),
        patch("src.services.sns_gateway.session_service", sessions),
        patch(
            "src.services.sns_gateway.process_proxy_request",
            new=_round(texts, failed=failed, seen=seen),
        ),
    ):
        returned = await SNSGatewayService().on_event(adapter, make_event("hi"))
    stored = [
        call for call in sessions.append_message_to_session.await_args_list
        if call.args[1] == "assistant"
    ]
    return returned, stored


class _OrderedAdapter(MockAdapter):
    def __init__(self, order: list[str]) -> None:
        super().__init__(agent_id="Alice")
        self.order = order

    async def send_message(self, channel_id: str, response: Any) -> bool:
        self.order.append("send")
        return await super().send_message(channel_id, response)


class _RefusingAdapter(MockAdapter):
    async def send_message(self, channel_id: str, response: Any) -> bool:
        await super().send_message(channel_id, response)
        return False


class _RaisingAdapter(MockAdapter):
    async def send_message(self, channel_id: str, response: Any) -> bool:
        await super().send_message(channel_id, response)
        raise ConnectionError


# ── SNS ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reply_is_sent_before_it_is_saved() -> None:
    order: list[str] = []
    adapter = _OrderedAdapter(order)
    returned, stored = await _sns(adapter, ["你好"], order=order)
    assert order == ["save user", "send", "save assistant"]
    assert returned == "你好"
    assert stored[0].args[2] == "你好"
    fields = stored[0].kwargs.get("extra_fields") or {}
    assert "deliveryFailed" not in fields
    assert "generationFailed" not in fields


@pytest.mark.asyncio
async def test_the_sns_round_asks_the_model_layer_to_leave_the_failure_text_to_it() -> None:
    seen: dict[str, Any] = {}
    await _sns(MockAdapter(agent_id="Alice"), ["好"], seen=seen)
    assert seen["caller_sends_failure_text"] is True


@pytest.mark.asyncio
async def test_a_failed_round_sends_its_text_and_the_notice_but_saves_only_the_text() -> None:
    adapter = MockAdapter(agent_id="Alice")
    returned, stored = await _sns(adapter, ["查到一半"], failed=True)
    assert returned == f"查到一半\n\n{SNS_FAILURE_NOTICE}"
    assert [message.text for _channel, message in adapter.sent_messages] == [returned]
    assert stored[0].args[2] == "查到一半"
    assert stored[0].kwargs["extra_fields"]["generationFailed"] is True


@pytest.mark.asyncio
async def test_a_round_that_failed_before_any_text_still_leaves_a_marked_node() -> None:
    adapter = MockAdapter(agent_id="Alice")
    returned, stored = await _sns(adapter, [], failed=True)
    assert returned == SNS_FAILURE_NOTICE
    assert [message.text for _channel, message in adapter.sent_messages] == [SNS_FAILURE_NOTICE]
    assert len(stored) == 1
    assert stored[0].args[2] == ""
    assert stored[0].kwargs["extra_fields"]["generationFailed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [_RefusingAdapter, _RaisingAdapter])
async def test_an_undelivered_reply_is_saved_with_a_mark(adapter_cls: type[MockAdapter]) -> None:
    adapter = adapter_cls(agent_id="Alice")
    _returned, stored = await _sns(adapter, ["你好"])
    assert len(adapter.sent_messages) == 1
    assert stored[0].args[2] == "你好"
    fields = stored[0].kwargs["extra_fields"]
    assert fields["deliveryFailed"] is True
    assert fields["deliveryPlatform"] == "telegram"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved", "logged"),
    [
        (False, "SESSION_SAVE_FAILED"),
        (ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH), "SESSION_SCOPE_MISMATCH"),
        (SessionPersistenceError(), "SESSION_SAVE_FAILED"),
    ],
    ids=["refused", "scope-changed", "persistence-error"],
)
async def test_a_save_failure_after_sending_is_only_logged(
    caplog: pytest.LogCaptureFixture, saved: bool | Exception, logged: str
) -> None:
    adapter = MockAdapter(agent_id="Alice")
    with caplog.at_level(logging.WARNING, logger="src.services.sns_gateway"):
        returned, stored = await _sns(adapter, ["你好"], saved=saved)
    assert returned == "你好"
    assert len(adapter.sent_messages) == 1
    assert len(stored) == 1
    assert logged in caplog.text


@pytest.mark.asyncio
async def test_a_conversation_that_changed_hands_is_refused_before_sending() -> None:
    """串流期間對話被改歸別的靈魂：送出前的歸屬檢查拒絕，外人收不到這則回覆."""
    adapter = MockAdapter(agent_id="Alice")
    identity_service, sessions = _make_mocks()
    sessions.read_execution_snapshot = AsyncMock(
        side_effect=ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
    )
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=None),
        patch("src.services.sns_gateway.identity_service", identity_service),
        patch("src.services.sns_gateway.session_service", sessions),
        patch("src.services.sns_gateway.process_proxy_request", new=_round(["你好"])),
        pytest.raises(ToukaException) as raised,
    ):
        await SNSGatewayService().on_event(adapter, make_event("hi"))
    assert raised.value.code == ErrorCode.SESSION_SCOPE_MISMATCH
    assert adapter.sent_messages == []


@pytest.mark.asyncio
async def test_an_empty_round_sends_and_saves_nothing() -> None:
    adapter = MockAdapter(agent_id="Alice")
    returned, stored = await _sns(adapter, [])
    assert returned == ""
    assert adapter.sent_messages == []
    assert stored == []


@pytest.mark.asyncio
async def test_a_blank_reply_is_not_sent_and_not_marked_undelivered() -> None:
    """只有空白的回覆平台本來就會拒收：不送，也不記成沒送到."""
    adapter = _RefusingAdapter(agent_id="Alice")
    _returned, stored = await _sns(adapter, ["  \n "])
    assert adapter.sent_messages == []
    [call] = stored
    assert "deliveryFailed" not in call.kwargs["extra_fields"]


# ── 心跳 ────────────────────────────────────────────────────────────


async def _pulse(texts: list[str], *, failed: bool) -> tuple[dict[str, Any], PipelineContext]:
    """跑一個脈衝，回傳最後存下的對話樹與這個脈衝用的 ctx."""
    sid = "sess-pulse-mark"
    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "開始")
    ctx = PipelineContext(request_id="req-pulse-mark", identity=SOUL)

    async def fake_dispatch(**kwargs: Any) -> Any:
        round_ctx = kwargs["context"]

        async def body() -> Any:
            for text in texts:
                yield _chunk(text).decode()
            if failed:
                round_ctx["model_failed"] = True
            yield "data: [DONE]\n\n"

        return SimpleNamespace(body_iterator=body())

    with (
        patch("src.utils.async_db.SessionService.read_execution_snapshot", new_callable=AsyncMock,
              return_value=SessionSnapshot({"id": sid, "title": "t", "messages": tree}, "rev")),
        patch("src.utils.async_db.SessionService.conditional_save_session", new_callable=AsyncMock,
              return_value=SavedResult(status="saved", representation="full", revision="next")
              ) as save,
        patch("src.routers.heartbeat.dispatch_request", new=fake_dispatch),
        patch("src.routers.heartbeat.autonomy_service"),
        patch("src.routers.heartbeat.rhythm_service"),
        patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"),
    ):
        await _execute_single_pulse(
            final_session_id=sid,
            identity=SOUL,
            model_override=None,
            ctx=ctx,
            pulse_slots=1,
            t0=0.0,
            init_prompt="test",
        )
    return save.await_args_list[-1].args[2].messages, ctx


def _assistants(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in tree["messages"].values() if node["role"] == "assistant"]


@pytest.mark.asyncio
async def test_a_pulse_asks_the_model_layer_to_leave_the_failure_text_to_it() -> None:
    _tree, ctx = await _pulse(["好"], failed=False)
    assert ctx.get("caller_sends_failure_text") is True


@pytest.mark.asyncio
async def test_a_failed_pulse_keeps_what_it_wrote_and_marks_the_node() -> None:
    tree, _ctx = await _pulse(["查到一半"], failed=True)
    [reply] = _assistants(tree)
    assert reply["content"] == "查到一半"
    assert reply["generationFailed"] is True


@pytest.mark.asyncio
async def test_a_pulse_that_failed_before_any_text_still_leaves_a_marked_node() -> None:
    tree, _ctx = await _pulse([], failed=True)
    [reply] = _assistants(tree)
    assert reply["content"] == ""
    assert reply["generationFailed"] is True


@pytest.mark.asyncio
async def test_a_failure_after_a_split_marks_the_last_node_of_the_pulse() -> None:
    tree, _ctx = await _pulse(["第一步做完 --- 第 1 次 自主行動"], failed=True)
    [reply] = _assistants(tree)
    assert reply["content"].startswith("第一步做完")
    assert reply["generationFailed"] is True


@pytest.mark.asyncio
async def test_a_failure_after_a_split_and_more_text_marks_only_the_newest_node() -> None:
    tree, _ctx = await _pulse(["第一步做完 --- 第 1 次 自主行動", "第二步做到一半"], failed=True)
    first, second = _assistants(tree)
    assert first["content"].startswith("第一步做完")
    assert "generationFailed" not in first
    assert second["content"] == "第二步做到一半"
    assert second["generationFailed"] is True


@pytest.mark.asyncio
async def test_a_pulse_that_did_not_fail_is_not_marked() -> None:
    tree, _ctx = await _pulse(["做完了"], failed=False)
    [reply] = _assistants(tree)
    assert "generationFailed" not in reply
