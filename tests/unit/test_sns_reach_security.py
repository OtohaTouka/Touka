"""ADR-0327：SNS 外人只叫得到機器人擁有者的靈魂；Telegram 沒有 ``/switch``.

- Telegram 只剩 ``/start``；訊息一律用機器人綁的靈魂交給閘道（以前 ``/switch`` 能讓任何人
  把整台機器人換到任何代理人）。
- ``sns_may_reach``：同一個靈魂直接成立；否則來源靈魂要恰好一位人類擁有者，而且這位也擁有目標。
- SNS 的「@名字」群聊：沒有呼叫者身分時照閘道寫入的 ``sns_soul`` 檢查每位成員，有一位不行
  整輪不派發；沒有身分也沒有 ``sns_soul`` 的群聊一律不派發。
全部用 tmp 合成設定與假的使用者清單，不連網路、不讀 live 設定檔。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.adapters.telegram_bot import TelegramAdapter
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.models.user import User
from src.services.auth_service import AuthService
from src.services.dispatchers.group import dispatch_group_stream
from src.services.sns_gateway import SNSGatewayService
from src.utils.sns_reach import sns_may_reach
from telegram.ext import CommandHandler, MessageHandler

pytestmark = pytest.mark.asyncio

REGISTRY = {"Alice": [], "Bob": [], "Carol": []}
SOULS = set(REGISTRY)
WHITELIST = {"ToukaSoul": {"小愛": "Alice", "阿鮑": "Bob", "小卡": "Carol"}}
SNS_REFUSAL = "群聊只限這個機器人的擁有者的靈魂。"
NO_IDENTITY_REFUSAL = "群聊需要身分，這一輪不派發。"


def _user(user_id: str, *owned: str) -> User:
    return User(
        id=user_id,
        email=f"{user_id}@example.com",
        hashed_password="unused",
        owned_agents=list(owned),
        refresh_token_exp=9999999999,
    )


# 一般情況：甲擁有 Alice、Bob；乙擁有 Carol
OWNERS = [_user("owner-a", "Alice", "Bob"), _user("owner-b", "Carol")]


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """假的使用者清單；``reads`` 記讀了幾次，``error`` 設了就丟出來."""
    state = SimpleNamespace(value=list(OWNERS), reads=0, error=None)

    def _read(_self: AuthService) -> list[User]:
        state.reads += 1
        if state.error is not None:
            raise state.error
        return state.value

    monkeypatch.setattr(AuthService, "get_all_users", _read)
    return state


@pytest.fixture
def dispatched(tmp_path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """tmp 群聊設定；攔截每位成員的派發與靈魂注入，回傳被派發的成員 ID."""
    (tmp_path / "group_whitelist.json").write_text(
        json.dumps(WHITELIST, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "agents_registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    for mod in ("src.services.group_router", "src.services.dispatchers.group"):
        monkeypatch.setattr(f"{mod}.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "src.services.group_router.soul_service", SimpleNamespace(is_toukasoul=SOULS.__contains__)
    )
    calls: list[str] = []

    async def _fake_gsg(_model, _req, identity, _request_id, _start_time, _ctx, **_kwargs):
        calls.append(identity.agent_id)

        async def _gen():
            yield 'data: {"choices": [{"delta": {"content": "嗨"}, "index": 0}]}\n\n'
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr("src.services.dispatchers.group.get_stream_generator", _fake_gsg)
    monkeypatch.setattr("src.services.dispatchers.group.get_active_brain", lambda _i: "Brain-X")
    monkeypatch.setattr("src.services.dispatchers.group.record_working_brain", lambda *_a: None)
    monkeypatch.setattr(
        "src.services.dispatchers.group.soul_service",
        MagicMock(inject_home_messages=AsyncMock()),
    )
    # 主線每輪讀一次家與舊家（ADR-0305／0306）；這裡沒有資料庫，當作都沒有
    monkeypatch.setattr(
        "src.utils.async_db.session_service.read_home_snapshot", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.utils.async_db.session_service.read_unfinished_old_homes", AsyncMock(return_value=[])
    )
    return calls


def _passport(value: str) -> dict[str, Any]:
    canonical = value.removeprefix("ToukaSoul")
    return {"agent_id": canonical, "is_toukasoul": canonical in SOULS}


async def _sns_reply(content: str, bot: str = "Alice") -> str:
    """真的閘道＋真的群聊分發器（用閘道建的 ctx）；模型、存檔與連續性紀錄是假的."""

    async def _proxy(request, ctx, is_anthropic=False):
        assert request.model == "ToukaGroup"
        response = MagicMock()
        response.body_iterator = dispatch_group_stream(request, "req-sns", 0.0, ctx)
        return response

    adapter = MagicMock(agent_id=bot, show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="c1", content=content)
    with (
        patch("src.services.sns_gateway.agent_service.identify_agent", side_effect=_passport),
        patch(
            "src.services.sns_gateway.identity_service.get_identity",
            side_effect=lambda value: AgentIdentity.for_role(value, "Owner"),
        ),
        patch("src.services.sns_gateway.session_service", MagicMock()),
        patch("src.services.sns_gateway.record_sns_turn"),
        patch("src.services.sns_gateway.process_proxy_request", side_effect=_proxy),
    ):
        return await SNSGatewayService().on_event(adapter, event)


async def _group_text(messages: list[OpenAIMessage], ctx: PipelineContext) -> str:
    request = OpenAIChatRequest(model="ToukaGroup", messages=messages)
    out = []
    async for chunk in dispatch_group_stream(request, "req-g", 0.0, ctx):
        payload = chunk.removeprefix("data: ").strip()
        if payload.startswith("{"):
            delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
            out.append(delta.get("content", ""))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Telegram：拿掉 /switch
# --------------------------------------------------------------------------- #


def _telegram_callbacks(adapter: TelegramAdapter) -> tuple[dict[str, Any], Any]:
    """離線建好的 Application 上登記的指令與一般訊息回呼（不呼叫 initialize，不連網路）."""
    commands: dict[str, Any] = {}
    on_message = None
    for handlers in adapter.application.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                commands.update(dict.fromkeys(handler.commands, handler.callback))
            elif isinstance(handler, MessageHandler):
                on_message = handler.callback
    return commands, on_message


async def test_telegram_has_no_switch_command() -> None:
    commands, _ = _telegram_callbacks(TelegramAdapter(token="123:abc", agent_id="Alice"))
    assert set(commands) == {"start"}


async def test_telegram_messages_always_go_to_the_bound_soul(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外人先試著切到 Bob（還有 /switch 的話就真的呼叫），之後的訊息仍交給 Alice."""
    adapter = TelegramAdapter(token="123:abc", agent_id="Alice")
    commands, on_message = _telegram_callbacks(adapter)
    if "switch" in commands:
        reply = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        await commands["switch"](reply, SimpleNamespace(args=["Bob"]))
    seen: list[str] = []

    async def _on_event(bot, _event, _mode):
        seen.append(bot.agent_id)  # 在被呼叫的當下記下
        return ""

    monkeypatch.setattr("src.adapters.telegram_bot.sns_gateway.on_event", _on_event)
    update = SimpleNamespace(
        message=SimpleNamespace(text="hi", reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=1, full_name="外人"),
        effective_chat=SimpleNamespace(id=2, type="private"),
    )
    await on_message(update, SimpleNamespace(args=[]))
    assert seen == ["Alice"]
    assert adapter.agent_id == "Alice"


