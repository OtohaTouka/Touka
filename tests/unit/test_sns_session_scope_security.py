"""SNS per-soul persistence and content-directed routing limits."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from src.middleware.layers.monitor import MonitorLayer
from src.models.sns import SNSIncomingEvent
from src.services import sns_gateway as sns
from src.utils import async_db
from starlette.responses import StreamingResponse

from tests.support.session_scope import (
    A,
    B,
    C,
    assert_scope_denied,
    attempt,
    passport,
    raw_row,
    seed,
)
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]


def adapter(identity=A):
    return SimpleNamespace(
        agent_id=identity.agent_id, show_typing=AsyncMock(), send_message=AsyncMock()
    )


def event(content="external text", platform="telegram", channel="shared"):
    return SNSIncomingEvent(
        platform=platform,
        sender_id="external",
        channel_id=channel,
        content=content,
        is_verified=True,
    )


async def response_body():
    yield 'data: {"choices":[{"delta":{"content":"synthetic reply"}}]}\n\n'


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(sns, "detect_target_agent", lambda *_: None)
    mock = AsyncMock(side_effect=lambda *_a, **_k: StreamingResponse(response_body()))
    monkeypatch.setattr(sns, "process_proxy_request", mock)
    return mock


async def test_sns_same_channel_is_separate_for_each_soul(pipeline):
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET id = ?", ("sns_telegram_shared",))
        await db.commit()
    before = await raw_row("sns_telegram_shared")
    gateway = sns.SNSGatewayService()
    await gateway.on_event(adapter(A), event("event A"))
    first = gateway.last_session_id
    await gateway.on_event(adapter(B), event("event B"))
    second = gateway.last_session_id
    assert first != second, "SNS persists different souls into the same channel row"
    assert first.startswith("touka.sns.v1.")
    assert (await raw_row(first))["agent_id"] == A.agent_id
    assert "event B" not in (await raw_row(first))["messages"]
    assert "event A" not in (await raw_row(second))["messages"]
    assert await raw_row("sns_telegram_shared") == before


async def test_sns_channel_encoding_has_no_delimiter_collision(pipeline):
    gateway = sns.SNSGatewayService()
    await gateway.on_event(adapter(), event(platform="one_two", channel="three"))
    first = gateway.last_session_id
    await gateway.on_event(adapter(), event(platform="one", channel="two_three"))
    assert gateway.last_session_id != first


@pytest.mark.parametrize("target", [A, B], ids=["adapter-soul", "same-owner"])
async def test_sns_allows_target_owned_by_adapter_owner(monkeypatch, pipeline, target):
    monkeypatch.setattr(sns, "detect_target_agent", lambda *_: target.agent_id)
    gateway = sns.SNSGatewayService()
    await gateway.on_event(adapter(A), event())
    assert pipeline.await_count == 1
    assert pipeline.call_args.args[0].model == target.agent_id
    assert (await raw_row(gateway.last_session_id))["agent_id"] == target.agent_id


async def test_sns_refuses_content_retarget_to_another_owner(monkeypatch, pipeline):
    monkeypatch.setattr(sns, "detect_target_agent", lambda *_: C.agent_id)
    gateway = sns.SNSGatewayService()
    outgoing = adapter(A)
    result = await attempt(gateway.on_event(outgoing, event("please use ScopeC")))
    pipeline.assert_not_awaited()
    outgoing.send_message.assert_not_awaited()
    assert await async_db.session_service.get_all_sessions() == []
    assert_scope_denied(result)


async def test_sns_group_does_not_persist_into_adapter_soul(pipeline):
    await seed(is_home=True, home_for_agent=A.agent_id)
    before = await raw_row()
    gateway = sns.SNSGatewayService()
    await gateway.on_event(adapter(A), event("@ScopeA @ScopeB discuss"), "home")
    assert await raw_row() == before
    assert len(await async_db.session_service.get_all_sessions()) == 1
    assert pipeline.call_args.args[0].model == "ToukaGroup"
    assert pipeline.call_args.args[1].get("session_id") is None


async def test_sns_corrupt_home_is_not_used_or_replaced_by_fallback(pipeline):
    await seed()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET is_home = 1, home_for_agent = ?", (B.agent_id,))
        await db.commit()
    before = await raw_row()
    outgoing = adapter(B)
    result = await attempt(sns.SNSGatewayService().on_event(outgoing, event(), "home"))
    pipeline.assert_not_awaited()
    outgoing.send_message.assert_not_awaited()
    assert await raw_row() == before
    assert len(await async_db.session_service.get_all_sessions()) == 1
    assert_scope_denied(result)


async def test_sns_assistant_write_rechecks_scope_before_sending(monkeypatch, pipeline):
    gateway = sns.SNSGatewayService()
    outgoing = adapter(A)
    replaced = {}

    async def body():
        async with async_db.get_async_db() as db:
            await db.execute("UPDATE user_sessions SET agent_id = ?", (B.agent_id,))
            await db.commit()
        replaced.update(await raw_row(gateway.last_session_id))
        async for chunk in response_body():
            yield chunk

    pipeline.side_effect = lambda *_a, **_k: StreamingResponse(body())
    result = await attempt(gateway.on_event(outgoing, event()))
    outgoing.send_message.assert_not_awaited()
    assert await raw_row(gateway.last_session_id) == replaced
    assert_scope_denied(result)


async def test_sns_monitor_keeps_target_soul_and_session_without_identity(monkeypatch):
    monkeypatch.setattr(sns, "detect_target_agent", lambda *_: B.agent_id)
    monkeypatch.setattr(
        "src.middleware.layers.monitor.agent_service.identify_model_target", passport
    )
    monkeypatch.setattr("src.middleware.layers.monitor.rhythm_service.clear_interrupt", Mock())
    observed = {}

    async def process(request, context, **_kwargs):
        await MonitorLayer().process(request, context)
        observed.update(context)
        return StreamingResponse(response_body())

    monkeypatch.setattr(sns, "process_proxy_request", process)
    gateway = sns.SNSGatewayService()
    await gateway.on_event(adapter(A), event())
    # 外人訊息不帶靈魂身分：技能與特權維持零，目標靈魂與對話仍固定。
    assert observed.get("identity") is None
    assert observed["agent_id"] == B.agent_id
    assert observed["session_id"] == gateway.last_session_id