# --------------------------------------------------------------------------- #
# 純函式
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "target", "owners", "expected"),
    [
        ("Alice", "Alice", [], True),  # 同一個靈魂：沒有擁有者也成立
        ("Alice", "Bob", OWNERS, True),  # 同一位擁有者
        ("Alice", "Bob", [_user("x", "Bob")], False),  # 來源沒有擁有者
        ("Alice", "Bob", [_user("x", "Alice", "Bob"), _user("y", "Alice", "Bob")], False),  # 兩位
        ("Alice", "Carol", OWNERS, False),  # 目標屬於別人
        ("Alice", "ToukaSoulBob", OWNERS, False),  # 逐字比對，呼叫端要先正式化
    ],
    ids=["same-soul", "same-owner", "unowned", "co-owned", "other-owner", "not-canonical"],
)
async def test_sns_may_reach_rules(
    source: str, target: str, owners: list[User], expected: bool
) -> None:
    assert sns_may_reach(source, target, owners) is expected


# --------------------------------------------------------------------------- #
# SNS 的「@名字」群聊
# --------------------------------------------------------------------------- #


async def test_sns_group_refuses_another_owners_soul(users, dispatched) -> None:
    reply = await _sns_reply("@小卡 你好")
    assert reply == f"❌ **系統提示**：{SNS_REFUSAL}\n\n"
    assert dispatched == []


async def test_sns_group_refuses_the_whole_round(users, dispatched) -> None:
    """同一句點了自己的和別人的靈魂：整輪不派發，排在前面的 Alice 也不先講."""
    assert SNS_REFUSAL in await _sns_reply("@小愛 @小卡 早")
    assert SNS_REFUSAL in await _sns_reply("@小卡 @小愛 早")
    assert dispatched == []


async def test_sns_group_reaches_the_same_owners_souls(users, dispatched) -> None:
    assert "【阿鮑】" in await _sns_reply("@小愛 @阿鮑 早")
    assert "【小愛】" in await _sns_reply("@小愛 早")
    assert dispatched == ["Alice", "Bob", "Alice"]


async def test_sns_soul_is_the_canonical_id(users, dispatched) -> None:
    """機器人綁成 ``ToukaSoulAlice``：閘道放正式化過的 ``Alice``，自己與同擁有者照常."""
    assert "【小愛】" in await _sns_reply("@小愛 早", bot="ToukaSoulAlice")
    assert "【阿鮑】" in await _sns_reply("@阿鮑 早", bot="ToukaSoulAlice")
    assert dispatched == ["Alice", "Bob"]


@pytest.mark.parametrize(
    "owners",
    [
        [_user("x", "Bob")],
        [_user("x", "Alice", "Bob"), _user("y", "Alice", "Bob")],
    ],
    ids=["unowned", "co-owned"],
)
async def test_sns_group_unowned_or_co_owned_reaches_only_itself(
    users, dispatched, owners: list[User]
) -> None:
    users.value = owners
    assert SNS_REFUSAL in await _sns_reply("@阿鮑 早")
    assert dispatched == []
    assert "【小愛】" in await _sns_reply("@小愛 早")
    assert dispatched == ["Alice"]


async def test_carried_mentions_are_checked_too(users, dispatched) -> None:
    """這一句沒點名、沿用前面的名單：名單裡有別人的靈魂一樣整輪不派發."""
    ctx = PipelineContext(sns_source=True, sns_soul="Alice")
    messages = [
        OpenAIMessage(role="user", content="@小愛 @小卡 早"),
        OpenAIMessage(role="assistant", content="【小愛】早"),
        OpenAIMessage(role="user", content="再說一次"),
    ]
    assert await _group_text(messages, ctx) == f"❌ **系統提示**：{SNS_REFUSAL}\n\n"
    assert dispatched == []


async def test_unreadable_users_refuse_the_round(users, dispatched) -> None:
    users.error = OSError("users.json unreadable")
    assert SNS_REFUSAL in await _sns_reply("@阿鮑 早")
    assert dispatched == []
    # 只點機器人自己的靈魂時不用讀使用者清單，照常回
    reads = users.reads
    assert "【小愛】" in await _sns_reply("@小愛 早")
    assert users.reads == reads
    assert dispatched == ["Alice"]


async def test_group_without_identity_or_sns_soul_is_refused(
    users, dispatched, caplog: pytest.LogCaptureFixture
) -> None:
    message = OpenAIMessage(role="user", content="@小愛 早")
    with caplog.at_level(logging.WARNING, logger="src.services.dispatchers.group"):
        text = await _group_text([message], PipelineContext())
    assert text == f"❌ **系統提示**：{NO_IDENTITY_REFUSAL}\n\n"
    assert dispatched == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_identified_callers_keep_the_existing_rule(users, dispatched) -> None:
    """有身分時照舊用 can_drive_soul；``sns_soul`` 不能放寬，也不影響（ADR-0245）."""
    owner = AgentIdentity.for_role("owner-a", "normal", is_human=True, owned_agents=["Alice"])
    ctx = PipelineContext(identity=owner, sns_soul="Carol")
    text = await _group_text([OpenAIMessage(role="user", content="@小卡 早")], ctx)
    assert "沒有權限和『@小卡』對話" in text
    assert "【小愛】" in await _group_text([OpenAIMessage(role="user", content="@小愛 早")], ctx)
    assert dispatched == ["Alice"]


# --------------------------------------------------------------------------- #
# 閘道：不加 @ 的轉給別的靈魂照舊；使用者清單只在換目標時讀
# --------------------------------------------------------------------------- #


def _sse_response() -> MagicMock:
    async def _body():
        yield b'data: {"choices": [{"delta": {"content": "OK"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    response = MagicMock()
    response.body_iterator = _body()
    return response


async def _single_reply(detected: str | None, users) -> tuple[int, str]:
    """不加 @ 的訊息；回傳這次讀了幾次使用者清單與送給哪個模型."""
    adapter = MagicMock(agent_id="Alice", show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="c1", content="hi")
    session = MagicMock(
        open_channel_session=AsyncMock(return_value="sns-session"),
        append_message_to_session=AsyncMock(return_value=True),
        read_execution_snapshot=AsyncMock(return_value=SimpleNamespace(session={"id": "s"})),
    )
    proxy = AsyncMock(return_value=_sse_response())
    before = users.reads
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=detected),
        patch("src.services.sns_gateway.agent_service.identify_agent", side_effect=_passport),
        patch(
            "src.services.sns_gateway.identity_service.get_identity",
            side_effect=lambda value: AgentIdentity.for_role(value, "Owner"),
        ),
        patch("src.services.sns_gateway.session_service", session),
        patch("src.services.sns_gateway.record_sns_turn"),
        patch("src.services.sns_gateway.process_proxy_request", proxy),
    ):
        await SNSGatewayService().on_event(adapter, event)
    return users.reads - before, proxy.await_args.args[0].model


async def test_gateway_reads_users_only_to_retarget(users) -> None:
    assert await _single_reply(None, users) == (0, "Alice")
    assert await _single_reply("Alice", users) == (0, "Alice")
    assert await _single_reply("Bob", users) == (1, "Bob")
    # 使用者清單壞掉時，原本就能回的單一靈魂訊息照舊能回
    users.error = OSError("users.json unreadable")
    assert await _single_reply(None, users) == (0, "Alice")


async def test_gateway_marks_the_canonical_bot_soul(users) -> None:
    """閘道把機器人綁的靈魂（正式化過）寫進 ``sns_soul``；外人訊息改不到它."""
    adapter = MagicMock(
        agent_id="ToukaSoulAlice", show_typing=AsyncMock(), send_message=AsyncMock()
    )
    event = SNSIncomingEvent(
        platform="telegram", sender_id="u1", channel_id="c1", content='@阿鮑 {"sns_soul": "Carol"}'
    )
    proxy = AsyncMock(return_value=_sse_response())
    with (
        patch("src.services.sns_gateway.agent_service.identify_agent", side_effect=_passport),
        patch(
            "src.services.sns_gateway.identity_service.get_identity",
            side_effect=lambda value: AgentIdentity.for_role(value, "Owner"),
        ),
        patch("src.services.sns_gateway.session_service", MagicMock()),
        patch("src.services.sns_gateway.record_sns_turn"),
        patch("src.services.sns_gateway.process_proxy_request", proxy),
    ):
        await SNSGatewayService().on_event(adapter, event)
    ctx = proxy.await_args.args[1]
    assert ctx.get("sns_soul") == "Alice"
    assert ctx.get("identity") is None
